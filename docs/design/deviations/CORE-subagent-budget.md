# Deviations — CORE-subagent-budget (a turn that runs out of tool rounds must still report)

Seen in the desktop app, session `s-201c69d433d8`: the main agent delegated a task, the child ran
about fifty `read_file` / `shell` rounds, and the turn ended with
`error{internal, "stopped after 50 tool rounds"}` followed by `turn.done{reason:"error"}` — **no
`message.done` at all**. `_ChildWatcher` only ever learns the child's answer from `message.done`,
so `record.summary` stayed empty, `_last_assistant_text` found the child's opening line, and
`delegate_task` handed the parent a one-line summary of nothing. The parent, reasonably, announced
that "the first request ended without a report" and delegated the same work three more times.

Recorded per the deviations-log convention (`docs/design/deviations/README.md`).

`PROTOCOL_VERSION` stays **`1.5.0`**. The only protocol change is one additional value in the
`turn.done.reason` union, which is additive: a surface that does not know `"budget"` must treat it
as an ordinary end of turn, exactly as it would treat a reason added tomorrow.

## D1 — the budget writes a report before it ends the turn

`_drive` (`agent/loop.py`) now calls `_budget_report` the moment `rounds_left` reaches zero, on
every path, before anything else is decided. It is one provider call **with no tools** — the specs
list passed to `_model_turn` is empty — carrying `BUDGET_INSTRUCTION`: *stop and report what you
did, what you found, what remains, which files you changed*. The answer is appended to the history
as the assistant turn it is and published as a normal `message.done`, so every consumer that was
already listening (the TUI, the chat gateways, `_ChildWatcher`) picks it up with no new code.

**The instruction is a local message, not a history entry.** It is appended to the message list for
that one call, the way `_model_turn` already resumes an output-limit stop. Putting it in the
history would make it the session's most recent *user* message, and two things are read off that:
`detected_language` (so the checkpoint question would switch to English mid-conversation) and any
later recall. Only the report itself joins the history.

**It never fails a turn.** A provider error inside `_budget_report` is logged and swallowed, and an
empty answer is replaced by `BUDGET_EMPTY_REPORT`. There is no path on which the budget ends a turn
with nothing said.

## D2 — `turn.done{reason:"budget"}` instead of `error`

The `error{internal}` event is gone. Running out of rounds is not a fault: the turn did work, said
what it did, and stopped at a limit the operator configured. `TurnReason` gains `"budget"`
(`server/protocol.py`), and the headless exit map (`cli/render.py`) sends it to **1**, the code it
already produced when the same situation was reported as an error — the work is unfinished, and a
script that checks the exit status should keep hearing so.

`c0c10a7`'s checkpoint survives, in second place: **report first, then ask**. The question is put
only to a session someone is watching (`not unattended and not session.is_subagent`); a delegated
or scheduled turn has nobody to answer and ends on its report. When the person chooses to continue,
`BUDGET_CONTINUE_INSTRUCTION` is appended as a user message — without it the model's next turn
would be an answer to its own report rather than a resumption of the work.

## D3 — the budget is configurable, and the child is told what it is

`tool_rounds_for(core, session)` (`agent/loop.py`) owns the whole precedence chain, highest first:

1. the agent definition's `tool_rounds:` frontmatter, carried on the child as `session.tool_rounds`;
2. `agents.toolRounds[<agent name>]`, when the setting is a mapping (shaped like `agents.models`);
3. `agents.toolRounds` as a number, or the mapping's `"default"` (or `"*"`) key;
4. `agent.max_tool_rounds`, **floored at `SUBAGENT_TOOL_ROUNDS` (80) for a delegated child**.

**Why a floor rather than the flat default of 50 the story asked for.** `agent.max_tool_rounds` has
been 200 since CORE-fixes-v017, and the story was written against the 50 the contract doc still
quoted. Lowering an existing default is not a fix for a child that ran out of rounds, so the number
only ever moves up: a worker gets at least 80 rounds even when the session default is set lower,
because it reads far more than the session that delegated to it and the parent sees only its final
report. Anything explicitly configured wins outright, floor included.

The resolved number is appended to the child's brief (`BUDGET_LINE`, `agent/subagent.py`): *you
have N tool rounds, leave enough of them to write your report*. A worker that knows its budget
spends it deliberately; one that does not surveys until it is cut off.

## D4 — what `delegate_task` hands back

`SubagentResult` gains `reason`, `rounds_used` and `last_calls`, filled in by `_ChildWatcher` (which
now also reads the child's `turn.done`) and by `_execute` (which reads `session.rounds_used`, a
counter the loop keeps per turn). `render_report` (`tools/delegate.py`) turns them into what the
parent model actually reads:

```text
status: done
reason: budget
roundsUsed: 80

<the child's report>

The last tool calls it made:
- read_file {"path": "core/snowpea_core/agent/loop.py"}
…

This task is unfinished: the child used its whole tool-round budget. …
```

The last three calls are included for every partial reason (`budget`, `timeout`, `error`,
`interrupted`, `denied`) so the parent can see where the child was rather than guess. A budget
result is `ok=True`: there is a usable report, and returning it as a failed call is what invites
the parent to re-delegate. **The output is never the empty string** — `NO_REPORT` stands in when
everything else is missing.

## Tests

`tests/test_subagent_budget.py` (fake provider, `agents.toolRounds = {"default": 3}`): a child that
loops on `read_file` ends with a `message.done` carrying its report and `turn.done{budget}` and no
`error` event; the parent's result is `reason="budget"`, `roundsUsed=3`, the last three calls, and a
rendered report that says the task is unfinished; the brief carried `BUDGET_LINE`; the precedence
chain and the subagent floor hold; a definition round-trips `tool_rounds:`; an empty summary still
renders something. `tests/test_tool_rounds.py` covers the attended path: the report is published
first, then the checkpoint question, then the turn continues; choosing "stop" ends it with
`turn.done{budget}` and no `error`.
