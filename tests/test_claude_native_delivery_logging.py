"""Delivery diagnostics distinguish missing evidence from a submitted prompt."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.debug_logging import current_session_id_scope, record_to_row
from omnigent.harnesses.claude_native import bridge


@pytest.mark.parametrize(
    ("scenario", "verification", "outcome"),
    [
        ("normal", "draft_absent", "returned"),
        ("unknown_command", "draft_absent", "returned"),
        ("blank_line", "unverified", "returned"),
        ("retry", "draft_absent", "returned"),
        ("timeout", "draft_still_present", "error"),
        ("empty_capture", "inconclusive_capture", "returned"),
        ("missing_glyph", "inconclusive_capture", "returned"),
        ("transport_error", "not_started", "error"),
        ("startup_error", "not_started", "error"),
        ("cancelled", "not_started", "interrupted"),
    ],
)
def test_delivery_diagnostics(
    scenario: str,
    verification: str,
    outcome: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "private customer prompt"
    if scenario == "unknown_command":
        secret = "/private-customer-command"
    content = "\n" + secret if scenario == "blank_line" else secret
    elapsed = 0.0
    pane = "❯ "
    enters = 0

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    def ready(*args: object, **kwargs: object) -> None:
        if scenario == "startup_error":
            raise RuntimeError(secret)
        if scenario == "cancelled":
            raise bridge.ClaudeInjectionCancelled(secret)

    def run_tmux(socket: str, *args: str) -> None:
        nonlocal pane, enters
        if args[0] == "paste-buffer":
            if scenario == "transport_error":
                raise RuntimeError(secret)
            pane = "❯ " + content
        if args[-1] == "Enter":
            enters += 1
            if scenario in {"blank_line", "timeout"} or (scenario == "retry" and enters == 1):
                return
            pane = {"empty_capture": "", "missing_glyph": "terminal output"}.get(scenario, "❯ ")

    monkeypatch.setattr(bridge, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep))
    monkeypatch.setattr(
        bridge,
        "_wait_for_tmux_info",
        lambda *_a, **_k: {"socket_path": "/unused/socket", "tmux_target": "main"},
    )
    monkeypatch.setattr(bridge, "_restore_occupied_input", lambda *_a, **_k: None)
    monkeypatch.setattr(bridge, "_wait_for_claude_prompt_ready", ready)
    monkeypatch.setattr(bridge, "_run_tmux", run_tmux)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_a, **_k: pane)
    if scenario == "unknown_command":
        monkeypatch.setattr(bridge, "_unknown_command_rejection_appeared", lambda *_a, **_k: True)
    monkeypatch.setattr(bridge, "_PASTE_COMMIT_TIMEOUT_S", 0.03)
    monkeypatch.setattr(bridge, "_PASTE_SETTLE_S", 0.0)
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 0.06)
    monkeypatch.setattr(bridge, "_SUBMIT_RETRY_INTERVAL_S", 0.02)

    with (
        caplog.at_level("INFO", logger=bridge.__name__),
        current_session_id_scope("child-session"),
    ):
        if outcome == "returned":
            bridge.inject_user_message(tmp_path, content=content)
        else:
            with pytest.raises(RuntimeError):
                bridge.inject_user_message(tmp_path, content=content)

    records = [
        r for r in caplog.records if getattr(r, "event_name", "").startswith("claude_native_")
    ]
    assert records[0].event_name == "claude_native_delivery_started"
    assert records[-1].event_name == "claude_native_delivery_finished"
    assert records[-1].attributes["verification"] == verification
    assert records[-1].attributes["outcome"] == outcome
    assert len({r.attributes["delivery_id"] for r in records}) == 1
    assert all(r.session_id == "child-session" for r in records)
    assert bridge._prompt_delivery_trace.get() is None
    rows = [record_to_row(r, "harness") for r in records]
    assert secret not in json.dumps(rows)
    assert secret not in caplog.text
    assert rows[-1]["attributes"]["verification"] == verification

    if scenario == "unknown_command":
        assert enters == 2
        assert records[-1].attributes["attempt"] == 2

    if scenario == "blank_line":
        warning = next(r for r in records if r.event_name == "claude_native_submit_unverified")
        observed = next(r for r in records if r.event_name == "claude_native_draft_observed")
        assert warning.levelname == "WARNING"
        assert observed.attributes["draft_seen"] is False
        assert observed.attributes["needle_visible_below_prompt"] is True
        assert records[0].attributes["leading_blank_line"] is True
        assert enters == 1
        assert pane == "❯ \n" + secret
    elif scenario in {"normal", "retry", "timeout", "empty_capture", "missing_glyph"}:
        observed = next(r for r in records if r.event_name == "claude_native_submit_verification")
        assert observed.attributes["retries"] == enters - 1
        if scenario == "retry":
            assert enters == 2
        if scenario in {"empty_capture", "missing_glyph"}:
            assert observed.levelname == "WARNING"
    if scenario in {"startup_error", "cancelled"}:
        assert records[-1].attributes["stage"] == "waiting_for_prompt"
    elif scenario == "transport_error":
        assert records[-1].attributes["stage"] == "pasting"
