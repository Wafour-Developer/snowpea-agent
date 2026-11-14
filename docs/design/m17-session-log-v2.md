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
