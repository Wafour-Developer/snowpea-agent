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
    JobRunInfo,
    JobRunsParams,
    JobRunsResult,
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
    "job.runs",
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
    if job is not None and job.target == "jarvis-task":
        # A Jarvis task's prompt runs unattended and is handed to Jarvis: only a
        # person rewrites or fires it (security review M6/M9).
        from snowpea_core.server.person_gate import require_person

        require_person(conn, f"{action} Jarvis's scheduled task {job_id}")
    if job is not None and action not in REMOTE_LOGIN_ANY_JOB:
        # Security review 6, M-4: a remote login changes, cancels, re-enables or
        # deletes only the jobs it created; a local person's or a browser's it may
        # read and run as saved (the same class as prompting a session).
        from snowpea_core.server import settings_gate
        from snowpea_core.server.person_gate import connection_kind

        if connection_kind(conn) == "remote_login" and job.created_by != "remote_login":
            settings_gate.refuse_local_only(
                conn, core, f"{action} a scheduled job it did not create",
                method=f"job.{_ACTION_METHOD.get(action, action)}", detail="job")


#: ``_guard`` actions a remote web login may take on a job it did not create.
REMOTE_LOGIN_ANY_JOB: frozenset[str] = frozenset({"run"})
_ACTION_METHOD = {"run": "runNow"}
#: Delivery channels a remote login may give a job: none (in-app: the session
#: that made it, else the log) or the log.  A messenger chat leaves the machine.
REMOTE_LOGIN_JOB_CHANNELS: frozenset[str | None] = frozenset({None, "", "log"})


async def job_schedule_handler(
    conn: RpcConnection, params: JobScheduleParams, core: Core
) -> JobScheduleResult:
    """``job.schedule`` — parse the spec, store the job, report its first firing."""
    from snowpea_core.scheduler.jobs import iso

    template = params.sessionTemplate
    mode = template.mode if template and template.mode else params.mode
    if mode:
        # Every run starts in this mode with nobody asked: a mode wider than the
        # default is a raise, a person's (security review 4; session/mode_guard.py).
        # A re-created job is a new job: no earlier job vouches for it.
        from snowpea_core.session import mode_guard

        workdir = params.workdir or str(core.paths.home)
        mode_guard.check(mode_guard.ModeCaller.from_conn(conn, params.actor), None,
                         core.sessions.default_mode(workdir), mode)
    owner: str | None = None
    if params.meta is not None:
        owner = _owner_id(conn)
        _check_meta(params.meta)
    from snowpea_core.server.person_gate import connection_kind

    creator = connection_kind(conn)
    # Security review 6, M-4/L-2: whose browser the runs drive is judged now, for
    # the job's creator (a remote login or a connector never borrows one; a
    # browser names itself or "browser"); the scheduler's in-process sessions
    # inherit it.  Every job is a prompt job: a jarvis-task target cannot be set
    # over RPC and no job kind runs a shell line.
    from snowpea_core.server.settings_gate import host_choice_refusal

    host_choice_refusal(conn, template.hostToolsFrom if template is not None else None)
    if creator == "remote_login" and params.channel not in REMOTE_LOGIN_JOB_CHANNELS:
        # M-4: no delivery that leaves the machine (job.update cannot change a
        # channel at all, so a saved one stays as a local person set it).
        from snowpea_core.server import settings_gate

        settings_gate.refuse_local_only(conn, core, "schedule a job that delivers to a messenger",
                                        method="job.schedule", detail="channel")
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
            created_by=creator if creator != "none" else None,
            meta=params.meta,
            owner_client_id=owner,
            title=params.title,
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
            task=params.task,
            title=params.title,
        )
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    if job is None:
        raise RpcError(errors.INVALID_PARAMS, f"no such job: {params.jobId}")
    return job.info(owner=_is_owner(conn, job))


async def job_runs_handler(
    _conn: RpcConnection, params: JobRunsParams, core: Core
) -> JobRunsResult:
    """``job.runs`` — one job's occurrences from ``job_runs``, newest first (1.33.0).

    An unknown job is ``invalid_params``, as ``job.update`` and ``job.delete``
    answer it.
    """
    scheduler = services(core)
    if await scheduler.get(params.jobId) is None:
        raise RpcError(errors.INVALID_PARAMS, f"no such job: {params.jobId}")
    triggers = ("schedule", "runNow")
    runs = [
        JobRunInfo(
            runId=f"{params.jobId}@{row['scheduled_ts']}",
            scheduledAt=row["scheduled_ts"],
            startedAt=row["started_at"],
            endedAt=row.get("finished_at"),
            status=row.get("status") or "running",
            sessionId=row.get("session_id"),
            error=row.get("error"),
            trigger=row.get("run_trigger") if row.get("run_trigger") in triggers else None,
        )
        for row in await scheduler.store.runs(params.jobId, limit=params.limit)
    ]
    return JobRunsResult(runs=runs)


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
    dispatcher.register("job.runs", job_runs_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_job_handlers"]
