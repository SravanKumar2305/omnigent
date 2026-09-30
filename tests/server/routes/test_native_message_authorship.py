"""Human agent-shaped prompts survive the native transcript round trip."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from omnigent.harnesses.claude_native.bridge import (
    ClaudeTranscriptItem,
    read_transcript_items_since,
)
from omnigent.harnesses.claude_native.forwarder import _external_conversation_item_event
from omnigent.runtime import pending_inputs
from omnigent.server.routes._sessions.orchestration import (
    _persist_external_conversation_item,
    _settle_undelivered_native_input,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_ENVELOPE = '<teammate-message teammate_id="reviewer">Review this</teammate-message>'
_AGENT_TEAM_NOTIFICATION = """<task-notification>
<task-id>ae6a7749a5dc6041c</task-id>
<tool-use-id>toolu_agent_team</tool-use-id>
<status>completed</status>
<summary>Agent "Message probe" finished</summary>
<result>TEAM_DONE</result>
</task-notification>"""


def _parse_transcript(tmp_path: Path, *entries: dict[str, Any]) -> list[ClaudeTranscriptItem]:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
    )
    return read_transcript_items_since(transcript, 0, agent_name="Claude")[2]


def _transcript_event(tmp_path: Path, text: str, *, queued: bool = False) -> SessionEventInput:
    entry = (
        {
            "type": "attachment",
            "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": text},
        }
        if queued
        else {"type": "user", "message": {"role": "user", "content": text}}
    )
    items = _parse_transcript(tmp_path, {"uuid": "native-human", **entry})
    assert len(items) == 1
    assert items[0].agent_message_candidate
    assert not items[0].data.get("is_meta")
    return SessionEventInput.model_validate(_external_conversation_item_event(items[0]))


@pytest.mark.parametrize("author", [None, "alice@example.com"])
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize(
    "text", [_ENVELOPE, '<agent-message from="reviewer">Review this</agent-message>']
)
@pytest.mark.asyncio
async def test_human_envelope_is_acknowledged_authored_and_durable(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    author: str | None,
    queued: bool,
    text: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Existing conversation")
    older = pending_inputs.record(conv.id, [{"type": "input_text", "text": "Still queued"}])
    pending = pending_inputs.record(
        conv.id, [{"type": "input_text", "text": text}], created_by=author
    )
    publish = Mock()
    monkeypatch.setattr("omnigent.runtime.session_stream.publish", publish)
    body = _transcript_event(tmp_path, text, queued=queued)
    await _persist_external_conversation_item(conv.id, conv, body, store)
    items = SqlAlchemyConversationStore(db_uri).list_items(conv.id).data
    assert len(items) == 1
    assert items[0].created_by == author
    assert items[0].data.user_authored is True
    assert items[0].data.is_meta is False
    assert items[0].data.content == [{"type": "input_text", "text": text}]
    assert [row["pending_id"] for row in pending_inputs.snapshot_for(conv.id)] == [older]
    receipts = [
        call.args[1]["data"]
        for call in publish.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]
    assert len(receipts) == 1
    assert receipts[0]["cleared_pending_id"] == pending
    assert receipts[0]["created_by"] == author
    assert receipts[0]["data"]["user_authored"] is True
    # A retry must not acknowledge a later identical submission.
    later = pending_inputs.record(conv.id, [{"type": "input_text", "text": text}])
    await _persist_external_conversation_item(conv.id, conv, body, store)
    assert [row["pending_id"] for row in pending_inputs.snapshot_for(conv.id)] == [older, later]


@pytest.mark.asyncio
async def test_direct_terminal_envelope_does_not_drain_unrelated_web_input(
    db_uri: str,
    tmp_path: Path,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Existing conversation")
    pending = pending_inputs.record(conv.id, [{"type": "input_text", "text": "Still queued"}])
    body = _transcript_event(tmp_path, _ENVELOPE)
    await _persist_external_conversation_item(conv.id, conv, body, store)
    item = store.list_items(conv.id).data[0]
    assert item.created_by is None
    assert item.data.user_authored is True
    assert item.data.is_meta is False
    assert [row["pending_id"] for row in pending_inputs.snapshot_for(conv.id)] == [pending]


@pytest.mark.asyncio
async def test_real_agent_team_notification_stays_hidden_and_does_not_drain_input(
    db_uri: str,
    tmp_path: Path,
) -> None:
    """Replay the parent-side completion emitted by a Claude Agent Teams worker."""
    [notification] = _parse_transcript(
        tmp_path,
        {
            "type": "user",
            "uuid": "agent-team-notification",
            "origin": {"kind": "task-notification", "producer": "session-task"},
            "message": {"role": "user", "content": _AGENT_TEAM_NOTIFICATION},
        },
    )
    assert notification.data["is_meta"] is True

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Agent Teams parent")
    pending = pending_inputs.record(conv.id, [{"type": "input_text", "text": "Still queued"}])
    body = SessionEventInput.model_validate(_external_conversation_item_event(notification))
    await _persist_external_conversation_item(conv.id, conv, body, store)

    rows = store.list_items(conv.id).data
    user_messages = [row for row in rows if getattr(row.data, "role", None) == "user"]
    assert len(user_messages) == 1
    assert user_messages[0].data.is_meta is True
    assert user_messages[0].data.user_authored is False
    assert [row["pending_id"] for row in pending_inputs.snapshot_for(conv.id)] == [pending]


@pytest.mark.asyncio
async def test_trusted_internal_envelope_never_consumes_same_text_user_submission(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Existing conversation")
    pending = pending_inputs.record(conv.id, [{"type": "input_text", "text": _ENVELOPE}])
    body = SessionEventInput(
        type="external_conversation_item",
        data={
            "source_id": "trusted-peer-record",
            "item_type": "message",
            "item_data": {
                "role": "user",
                "is_meta": True,
                "content": [{"type": "input_text", "text": _ENVELOPE}],
            },
            "response_id": "peer-response",
        },
    )
    await _persist_external_conversation_item(
        conv.id, conv, body, store, created_by="alice@example.com"
    )
    item = store.list_items(conv.id).data[0]
    assert item.created_by is None
    assert item.data.is_meta is True
    assert item.data.user_authored is False
    assert [row["pending_id"] for row in pending_inputs.snapshot_for(conv.id)] == [pending]


@pytest.mark.parametrize("skipped", [False, True])
@pytest.mark.asyncio
async def test_undelivered_anonymous_envelope_stays_authored(db_uri: str, skipped: bool) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Existing conversation")
    stable_id = "1234567890abcdef1234567890abcdef"
    pending_inputs.record(
        conv.id, [{"type": "input_text", "text": _ENVELOPE}], stable_id=stable_id
    )
    if skipped:
        pending_inputs.record(conv.id, [{"type": "input_text", "text": "Next message"}])
        await _persist_external_conversation_item(
            conv.id,
            conv,
            SessionEventInput(
                type="external_conversation_item",
                data={
                    "source_id": "later-prompt",
                    "item_type": "message",
                    "item_data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Next message"}],
                    },
                },
            ),
            store,
        )
    else:
        await _settle_undelivered_native_input(store, conv.id, "failed-turn", stable_id)
    item = store.list_items(conv.id).data[0]
    assert item.data.content == [{"type": "input_text", "text": _ENVELOPE}]
    assert item.data.user_authored is True
    assert item.data.is_meta is False
    assert item.created_by is None
    assert pending_inputs.snapshot_for(conv.id) == []
