# Deviations — CORE-session-delete (`session.deleteSaved`, `/session delete`, `/session clear`)

Once sessions survived a daemon restart they also had to be removable, or the picker grew without
bound and every abandoned thread stayed on disk. `ce413b1` adds one RPC method and two slash
commands. Recorded here per `docs/design/deviations/README.md`.

1. **The method is named `session.deleteSaved`, not `session.delete`.** The longer name is the
   whole point: it must be unmistakable at the call site that this removes *persisted* rows and
   never touches a session that is currently running. A method named `session.delete` sitting next
   to `session.close` in `METHODS` would read as the harsher sibling of close.

2. **Live sessions are filtered out server-side rather than raising.** `session_delete_saved_handler`
   (`server/session_handlers.py:253-274`) builds `live_ids` from `core.sessions.list()` and excludes
   those ids from the delete set. Erroring instead would make `/session clear --all` unusable from
   inside a session — which is the only place a user would ever type it.

3. **One transaction, three tables.** `store.delete_sessions()` (`session/store.py:146-166`) deletes
   from `messages`, `events` and `sessions` under one lock and one `commit()`, so a crash cannot
   leave orphaned message rows pointing at a session id that no longer exists.

4. **`deleted` now reports rows actually removed, not the number of ids asked for.** This is a
   correction to how the method first shipped: `ce413b1` returned `len(ids)`, which overcounted an
   id that was never stored. At HEAD the count is `max(cursor.rowcount, 0)` from the
   `DELETE FROM sessions` statement (`session/store.py:159-162`), fixed under CORE-fixes-v017 R6.

5. **Deleting a session now also purges its on-disk bytes.** As first written, delete left
   `<home>/attachments/<id>/` behind forever — an unbounded disk leak and a privacy surprise for
   anyone who deleted a thread *because* of what they had pasted into it. At HEAD
   `_purge_session_files` (`server/session_handlers.py:277-295`) removes the attachment and audio
   directories after the rows go, fixed under CORE-fixes-v017 R4. The purge is best-effort: the
   rows are already gone, so a file that will not delete logs a warning and must not fail the RPC.
