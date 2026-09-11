"""Scheduled jobs (M5 contract §2).

The daemon holds one :class:`~snowpea_core.scheduler.scheduler.Scheduler` on
``Core.scheduler``; ``wire_scheduler`` builds it and activates the schedule
tools, and ``start_scheduler`` / ``stop_scheduler`` bracket its asyncio task
around the daemon's own lifetime.  Everything else here is a thin accessor so
callers (RPC handlers, the ``/schedule`` command, tools) never reach into
``Core`` by hand.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from snowpea_core.scheduler.jobs import Job, Jobs, JobStore, iso, parse_iso
from snowpea_core.scheduler.nl_parse import Spec, first_run, next_run, parse_spec
from snowpea_core.scheduler.scheduler import LOG_CHANNEL, Scheduler
from snowpea_core.scheduler.tools import format_job, register_schedule_tools

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.scheduler")


def services(core: Core) -> Scheduler:
    """The daemon's scheduler, raising when it was never wired."""
    scheduler = getattr(core, "scheduler", None)
    if scheduler is None:
        raise RuntimeError("the scheduler is not wired; call wire_core first")
    return scheduler  # type: ignore[no-any-return]


def wire_scheduler(core: Core) -> Scheduler:
    """Build the scheduler, hang it off ``Core`` and activate its tools."""
    scheduler = Scheduler(core, JobStore.open(core.paths), core.settings.scheduler)
    core.scheduler = scheduler
    register_schedule_tools(core.tools)
    return scheduler


async def start_scheduler(core: Core) -> None:
    """Catch up on missed jobs and start ticking (called once the daemon is up)."""
    scheduler = getattr(core, "scheduler", None)
    if scheduler is None:
        return
    try:
        await scheduler.start()
    except Exception:  # noqa: BLE001 - a bad job table must not stop the daemon
        log.exception("the scheduler could not start")


async def stop_scheduler(core: Core) -> None:
    """Cancel the tick task and close the job store."""
    scheduler = getattr(core, "scheduler", None)
    if scheduler is None:
        return
    await scheduler.stop()
    scheduler.close()


__all__ = [
    "LOG_CHANNEL",
    "Job",
    "JobStore",
    "Jobs",
    "Scheduler",
    "Spec",
    "first_run",
    "format_job",
    "iso",
    "next_run",
    "parse_iso",
    "parse_spec",
    "register_schedule_tools",
    "services",
    "start_scheduler",
    "stop_scheduler",
    "wire_scheduler",
]
