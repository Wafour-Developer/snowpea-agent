# Deviations — CORE-session-resume (persistent resume, the saved-session picker, `lastPrompt`)

`/resume` used to mean "reattach to a session this daemon still holds in memory"; after a daemon
restart the conversation was gone even though its rows were still in SQLite. Five commits
(`f55d57f`, `569caf9`, `633db7d`, `7b87085`, `0b3e09c`) turn resume into a real restore, add a
picker over saved sessions, and make the TUI draft editable. Recorded here per
`docs/design/deviations/README.md`.

1. **History is snapshotted with `store.replace_messages()` after every turn — a whole-history
   rewrite, not an append.** `agent/loop.py:74-86` writes the full `session.history.snapshot()`
   back on each turn. An append-only message log would have been cheaper per turn but wrong:
   compaction mutates provider history *in place*, so an append log and the live history diverge
   the first time `/compact` runs, and reconciling them would mean replaying compaction events on
   read. Rewriting is idempotent and needs no reconciliation.

2. **The snapshot is best-effort: every exception is swallowed to a `log.debug`.** A storage
   failure must never fail a turn the user has already paid the provider for. It is also skipped
   entirely once `core.stopping` is set, for the same reason the final write is skipped in
   `run_turn` (see `CORE-session-race.md`).

3. **`session.resume` gained restore semantics without a protocol version bump.** None of the five
   commits touched `PROTOCOL_VERSION`. The params and result shapes are unchanged — the method
   simply succeeds now in a case that used to return "no such session" — so there is nothing an
   existing client has to learn. (`PROTOCOL_VERSION` reads `1.4.0` at HEAD; that is other work.)

4. **`session.list` grew optional params rather than a new `session.listSaved` method.**
   `SessionListParams{includeClosed=False, workdir=None}` (`server/protocol.py:302-304`) replaces
   `Empty`. Both fields default to the old behaviour, so the change is backward compatible for
   every caller that sends nothing, and there stays exactly one listing surface for a client to
   implement. A second method would have meant two result shapes to merge client-side, which is
   precisely the merge the handler now does once, server-side.

5. **Live sessions win over stored rows on id collision, and rows sort by `createdAt` descending.**
   `session_list_handler` builds a dict keyed by id from the live list first and skips any stored
   row that repeats an id, so a live session is never shadowed by its own stale snapshot. `seq`
   for a stored-only row comes from `store.max_seq()`.

6. **`lastPrompt` is computed on every `session.list` call, not only when `includeClosed` is set.**
   The enrichment loop runs unconditionally whenever a store is present, which costs one
   `messages()` read per row. That is a real cost on a long list, and it was accepted deliberately:
   a field that is sometimes populated and sometimes `None` depending on an unrelated flag is a
   contract every client has to special-case, and the picker needs the label for live sessions too.

7. **The TUI holds `initialSessionId` as a prop and the current id in local state.** `app.tsx` used
   to take `sessionId` as a fixed prop; resuming now dispatches a `session/reset` action
   (`tui/src/state/store.ts:204`) that clears the refs as well as the transcript. Mutating a prop's
   worth of state in place left stale turn refs pointing at the previous session.

8. **Bare `/resume` opens a picker; `/resume <sessionId>` is a distinct parse.** The argument form
   landed first (`f55d57f`), the picker two commits later (`633db7d`), and both were kept — the
   picker is for a human, the explicit id is for a script or a copied line from a log.
