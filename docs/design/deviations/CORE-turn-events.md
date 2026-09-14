# Deviations — CORE-turn-events (the three progress events the desktop app was missing)

The desktop app's working indicator was built against the event stream as it stood and had to
invent three things the core never said: when a turn began, what a long command was printing while
it ran, and that a compaction was under way. Its own deviations log
(`snowpea-ide/docs/design/deviations/IDE-PROGRESS.md`, D1–D3) named each gap precisely. This story
closes all three in the core, additively.

Recorded per the deviations-log convention (`docs/design/deviations/README.md`).

`PROTOCOL_VERSION` stays **`1.5.0`**. Every change here adds a `session.event` kind; no existing
payload gained, lost or changed a field, and no method changed shape. A surface that ignores the
three new kinds behaves exactly as it did before — the TUI does precisely that, through the
`default:` arm of its store reducer.

## D1 — `turn.started {turnId, prompt, queued}`

`SESSION_EVENT_KINDS` had `turn.queued`, `turn.dequeued` and `turn.done` but nothing that announced
a turn *beginning*, so a surface's elapsed clock could only start on the first `message.delta` it
happened to overhear. A turn already in flight when a window opened was therefore timed from when
that window first heard it.

It is emitted in `run_turn` (`agent/loop.py`), at the top, before the `try` — which is after any
wait in the prompt FIFO and before anything the turn produces, including the auto-compaction
`_drive` does on its way in. Exactly one per turn, on every path, whether the turn was started by
`session.prompt`, by the queue runner, or by a direct `run_turn` caller.

**`queued` is measured, not guessed.** `_drain_turns` keeps a `waited` flag that turns true the
first time it pops the FIFO, and passes it to `run_turn(..., queued=waited)`. The first turn of a
drain never waited; every subsequent one did. The alternative — deriving it from whether a
`turn.queued` was seen — would make the flag a property of what a client observed rather than of
what happened.

**`prompt` carries the text, not a digest.** It is `text or None`, the same string
`message.user` already publishes, so there is nothing here a surface could not already see; it is
in the payload so the turn can be labelled without correlating two events.

## D2 — `tool.progress {callId, name, stream, chunk, seq, truncated}`

`tool.result` was the only event a call produced and it arrived once, whole. A ten-minute build
showed a spinner and nothing else.

**The event is advisory and says so in its own docstring.** `tool.result` remains the authoritative
record of what a call returned and what the model saw; `tool.progress` is a tail a surface may
render and may drop. Nothing in the core reads it back.

**Two tools implement it; the rest are untouched.**

- **`shell`** (`tools/shell.py`) streams the real thing. This needed a backend that can report
  output before a command exits, so `exec/backend.py` gained an optional `StreamingBackend`
  protocol (`run_stream(..., on_chunk)`) that only `LocalBackend` implements. `LocalBackend.run`
  is now a one-line delegation to `run_stream(on_chunk=None)`, so the local backend has one
  implementation rather than two that could drift — and the docker and ssh backends, which cannot
  stream, are not touched at all and fall back to `run` through an `isinstance` check.
- **`delegate_task`** (`tools/delegate.py`) forwards the child's last text. A delegation is a tool
  call that can run for minutes and its only real progress is what the child is doing, which
  `subagent.update` already carries. `SubagentRecord` gained an optional `progress` sink and
  `SubagentManager.emit_update` publishes the same `lastText` as a `tool.progress` chunk on the
  delegating call, immediately before the `subagent.update` itself. Duplication on purpose: a
  surface showing tool cards and a surface showing a subagent tree are different surfaces, and
  neither should have to correlate with the other to show a line of text.

**The sink is per call, and its absence is the off switch.** `ToolContext` gained `call_id` and
`progress`; `_run_one_call` builds a `ProgressEmitter(core, session.id, call.id, call.name)` for
every call. The emitter owns the `seq` counter — so one call's chunks are ordered without the tool
counting anything — and swallows emit failures, because a surface that went away must never fail
the tool whose output it was watching. A `ToolContext` built without a sink (tests, internal
callers) takes the non-streaming path, which is why `run_stream` had to behave identically with
`on_chunk=None`.

**Coalescing, not one event per line.** `LocalBackend.run_stream` drains both pipes continuously
into a buffer and a ticker flushes it every `FLUSH_INTERVAL` (0.1 s) in pieces of at most
`CHUNK_LIMIT` (4096 characters). A command printing a thousand lines a second produces ten events a
second, not a thousand. Decoding uses an incremental UTF-8 decoder so a multi-byte character split
across two pipe reads is not turned into two replacement characters.

**Ordering is structural, not timed.** The ticker is cancelled and a final `flush()` runs before
`run_stream` returns, so every chunk a client will ever see is emitted before the tool returns and
therefore before `_run_one_call` publishes `tool.result`. No test has to sleep to prove it.

**The cap announces itself once.** `shell` counts the UTF-8 bytes it has streamed and stops at
`MAX_STREAMED_BYTES` (256 KB), emitting one final progress with `truncated: true` and an empty
`chunk`. Note that this rarely fires in practice: `exec/backend.MAX_OUTPUT` already clips a
*captured* result at 60 000 characters, and the two limits are deliberately independent — the
stream cap protects the event log and the wire, the capture limit protects the model's context.

## D3 — `compaction.started {reason, before}`

`compaction` is published after the fact, as a transcript divider, so a surface could only say
*Compacting…* by watching the command it had itself issued — and an automatic compaction, which
nobody issued, stayed invisible until its divider landed.

`compact_session` now emits `compaction.started` before it summarises. **It is emitted after the
short-history check, not before it**: a history too short to be worth summarising returns
`compacted: false` and does no work, and announcing a compaction that never happens would leave a
surface stuck on *Compacting…* with no completion to clear it. So the pair is either both or
neither.

`reason` is `"auto"`/`"manual"` where `compaction` spells the same fact `auto: true`/`false`. The
wording differs because the payloads are read in different places and `reason` is what
`turn.done`/`turn.dequeued` already call this kind of field; the value is derived from the same
`auto` argument, so the two cannot disagree.

## What was not done

**`tool.progress` for every tool.** `read_file`, `grep`, `web_search` and the rest finish in
milliseconds or produce their output all at once; a progress event for them would be an event whose
only content is "still running", which the existing `tool.call`/`tool.result` pair already implies.

**Determinate progress.** Nothing in the stream knows how far along a turn or a command is, and the
core is not going to start guessing. `tool.progress` reports what has been produced, not what
fraction remains, and IDE-PROGRESS D7's indeterminate sweep stays the honest rendering.

**A turn id on `tool.call`.** IDE-PROGRESS D5 filters a turn's tool calls by timestamp because
`tool.call` carries no turn id. That is a real gap, but it is a change to an existing payload rather
than an addition, and this story was scoped to additive events.

## Tests

`tests/test_progress_events.py`, with `tests/fixtures/providers/fake/progress.json` (a step that
calls `shell` with a `python3 -c` command printing six lines at 80 ms intervals, skipped when there
is no `python3` on PATH):

- `turn.started` precedes the first `message.delta` and `turn.done`, once, with the prompt text and
  `queued: false`.
- Two prompts sent back-to-back during the slow command: the first turn's start is `queued: false`,
  the second's is `queued: true`, and the second start follows the `turn.dequeued`.
- The slow shell call's `tool.progress` events sit between `tool.call` and `tool.result`, number
  their `seq` from 0 with one `callId`, and reassemble to exactly the stdout the `tool.result`
  reports.
- A command that outpaces `MAX_STREAMED_BYTES` gets exactly one final `truncated: true` with an
  empty chunk, and its result is unaffected.
- A `ToolContext` with no sink streams nothing.
- A delegation's `emit_update` publishes the child's last text as a `tool.progress` chunk on the
  delegating call and leaves `subagent.update` unchanged.
- `compaction.started` precedes `compaction`, reports the same `before`, and is **not** emitted for
  a history too short to compact.

`tests/test_context.py::test_auto_compaction_fires_at_the_threshold` was updated: the event that
now sits immediately before `compaction` is `compaction.started`, and the test pins that ordering
rather than loosening its assertion. `tests/test_session_loop.py`'s `fake_run_turn` gained the new
`queued` keyword.
