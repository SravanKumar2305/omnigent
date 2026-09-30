"""Sub-agent lifecycle notices reveal the child chat and Agents panel."""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    reset_mock_llm,
    seed_committed_turn,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

_CHILD_TITLE = "Research Omnigent public positioning"
_CHILD_REPLY = "Omnigent supports CLI, desktop, mobile, and web workflows."
_TEAM_BODY = "Internal teammate delivery: positioning research is complete."


@pytest.mark.min_server_version("0.17.0")
@pytest.mark.workspace_panel_product_default
@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile"])
def test_subagent_notices_link_to_child_and_reveal_agents(
    page: Page,
    seeded_session: tuple[str, str],
    mobile: bool,
) -> None:
    """Live notices survive reload, hide team traffic, and open the child."""
    base_url, parent_id = seeded_session
    page.set_viewport_size(
        {"width": 390, "height": 844} if mobile else {"width": 1440, "height": 1000}
    )
    seed_committed_turn(
        parent_id,
        prompt="Research Omnigent's public positioning.",
        reply="I'll delegate the public research to a sub-agent.",
    )
    page.goto(f"{base_url}/c/{parent_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    if not mobile:
        expect(page.get_by_role("button", name="Expand right panel")).to_be_visible()
        expect(page.get_by_role("complementary", name="Workspace")).to_have_count(0)

    # Exercise real registration/status producers while the parent is open.
    started = httpx.post(
        f"{base_url}/v1/sessions/{parent_id}/events",
        json={
            "type": "external_codex_subagent_start",
            "data": {
                "thread_id": "thread_public_research",
                "agent_nickname": _CHILD_TITLE,
                "agent_role": "researcher",
            },
        },
        timeout=30.0,
    )
    started.raise_for_status()
    child_id = started.json()["child_session_id"]
    try:
        notices = page.get_by_test_id("subagent-activity")
        expect(notices).to_have_count(1, timeout=30_000)
        expect(notices.first).to_contain_text(f"Started {_CHILD_TITLE}")

        # Native Codex owns this child's execution; the test runner must not wake the parent.
        unbound = httpx.patch(
            f"{base_url}/v1/sessions/{child_id}", json={"runner_id": ""}, timeout=10.0
        )
        unbound.raise_for_status()
        assert unbound.json()["runner_id"] is None
        seed_committed_turn(child_id, prompt="Research public positioning.", reply=_CHILD_REPLY)
        finished = httpx.post(
            f"{base_url}/v1/sessions/{child_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "response_id": "codex_child_turn_1"},
            },
            timeout=30.0,
        )
        finished.raise_for_status()
        expect(notices).to_have_count(2, timeout=30_000)
        expect(notices.last).to_contain_text(f"Completed {_CHILD_TITLE}")

        page.reload()
        expect(notices).to_have_count(2, timeout=30_000)

        child_path = f"/c/{child_id}?panel=agents"
        for notice in notices.all():
            expect(notice.get_by_role("link", name=_CHILD_TITLE, exact=True)).to_have_attribute(
                "href", child_path
            )
            expect(notice).not_to_contain_text("(ID)")
        notices.last.get_by_role("link", name=_CHILD_TITLE, exact=True).click()
        expect(page).to_have_url(re.compile(re.escape(child_path) + "$"))
        if mobile:
            panel = page.get_by_test_id("subagents-panel-drawer")
            expect(panel).to_have_attribute("data-state", "open")
        else:
            panel = page.get_by_role("complementary", name="Workspace")
            expect(panel).to_be_visible()
            expect(panel.get_by_role("tab", name=re.compile("^Agents"))).to_have_attribute(
                "data-state", "active"
            )
            expect(
                page.get_by_test_id("assistant-text-section").get_by_text(_CHILD_REPLY, exact=True)
            ).to_be_visible(timeout=30_000)
        expect(panel.locator(f'[data-child-session-id="{child_id}"]')).to_be_visible(
            timeout=30_000
        )
        if mobile:
            panel.locator(f'[data-child-session-id="{child_id}"]').click()
            expect(panel).to_have_attribute("data-state", "closed")
            expect(panel).not_to_be_in_viewport()
            expect(
                page.get_by_test_id("assistant-text-section").get_by_text(_CHILD_REPLY, exact=True)
            ).to_be_visible(timeout=30_000)
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)


@pytest.mark.nightly
@pytest.mark.min_server_version("0.17.0")
@pytest.mark.timeout(300)
def test_human_teammate_markup_survives_native_acknowledgement(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Real native input stays visible; simulated internal transcript traffic does not."""
    base_url, session_id = native_claude_mock_session
    human_text = '<teammate-message teammate_id="reviewer">Review this</teammate-message>'
    reply = "The human-submitted review request was received."
    reset_mock_llm(mock_llm_server_url)
    for model in ("default", _CLAUDE_MOCK_MODEL):
        set_fallback_mock_llm(mock_llm_server_url, model, reply)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    _send(page, human_text)

    # Exercise the transport API with a bridge-classified internal delivery.
    internal = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "is_meta": True,
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                '<teammate-message teammate_id="researcher">'
                                f"{_TEAM_BODY}</teammate-message>"
                            ),
                        }
                    ],
                },
                "response_id": "resp_internal_team_delivery",
                "source_id": "internal_team_delivery",
            },
        },
        timeout=10.0,
    )
    internal.raise_for_status()
    user_bubbles = page.locator('[data-testid="message-bubble"][data-role="user"]')
    expect(user_bubbles).to_have_count(1, timeout=60_000)
    expect(user_bubbles).to_contain_text("Review this")
    # Copy link becomes enabled when the transcript acknowledges the pending bubble.
    expect(user_bubbles.get_by_role("button", name="Copy link", exact=True)).to_be_enabled(
        timeout=60_000
    )
    expect(page.get_by_text(_TEAM_BODY, exact=False)).to_have_count(0)

    page.reload()
    _ensure_chat_view(page)
    expect(user_bubbles).to_have_count(1, timeout=30_000)
    expect(user_bubbles).to_contain_text("Review this")
    expect(page.get_by_text(_TEAM_BODY, exact=False)).to_have_count(0)
