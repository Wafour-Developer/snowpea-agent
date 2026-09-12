# Deviations — CORE-session-race (session-store shutdown race)

Follow-up to item 5 of `docs/design/deviations/CORE-memory-race.md`, which
fixed the same shape of bug for the *memory* store but left it open for the
*session* store: `Daemon.stop` never cancelled or awaited a session's
in-flight `turn_task` before closing `session/store.py`, so a turn's own
final `turn.done` write could land on an already-closed SQLite connection and
raise `sqlite3.ProgrammingError: Cannot operate on a closed database` out of
a background task. Recorded here per `docs/design/deviations/README.md`.

1. **`session/store.py` gained the same `_closed`/`StoreClosed` guard
   `memory/store.py` already has for `MemoryClosed`.** `Store._execute` and
   `Store._query` raise `StoreClosed` (a `RuntimeError` subclass) instead of
   reaching the closed `sqlite3.Connection`, and `Store.close()` is now
   idempotent (a second call is a no-op), matching `MemoryStore` exactly.

2. **`SessionManager.close_all()` is new.** `Daemon.stop` calls it
   immediately after setting `core.stopping = True`, and *before* the update
   watcher, lifecycle, scheduler, gateway, session store and memory are torn
   down. It cancels every live session's `turn_task` up front, then awaits
   all of them together (`asyncio.gather` under one `asyncio.wait_for` with a
   5s bound) so a task stuck outside a cancellable await cannot block
   shutdown forever, then closes each session the normal way via the
   pre-existing `SessionManager.close()` — safe here because the session
   store is still open at that point. This is the piece CORE-memory-race
   explicitly deferred ("Daemon.stop never cancels or awaits
   session.turn_task").

3. **`EventHub.emit` and `SessionManager.close`'s `close_session` write now
   catch `StoreClosed` and drop the write silently (logged at `debug`).**
   This is defense in depth on top of (2): `close_all` should mean nothing
   is still writing by the time the store actually closes, but a call that
   is not routed through `close_all` (a stray `session.close` racing
   shutdown from an RPC handler, say) is now safe too, exactly like
   `nudge_after_turn`/`context_for_turn` catching `MemoryClosed` in
   CORE-memory-race.

4. **`agent/loop.py`'s `run_turn` skips the final `turn.done` event write on
   `asyncio.CancelledError` when `core.stopping` is set.** A turn cancelled
   by `close_all` during shutdown has nothing useful left to tell any
   subscriber (sockets are being torn down in the same `Daemon.stop` call),
   so this avoids a pointless write racing shutdown even before (1)/(3)
   would catch it. A turn cancelled for any other reason (e.g. a
   `session.close` call while the daemon keeps running) still emits
   `turn.done{interrupted}` as before — `core.stopping` is the only thing
   this branches on.

5. **Regression test**: `tests/test_session_loop.py::test_stop_mid_turn_is_clean`
   (and `_repeated`, looping the same scenario 5x over fresh homes) uses a
   new fake-provider fixture (`tests/fixtures/providers/fake/shutdown_race.json`)
   whose `slow reply` step has `delaySec: 2`. The test starts that prompt,
   sleeps briefly so the turn task is genuinely inside the provider's delay,
   then calls `daemon.stop()` immediately and asserts it raises nothing and
   logs nothing at `ERROR`. It then reopens `session/store.py`'s `Store`
   directly on the same `$SNOWPEA_HOME` and confirms the session row reads
   back with `closed_at` set and the file is not corrupted (a fresh
   `Store.open` succeeding at all is itself the corruption check, since a
   `sqlite3.ProgrammingError`/`DatabaseError` from a torn write would fail
   the open or the `SELECT`).

6. **Not attempted, out of scope for this story: turn resumption.** A turn
   cancelled by `close_all` is simply gone — the in-flight tool-call/model
   round it was in the middle of is not persisted or replayed on the next
   daemon start. `session.list`/the session-store row correctly show the
   session as closed, which is all this story's regression test checks; a
   client that wants "resume where the turn left off" after a daemon
   restart would need a separate design (e.g. persisting partial turn state
   before cancelling, well beyond "stop safely").
