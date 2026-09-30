# Claude native prompt delivery diagnostics

When a prompt remains in Claude's terminal composer, inspect the bridge delivery
records alongside the runner's submission-hook record. A completed Omnigent
delivery turn does not prove Claude started a model turn.

These events use the existing process logger and debug-log sink. Hosts with the sink configured send them to their configured debug-log table.
Other deployments need to collect
their local harness/runner logs; this instrumentation does not enable log shipping.

## Events

| Event | Meaning |
| --- | --- |
| `claude_native_delivery_started` | One web/SDK message entered the bridge. Records byte/newline counts and whether its first line is blank, without prompt text. |
| `claude_native_delivery_stage` | Entered a stage: waiting for tmux, restoring input, waiting for the prompt, pasting, waiting for the draft, submitting, or verifying. |
| `claude_native_draft_observed` | Draft polling ended. Records `draft_seen`, wait duration, poll/empty-capture counts, and a content-free summary of the final capture. |
| `claude_native_submit_sent` | The initial tmux Enter command returned successfully. This is not an acceptance acknowledgment. |
| `claude_native_submit_unverified` | **Warning:** the draft was never observed, so Enter was sent without submission verification. |
| `claude_native_submit_verification` | Records the verification observation, duration, polls and retry count. |
| `claude_native_delivery_finished` | The bridge returned, raised, or was cancelled; includes the last stage, verification result and exception class, without exception text. |
| `claude_native_prompt_submit_hook` | The runner observed Claude's parent-session `UserPromptSubmit` hook. This establishes submission reached Claude; later policy/routing hooks can still block execution. |

Bridge events share `attributes.delivery_id` and a session ID. `attempt` increases
when an unknown slash command is re-delivered as plain text. `elapsed_ms` measures
from entry, including the injection-lock wait; differences between stage times
locate startup or transport delays. Hook events are emitted independently by the
runner and correlate by session and `hook_recorded_at`, not delivery ID. They can
also originate from terminal-typed prompts. A replayed hook can be recognized by
its Claude session ID, cursor and recorded timestamp.

Verification results deliberately describe observations:

- `unverified`: no visible draft was established before Enter.
- `draft_absent`: the draft no longer matched in a capture with a prompt glyph.
  This remains a visual heuristic, not proof of model execution.
- `inconclusive_capture`: verification returned after an empty capture or one
  without a prompt glyph. This warns about missing evidence without changing
  delivery behavior.
- `draft_still_present`: retries exhausted the submission timeout.
- `not_started`: no verification result was reached, for example a startup,
  pending-question, transport or cancellation failure.

No new event contains prompt text, the matching needle, raw pane contents, or
raw hook payloads. Capture summaries include dimensions and whether the needle
appeared below the last prompt glyph; wrapped or hidden text may not match.

## Query a session

Use the incident's UTC window and session ID:

```sql
SELECT client_time, source, app_version, level, event_name, attributes
FROM <configured_debug_log_table>
WHERE client_time BETWEEN :start_ts AND :end_ts
  AND session_id = :session_id
  AND event_name LIKE 'claude_native_%'
ORDER BY client_time
LIMIT 500;
```

For silent-delivery candidates, search for `claude_native_submit_unverified` and
`claude_native_submit_verification` with
`attributes['verification'] = 'inconclusive_capture'`. Correlate them with session
creation to isolate first prompts. Check subsequent submission hooks and output
before concluding the message remained unsent: missing hooks can also mean
missing telemetry or a stopped forwarder.

## Verify locally

```sh
python -m pytest tests/test_claude_native_delivery_logging.py -q
```

The tests simulate a leading-blank-line draft whose Enter is swallowed, retry
recovery, timeout, missing captures, startup/transport errors and cancellation.
They check correlation, unchanged delivery outcomes, table serialization and
absence of prompt text in emitted diagnostics.
