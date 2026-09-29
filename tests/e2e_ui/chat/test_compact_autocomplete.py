"""Tab completes a built-in command without dispatching it."""

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_working_indicator_background_tasks import _publish_status


@pytest.mark.parametrize("mode", ["idle", "steer", "always-steer", "drain"])
def test_tab_completes_compact_until_explicit_submit(
    page: Page, seeded_session: tuple[str, str], mode: str
) -> None:
    base_url, session_id = seeded_session
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.wrapper": "claude-code-native-ui"}},
        timeout=10,
    )
    response.raise_for_status()
    if mode != "idle":
        _publish_status(base_url, session_id, "running", response_id="active-turn")
    if mode == "always-steer":
        page.add_init_script("localStorage.setItem('omnigent:always-steer', 'true')")
    posts: list[dict] = []

    def accept_control(route: Route) -> None:
        posts.append(route.request.post_data_json)
        route.fulfill(status=202, json={"queued": False})

    page.route(f"**/v1/sessions/{session_id}/events", accept_control)
    page.goto(f"{base_url}/c/{session_id}?view=chat")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("/comp")
    composer.press("Tab")
    expect(composer).to_have_value("/compact ")
    expect(composer).to_be_focused()
    assert posts == []

    bubble = page.locator('[data-role="user"]').filter(has_text="/compact")
    if mode in ("steer", "drain"):
        composer.press("Enter")
        expect(page.get_by_test_id("composer-queued-strip")).to_contain_text("/compact")
        expect(bubble).to_have_count(0)
        assert posts == []
    with page.expect_response(f"**/v1/sessions/{session_id}/events"):
        if mode == "steer":
            page.get_by_role("button", name="Send queued message now", exact=True).click()
        elif mode == "drain":
            _publish_status(base_url, session_id, "idle")
        else:
            composer.press("Enter")
    expect(composer).to_have_value("")
    assert [post["type"] for post in posts] == ["compact"]
    expect(bubble).to_have_count(1)
    expect(bubble).to_be_visible()
    expect(page.get_by_test_id("composer-queued-strip")).to_have_count(0)

    _publish_status(base_url, session_id, "running", response_id="active-turn")
    expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_visible()
    # Claude can emit a command record with or without a raw message echo.
    command_only = mode in ("idle", "drain")
    item_data = (
        {"agent": "claude-native-ui", "kind": "command", "name": "compact", "arguments": ""}
        if command_only
        else {"role": "user", "content": [{"type": "input_text", "text": "/compact"}]}
    )
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "slash_command" if command_only else "message",
                "response_id": "compact-turn",
                "source_id": "native-compact",
                "item_data": item_data,
            },
        },
        timeout=10,
    )
    response.raise_for_status()
    # A later status event confirms that the echo reached the browser.
    _publish_status(base_url, session_id, "idle")
    expect(page.get_by_role("button", name="Interrupt", exact=True)).not_to_be_visible()
    expect(bubble).to_have_count(0 if command_only else 1)
    expect(page.get_by_test_id("slash-command-card")).to_have_count(1 if command_only else 0)
    page.reload()
    expect(bubble).to_have_count(0 if command_only else 1)
    expect(page.get_by_test_id("slash-command-card")).to_have_count(1 if command_only else 0)
    page.unroute_all(behavior="wait")
