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
    closed_at      TEXT
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
            self._conn.commit()

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
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT OR REPLACE INTO sessions"
            " (id, workdir, mode, provider, model, origin_surface, created_at, closed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (session_id, workdir, mode, provider, model, origin_surface, created_at),
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


__all__ = ["SCHEMA", "Store", "StoreClosed"]
