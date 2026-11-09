"""A repeating job that keeps failing pauses itself and says so once."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from _support import make_daemon

from snowpea_core.scheduler.jobs import Job, utc_now


async def test_three_failures_in_a_row_pause_a_repeating_job(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        scheduler = daemon.core.scheduler
        said: list[str] = []

        async def deliver(job: Job, text: str) -> None:
            said.append(text)

        scheduler.deliver = deliver  # type: ignore[method-assign]
        job = await scheduler.store.insert(
            Job(spec="every day at 23:00", kind="cron", cron="0 23 * * *", task="report")
        )
        base = utc_now() - timedelta(days=3)
        for day in range(3):
            moment = base + timedelta(days=day)
            await scheduler.store.claim(job.id, moment)
            await scheduler.store.finish(job.id, moment, "denied_by_timeout")
            await scheduler._advance(job, moment, status="denied_by_timeout")
        fresh = await scheduler.store.get(job.id)
        assert fresh is not None and fresh.enabled is False
        assert len(said) == 1 and "paused" in said[0]
    finally:
        await daemon.stop()
