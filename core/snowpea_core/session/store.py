"""SQLite persistence for sessions, their event log and their message history.

One file, ``$SNOWPEA_HOME/state.db``.  ``sqlite3`` is synchronous, so every
public method here is ``async`` and pushes the actual work onto a worker thread
with :func:`asyncio.to_thread`; a :class:`threading.Lock` serialises access to
the single shared connection.  See ``docs/design/m1-core-contract.md`` §4.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from snowpea_core.config.paths import Paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    workdir        TEXT NOT NULL,
    mode           TEXT NOT NULL,
    provider       TEXT,
    model          TEXT,
    origin_surface TEXT,
    created_at     TEXT NOT NULL,
    closed_at      TEXT,
    parent_session_id TEXT,
    kind           TEXT NOT NULL DEFAULT 'chat',
    job_id         TEXT,
    effort         TEXT
);
CREATE TABLE IF NOT EXISTS events (
    session_id   TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts           TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS messages (
    session_id   TEXT NOT NULL,
    idx          INTEGER NOT NULL,
    role         TEXT NOT NULL,
    content_json TEXT NOT NULL,
    PRIMARY KEY (session_id, idx)
);
CREATE INDEX IF NOT EXISTS events_session_seq ON events (session_id, seq);
"""

#: Columns added to ``sessions`` after the first release.  Applied on every
#: open with ``ALTER TABLE ... ADD COLUMN`` when they are missing, so a state.db
#: written by an older build keeps working and its rows read back as ordinary
#: chat threads (CORE-session-kind).
SESSION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("parent_session_id", "TEXT"),
    ("kind", "TEXT NOT NULL DEFAULT 'chat'"),
    ("job_id", "TEXT"),
    ("effort", "TEXT"),
)


class StoreClosed(RuntimeError):
    """A :class:`Store` operation was attempted after :meth:`Store.close`.

    Raised instead of letting the underlying ``sqlite3.ProgrammingError`` leak
    out of a session turn task that outlived the daemon that scheduled it
    (CORE-session-race, the same shape as ``MemoryClosed`` in
    ``memory/store.py`` for CORE-memory-race).
    """


class Store:
    """Async facade over one SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns a newer build needs to an older ``sessions`` table.

        Idempotent: existing columns are skipped, so opening a fresh database
        (where ``SCHEMA`` already declares them) does nothing.  Called with
        :attr:`_lock` held.
        """
        existing = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(sessions)")}
        for name, decl in SESSION_COLUMNS:
            if name in existing:
                continue
            self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")

    @classmethod
    def open(cls, paths: Paths) -> Store:
        """Open (creating if needed) the store under ``$SNOWPEA_HOME``."""
        paths.ensure()
        return cls(paths.state_db)

    # -- plumbing ------------------------------------------------------
    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            if self._closed:
                raise StoreClosed("session store is closed")
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            if self._closed:
                raise StoreClosed("session store is closed")
            return list(self._conn.execute(sql, params))

    # -- sessions ------------------------------------------------------
    async def insert_session(
        self,
        session_id: str,
        workdir: str,
        mode: str,
        provider: str | None,
        model: str | None,
        origin_surface: str | None,
        created_at: str,
        parent_session_id: str | None = None,
        kind: str = "chat",
        job_id: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT OR REPLACE INTO sessions"
            " (id, workdir, mode, provider, model, origin_surface, created_at, closed_at,"
            "  parent_session_id, kind, job_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                session_id,
                workdir,
                mode,
                provider,
                model,
                origin_surface,
                created_at,
                parent_session_id,
                kind or "chat",
                job_id,
            ),
        )

    async def update_model(
        self, session_id: str, provider: str | None, model: str | None
    ) -> None:
        """Persist a session's pinned route so it survives a restart.

        ``/model`` used to write only ``settings.providers.<vendor>.model`` and
        mutate the live session, so the choice was lost the moment the session
        was restored from the store (CORE-model-assignment B-P2-3).
        """
        await asyncio.to_thread(
            self._execute,
            "UPDATE sessions SET provider = ?, model = ? WHERE id = ?",
            (provider, model, session_id),
        )

    async def update_effort(self, session_id: str, effort: str | None) -> None:
        """Persist the session's effort pin so it survives a resume.

        A pin that vanished on restore would be worse than no pin at all: the
        session would quietly go back to the configured tier while the surface
        still showed the one the user chose (CORE-effort).
        """
        await asyncio.to_thread(
            self._execute,
            "UPDATE sessions SET effort = ? WHERE id = ?",
            (effort, session_id),
        )

    async def update_mode(self, session_id: str, mode: str) -> None:
        await asyncio.to_thread(
            self._execute, "UPDATE sessions SET mode = ? WHERE id = ?", (mode, session_id)
        )

    async def close_session(self, session_id: str, closed_at: str) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE sessions SET closed_at = ? WHERE id = ?",
            (closed_at, session_id),
        )

    async def list_sessions(self, *, include_closed: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sessions"
        if not include_closed:
            sql += " WHERE closed_at IS NULL"
        sql += " ORDER BY created_at"
        rows = await asyncio.to_thread(self._query, sql)
        return [dict(row) for row in rows]

    def session_workdirs(self, *, limit: int = 50) -> list[str]:
        """Distinct workdirs of stored sessions, newest first (M15 §B5a).

        Synchronous on purpose: :meth:`SkillLoader.workdirs` is called from the
        synchronous scan that ``wire_core`` runs before the event loop owns the
        daemon, and one indexed ``SELECT`` is cheaper than making the whole
        scan path async.
        """
        try:
            rows = self._query(
                "SELECT workdir, MAX(created_at) AS last FROM sessions "
                "GROUP BY workdir ORDER BY last DESC LIMIT ?",
                (int(limit),),
            )
        except (StoreClosed, sqlite3.Error):
            return []
        return [str(row["workdir"]) for row in rows if row["workdir"]]

    async def session(self, session_id: str) -> dict[str, Any] | None:
        """Return one persisted session, including a cleanly closed one."""
        rows = await asyncio.to_thread(
            self._query, "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        return dict(rows[0]) if rows else None

    async def reopen_session(self, session_id: str) -> None:
        """Mark a persisted session live again without replacing its metadata."""
        await asyncio.to_thread(
            self._execute, "UPDATE sessions SET closed_at = NULL WHERE id = ?", (session_id,)
        )

    async def delete_sessions(self, session_ids: list[str]) -> int:
        """Delete saved session rows and their conversation data atomically."""
        ids = list(dict.fromkeys(session_ids))
        if not ids:
            return 0

        def delete() -> int:
            with self._lock:
                if self._closed:
                    raise StoreClosed("session store is closed")
                marks = ",".join("?" for _ in ids)
                self._conn.execute(f"DELETE FROM messages WHERE session_id IN ({marks})", ids)
                self._conn.execute(f"DELETE FROM events WHERE session_id IN ({marks})", ids)
                cursor = self._conn.execute(f"DELETE FROM sessions WHERE id IN ({marks})", ids)
                # Rows actually removed, not ids asked for: an id that was
                # never stored must not be counted (CORE-fixes-v017 R6).
                deleted = max(cursor.rowcount, 0)
                self._conn.commit()
                return deleted

        return await asyncio.to_thread(delete)

    # -- events --------------------------------------------------------
    async def append_event(
        self, session_id: str, seq: int, kind: str, payload: dict[str, Any], ts: str
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT OR REPLACE INTO events (session_id, seq, kind, payload_json, ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, kind, json.dumps(payload), ts),
        )

    async def events_after(self, session_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        """Stored events with ``seq`` strictly greater than ``after_seq``."""
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM events WHERE session_id = ? AND seq > ? ORDER BY seq",
            (session_id, after_seq),
        )
        return [
            {
                "sessionId": row["session_id"],
                "seq": row["seq"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "ts": row["ts"],
            }
            for row in rows
        ]

    #: Event kinds that open and close a turn.  Only these are read back when
    #: looking for a turn a crash left open (CORE-dangling-turns).
    TURN_MARKERS = ("turn.started", "turn.done")

    def _open_turns(self, session_id: str | None) -> dict[str, list[str]]:
        """Turn ids that were started and never finished, per session.

        A crash or a ``kill -9`` leaves ``turn.started`` in the log with no
        ``turn.done`` after it, so every client that replays the history sees a
        turn that runs forever.  Matching is by turn id rather than by "what
        was the last marker": ``flush_queued_turns`` writes ``turn.done`` for
        dropped prompts *while* the real turn is still running, so the last
        marker in the log is not always the running turn's.

        Called with :attr:`_lock` held.
        """
        sql = (
            "SELECT session_id, seq, kind, payload_json FROM events"
            " WHERE kind IN (?, ?)"
        )
        params: tuple[Any, ...] = self.TURN_MARKERS
        if session_id is not None:
            sql += " AND session_id = ?"
            params = (*params, session_id)
        sql += " ORDER BY session_id, seq"
        open_turns: dict[str, list[str]] = {}
        for row in self._conn.execute(sql, params):
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):  # pragma: no cover - a corrupt row
                continue
            turn_id = str(payload.get("turnId") or "")
            if not turn_id:
                continue
            pending = open_turns.setdefault(str(row["session_id"]), [])
            if row["kind"] == "turn.started":
                if turn_id not in pending:
                    pending.append(turn_id)
            elif turn_id in pending:
                pending.remove(turn_id)
        return {key: value for key, value in open_turns.items() if value}

    def _repair(self, session_id: str | None, ts: str) -> list[dict[str, Any]]:
        """Append a synthetic ``turn.done`` for every turn left open."""
        written: list[dict[str, Any]] = []
        with self._lock:
            if self._closed:
                raise StoreClosed("session store is closed")
            for sid, turn_ids in self._open_turns(session_id).items():
                row = self._conn.execute(
                    "SELECT MAX(seq) AS m FROM events WHERE session_id = ?", (sid,)
                ).fetchone()
                seq = int(row["m"] or 0)
                for turn_id in turn_ids:
                    seq += 1
                    payload = {
                        "turnId": turn_id,
                        "reason": "interrupted",
                        "synthetic": True,
                    }
                    self._conn.execute(
                        "INSERT OR REPLACE INTO events"
                        " (session_id, seq, kind, payload_json, ts) VALUES (?, ?, ?, ?, ?)",
                        (sid, seq, "turn.done", json.dumps(payload), ts),
                    )
                    written.append(
                        {
                            "sessionId": sid,
                            "seq": seq,
                            "kind": "turn.done",
                            "payload": payload,
                            "ts": ts,
                        }
                    )
            self._conn.commit()
        return written

    async def repair_dangling_turns(
        self, session_id: str | None = None, *, ts: str | None = None
    ) -> list[dict[str, Any]]:
        """Close turns a crash left open; returns the events it wrote.

        Called once when the daemon starts (so every stored thread is
        consistent before any client connects) and again for one session in
        :meth:`SessionManager.restore`, for a thread whose row was written
        after that scan.  Idempotent: a repaired turn has a ``turn.done`` and
        is no longer open (CORE-dangling-turns).
        """
        from snowpea_core.config.paths import utc_now

        return await asyncio.to_thread(self._repair, session_id, ts or utc_now())

    async def max_seq(self, session_id: str) -> int:
        rows = await asyncio.to_thread(
            self._query, "SELECT MAX(seq) AS m FROM events WHERE session_id = ?", (session_id,)
        )
        value = rows[0]["m"] if rows else None
        return int(value) if value is not None else 0

    # -- messages ------------------------------------------------------
    async def append_message(self, session_id: str, idx: int, role: str, content: Any) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT OR REPLACE INTO messages (session_id, idx, role, content_json)"
            " VALUES (?, ?, ?, ?)",
            (session_id, idx, role, json.dumps(content)),
        )

    async def replace_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Atomically replace the resumable provider history for one session."""

        def replace() -> None:
            with self._lock:
                if self._closed:
                    raise StoreClosed("session store is closed")
                self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                self._conn.executemany(
                    "INSERT INTO messages (session_id, idx, role, content_json)"
                    " VALUES (?, ?, ?, ?)",
                    [
                        (session_id, index, item["role"], json.dumps(item["content"]))
                        for index, item in enumerate(messages)
                    ],
                )
                self._conn.commit()

        await asyncio.to_thread(replace)

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT role, content_json FROM messages WHERE session_id = ? ORDER BY idx",
            (session_id,),
        )
        return [{"role": row["role"], "content": json.loads(row["content_json"])} for row in rows]

    def close(self) -> None:
        """Idempotent: a second call (e.g. from a double ``Daemon.stop``) is a no-op."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()


__all__ = ["SCHEMA", "SESSION_COLUMNS", "Store", "StoreClosed"]
