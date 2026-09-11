# Deviations — CORE-memory-race (memory nudge outliving its store on shutdown)

`tests/test_named_agent_persistence.py::test_named_agents_survive_a_restart_with_isolated_memory`
failed once (flaky, passes in isolation) with `sqlite3.ProgrammingError: Cannot operate on a closed
database`, raised inside the memory auto-remember ("nudge") background work after
`MemoryServices.close()` had already closed the `MemoryStore` it depended on. Recorded here per the
deviations-log convention (`docs/design/deviations/README.md`).

1. **Root cause: `MemoryStore._write`/`_query`/`_delete` and `MemoryStore.close` already shared one
   `threading.Lock`, but that only serialised *concurrent* access — it did nothing to stop a call
   that starts *after* `close()` from touching the now-closed `sqlite3.Connection`.** A nudge that
   was scheduled but had not yet reached its `asyncio.to_thread` call when `close()` ran would still
   run afterwards, acquire the (now free) lock, and call `self._conn.execute(...)` on a closed
   connection — hence the raw `ProgrammingError`. The fix adds an explicit `self._closed` flag,
   checked under the same lock at the top of every read/write/delete, and a new `MemoryClosed`
   (`RuntimeError` subclass) raised instead of letting the call reach the closed connection.
   `close()` itself is now idempotent (a second call is a no-op) since `Daemon.stop` and a test
   double could plausibly call it twice.

2. **`MemoryServices.close()` became `async`.** It now cancels and awaits every task tracked via the
   new `MemoryServices._track()` (the coroutine `context_for_turn`/`nudge_after_turn` actually run),
   *before* calling `self.store.close()`. This mattered even though `MemoryStore` also gained the
   `_closed` guard from (1): a task cancelled while genuinely mid-write inside `asyncio.to_thread`
   does not stop the underlying thread (Python cannot forcibly kill a running thread), so without
   draining `_tasks` first, `store.close()` could still run concurrently with that thread — safe now
   only because of the shared lock in (1), but `close()` awaiting the tasks first is what keeps
   shutdown deterministic (no "task was destroyed but it is pending" warnings, no in-flight write
   racing the daemon's own teardown log lines). The three call sites (`app_server.py::Daemon.stop`,
   and two `MemoryServices.open(...).close()` pairs in `tests/test_memory_recall.py`) were updated to
   `await`.

3. **`Core.stopping` is new, additive state**, set to `True` as the very first thing
   `Daemon.stop` does once `self.core` exists (before cancelling the update watcher, before
   `lifecycle.stop()`, before anything else). `nudge_after_turn` checks it and returns `None`
   immediately without touching the store at all — this is a fast-path that avoids even scheduling
   the tracked task once shutdown has begun, on top of (1) and (2) catching whatever still slips
   through. `context_for_turn` does not check it: recall only runs once, at the very start of a
   turn's `_drive`, long before `nudge_after_turn` — by the time shutdown could plausibly race it,
   the turn would already be racing far more than memory, and `context_for_turn` already treats every
   failure (including the new `MemoryClosed`) as `""`, which is its documented "never raises"
   contract.

4. **`nudge_after_turn`/`context_for_turn` now catch `MemoryClosed` and `asyncio.CancelledError`
   explicitly, ahead of the pre-existing broad `except Exception`.** Catching `CancelledError` here
   is deliberate and scoped: the cancellation belongs to the *child* task created by
   `MemoryServices._track`, not to the coroutine doing the `await` (which is itself part of the
   session's turn task and is not being cancelled) — so swallowing it here does not suppress a
   caller's own cancellation, it only means "the thing I was waiting on got cancelled out from under
   me by a shutdown in progress," which is exactly the no-op both functions already promise for any
   other memory failure.

5. **Not fixed, and out of scope for this story: `Daemon.stop` never cancels or awaits
   `session.turn_task`.** `SessionManager.close` does the right thing (`task.cancel()` for an
   in-flight turn) but `Daemon.stop` never calls `sessions.close()` for the sessions it holds — it
   only cancels the update-check tasks, stops the lifecycle/scheduler/gateway/named-agents, and
   closes sockets, the session store and the memory store, in that order. A turn still running at
   that point keeps going against an event loop the daemon otherwise considers stopped, and its own
   `turn.done` write to the *session* store (`session/store.py`, not `memory/store.py`) has the exact
   same shape of race this story fixes for memory — just unfixed, because it is a different
   subsystem and outside CORE-memory-race's stated scope (memory teardown only). The regression test
   added for this story (`test_close_during_a_delayed_nudge_is_clean[_repeated]` in
   `tests/test_memory_recall.py`) therefore drives `MemoryServices`/`nudge_after_turn` directly rather
   than through a full daemon + RPC turn: an earlier version of the test that stopped a real `Daemon`
   mid-turn reliably reproduced *this* session-store race instead of (or in addition to) the memory
   one, which would have made the regression test flaky/misleading for the thing it is meant to
   pin down. Worth a follow-up story: either have `Daemon.stop` close every live session before
   tearing down the stores it depends on, or give `session/store.py` the same `_closed`/`StoreClosed`
   treatment as (1).
