# M12 Contract — Terminal UI Surface (binding)

The TUI is a **client**, not a second copy of the daemon: everything here is surface-local behaviour
on top of the protocol. Where this document and [`../protocol.md`](../protocol.md) disagree, the
protocol wins. Builds on [`m1-core-contract.md`](m1-core-contract.md) §13 (TUI deviations, US-008)
and §11 (SDK). Source of record: `tui/src/`.

Acceptance criteria: **AC-37 … AC-42**.

## 1. Layout

Two layouts, one component tree. Inline (default) writes into the terminal's scrollback and keeps
it: the logo is printed once, finished transcript entries go to Ink's `<Static>`, and the only live
region is the tail. `--fullscreen` opts into the alternate-buffer layout
(`tui/src/components/FullscreenLayout.tsx`). `--no-fullscreen`, `--fullscreen=false` and
`SNOWPEA_TUI_INLINE=1` force inline (`tui/src/index.tsx`, `fullscreen: fullscreenRequested &&
!inlineRequested`). Everything in the fullscreen branch MUST be inert while inline — `app.tsx`
guards the alternate-buffer work with `if (fullscreen)` and computes no transcript lines otherwise —
so the two layouts cannot drift into two behaviours.

Regions, top to bottom: transcript → (update banner) → bottom panels → status HUD.

**AC-37.** The persistent bottom regions MUST be separated by full-width rules
(`components/SectionRule.tsx`): one terminal-width `─` run, optionally ending in a label (`app.tsx`
passes `Team: <id>` when a team is active), truncated with `.slice(0, width)` and
`wrap="truncate-end"` inside an `overflow="hidden"` box so it can never wrap and shift the layout by
a row. A bottom region without a rule above it is a layout bug — the rules are what make the regions
readable as regions rather than as one run-on block.

## 2. Modes and the HUD

`layout/hud.ts::buildHudSegments` builds the segment list in a fixed order: toast, version, cwd,
model, **mode**, context, tokens, TTS indicator, tool count, session, daemon, approvals, running
command, status. Mode is shown **before** status, not after; status is always last. Segments with
nothing to say are omitted entirely rather than drawn empty, and `layoutHud` drops the least
important segments first (`priority`) before it truncates.

Mode changes are local key gestures that call `session.setMode`:

- `⇧Tab` cycles `accept → auto → plan` (`state/mode.ts::MODE_CYCLE_ORDER` and `cycleMode`; an
  unknown mode resets to `accept`). Ink 5 reports the gesture as `key.tab && key.shift`; some
  terminals instead send a raw `[Z`, so both are handled.
- `Ctrl+P` toggles plan mode (`plan ↔ accept`).
- `/plan`, `/accept`, `/auto`, `/mode` remain the protocol path and MUST keep working identically.

Context is rendered from the `context` session event (M11 §1) by `layout/bottom.ts::contextSegment`:
`ctx 34% (68k/200k)` when the window is known, `ctx 12.3k used` when it is not, and the segment is
omitted entirely until a `context` event has arrived at all. An estimated reading is prefixed `~`.
The `?` spelling belongs to the CLI renderer, not this surface. Above `CONTEXT_ALERT_PERCENT` (80%),
`contextWarning` adds its own row — `[!!] context 96% — /compact to free space` — because a bare
percentage tells the user nothing they can act on.

## 3. Panels

| component | opened by | contract |
|---|---|---|
| `HelpPanel` | `F1`, `/help` | §4 |
| `AgentPanel` / `AgentTranscript` | `Ctrl+A` | subagents under the current session row, idle agents collapsed past `MAX_IDLE_ROWS`; `Enter` opens a delegate's conversation, `Esc` comes back |
| `ApprovalQueue` | always visible when non-empty; `Ctrl+R` focuses it | advisory; a failure MUST NOT block the session |
| `ApprovalPrompt` | server `approval.request` | `y`/`n` + scope resolves the promise; origin surface only |
| `DiffView` / `ToolCall` / `ToolSummary` | `Ctrl+O` expands the newest | — |
| `UpdateBanner` | `system.checkUpdate` says available, or `U` on empty input, or `/update` | y/n confirm; refuses while a turn is active |
| `AttachmentChips` | paste / drag / clipboard image / `/attach` | one chip per pending attachment |

## 4. Help panel

**AC-38.** The help panel MUST be dismissible and MUST have a bounded height — it must never expand
into terminal scrollback (`components/HelpPanel.tsx`, whose first line states the rule). `Esc`, `F1`,
`q` and `Enter` all close it; `↑↓` / `PgUp` / `PgDn` scroll within the bound.

It lists the workflow commands from one constant — `HelpPanel.WORKFLOW_COMMANDS = ralph, ralplan,
ultrawork, deepinit, deep-research, deep-interview, plan, accept, auto` (the nine AC-03 requires) —
and a `KEYS` table of seventeen lines, which MUST document at minimum:

```
Esc / F1 / q / Enter close help · ↑↓ / PgUp / PgDn scroll
$agent-name task delegates directly to a member of the active team
Ctrl+C quit · Esc outside help interrupts the current turn
Ctrl+O expand the newest tool call or diff · Ctrl+A open the agent panel
⇧Tab cycles accept -> auto -> plan · Ctrl+P toggles plan mode
```

and in practice also covers `/compact`, prompt history, agent navigation, `/resume` and `R`,
attachments, `/voice` / `/tts`, and the two approval key maps.

F1 is read from raw terminal escape sequences — `OP`, `[11~`, `[[A` — because Ink 5
removes F1 from `useInput`.

## 5. Slash commands

**AC-39.** The command table MUST NOT be hard-coded. `tui/src/slash/registry.ts` takes
`command.list` from the daemon and merges in only the **surface-only** commands — the ones the daemon
cannot implement because they are about this client's own session selection:

| command | summary |
|---|---|
| `/resume` | `Resume the last session in this directory, or /resume <sessionId>.` |
| `/session` | `Delete saved sessions: /session delete <id> \| clear [--all].` |
| `/sessions` | `List saved sessions and choose one to resume.` |

Each carries `source: "tui"`. A surface command is dropped from the merge if the daemon already
advertises that name (`withSurfaceCommands` filters on the daemon's name set), so a future core
`command.list` entry silently takes over. `registry.parse` splits only the leading `/name` and passes
the remainder through raw, because argument parsing belongs to the server-side command's
`argsSchema`.

`/update` is the one deliberate interception of a core command (`app.tsx::submit`): it calls
`client.checkUpdate(true)` and opens the same y/n banner the `U` key opens, rather than forwarding to
`command.run`, because the task requires a confirmation in this one surface. The registry also
re-reads `command.list` after `/help`, `/skill` and `/plugin`, since skills and plugins change the
table. Everything else starting with `/` goes straight to `command.run`.

## 6. Resume picker and saved sessions

**AC-40.** `/sessions` calls `session.list({includeClosed: true, workdir})` and renders a picker;
choosing a row calls `session.resume(sessionId)`, which restores a persisted session even after a
daemon restart (M1 §17-3). The active session is filtered out. Each row MUST be identifiable without
the user remembering an id — it is built from the row's `workdir`, `createdAt` and `lastPrompt`, the
session's last user message (M1 §17-2). Rows arrive already sorted newest-first; the picker MUST NOT
re-sort them, and does not.

`/resume` with no argument opens the same picker; `/resume <sessionId>` resumes that row directly,
and `R` on an empty input line is the same gesture. Both `/resume` and `/sessions` are **refused
while a turn is active**, with the toast `interrupt the current turn before resuming another
session`. A failed listing is an inline error message, never a crash.

`/session delete <id>` and `/session clear [--all]` map onto `session.deleteSaved`
(`{sessionId}` / `{workdir}` / `{all: true}`) and report the deleted count as a toast. The daemon
refuses to delete a live session, so the picker MUST NOT filter live rows out on its own — a row that
fails to delete is a row that is still running, and that is what the user should be told.

## 7. Short delegation

**AC-41.** `$agent-name <task>` in the input line delegates directly, matched by
`/^\$([A-Za-z0-9._-]+)\s+([\s\S]+)$/` in `app.tsx::submit` and dispatched as
`agent.spawn{sessionId, name, task}` rather than as a `session.prompt`. The daemon does not know the
input came from a shortcut, so the M6/M7 §3.1 refusal rules apply unchanged: an unknown name, or a
non-member while a team is active, comes back as a refused `subagent.done` and MUST be rendered as an
error, not swallowed — the call's rejection is dispatched as an `error` message.

`layout/agents.ts::buildAgentRows` draws the **session's own row first** (`currentLabel`, default
`main`), then live subagents from `subagent.*` events, then team rows derived from
`state.teamTasks` (`team.task.update` events — team rows come from the task stream, **not** from an
`agent.list` row), then idle named agents from `agent.list`. Idle rows are filtered only on
`kind !== "subagent"`, so a `kind` the client does not recognise MUST be rendered as an ordinary idle
row rather than dropped.

## 8. Input line, draft and queued prompts

The input line (`components/Chat.tsx`) is implemented directly with `useInput` (Ink 5 ships no
text-input component and a bundle dependency was not worth it). It MUST support an **editable draft
with a movable cursor** — left/right, word motion, home/end and mid-string insertion — not
append-only typing. `↑/↓` walk history when the palette is closed and select candidates when it is
open; `Tab` completes a command; `Enter` sends; `Esc` interrupts the current turn; `Ctrl+C` quits.
`U` and `R` on an empty draft are the update and resume gestures; once the draft is non-empty they
are just letters.

**AC-42.** Submitting while a turn is running MUST be accepted, not blocked. `app.tsx::submit` gates
only on an in-progress resume or update, never on `state.turnActive`, and sends `session.prompt`; the
daemon queues it (M9 §5) and answers with a `turnId` whose `turn.done` arrives later. Attachments are
captured **when the prompt is submitted** — the chips are snapshotted into the user message and
`setAttachments([])` runs immediately — so they belong to the queued entry rather than to the input
line.

**Queued-prompt indicator: pending.** The daemon emits `turn.queued` / `turn.dequeued` (M9 §5), but
no component in `tui/src/` consumes those kinds at HEAD; `queued` appears only as a *subagent* status
in `state/store.ts` and `layout/agents.ts`. When the indicator lands it MUST render two consequences
honestly:

- `Esc` interrupts the turn in flight **and drops the queue** (`session.interrupt` now calls
  `flush_queued_turns`), so every queued prompt gets `turn.dequeued{reason:"dropped"}` followed by
  `turn.done{reason:"interrupted"}`. The indicator must clear on those events, not linger.
- A daemon restart loses the queue. Pending indicators MUST be cleared on `disconnected`, not left
  waiting for a `turn.done` that will never come.

## 9. Reconnection and event hygiene

Connection state (connected / reconnecting / closed) is derived from the SDK's lifecycle events —
`disconnected{willRetry}` maps to `reconnecting` or `closed`, `reconnected` maps to `connected` — not
from a protocol notification. The SDK performs the `session.resume(afterSeq)` replay; the TUI only
displays it. `tui/src/rpc/client.ts` additionally tracks the highest `seq` per session
(`lastSeqFor` prefers the SDK's own high-water mark) and drops anything at or below it — the SDK
already de-duplicates, so this is redundant, and it stays as cheap insurance against drawing an event
twice.

`tui/src/rpc/sdk.ts` MUST **derive** `SessionEvent`, `ApprovalRequestParams`, `Mode`, `CommandInfo`
and the update shapes from `@snowpea/sdk`'s generated types rather than re-declaring them
(`type Mode = SessionSetModeParams["mode"]`, `type CommandInfo =
NonNullable<CommandListResult["commands"]>[number]`, …), so a protocol regeneration cannot drift
silently from the TUI.

## 10. Exit codes

`tui/src/index.tsx::main` returns `0` on a normal exit, `1` on a daemon connection or
session-create failure, `2` on an argument-parse error, and `RESTART_EXIT_CODE = 75` (EX_TEMPFAIL)
to request a relaunch after an update. `cli/main.py` mirrors the constant as `TUI_RESTART_EXIT = 75`
and, on seeing it, calls `relaunch()`, which waits up to `RESTART_DRAIN_SEC = 15.0` for the old
daemon's pid to disappear and then `os.execv`s the same argv. `75` is unused by every other snowpea
exit path.

## 11. Tests

The suite under `tui/test/` is the contract's executable half and MUST cover at least:
`help-ui`, `resume`, `direct-delegate`, `section-rule`, `mode-cycle`, `palette`, `slash`, `hud`,
`bottom`, `layout`, `frame`, `flicker`, `panel`, `approval`, `attach-ui`, `attachments`,
`update-ui`, `voice`, `markdown` (Unicode table alignment), `history`, `store`, `rpc-client`.
The directory also carries `agents`, `app`, `audio-runtime`, `audio-tools`, `chat`, `clipboard`,
`diff`, `focus`, `inline`, `logo`, `statics`, `summary`, `update`, `wordmark` and `working`.
