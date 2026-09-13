"""The :class:`Job` record and its SQLite home (M5 contract §2).

Jobs live in the same ``$SNOWPEA_HOME/state.db`` as sessions and memories, in
two tables:

``jobs``
    one row per registered job, including the next firing time so a restarted
    daemon picks up where it left off.
``job_runs``
    one row per *occurrence*, keyed ``(job_id, scheduled_ts)``.  The primary
    key is the double-fire guard: two ticks that resolve to the same scheduled
    instant can both try to claim it, and exactly one insert wins.

Like :class:`snowpea_core.session.store.Store`, every public method is
``async`` and pushes the blocking ``sqlite3`` call onto a worker thread.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from snowpea_core.config.paths import Paths
from snowpea_core.scheduler.nl_parse import Kind, Spec
from snowpea_core.server.protocol import JobInfo, Mode

JobStatus = Literal["ok", "error", "denied_by_timeout"]
JobState = Literal["scheduled", "running", "cancelled"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    spec         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    cron         TEXT,
    interval_sec INTEGER,
    next_run     TEXT,
    task         TEXT NOT NULL,
    mode         TEXT NOT NULL DEFAULT 'accept',
    channel      TEXT,
    origin_session_id TEXT,
    agent        TEXT,
    workdir      TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    state        TEXT NOT NULL DEFAULT 'scheduled',
    last_run     TEXT,
    last_status  TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_next_run ON jobs (enabled, next_run);
CREATE TABLE IF NOT EXISTS job_runs (
    job_id       TEXT NOT NULL,
    scheduled_ts TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT,
    session_id   TEXT,
    PRIMARY KEY (job_id, scheduled_ts)
);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_job_id() -> str:
    return f"j-{uuid.uuid4().hex[:12]}"


def iso(moment: datetime | None) -> str | None:
    """Timezone-aware datetime -> ``2026-09-11T00:00:00Z``."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class Job(BaseModel):
    """One scheduled prompt."""

    id: str = Field(default_factory=new_job_id)
    spec: str
    kind: Kind = "once"
    cron: str | None = None
    interval_sec: int | None = None
    next_run: datetime | None = None
    task: str = ""
    mode: Mode = "accept"
    channel: str | None = None
    origin_session_id: str | None = None
    agent: str | None = None
    workdir: str | None = None
    enabled: bool = True
    state: JobState = "scheduled"
    last_run: datetime | None = None
    last_status: JobStatus | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def from_spec(
        cls,
        parsed: Spec,
        task: str,
        *,
        mode: Mode = "accept",
        channel: str | None = None,
        origin_session_id: str | None = None,
        agent: str | None = None,
        workdir: str | None = None,
        next_run: datetime | None = None,
    ) -> Job:
        """Build a job from a parsed :class:`~snowpea_core.scheduler.nl_parse.Spec`."""
        return cls(
            spec=parsed.display,
            kind=parsed.kind,
            cron=parsed.cron,
            interval_sec=parsed.interval_sec,
            next_run=next_run,
            task=task,
            mode=mode,
            channel=channel,
            origin_session_id=origin_session_id,
            agent=agent,
            workdir=workdir,
        )

    def to_spec(self) -> Spec:
        """The :class:`Spec` this job was built from, rebuilt from its columns."""
        return Spec(
            kind=self.kind,
            display=self.spec,
            cron=self.cron,
            interval_sec=self.interval_sec,
            at=self.next_run,
        )

    def info(self) -> JobInfo:
        """Protocol view (``job.list``)."""
        return JobInfo(
            jobId=self.id,
            spec=self.spec,
            kind=self.kind,
            task=self.task,
            mode=self.mode,
            channel=self.channel,
            originSessionId=self.origin_session_id,
            nextRunAt=iso(self.next_run),
            state=self.state,
            enabled=self.enabled,
            lastRunAt=iso(self.last_run),
            lastStatus=self.last_status,
        )

    def row(self) -> tuple[Any, ...]:
        return (
            self.id,
            self.spec,
            self.kind,
            self.cron,
            self.interval_sec,
            iso(self.next_run),
            self.task,
            self.mode,
            self.channel,
            self.origin_session_id,
            self.agent,
            self.workdir,
            1 if self.enabled else 0,
            self.state,
            iso(self.last_run),
            self.last_status,
            iso(self.created_at),
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            spec=row["spec"],
            kind=row["kind"],
            cron=row["cron"],
            interval_sec=row["interval_sec"],
            next_run=parse_iso(row["next_run"]),
            task=row["task"],
            mode=row["mode"],
            channel=row["channel"],
            origin_session_id=row["origin_session_id"],
            agent=row["agent"],
            workdir=row["workdir"],
            enabled=bool(row["enabled"]),
            state=row["state"],
            last_run=parse_iso(row["last_run"]),
            last_status=row["last_status"],
            created_at=parse_iso(row["created_at"]) or utc_now(),
        )


#: Aliases so the annotations below still mean the builtin ``list`` even
#: though :class:`JobStore` defines a method called ``list``.
Jobs = list[Job]
Rows = list[dict[str, Any]]

_COLUMNS = (
    "id, spec, kind, cron, interval_sec, next_run, task, mode, channel, origin_session_id, agent,"
    " workdir, enabled, state, last_run, last_status, created_at"
)
_PLACEHOLDERS = ", ".join("?" * 17)


class JobStore:
    """Async facade over the ``jobs`` and ``job_runs`` tables."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {
                str(row[1]) for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "origin_session_id" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN origin_session_id TEXT")
            self._conn.commit()

    @classmethod
    def open(cls, paths: Paths) -> JobStore:
        paths.ensure()
        return cls(paths.state_db)

    # -- plumbing ------------------------------------------------------
    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor.rowcount

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    # -- jobs ----------------------------------------------------------
    async def insert(self, job: Job) -> Job:
        await asyncio.to_thread(
            self._execute,
            f"INSERT OR REPLACE INTO jobs ({_COLUMNS}) VALUES ({_PLACEHOLDERS})",
            job.row(),
        )
        return job

    async def update(self, job: Job) -> Job:
        return await self.insert(job)

    async def get(self, job_id: str) -> Job | None:
        rows = await asyncio.to_thread(self._query, "SELECT * FROM jobs WHERE id = ?", (job_id,))
        return Job.from_row(rows[0]) if rows else None

    async def list(self, *, include_cancelled: bool = True) -> Jobs:
        sql = "SELECT * FROM jobs"
        if not include_cancelled:
            sql += " WHERE state != 'cancelled'"
        sql += " ORDER BY created_at"
        rows = await asyncio.to_thread(self._query, sql)
        return [Job.from_row(row) for row in rows]

    async def due(self, moment: datetime) -> Jobs:
        """Enabled jobs whose ``next_run`` has arrived."""
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM jobs WHERE enabled = 1 AND next_run IS NOT NULL"
            " AND next_run <= ? ORDER BY next_run",
            (iso(moment),),
        )
        return [Job.from_row(row) for row in rows]

    async def delete(self, job_id: str) -> bool:
        removed = await asyncio.to_thread(self._execute, "DELETE FROM jobs WHERE id = ?", (job_id,))
        return bool(removed)

    async def enabled_count(self) -> int:
        rows = await asyncio.to_thread(
            self._query, "SELECT COUNT(*) AS n FROM jobs WHERE enabled = 1"
        )
        return int(rows[0]["n"]) if rows else 0

    # -- runs ----------------------------------------------------------
    async def claim(
        self, job_id: str, scheduled_ts: datetime, session_id: str | None = None
    ) -> bool:
        """Reserve one occurrence; ``False`` when it already ran (double fire)."""
        claimed = await asyncio.to_thread(
            self._execute,
            "INSERT OR IGNORE INTO job_runs (job_id, scheduled_ts, started_at, session_id)"
            " VALUES (?, ?, ?, ?)",
            (job_id, iso(scheduled_ts), iso(utc_now()), session_id),
        )
        return bool(claimed)

    async def finish(
        self,
        job_id: str,
        scheduled_ts: datetime,
        status: str,
        session_id: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE job_runs SET finished_at = ?, status = ?, session_id = COALESCE(?, session_id)"
            " WHERE job_id = ? AND scheduled_ts = ?",
            (iso(utc_now()), status, session_id, job_id, iso(scheduled_ts)),
        )

    async def runs(self, job_id: str) -> Rows:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM job_runs WHERE job_id = ? ORDER BY scheduled_ts",
            (job_id,),
        )
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = [
    "SCHEMA",
    "Job",
    "JobState",
    "JobStatus",
    "JobStore",
    "Jobs",
    "Rows",
    "iso",
    "new_job_id",
    "parse_iso",
    "utc_now",
]
