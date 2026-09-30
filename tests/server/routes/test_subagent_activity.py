"""Subagent lifecycle links persist once and never enter agent context."""

from typing import Any
from unittest.mock import Mock

import pytest

from omnigent.entities import (
    Conversation,
    ConversationItem,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
from omnigent.server.routes._sessions.orchestration import (
    _persist_external_conversation_item,
    _persist_external_conversation_items,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.server.subagent_activity import (
    native_subagent_terminal_status,
    record_claude_subagent_return,
    record_subagent_activity,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

LifecycleStore = tuple[SqlAlchemyConversationStore, Conversation]


@pytest.fixture
def lifecycle_store(db_uri: str) -> LifecycleStore:
    store = SqlAlchemyConversationStore(db_uri)
    return store, store.create_conversation()


def _child(
    store: SqlAlchemyConversationStore,
    parent: Conversation,
    *,
    title: str = "Explore:agent-1",
    labels: dict[str, str] | None = None,
    sub_agent_name: str | None = None,
) -> Conversation:
    child = store.create_conversation(
        parent_conversation_id=parent.id, title=title, sub_agent_name=sub_agent_name
    )
    if labels:
        store.set_labels(child.id, labels)
    return child


def _activity(store: SqlAlchemyConversationStore, parent_id: str) -> list[ConversationItem]:
    return [
        item
        for item in store.list_items(parent_id, type="resource_event").data
        if item.data.event_type.startswith("session.subagent.")
        and item.data.event_type != "session.subagent.completion-observed"
    ]


def _meta(text: str, return_id: str | None = None) -> MessageData:
    return MessageData(
        role="user",
        is_meta=True,
        content=[{"type": "input_text", "text": text}],
        subagent_return_id=return_id,
    )


@pytest.mark.parametrize(
    ("status", "options", "expected"),
    [
        ("idle", {"harness": "claude-native"}, None),
        ("idle", {"harness": "claude-native", "turn_completed": True}, "completed"),
        ("failed", {"harness": "claude-native"}, "failed"),
        ("idle", {"harness": "cursor-native", "turn_outcome": "cancelled"}, "cancelled"),
        ("idle", {"harness": "cursor-native", "turn_outcome": "failed"}, "failed"),
        ("idle", {"harness": "cursor-native", "turn_outcome": "completed"}, "completed"),
        ("idle", {"harness": "codex-native"}, "completed"),
        ("idle", {"harness": "claude-native", "turn_outcome": {}}, None),
        ("running", {"harness": "claude-native"}, None),
        ("idle", {"harness": None}, None),
        ("idle", {"harness": None, "turn_completed": True}, "completed"),
    ],
)
def test_native_terminal_status_respects_confirmed_outcomes(
    status: str, options: dict[str, Any], expected: str | None
) -> None:
    assert native_subagent_terminal_status(status, **options) == expected


@pytest.mark.asyncio
async def test_lifecycle_preserves_ids_and_deduplicates_each_returned_turn(
    lifecycle_store: LifecycleStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, parent = lifecycle_store
    child = _child(store, parent, title="Research")
    publish = Mock()
    monkeypatch.setattr("omnigent.server.subagent_activity.session_stream.publish", publish)

    await record_subagent_activity(child.id, "delegated", store, parent_id="wrong-parent")
    await record_subagent_activity(child.id, "delegated", store, parent_id=parent.id)
    await record_subagent_activity(child.id, "delegated", store, parent_id=parent.id)
    await record_subagent_activity(child.id, "returned", store, turn_id="first")
    await record_subagent_activity(child.id, "returned", store, turn_id="first")
    await record_subagent_activity(child.id, "returned", store, turn_id="second")

    items = store.list_items(parent.id).data
    assert [item.data.event_type for item in items] == [
        "session.subagent.delegated",
        "session.subagent.returned",
        "session.subagent.returned",
    ]
    assert publish.call_count == 3
    for call, item in zip(publish.call_args_list, items, strict=True):
        assert call.args[0] == parent.id
        assert call.args[1]["type"] == "response.output_item.done"
        assert call.args[1]["item"]["id"] == item.id
        assert call.args[1]["item"]["resource_id"] == child.id
        assert call.args[1]["item"]["resource"] == {"title": "Research"}


@pytest.mark.asyncio
async def test_native_return_without_turn_id_requires_child_output(
    lifecycle_store: LifecycleStore,
) -> None:
    store, parent = lifecycle_store
    child = _child(store, parent, title="Research")
    await record_subagent_activity(child.id, "returned", store)
    assert store.list_items(parent.id).data == []
    for turn_id in ("first", "second"):
        store.append(
            child.id,
            [
                NewConversationItem(
                    type="message",
                    response_id=turn_id,
                    data=MessageData(
                        role="assistant",
                        agent="Claude",
                        content=[{"type": "output_text", "text": "Done"}],
                    ),
                )
            ],
        )
        await record_subagent_activity(child.id, "returned", store)
        await record_subagent_activity(child.id, "returned", store)
    assert len(store.list_items(parent.id).data) == 2


@pytest.mark.parametrize(
    ("title", "sub_agent_name", "expected"),
    [
        ("codex-native-ui-subagent:thread-1", "Codex sub-agent", "Codex sub-agent"),
        ("antigravity-native-ui-subagent:cascade-1", None, "Sub-agent"),
        ("Explore:agent-1", None, "Explore"),
    ],
)
@pytest.mark.asyncio
async def test_native_lifecycle_title_hides_technical_prefixes(
    lifecycle_store: LifecycleStore,
    title: str,
    sub_agent_name: str | None,
    expected: str,
) -> None:
    store, parent = lifecycle_store
    child = _child(
        store,
        parent,
        title=title,
        sub_agent_name=sub_agent_name,
        labels={"omnigent.wrapper": "native-ui-subagent"},
    )
    await record_subagent_activity(child.id, "delegated", store)
    assert store.list_items(parent.id).data[0].data.resource == {"title": expected}


@pytest.mark.parametrize(
    ("data", "status"),
    [
        (
            FunctionCallOutputData(
                call_id="tool-1", output="Inspection finished.", subagent_return_id="agent-1"
            ),
            "completed",
        ),
        (
            _meta(
                "<task-notification><task-id>agent-1</task-id><status>completed</status>"
                "</task-notification>"
            ),
            "completed",
        ),
        (_meta('<agent-message from="researcher">Done.</agent-message>', "agent-1"), "completed"),
        (
            _meta(
                "<task-notification><task-id>agent-1</task-id><status>failed</status>"
                "</task-notification>"
            ),
            "failed",
        ),
    ],
    ids=["tool", "notification", "handback", "failed"],
)
@pytest.mark.asyncio
async def test_claude_completion_matches_real_child(
    lifecycle_store: LifecycleStore,
    data: FunctionCallOutputData | MessageData,
    status: str,
) -> None:
    store, parent = lifecycle_store
    child = _child(
        store,
        parent,
        labels={
            "omnigent.claude_native.subagent_id": "agent-1",
            "omnigent.claude_native.tool_use_id": "tool-1",
            "omnigent.claude_native.description": "Inspect authentication",
        },
    )
    item = NewConversationItem(
        type="function_call_output" if isinstance(data, FunctionCallOutputData) else "message",
        response_id="completion-record",
        data=data,
    )
    await record_claude_subagent_return(parent.id, item, store)
    await record_claude_subagent_return(parent.id, item, store)
    items = _activity(store, parent.id)
    assert len(items) == 1
    assert items[0].data.resource_id == child.id
    assert items[0].data.resource == {
        "title": "Inspect authentication",
        "status": status,
    }


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.asyncio
async def test_external_item_paths_record_returned_child_once(
    lifecycle_store: LifecycleStore, batched: bool
) -> None:
    store, parent = lifecycle_store
    child = _child(store, parent, labels={"omnigent.claude_native.subagent_id": "agent-1"})
    body = SessionEventInput(
        type="external_conversation_item",
        data={
            "source_id": "result-record",
            "item_type": "function_call_output",
            "item_data": {"call_id": "tool-1", "output": "Inspection finished."},
            "response_id": "parent-turn",
            "subagent_return_id": "agent-1",
        },
    )
    for _ in range(2):
        if batched:
            await _persist_external_conversation_items(parent.id, [body], store)
        else:
            await _persist_external_conversation_item(parent.id, parent, body, store)
    items = store.list_items(parent.id).data
    outputs = [item for item in items if item.type == "function_call_output"]
    activity = _activity(store, parent.id)
    assert len(outputs) == 1
    assert outputs[0].data.subagent_return_id == "agent-1"
    assert len(activity) == 1
    assert activity[0].data.resource_id == child.id


@pytest.mark.parametrize(
    "data",
    [
        FunctionCallOutputData(
            call_id="tool-1", output="Async agent launched successfully. agentId: agent-1"
        ),
        FunctionCallOutputData(call_id="tool-1", output='{"status":"async_launched"}'),
        _meta('<teammate-message teammate_id="reviewer">Hi</teammate-message>'),
    ],
    ids=["launch-text", "launch-json", "teammate-chatter"],
)
@pytest.mark.asyncio
async def test_non_completion_has_no_return_link(
    lifecycle_store: LifecycleStore,
    data: FunctionCallOutputData | MessageData,
) -> None:
    store, parent = lifecycle_store
    _child(store, parent, labels={"omnigent.claude_native.tool_use_id": "tool-1"})
    await record_claude_subagent_return(
        parent.id,
        NewConversationItem(
            type="function_call_output" if isinstance(data, FunctionCallOutputData) else "message",
            response_id="parent-turn",
            data=data,
        ),
        store,
    )
    assert store.list_items(parent.id).data == []


@pytest.mark.asyncio
async def test_parallel_generic_children_keep_their_task_names(
    lifecycle_store: LifecycleStore,
) -> None:
    store, parent = lifecycle_store
    for title in ("researcher:Auth audit", "researcher:Storage audit"):
        child = _child(store, parent, title=title, sub_agent_name="researcher")
        await record_subagent_activity(child.id, "delegated", store)
    assert [item.data.resource for item in store.list_items(parent.id).data] == [
        {"title": "Auth audit"},
        {"title": "Storage audit"},
    ]


@pytest.mark.parametrize("failed", [False, True], ids=["completed", "failed"])
@pytest.mark.asyncio
async def test_claude_result_arriving_before_child_discovery_is_reconciled(
    lifecycle_store: LifecycleStore, failed: bool
) -> None:
    store, parent = lifecycle_store
    body = SessionEventInput(
        type="external_conversation_item",
        data={
            "source_id": "early-result",
            "response_id": "parent-turn",
            "item_type": "function_call_output",
            "item_data": {
                "call_id": "tool-1",
                "output": '{"status":"failed","agentId":"agent-1"}' if failed else "Done.",
            },
            **({} if failed else {"subagent_return_id": "agent-1"}),
        },
    )
    await _persist_external_conversation_items(parent.id, [body], store)
    store.append(
        parent.id,
        [
            NewConversationItem(
                type="message",
                response_id=f"intervening-{index}",
                data=MessageData(
                    role="assistant",
                    agent="Claude",
                    content=[{"type": "output_text", "text": f"Update {index}"}],
                ),
            )
            for index in range(101)
        ],
    )
    child = _child(
        store,
        parent,
        labels={
            "omnigent.claude_native.subagent_id": "agent-1",
            "omnigent.claude_native.tool_use_id": "tool-1",
        },
    )
    await record_subagent_activity(child.id, "delegated", store)
    await record_subagent_activity(child.id, "delegated", store)
    if failed:
        assert [row.data.event_type for row in _activity(store, parent.id)] == [
            "session.subagent.delegated"
        ]
        await record_claude_subagent_return(
            parent.id,
            NewConversationItem(
                type="message",
                response_id="failure-record",
                data=_meta(
                    "<task-notification><tool-use-id>tool-1</tool-use-id>"
                    "<status>failed</status></task-notification>"
                ),
            ),
            store,
        )
    activity = _activity(store, parent.id)
    assert [row.data.event_type for row in activity] == [
        "session.subagent.delegated",
        "session.subagent.returned",
    ]
    assert activity[-1].data.resource["status"] == ("failed" if failed else "completed")
