"""Sub-agent lifecycle notices reveal the child chat and Agents panel."""

from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.entities import MessageData, NewConversationItem
from tests.e2e_ui.conftest import seed_committed_items, seed_committed_turn

_CHILD_TITLE = "Research Omnigent public positioning"
_CHILD_REPLY = "Omnigent supports CLI, desktop, mobile, and web workflows."
_TEAM_BODY = "Internal teammate delivery: positioning research is complete."


def _screenshot(page: Page, name: str) -> None:
    """Save demo images when E2E_SCREENSHOT_DIR is set for a local run."""
    if directory := os.environ.get("E2E_SCREENSHOT_DIR"):
        page.screenshot(path=str(Path(directory) / f"subagent-{name}.png"), animations="disabled")


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
    # Old Claude transcripts can contain user-role team delivery envelopes.
    seed_committed_items(
        parent_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_historical_team_delivery",
                data=MessageData(
                    role="user",
                    content=[
                        {
                            "type": "input_text",
                            "text": (
                                '<teammate-message teammate_id="researcher" '
                                f'color="blue" summary="Research complete">{_TEAM_BODY}'
                                "</teammate-message>"
                            ),
                        }
                    ],
                ),
            )
        ],
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
        expect(page.locator('[data-testid="message-bubble"][data-role="user"]')).to_have_count(1)
        expect(page.get_by_text(_TEAM_BODY, exact=False)).to_have_count(0)
        expect(page.get_by_text("teammate-message", exact=False)).to_have_count(0)

        child_path = f"/c/{child_id}?panel=agents"
        for notice in notices.all():
            expect(notice.get_by_role("link", name=_CHILD_TITLE, exact=True)).to_have_attribute(
                "href", child_path
            )
            expect(notice).not_to_contain_text("(ID)")
        layout = "mobile" if mobile else "desktop"
        _screenshot(page, f"activity-{layout}")

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
        _screenshot(page, f"agents-open-{layout}")
        if mobile:
            panel.locator(f'[data-child-session-id="{child_id}"]').click()
            expect(panel).to_have_attribute("data-state", "closed")
            expect(panel).not_to_be_in_viewport()
            expect(
                page.get_by_test_id("assistant-text-section").get_by_text(_CHILD_REPLY, exact=True)
            ).to_be_visible(timeout=30_000)
            _screenshot(page, "child-chat-mobile")
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
