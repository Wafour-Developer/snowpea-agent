"""RPC handlers for ``job.*`` (M5 contract §2, US-015).

Its own module rather than more of ``session_handlers``: the scheduler is a
separate subsystem, and ``build_dispatcher`` only needs the one
``register_job_handlers`` line.

A job registered over RPC is the user speaking through the CLI or the TUI, so
nothing here prompts for approval — the ``auto``-mode question the contract
asks lives on the ``schedule_create`` *tool* path, where it is the model
proposing the job.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from snowpea_core.scheduler import services
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    JobIdParams,
    JobListResult,
    JobScheduleParams,
    JobScheduleResult,
    Ok,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.server.job")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = (
    "job.schedule",
    "job.list",
    "job.cancel",
    "job.runNow",
)


async def job_schedule_handler(
    _conn: RpcConnection, params: JobScheduleParams, core: Core
) -> JobScheduleResult:
    """``job.schedule`` — parse the spec, store the job, report its first firing."""
    from snowpea_core.scheduler.jobs import iso

    try:
        job = await services(core).schedule(
            params.spec,
            params.task,
            mode=params.mode,
            channel=params.channel,
            agent=params.agent,
            workdir=params.workdir,
        )
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return JobScheduleResult(jobId=job.id, nextRunAt=iso(job.next_run))


async def job_list_handler(_conn: RpcConnection, _params: Empty, core: Core) -> JobListResult:
    """``job.list`` — every job with its next run, last run and last status."""
    jobs = await services(core).list()
    return JobListResult(jobs=[job.info() for job in jobs])


async def job_cancel_handler(_conn: RpcConnection, params: JobIdParams, core: Core) -> Ok:
    """``job.cancel`` — disable a job; its run history is kept."""
    if not await services(core).cancel(params.jobId):
        raise RpcError(errors.NOT_FOUND, f"no such job: {params.jobId}")
    return Ok(ok=True)


async def job_run_now_handler(_conn: RpcConnection, params: JobIdParams, core: Core) -> Ok:
    """``job.runNow`` — fire a job in the daemon process and wait for it."""
    job = await services(core).run_now(params.jobId)
    if job is None:
        raise RpcError(errors.NOT_FOUND, f"no such job: {params.jobId}")
    return Ok(ok=True)


def register_job_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("job.schedule", job_schedule_handler)
    dispatcher.register("job.list", job_list_handler)
    dispatcher.register("job.cancel", job_cancel_handler)
    dispatcher.register("job.runNow", job_run_now_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_job_handlers"]
