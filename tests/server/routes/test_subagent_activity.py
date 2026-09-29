"""Subagent lifecycle links persist once and never enter agent context."""

from unittest.mock import Mock

import pytest

from omnigent.entities import (
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


@pytest.mark.parametrize(
    ("status", "harness", "turn_outcome", "turn_completed", "expected"),
    [
        ("idle", "claude-native", None, None, None),
        ("idle", "claude-native", None, True, "completed"),
        ("failed", "claude-native", None, None, "failed"),
        ("idle", "cursor-native", "cancelled", None, "cancelled"),
        ("idle", "cursor-native", "failed", None, "failed"),
        ("idle", "cursor-native", "completed", None, "completed"),
        ("idle", "codex-native", None, None, "completed"),
        ("idle", "claude-native", {}, None, None),
        ("running", "claude-native", None, None, None),
        ("idle", None, None, None, None),
        ("idle", None, None, True, "completed"),
    ],
)
def test_native_terminal_status_respects_confirmed_outcomes(
    status: str,
    harness: str | None,
    turn_outcome: object,
    turn_completed: bool | None,
    expected: str | None,
) -> None:
    assert (
        native_subagent_terminal_status(
            status, harness=harness, turn_outcome=turn_outcome, turn_completed=turn_completed
        )
        == expected
    )


@pytest.mark.asyncio
async def test_lifecycle_preserves_ids_and_deduplicates_each_returned_turn(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id, title="Research")
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
async def test_native_return_without_turn_id_requires_child_output(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id, title="Research")
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
    db_uri: str, title: str, sub_agent_name: str | None, expected: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(
        parent_conversation_id=parent.id, title=title, sub_agent_name=sub_agent_name
    )
    store.set_labels(child.id, {"omnigent.wrapper": "native-ui-subagent"})
    await record_subagent_activity(child.id, "delegated", store)
    assert store.list_items(parent.id).data[0].data.resource == {"title": expected}


@pytest.mark.parametrize("completion", ["tool", "notification", "handback", "failed"])
@pytest.mark.asyncio
async def test_claude_completion_matches_real_child(db_uri: str, completion: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id, title="Explore:agent-1")
    store.set_labels(
        child.id,
        {
            "omnigent.claude_native.subagent_id": "agent-1",
            "omnigent.claude_native.tool_use_id": "tool-1",
            "omnigent.claude_native.description": "Inspect authentication",
        },
    )
    if completion == "tool":
        item = NewConversationItem(
            type="function_call_output",
            response_id="parent-turn",
            data=FunctionCallOutputData(
                call_id="tool-1", output="Inspection finished.", subagent_return_id="agent-1"
            ),
        )
    else:
        text = (
            "<task-notification><task-id>agent-1</task-id><status>completed</status>"
            "</task-notification>"
            if completion in {"notification", "failed"}
            else '<agent-message from="researcher">Inspection finished.</agent-message>'
        )
        if completion == "failed":
            text = text.replace("completed", "failed")
        item = NewConversationItem(
            type="message",
            response_id="notification-record",
            data=MessageData(
                role="user",
                is_meta=True,
                content=[{"type": "input_text", "text": text}],
                subagent_return_id="agent-1" if completion == "handback" else None,
            ),
        )
    await record_claude_subagent_return(parent.id, item, store)
    await record_claude_subagent_return(parent.id, item, store)
    items = store.list_items(parent.id).data
    assert len(items) == 1
    assert items[0].data.resource_id == child.id
    assert items[0].data.resource == {
        "title": "Inspect authentication",
        "status": "failed" if completion == "failed" else "completed",
    }


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.asyncio
async def test_external_item_paths_record_returned_child_once(db_uri: str, batched: bool) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id, title="Explore:agent-1")
    store.set_labels(child.id, {"omnigent.claude_native.subagent_id": "agent-1"})
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
    assert [item.type for item in items] == ["function_call_output", "resource_event"]
    assert items[0].data.subagent_return_id == "agent-1"
    assert items[1].data.resource_id == child.id
    assert items[1].data.event_type == "session.subagent.returned"


@pytest.mark.parametrize(
    "output",
    ["Async agent launched successfully. agentId: agent-1", '{"status":"async_launched"}'],
)
@pytest.mark.asyncio
async def test_async_claude_launch_is_not_a_return(db_uri: str, output: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id, title="Explore:agent-1")
    store.set_labels(child.id, {"omnigent.claude_native.tool_use_id": "tool-1"})
    await record_claude_subagent_return(
        parent.id,
        NewConversationItem(
            type="function_call_output",
            response_id="parent-turn",
            data=FunctionCallOutputData(call_id="tool-1", output=output),
        ),
        store,
    )
    assert store.list_items(parent.id).data == []


@pytest.mark.asyncio
async def test_teammate_chatter_has_no_invented_return_link(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    item = NewConversationItem(
        type="message",
        response_id="notification",
        data=MessageData(
            role="user",
            is_meta=True,
            content=[
                {
                    "type": "input_text",
                    "text": '<teammate-message teammate_id="reviewer">Hi</teammate-message>',
                }
            ],
        ),
    )
    await record_claude_subagent_return(parent.id, item, store)
    assert store.list_items(parent.id).data == []


@pytest.mark.asyncio
async def test_parallel_generic_children_keep_their_task_names(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    for title in ("researcher:Auth audit", "researcher:Storage audit"):
        child = store.create_conversation(
            parent_conversation_id=parent.id, title=title, sub_agent_name="researcher"
        )
        await record_subagent_activity(child.id, "delegated", store)
    assert [item.data.resource for item in store.list_items(parent.id).data] == [
        {"title": "Auth audit"},
        {"title": "Storage audit"},
    ]


@pytest.mark.parametrize("completion", ["tool", "handback", "failed"])
@pytest.mark.asyncio
async def test_claude_result_arriving_before_child_discovery_is_reconciled(
    db_uri: str, completion: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    body = SessionEventInput(
        type="external_conversation_item",
        data={
            "source_id": "early-result",
            "response_id": "parent-turn",
            "item_type": "message" if completion == "handback" else "function_call_output",
            "item_data": {
                "role": "user",
                "is_meta": True,
                "content": [
                    {
                        "type": "input_text",
                        "text": '<agent-message from="agent-1">Done.</agent-message>',
                    }
                ],
            }
            if completion == "handback"
            else {
                "call_id": "tool-1",
                "output": '{"status":"failed","agentId":"agent-1"}'
                if completion == "failed"
                else "Done.",
            },
            **({"subagent_return_id": "agent-1"} if completion != "failed" else {}),
        },
    )
    await _persist_external_conversation_items(parent.id, [body], store)
    child = store.create_conversation(parent_conversation_id=parent.id, title="Explore:agent-1")
    store.set_labels(
        child.id,
        {
            "omnigent.claude_native.subagent_id": "agent-1",
            "omnigent.claude_native.tool_use_id": "tool-1",
        },
    )
    await record_subagent_activity(child.id, "delegated", store)
    await record_subagent_activity(child.id, "delegated", store)
    if completion == "failed":
        assert [
            row.data.event_type for row in store.list_items(parent.id, type="resource_event").data
        ] == ["session.subagent.delegated"]
        await record_claude_subagent_return(
            parent.id,
            NewConversationItem(
                type="message",
                response_id="failure-record",
                data=MessageData(
                    role="user",
                    is_meta=True,
                    content=[
                        {
                            "type": "input_text",
                            "text": "<task-notification><tool-use-id>tool-1</tool-use-id>"
                            "<status>failed</status></task-notification>",
                        }
                    ],
                ),
            ),
            store,
        )
    assert [
        row.data.event_type for row in store.list_items(parent.id, type="resource_event").data
    ] == ["session.subagent.delegated", "session.subagent.returned"]
    assert store.list_items(parent.id, type="resource_event").data[-1].data.resource["status"] == (
        "failed" if completion == "failed" else "completed"
    )
