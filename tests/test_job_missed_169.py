"""1.69.0 (jobMissedRuns): a scheduled run missed past the catch-up window is not silent.

Feedback report: a weekly routine the daemon started too late for was only
logged; ``job.list`` showed last=ok and next=next week, so the person never
knew.  Now the occurrence is a ``job.runs`` row ``missed`` with its reason,
``job.event missed`` carries the reason, the job's channel (or the bound
person's private chat) is told, and ``JobInfo.lastError`` says so.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import connect, fake_provider, make_daemon

from snowpea_core.scheduler.jobs import utc_now
from snowpea_core.scheduler.scheduler import _span
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "scheduler.json"
#: An hour's catch-up window; no tick races the test's own catch_up call.
SETTINGS = {"scheduler": {"catchUpSec": 3600, "tickSec": 3600}}


async def test_capability_and_version() -> None:
    assert "jobMissedRuns" in CAPABILITIES and PROTOCOL_VERSION == "1.79.0"


async def test_a_run_missed_past_the_window_is_recorded_announced_and_told(
    tmp_path: Path,
) -> None:
    with fake_provider(FIXTURE):
        daemon = await make_daemon(tmp_path / "home", SETTINGS)
    assert daemon.core is not None
    core = daemon.core
    told: list[tuple[str, str]] = []

    async def share(title: str, text: str) -> int:
        told.append((title, text))
        return 1

    core.gateway.share_routine_notice = share
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            try:
                (tmp_path / "w").mkdir()
                job_id = str((await client.ok("job.schedule", {
                    "spec": "every monday at 9am", "task": "weekly report",
                    "workdir": str(tmp_path / "w"), "title": "weekly report",
                }))["jobId"])
                scheduler = core.scheduler
                job = await scheduler.store.get(job_id)
                # The daemon was down for three days past the Monday 09:00 run.
                due = utc_now().replace(microsecond=0) - timedelta(days=3)
                job.next_run = due
                await scheduler.store.update(job)

                assert await scheduler.catch_up() == 0
                await asyncio.sleep(0.05)

                [run] = await scheduler.store.runs(job_id)
                assert run["status"] == "missed"
                assert run["error"].startswith("missed: snowpea was not running")
                assert "3 d" in run["error"] and "1 h catch-up window" in run["error"]

                events = [e for e in client.of_method("job.event") if e["jobId"] == job_id]
                missed = [e for e in events if e["kind"] == "missed"]
                assert len(missed) == 1
                payload: dict[str, Any] = missed[0]["payload"]
                assert payload["reason"] == "daemon_down"
                assert payload["title"] == "weekly report"
                assert payload["error"] == run["error"]
                fresh = await scheduler.store.get(job_id)
                assert fresh.next_run is not None and fresh.next_run > utc_now()
                assert payload["nextRunAt"]

                # No channel: the bound person's private chat is told, named by the routine.
                assert len(told) == 1 and told[0][0] == "weekly report"
                assert "did not run" in told[0][1] and "Next run:" in told[0][1]

                listing = await client.ok("job.list", {})
                row = next(item for item in listing["jobs"] if item["jobId"] == job_id)
                assert row["lastError"] == run["error"]
                assert row["held"]["status"] == "missed"
            finally:
                await client.stop()
    finally:
        await daemon.stop()


async def test_a_run_inside_the_window_still_runs_and_is_not_missed(tmp_path: Path) -> None:
    with fake_provider(FIXTURE):
        daemon = await make_daemon(tmp_path / "home", SETTINGS)
        core = daemon.core
        assert core is not None
        try:
            fired: list[str] = []

            async def fire(job: Any, scheduled: Any, **_: Any) -> None:
                fired.append(job.id)

            scheduler = core.scheduler
            scheduler._fire = fire
            (tmp_path / "w").mkdir()
            job = await scheduler.schedule(
                "every monday at 9am", "weekly report", workdir=str(tmp_path / "w")
            )
            job.next_run = utc_now() - timedelta(minutes=10)
            await scheduler.store.update(job)
            assert await scheduler.catch_up() == 1
            assert fired == [job.id]
            assert await scheduler.store.runs(job.id) == []
        finally:
            await daemon.stop()


async def test_span_reads_like_a_person_would_say_it() -> None:
    assert _span(timedelta(seconds=30)) == "30 s"
    assert _span(timedelta(minutes=45)) == "45 min"
    assert _span(timedelta(hours=1)) == "1 h"
    assert _span(timedelta(hours=3, minutes=20)) == "3 h 20 min"
    assert _span(timedelta(days=3, hours=2)) == "3 d 2 h"
