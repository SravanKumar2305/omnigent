"""The server persists a web user message under its client-minted stable id.

Persisting a send under its 32-hex ``stable_id`` makes the append idempotent on
retry and lets the client recognize its own send coming back when a network drop
swallowed the POST's acknowledgement. Adoption is limited to the send path:
seeded ``initial_items`` keep store-assigned ids.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.server.routes.sessions import _build_new_item
from omnigent.server.schemas import SessionEventInput
from tests.server.helpers import create_test_agent

_STABLE_ID = "0f" * 16  # 32 lowercase hex chars, the shape web clients mint
_TEXT = "summarize the deploy status"


def _user_message(stable_id: object = _STABLE_ID) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"type": "input_text", "text": _TEXT}],
        "stable_id": stable_id,
    }


def test_build_new_item_adopts_web_send_stable_id_when_asked() -> None:
    """A user message's valid 32-hex ``stable_id`` becomes the item's stable id."""
    body = SessionEventInput(type="message", data=_user_message())

    item = _build_new_item(body, "resp_1", adopt_stable_id=True)

    assert item.stable_id == _STABLE_ID


@pytest.mark.parametrize(
    "data",
    [
        _user_message("abc123"),  # too short
        _user_message("0F" * 16),  # uppercase hex
        _user_message(42),  # not a string
        {
            "role": "assistant",
            "agent": "helper",
            "content": [{"type": "output_text", "text": "hello"}],
            "stable_id": _STABLE_ID,
        },
    ],
)
def test_build_new_item_ignores_unusable_stable_id(data: dict[str, Any]) -> None:
    """Anything but a user message's 32-hex id keeps the store-assigned id."""
    item = _build_new_item(
        SessionEventInput(type="message", data=data), "resp_1", adopt_stable_id=True
    )

    assert item.stable_id is None


def _stub_runner(monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    """Accept every forwarded turn with 202 so persist-before-forward completes."""
    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(202, json={"queued": True})),
        base_url="http://runner",
    )

    async def get_runner_client(*_: Any, **__: Any) -> httpx.AsyncClient:
        return fake_runner

    monkeypatch.setattr("omnigent.server.routes.sessions._get_runner_client", get_runner_client)
    return fake_runner


@pytest.mark.asyncio
async def test_web_send_persists_under_its_stable_id_and_dedupes_a_retry(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the store: the persisted item's id IS the stable id, once."""
    fake_runner = _stub_runner(monkeypatch)
    try:
        agent = await create_test_agent(client)
        create = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
        assert create.status_code == 201, create.text
        session_id = create.json()["id"]
        payload = {"type": "message", "data": _user_message()}

        first = await client.post(f"/v1/sessions/{session_id}/events", json=payload)
        assert first.status_code == 202, first.text
        # A client whose acknowledgement was lost retries with the same stable id.
        retry = await client.post(f"/v1/sessions/{session_id}/events", json=payload)
        assert retry.status_code == 202, retry.text
    finally:
        await fake_runner.aclose()

    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    assert [it["id"] for it in items if it["type"] == "message"] == [_STABLE_ID]


@pytest.mark.asyncio
async def test_seeded_user_message_keeps_a_store_assigned_id(client: httpx.AsyncClient) -> None:
    """``initial_items`` are not a send: a client id there is not adopted."""
    agent = await create_test_agent(client)

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [{"type": "message", "data": _user_message()}],
        },
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    message_ids = [it["id"] for it in items if it["type"] == "message"]
    assert len(message_ids) == 1
    assert message_ids != [_STABLE_ID]
