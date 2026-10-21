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

import json
import logging
from typing import TYPE_CHECKING, Any

from snowpea_core.scheduler import services
from snowpea_core.scheduler.jobs import Job
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    JobDeleteParams,
    JobDeleteResult,
    JobHeldRun,
    JobIdParams,
    JobInfo,
    JobListResult,
    JobNextRunForClientParams,
    JobNextRunForClientResult,
    JobPreviewParams,
    JobPreviewResult,
    JobScheduleParams,
    JobScheduleResult,
    JobUpdateParams,
    Ok,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.server.job")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = (
    "job.schedule",
    "job.preview",
    "job.list",
    "job.cancel",
    "job.runNow",
    "job.delete",
    "job.nextRunForClient",
    "job.update",
)


#: Largest ``meta`` a job may carry, as compact JSON.
META_MAX_BYTES = 4096


def _owner_id(conn: RpcConnection) -> str:
    """The browser clientId that may own a job, or ``unauthorized``."""
    client_id = getattr(conn, "client_id", None)
    if getattr(conn, "client_kind", None) != "browser" or not client_id:
        raise RpcError(
            errors.UNAUTHORIZED,
            "only a clientKind 'browser' connection with a clientId may set job meta",
        )
    return str(client_id)


def _check_meta(meta: dict[str, Any]) -> None:
    size = len(json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > META_MAX_BYTES:
        raise RpcError(
            errors.INVALID_PARAMS, f"meta is {size} bytes; at most {META_MAX_BYTES} are kept"
        )


def _is_owner(conn: RpcConnection, job: Job) -> bool:
    client_id = getattr(conn, "client_id", None)
    return (
        job.owned
        and getattr(conn, "client_kind", None) == "browser"
        and bool(client_id)
        and client_id == job.owner_client_id
    )


async def _guard(conn: RpcConnection, core: Core, job_id: str, action: str) -> None:
    """An owned job takes changes only from its owner's connection (1.23.0)."""
    job = await services(core).get(job_id)
    if job is not None and job.owned and not _is_owner(conn, job):
        raise RpcError(
            errors.UNAUTHORIZED, f"job {job_id} is owned by its browser; cannot {action} it"
        )


async def job_schedule_handler(
    conn: RpcConnection, params: JobScheduleParams, core: Core
) -> JobScheduleResult:
    """``job.schedule`` — parse the spec, store the job, report its first firing."""
    from snowpea_core.scheduler.jobs import iso

    template = params.sessionTemplate
    owner: str | None = None
    if params.meta is not None:
        owner = _owner_id(conn)
        _check_meta(params.meta)
    try:
        job = await services(core).schedule(
            params.spec,
            params.task,
            mode=(template.mode if template and template.mode else params.mode),
            channel=params.channel,
            agent=(template.agent if template and template.agent else params.agent),
            workdir=params.workdir,
            host_tools_from=template.hostToolsFrom if template else None,
            host_wait_sec=template.hostWaitSec if template else None,
            catch_up_window_min=template.catchUpWindowMinutes if template else None,
            created_via="rpc",
            meta=params.meta,
            owner_client_id=owner,
        )
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return JobScheduleResult(
        jobId=job.id,
        nextRunAt=iso(job.next_run),
        fingerprint=job.fingerprint() if job.owned else None,
    )


async def job_preview_handler(
    _conn: RpcConnection, params: JobPreviewParams, _core: Core
) -> JobPreviewResult:
    """``job.preview`` — what ``job.schedule`` would make of a spec; nothing is saved.

    The same parser and the same first firing as a real job, so a form can show
    "next run" before the user commits to it.
    """
    from snowpea_core.scheduler.jobs import iso
    from snowpea_core.scheduler.nl_parse import first_run, parse_spec

    try:
        spec = parse_spec(params.spec)
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    upcoming = first_run(spec)
    if upcoming is None:
        # job.schedule refuses this one too, with the same words.
        raise RpcError(
            errors.INVALID_PARAMS, f"the schedule {params.spec!r} has no future firing time"
        )
    normalized = spec.cron if spec.kind == "cron" else spec.describe()
    return JobPreviewResult(kind=spec.kind, nextRunAt=iso(upcoming), normalized=normalized)


async def job_list_handler(conn: RpcConnection, _params: Empty, core: Core) -> JobListResult:
    """``job.list`` — every job with its next run, last run and last status."""
    scheduler = services(core)
    rows = []
    for job in await scheduler.list():
        info = job.info(owner=_is_owner(conn, job))
        held = await scheduler.store.held_run(job.id)
        if held is not None:
            info.held = JobHeldRun(status=held["status"], scheduledAt=held["scheduled_ts"])
        rows.append(info)
    return JobListResult(jobs=rows)


async def job_cancel_handler(conn: RpcConnection, params: JobIdParams, core: Core) -> Ok:
    """``job.cancel`` — disable a job; its run history is kept."""
    await _guard(conn, core, params.jobId, "cancel")
    if not await services(core).cancel(params.jobId):
        raise RpcError(errors.NOT_FOUND, f"no such job: {params.jobId}")
    return Ok(ok=True)


async def job_run_now_handler(conn: RpcConnection, params: JobIdParams, core: Core) -> Ok:
    """``job.runNow`` — fire a job in the daemon process and wait for it."""
    await _guard(conn, core, params.jobId, "run")
    job = await services(core).run_now(params.jobId)
    if job is None:
        raise RpcError(errors.NOT_FOUND, f"no such job: {params.jobId}")
    return Ok(ok=True)


async def job_delete_handler(
    conn: RpcConnection, params: JobDeleteParams, core: Core
) -> JobDeleteResult:
    """``job.delete`` — the job, its schedule and run history; optionally its runs.

    With ``deleteRuns`` the run sessions go the way ``session.deleteSaved``
    takes them (rows, attachments, audio, workspace); a run still in progress
    is left alone.
    """
    from snowpea_core.server.session_handlers import _purge_session_files

    await _guard(conn, core, params.jobId, "delete")
    session_ids = await services(core).delete(params.jobId)
    if session_ids is None:
        raise RpcError(errors.INVALID_PARAMS, f"no such job: {params.jobId}")
    deleted = 0
    if params.deleteRuns and session_ids and core.store is not None:
        doomed: list[str] = []
        for session_id in session_ids:
            live = core.sessions.get(session_id)
            if live is not None and live.current_turn is not None:
                continue
            if live is not None:
                await core.sessions.close(session_id)
            doomed.append(session_id)
        deleted = await core.store.delete_sessions(doomed)
        for session_id in doomed:
            _purge_session_files(core, session_id, workspace=True)
            await core.sessions.announce_sessions_changed("deleted", session_id)
    return JobDeleteResult(jobId=params.jobId, deletedSessions=deleted)


async def job_next_run_for_client_handler(
    _conn: RpcConnection, params: JobNextRunForClientParams, core: Core
) -> JobNextRunForClientResult:
    """``job.nextRunForClient`` — so a browser can wake its host just in time."""
    from snowpea_core.scheduler.jobs import iso

    moment = await services(core).next_run_for_client(params.clientId)
    return JobNextRunForClientResult(nextRunAt=iso(moment) if moment else None)


async def job_update_handler(
    conn: RpcConnection, params: JobUpdateParams, core: Core
) -> JobInfo:
    """``job.update`` — a partial, validated patch of an existing job."""
    await _guard(conn, core, params.jobId, "update")
    template = params.sessionTemplate
    try:
        job = await services(core).update(
            params.jobId,
            catch_up_window_min=template.catchUpWindowMinutes if template else None,
            host_wait_sec=template.hostWaitSec if template else None,
            enabled=params.enabled,
            schedule=params.schedule,
        )
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    if job is None:
        raise RpcError(errors.INVALID_PARAMS, f"no such job: {params.jobId}")
    return job.info(owner=_is_owner(conn, job))


def register_job_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("job.schedule", job_schedule_handler)
    dispatcher.register("job.preview", job_preview_handler)
    dispatcher.register("job.list", job_list_handler)
    dispatcher.register("job.cancel", job_cancel_handler)
    dispatcher.register("job.runNow", job_run_now_handler)
    dispatcher.register("job.delete", job_delete_handler)
    dispatcher.register("job.nextRunForClient", job_next_run_for_client_handler)
    dispatcher.register("job.update", job_update_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_job_handlers"]
