# M17 — Session log v2 and Trajectory (M16 Phase 2)

Status: in progress, 2026-11-14. Parent plan: [m16-dsh-adoption.md](m16-dsh-adoption.md).

## Decision: additive, not 2.0

M16 planned a protocol 2.0 with a 1.x translation layer. Everything Trajectory and snowpea web
need can be added instead: new envelope fields, new events, new RPCs. Old clients ignore what
they do not know, so the TUI, the IDE and the browser webui keep working through the whole
phase with no translation layer. Each step below is a minor protocol version.

What we take from dsh (`docs/subsystems/session*.md`, `packages/client/ui-trajectory`):
turns and steps on every event, timing per model call, a pure projection that builds the
Trajectory rows from the same log the chat reads, subagents as their own sessions linked to
the parent, fork at a turn boundary, workspaces keyed by canonical path. What we keep:
SQLite (events and messages tables), checkpoint restore, our compaction (history is still
stored as messages; the event log stays the record of what happened).

## Steps

| Step | What | Protocol |
|---|---|---|
| 2a | **Turn and step on every event.** `SessionEvent` gains `turnId` and `step` (the model call within the turn, from 1). New events `step.started {turnId, step, provider, model}` and `step.done {turnId, step, outcome, ttftMs, decodeMs, totalMs, inputTokens, cachedInputTokens, outputTokens, reasoningChars, retries}`. Stored in two new `events` columns. | 1.44.0 |
| 2b | **Trajectory projection.** `session.trajectory {sessionId, turnId?, afterSeq?, limit?}` folds the event log into rows `system \| user \| context \| compacted \| message \| tool \| subtool` grouped by turn and step, each row carrying `sourceSeq`/`resultSeq`, timing and usage. Cached per session with a high-water mark. | 1.45.0 |
| 2c | **Fork.** `session.fork {sessionId, atTurnId?}` copies history and events up to the end of a finished turn into a new session (`forkedFrom`, `forkedAtSeq`), closes nothing in the source. Checkpoint restore stays as it is. | 1.46.0 |
| 2d | **Workspaces.** `workspace.list/add/rename/remove/reorder` over a registry keyed by canonical path (`realpath`), built first from session workdirs; pinned and archived sessions; `session.list` rows gain `workspaceId`. | 1.47.0 |
| 2e | **Subagent catalog.** The parent's `subagent.spawn/done` events already carry the child `sessionId`; `session.descendants {sessionId}` returns the tree for breadcrumbs. | 1.48.0 |
| 2f | **Deltas are not history.** `message.delta`/`message.reasoning` keep streaming but are marked ephemeral and pruned from the stored log when the turn's `message.done` lands; replay uses `message.done`. Only after every client is checked against it. | 1.49.0 |

Each step: tests, `docs/protocol.md` regenerated, the browser session told before tagging,
manual pages where users see something.

## 2a in detail

- `Session.current_step` counts model calls in the current turn; `run_turn` resets it, every
  provider call (including the budget probe and its report, an overflow retry, a summary for
  compaction is *not* a step: it is not the turn's model) increments it.
- `EventHub._emit` stamps `turnId = session.current_turn` and `step = session.current_step`
  (null outside a turn) on the envelope, and the store writes both.
- `step.done` comes from the same numbers `call_timing` already logs (`ttft`, `decode`,
  `total`, `in`, `cached`, `out`, `reasoning`, `outcome`), plus the retry count of that call.
- `session.resume` replays the two new fields; a row stored before 1.44 has them null.

## 2b in detail

- `session/trajectory.py` is a pure fold (`TrajectoryBuilder.apply`) over the stored events of
  the kinds it reads (`KINDS`; never the deltas). `session.trajectory {sessionId, afterSeq,
  limit}` keeps one builder per session (LRU of 32), folds only what was appended since its
  `last_seq`, and returns the rows that started *or finished* after `afterSeq`, so a client
  merges by `seq`.
- A turn that reports steps (1.44+) gets one `message` row per model call, from
  `step.started`/`step.done` (text, timing, usage); an older turn gets one per `message.done`.
- Checked against the 25 largest sessions of a real log: 7 889 rows in 0.17 s, no errors.

## 2c in detail — fork

- `turn.done` gains `historyLen` and `compactions` (the stored history length and the
  compaction count when the turn ended).
- `session.fork {sessionId, atTurnId?}` creates a new session in the same workdir with the
  same mode, model and agent, `forkedFrom`, `forkedAtTurnId`, and a title "<title> (fork)":
  - its history is the source history cut at that turn's `historyLen`; refused when a
    compaction happened after that turn (the cut point is inside the summary now), except
    for the latest finished turn, which forks the whole current history;
  - its event log is the source log up to that turn's `turn.done`, renumbered from 1, then a
    `session.forked {fromSessionId, atTurnId}` event the chat draws as "history above is
    inherited";
  - refused while the source turn is still running (`atTurnId` omitted means the latest
    finished turn).
- Checkpoint restore stays as it is; fork never touches the source session.

## 2d in detail — workspaces

- A `workspaces` table: `id`, `path` (realpath, unique), `title`, `position`, `created_at`.
  Filled on first open from the distinct session workdirs; a session whose workdir has no
  row gets one when it is created.
- `sessions` gains `pinned` and `archived`.
- RPC: `workspace.list` → workspaces in order, each with its session count;
  `workspace.add {path, title?}`, `workspace.rename {id, title}`, `workspace.remove {id}`
  (the registry row only — sessions stay), `workspace.reorder {ids}`;
  `session.pin {sessionId, pinned}`, `session.archive {sessionId, archived}` (refused while a
  turn runs unless `stop: true`).
- `session.list` rows gain `workspaceId`, `pinned`, `archived`, `lastUserAt` (the latest user
  prompt time, dsh's "last updated" order).
