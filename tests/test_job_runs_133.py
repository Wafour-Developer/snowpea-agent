"""Protocol 1.33.0: ``job.runs``, and ``job.update {task, title}``.

``job.list`` carries a job's last run and a held one only; ``job.runs`` reads
the per-occurrence history out of ``job_runs``, newest first, with the error a
failed run ended on.  A job gains an optional ``title`` (a nullable column,
migrated in like ``run_trigger``), and ``job.update`` can change its prompt.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import aiohttp
import pytest_asyncio
from _support import connect, fake_provider, make_daemon

from snowpea_core.scheduler.jobs import SCHEMA, JobStore, utc_now
from snowpea_core.server.app_server import Daemon

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "scheduler.json"
TIMEOUT = 20.0


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            await instance.stop()


async def test_job_runs_lists_each_run_newest_first(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        job_id = (
            await client.ok(
                "job.schedule",
                {
                    "spec": "0 9 * * *",
                    "task": "summarise the repo",
                    "channel": "log",
                    "workdir": str(tmp_path),
                },
            )
        )["jobId"]
        assert (await client.ok("job.runs", {"jobId": job_id}))["runs"] == []

        # An earlier firing, as the scheduler records one (two runNow calls in
        # the same second are one occurrence: the double-fire guard).
        assert daemon.core is not None
        store = daemon.core.scheduler.store
        earlier = utc_now() - timedelta(hours=1)
        assert await store.claim(job_id, earlier, "s-earlier", trigger="schedule")
        await store.finish(job_id, earlier, "error", error="RuntimeError: boom")
        # claim() stamps started_at with now; make the earlier run start earlier.
        store._execute(  # noqa: SLF001 - fixture setup
            "UPDATE job_runs SET started_at = scheduled_ts WHERE job_id = ?", (job_id,)
        )

        await client.ok("job.runNow", {"jobId": job_id})
        runs = (await client.ok("job.runs", {"jobId": job_id}))["runs"]
        assert len(runs) == 2
        newest, oldest = runs
        assert newest["startedAt"] > oldest["startedAt"]
        assert newest["status"] == "ok" and newest["trigger"] == "runNow"
        assert newest["endedAt"] and newest["sessionId"] and newest["error"] is None
        assert oldest["status"] == "error" and oldest["trigger"] == "schedule"
        assert oldest["error"] == "RuntimeError: boom" and oldest["sessionId"] == "s-earlier"
        for run in runs:
            assert run["runId"] == f"{job_id}@{run['scheduledAt']}"

        limited = (await client.ok("job.runs", {"jobId": job_id, "limit": 1}))["runs"]
        assert limited == [newest]

        missing = await client.call("job.runs", {"jobId": "j-nope"})
        assert missing["error"]["data"]["code"] == "invalid_params"
    finally:
        await client.stop()


async def test_a_failed_run_keeps_its_error(daemon: Daemon) -> None:
    assert daemon.core is not None
    store = daemon.core.scheduler.store
    moment = utc_now()
    assert await store.claim("j-x", moment, trigger="schedule")
    await store.finish("j-x", moment, "error", error="RuntimeError: boom")
    later = moment + timedelta(minutes=1)
    assert await store.claim("j-x", later, trigger="schedule")
    rows = await store.runs("j-x", limit=10)
    assert [row["status"] for row in rows] == [None, "error"]
    assert rows[1]["error"] == "RuntimeError: boom"


async def test_job_update_changes_task_and_title(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        scheduled = await client.ok(
            "job.schedule",
            {
                "spec": "0 9 * * *",
                "task": "summarise the repo",
                "title": "  Morning digest  ",
                "workdir": str(tmp_path),
            },
        )
        job_id = scheduled["jobId"]
        [listed] = (await client.ok("job.list", {}))["jobs"]
        assert listed["title"] == "Morning digest"

        changed = await client.ok(
            "job.update",
            {
                "jobId": job_id,
                "task": "list open issues",
                "title": "Issues",
                "schedule": "every 2 hours",
            },
        )
        assert changed["task"] == "list open issues" and changed["title"] == "Issues"
        assert changed["kind"] == "interval"

        kept = await client.ok("job.update", {"jobId": job_id, "enabled": True})
        assert kept["title"] == "Issues" and kept["task"] == "list open issues"
        cleared = await client.ok("job.update", {"jobId": job_id, "title": ""})
        assert cleared["title"] is None

        blank = await client.call("job.update", {"jobId": job_id, "task": "   "})
        assert blank["error"]["data"]["code"] == "invalid_params"
        [after] = (await client.ok("job.list", {}))["jobs"]
        assert after["task"] == "list open issues" and after["title"] is None
    finally:
        await client.stop()


async def test_an_old_database_gains_the_title_and_error_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    legacy = SCHEMA.replace(",\n    title        TEXT\n", "\n").replace(
        "    error        TEXT,\n", ""
    )
    assert "title" not in legacy and "error" not in legacy
    connection = sqlite3.connect(path)
    connection.executescript(legacy)
    connection.close()

    store = JobStore(path)
    try:
        jobs = {str(row[1]) for row in store._query("PRAGMA table_info(jobs)")}  # noqa: SLF001
        runs = {str(row[1]) for row in store._query("PRAGMA table_info(job_runs)")}  # noqa: SLF001
        assert "title" in jobs and "error" in runs
    finally:
        store.close()


def test_a_run_error_is_the_clipped_last_words() -> None:
    from snowpea_core.scheduler.scheduler import RUN_ERROR_CHARS, _run_error

    assert _run_error("ok", "fine") is None
    assert _run_error("denied_by_timeout", "") == "denied_by_timeout"
    long = _run_error("error", "x" * (RUN_ERROR_CHARS * 2))
    assert long is not None and len(long) == RUN_ERROR_CHARS and long.endswith("…")
