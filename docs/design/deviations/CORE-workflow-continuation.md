# Workflow continuation and surface agent panels (2026-10-02)

## Round checkpoints

A main-agent round cap is a checkpoint. When the budget probe requests more tools,
the loop records its report and automatically renews the round allowance up to
`agent.auto_budget_continuations` times (default ten) in the same turn and
conversation. It emits `hook.continue` for each
renewal, so attached surfaces can show that work is continuing. The current
permission mode and interrupt event remain in force.

After those automatic renewals, attended runs use the existing Continue/Stop
question. Unattended runs return a partial report with `reason: budget`. The
bounded window prevents an endless read loop from consuming unbounded tokens.
Subagents retain their configured incomplete-retry limit.

A budget probe's final answer must pass the same verify-on-stop and Stop-hook
checks as any other final answer. Reaching a round boundary cannot bypass them.

Regression evidence: `tests/test_tool_rounds.py` checks automatic continuation,
context retention through successive writes, bounded renewals, explicit stopping,
and work that finishes exactly at the cap.

## Workflow handoff and surface panels

Plain slash-command turns now persist a bounded, redacted user/assistant handoff
into the main session history, including failures and interruptions. Ralph adds
story statuses and state-file references. Reviewer rejection triggers bounded
repair iterations, and only an explicit positive verdict approves completion.

TUI and IDE collapse delegate lists to two newest rows plus More. Terminal rows
leave compact view after ten seconds but remain available in expanded history.
Returning focus to the command composer collapses the expanded list. Replayed
slash-command echoes reuse the optimistic user row rather than displaying twice.

Child retries default to three and carry bounded recent tool results. Explicit
timeouts, denied actions and user interruptions remain stop boundaries; binary
content and sensitive messages are not copied into textual checkpoints.

Regression evidence: `test_command_history.py`, `test_ralph_e2e.py`,
`test_session_loop.py`, `test_subagent_budget.py`, TUI `app-agents.test.tsx`,
and IDE `SubagentTree.test.ts` plus the session-store replay tests.
