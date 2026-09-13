# Deviations — CORE-schedule-origin (`origin_session_id`, reminders delivered back to their session)

A scheduled reminder created with `/schedule` from inside the TUI was written to `jobs.log` and
nowhere else, so the user who asked for it never saw it. `0f41a4d` stamps the creating session onto
the job and delivers the answer back into that conversation. Recorded here per
`docs/design/deviations/README.md`.

1. **The `origin_session_id` column is added by an online `ALTER TABLE` at store init, not by a
   migration framework.** `scheduler/jobs.py:243-244` reads the table's columns and issues
   `ALTER TABLE jobs ADD COLUMN origin_session_id TEXT` when the column is missing; the `CREATE TABLE`
   text (`:48`) carries it for fresh databases. The scheduler store is one file with a handful of
   columns and no migration history to speak of — introducing a versioned migration runner for one
   nullable column would be more machinery than the thing it manages, and an `ADD COLUMN` with a NULL
   default is the one schema change SQLite performs cheaply and without a table rewrite.

2. **A closed session is restored purely to deliver, then closed again.** `_deliver_to_session`
   (`scheduler/scheduler.py:354-377`) calls `sessions.restore()` when the session is not live, emits
   the event, and closes it again in a `finally` guarded by `contextlib.suppress`. The alternative was
   to drop the reminder when its session is not open, which would make a reminder set at the end of a
   working day useless — the session is almost never still live when the job fires. Leaving the
   restored session open instead would slowly accumulate sessions nobody asked to reopen.

3. **An explicit `job.channel` is additive rather than a replacement.** `deliver()` calls
   `_deliver_to_session` **first**, unconditionally, and only then looks at the channel
   (`scheduler.py:338-352`); the jobs log still gets the line when the session delivery failed or when
   a channel was named explicitly. Before this change, naming a channel meant the creating session
   heard nothing at all. Note the one hole in the "additive" story: when a gateway is bound and
   accepts the message, `deliver()` returns early and skips the log append, so an explicit channel
   that succeeds is recorded in the session and the gateway but not in `jobs.log`.

4. **A missing session degrades to a `log.warning` plus the jobs log, never an error.**
   `scheduler.py:363-365`. The job already ran and its answer already exists; failing the run because
   the conversation that requested it has since been deleted would turn a delivery problem into a
   scheduling problem, and would retry work that succeeded.

5. **The delivered event is a plain `message.done` with a fixed prefix**,
   `"⏰ Scheduled reminder ({job.id})"` (`scheduler.py:371`), rather than a new event kind. Every
   surface already renders `message.done`; a new kind would have needed a client change in each of
   them before anyone could see a reminder at all. The prefix carries the job id so the user can act
   on it (`/schedule delete <id>`) without a structured field.

6. **The stamping happens at both creation sites** — `commands/schedule_cmd.py:113` for the slash
   command and `scheduler/tools.py:112` for the agent tool — rather than being defaulted inside
   `add_job()`, because the store has no notion of a current session and a job created by the gateway
   legitimately has no origin.

7. **`docs/protocol.md` was regenerated in this commit**, which is also where `session.deleteSaved`,
   the `session.list` params and `SessionSummary.lastPrompt` first reached the generated protocol
   documentation — they had landed in `protocol.py` several commits earlier without a regeneration.
