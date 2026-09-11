"""Persistence for team mode: the shared task board and the team message log.

The M7 contract (§5) asks for two tables in the daemon's state database,
``team_tasks`` and ``team_messages``.  They live in their own module with their
own connection to ``$SNOWPEA_HOME/state.db`` rather than inside
:class:`~snowpea_core.session.store.Store`, so adding team mode changes no file
the rest of the daemon already owns.

Everything here is ``async`` and pushes the blocking ``sqlite3`` work onto a
worker thread, exactly as ``session/store.py`` does.  The one operation that has
to be atomic is :meth:`TeamStore.claim`: a worker takes a task with a
conditional ``UPDATE`` and learns from the row count whether it won the race, so
two workers can never own the same task.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snowpea_core.config.paths import Paths

#: Task states, in the order a task normally walks through them (contract §5).
QUEUED = "queued"
CLAIMED = "claimed"
DONE = "done"
CONFLICT = "conflict"
MERGED = "merged"
FAILED = "failed"

#: Nothing more will happen to a task in one of these states.
TERMINAL: frozenset[str] = frozenset({MERGED, FAILED})

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    workdir     TEXT NOT NULL,
    repo        TEXT NOT NULL,
    task        TEXT NOT NULL,
    workers     INTEGER NOT NULL,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_tasks (
    team_id        TEXT NOT NULL,
    id             TEXT NOT NULL,
    idx            INTEGER NOT NULL,
    title          TEXT NOT NULL,
    status         TEXT NOT NULL,
    agent_n        INTEGER,
    retries        INTEGER NOT NULL DEFAULT 0,
    conflict_hunks TEXT NOT NULL DEFAULT '',
    depends_on     TEXT NOT NULL DEFAULT '[]',
    done_seq       INTEGER NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    branch         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (team_id, id)
);
CREATE TABLE IF NOT EXISTS team_messages (
    team_id  TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    agent_n  INTEGER,
    task_id  TEXT,
    kind     TEXT NOT NULL,
    text     TEXT NOT NULL,
    ts       TEXT NOT NULL,
    PRIMARY KEY (team_id, seq)
);
CREATE INDEX IF NOT EXISTS team_tasks_board ON team_tasks (team_id, status, idx);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class TaskRow:
    """One row of the shared task board."""

    team_id: str
    id: str
    idx: int
    title: str
    status: str = QUEUED
    agent_n: int | None = None
    retries: int = 0
    conflict_hunks: str = ""
    depends_on: list[str] = field(default_factory=list)
    done_seq: int = 0
    note: str = ""
    branch: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> TaskRow:
        try:
            deps = json.loads(row["depends_on"])
        except (ValueError, TypeError):
            deps = []
        return cls(
            team_id=row["team_id"],
            id=row["id"],
            idx=int(row["idx"]),
            title=row["title"],
            status=row["status"],
            agent_n=None if row["agent_n"] is None else int(row["agent_n"]),
            retries=int(row["retries"]),
            conflict_hunks=row["conflict_hunks"] or "",
            depends_on=[str(item) for item in deps] if isinstance(deps, list) else [],
            done_seq=int(row["done_seq"]),
            note=row["note"] or "",
            branch=row["branch"] or "",
        )


@dataclass
class TeamRow:
    """The header row for one team run."""

    id: str
    session_id: str
    workdir: str
    repo: str
    task: str
    workers: int
    state: str
    created_at: str

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> TeamRow:
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            workdir=row["workdir"],
            repo=row["repo"],
            task=row["task"],
            workers=int(row["workers"]),
            state=row["state"],
            created_at=row["created_at"],
        )


class TeamStore:
    """Async facade over the two team tables."""

    #: Attribute the store is cached under on ``Core``.
    CORE_ATTR = "_team_store"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @classmethod
    def open(cls, paths: Paths) -> TeamStore:
        paths.ensure()
        return cls(paths.state_db)

    # -- plumbing ------------------------------------------------------
    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """Run a statement; returns how many rows it touched."""
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return int(cursor.rowcount)

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    # -- teams ---------------------------------------------------------
    async def create_team(
        self,
        team_id: str,
        session_id: str,
        workdir: str,
        repo: str,
        task: str,
        workers: int,
        state: str = "running",
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT OR REPLACE INTO teams"
            " (id, session_id, workdir, repo, task, workers, state, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (team_id, session_id, workdir, repo, task, workers, state, utc_now()),
        )

    async def set_team_state(self, team_id: str, state: str) -> None:
        await asyncio.to_thread(
            self._execute, "UPDATE teams SET state = ? WHERE id = ?", (state, team_id)
        )

    async def team(self, team_id: str) -> TeamRow | None:
        rows = await asyncio.to_thread(
            self._query, "SELECT * FROM teams WHERE id = ?", (team_id,)
        )
        return TeamRow.from_sqlite(rows[0]) if rows else None

    async def teams(self) -> list[TeamRow]:
        rows = await asyncio.to_thread(self._query, "SELECT * FROM teams ORDER BY created_at")
        return [TeamRow.from_sqlite(row) for row in rows]

    async def latest_team(self) -> TeamRow | None:
        rows = await asyncio.to_thread(
            self._query, "SELECT * FROM teams ORDER BY created_at DESC, rowid DESC LIMIT 1"
        )
        return TeamRow.from_sqlite(rows[0]) if rows else None

    # -- tasks ---------------------------------------------------------
    async def add_task(
        self,
        team_id: str,
        task_id: str,
        idx: int,
        title: str,
        depends_on: list[str] | None = None,
    ) -> TaskRow:
        await asyncio.to_thread(
            self._execute,
            "INSERT OR REPLACE INTO team_tasks"
            " (team_id, id, idx, title, status, agent_n, retries, conflict_hunks,"
            "  depends_on, done_seq, note, branch)"
            " VALUES (?, ?, ?, ?, ?, NULL, 0, '', ?, 0, '', '')",
            (team_id, task_id, idx, title, QUEUED, json.dumps(list(depends_on or []))),
        )
        return TaskRow(
            team_id=team_id,
            id=task_id,
            idx=idx,
            title=title,
            depends_on=list(depends_on or []),
        )

    async def tasks(self, team_id: str) -> list[TaskRow]:
        rows = await asyncio.to_thread(
            self._query, "SELECT * FROM team_tasks WHERE team_id = ? ORDER BY idx", (team_id,)
        )
        return [TaskRow.from_sqlite(row) for row in rows]

    async def task(self, team_id: str, task_id: str) -> TaskRow | None:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM team_tasks WHERE team_id = ? AND id = ?",
            (team_id, task_id),
        )
        return TaskRow.from_sqlite(rows[0]) if rows else None

    async def claim(self, team_id: str, task_id: str, agent_n: int, branch: str) -> bool:
        """Take a claimable task for ``agent_n``; False if somebody got there first.

        The whole decision is one conditional ``UPDATE``: the status predicate is
        the lock and the row count is the answer, so two workers polling the
        board at the same instant can never both own the task.  ``conflict`` is
        claimable as well as ``queued`` — that is what re-queueing a conflicted
        task to the same agent means.
        """
        touched = await asyncio.to_thread(
            self._execute,
            "UPDATE team_tasks SET status = ?, agent_n = ?, branch = ?"
            " WHERE team_id = ? AND id = ? AND status IN (?, ?)"
            " AND (agent_n IS NULL OR agent_n = ?)",
            (CLAIMED, agent_n, branch, team_id, task_id, QUEUED, CONFLICT, agent_n),
        )
        return touched > 0

    async def set_status(
        self,
        team_id: str,
        task_id: str,
        status: str,
        *,
        agent_n: int | None = None,
        retries: int | None = None,
        conflict_hunks: str | None = None,
        note: str | None = None,
        done_seq: int | None = None,
    ) -> None:
        sets = ["status = ?"]
        params: list[Any] = [status]
        for column, value in (
            ("agent_n", agent_n),
            ("retries", retries),
            ("conflict_hunks", conflict_hunks),
            ("note", note),
            ("done_seq", done_seq),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        params.extend([team_id, task_id])
        await asyncio.to_thread(
            self._execute,
            f"UPDATE team_tasks SET {', '.join(sets)} WHERE team_id = ? AND id = ?",
            tuple(params),
        )

    # -- messages ------------------------------------------------------
    async def post(
        self,
        team_id: str,
        kind: str,
        text: str,
        *,
        agent_n: int | None = None,
        task_id: str | None = None,
    ) -> int:
        """Append to the team board; returns the message's sequence number."""
        return await asyncio.to_thread(
            self._post_sync, team_id, kind, text, agent_n, task_id
        )

    def _post_sync(
        self, team_id: str, kind: str, text: str, agent_n: int | None, task_id: str | None
    ) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM team_messages WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            seq = int(row["m"]) + 1
            self._conn.execute(
                "INSERT INTO team_messages (team_id, seq, agent_n, task_id, kind, text, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (team_id, seq, agent_n, task_id, kind, text, utc_now()),
            )
            self._conn.commit()
            return seq

    async def messages(self, team_id: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM team_messages WHERE team_id = ? ORDER BY seq",
            (team_id,),
        )
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def get_store(core: Any) -> TeamStore:
    """The daemon's one :class:`TeamStore`, created on first use."""
    store = getattr(core, TeamStore.CORE_ATTR, None)
    if store is None:
        store = TeamStore.open(core.paths)
        setattr(core, TeamStore.CORE_ATTR, store)
    return store


__all__ = [
    "CLAIMED",
    "CONFLICT",
    "DONE",
    "FAILED",
    "MERGED",
    "QUEUED",
    "SCHEMA",
    "TERMINAL",
    "TaskRow",
    "TeamRow",
    "TeamStore",
    "get_store",
    "utc_now",
]
