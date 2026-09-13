# Deviations — CORE-prompt-queue (prompts submitted during an active turn)

A user keeps typing while tools and subagents run. Before `fe632c2` a second submission started a
second provider loop against the same `session.history`, so two turns interleaved their writes into
one conversation. The fix is a per-session FIFO drained by a single task. Recorded here per
`docs/design/deviations/README.md`.

1. **The queue is a plain in-memory `list` on `Session`, deliberately not persisted.**
   `Session.queued_turns` (`session/session.py:65`) is a dataclass field; a daemon restart drops
   anything still waiting. Persisting it would mean the store has to hold half-submitted user
   intent and replay it into a session that may have been restored with a different mode, workdir
   or model — a prompt firing by itself after a restart is worse than a prompt that was lost while
   the user was watching. This is a recorded known gap, not an oversight: a restart loses pending
   prompts.

2. **Attachments are captured at enqueue time, not at run time.** `start_turn`
   (`agent/loop.py:112-118`) calls `pending.take(session.id)` synchronously and carries the result
   on the `QueuedTurn`; `run_turn`/`_drive` gained an `attachments` parameter so the pre-captured
   list flows through. Resolving attachments when the turn finally runs would have re-read a
   pending set the user has since replaced, and a file moved or deleted during the wait would
   resolve to nothing.

3. **One drain task owns the whole queue.** `_drain_turns` (`agent/loop.py:156-183`) runs the first
   turn and then pops FIFO until the queue is empty; `session.turn_task` holds that task, so
   `start_turn` can tell "a turn is live" from "nothing is running" with one check. Spawning a task
   per queued prompt would have reintroduced exactly the concurrency the change removes.

4. **An interrupt now clears the queue — it did not when this first shipped.** As written in
   `fe632c2`, `_drain_turns` cleared `session.interrupt` at the top of every iteration, so pressing
   stop killed the running turn and the next queued prompt started immediately: the single most
   surprising behaviour in the change. At HEAD the loop still clears the flag before each turn, but
   checks it again after the turn returns and calls `flush_queued_turns()`
   (`agent/loop.py:136-153, 171-174`), which drops every pending prompt and emits a
   `turn.done(reason="interrupted")` for each. Fixed under CORE-fixes-v017 R3.

5. **The queue is visible to clients — also a later fix.** `fe632c2` emitted nothing at enqueue, so
   a queued prompt was indistinguishable from a dropped keystroke on every surface. At HEAD
   `start_turn` emits `turn_queued(turn_id, waiting, waiting)` and `_drain_turns` emits
   `turn_dequeued(...)` when a prompt starts (CORE-fixes-v017 R5). The emit from the synchronous
   `start_turn` goes through `_emit_soon`, an `ensure_future`, so submission order is preserved
   without making `start_turn` a coroutine and changing every caller.
