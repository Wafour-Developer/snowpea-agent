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

from snowpea_core.scheduler import access, services
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
    JobWaitingApproval,
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
    """The scheduler's one rule for changing a job (``scheduler/access.py``):
    an owned job only from its owner's connection (1.23.0), a Jarvis task only
    from a person, a remote login only the jobs it created."""
    from snowpea_core.server.person_gate import snapshot

    await access.guard_job(core, snapshot(conn), job_id, action)


def _grant_refusal(conn: RpcConnection, actor: str | None, person_confirm: str | None,
                   core: Core, method: str,
                   what: str = "allow commands for a scheduled routine") -> None:
    """Only a person pre-approves what a routine runs (1.68.0, ``jobApprovals``).

    A grant runs unasked with nobody watching, so it is held like a typed
    allowlist rule: a local IDE/TUI connection, or a local browser 0.4.0+ whose
    own confirmation window confirmed it (``actor: "person"`` + ``personConfirm:
    "window"``).  A remote web login, a connector and an in-process caller
    (Jarvis, the model) are refused with ``jarvis_person_only``.  The same
    rule holds a routine's mode raise and a change of what a routine that holds
    grants or runs above ``accept`` does (security review of 1.68.0, M1/L1).
    """
    from snowpea_core.server import settings_gate
    from snowpea_core.server.person_gate import connection_kind

    kind = connection_kind(conn)
    if kind == "person":
        return
    if kind == "remote_login":
        settings_gate.refuse_local_only(conn, core, what, method=method, detail="grants")
    if kind == "browser":
        if settings_gate.browser_remote(conn):
            settings_gate.refuse(conn, what, reason=settings_gate.BROWSER_REMOTE)
        if not settings_gate.browser_confirms(conn):
            settings_gate.refuse(conn, what, reason=settings_gate.BROWSER_LEGACY)
        if (actor != settings_gate.ACTOR_PERSON
                or person_confirm != settings_gate.PERSON_CONFIRM_WINDOW):
            settings_gate.refuse(conn, what, reason=settings_gate.BROWSER_UNCONFIRMED)
        return
    settings_gate.refuse(conn, what)


def _built_grants(inputs: Any) -> list[dict[str, Any]]:
    """``JobGrantInput`` items as stored grants; ``invalid_params`` names a bad one."""
    from snowpea_core.config.paths import utc_now
    from snowpea_core.scheduler.grants import BAD_GRANT, from_input

    out: list[dict[str, Any]] = []
    for index, item in enumerate(inputs or ()):
        try:
            out.append(from_input(item.tool, item.pattern, item.line, now=utc_now()))
        except ValueError as exc:
            raise RpcError(errors.INVALID_PARAMS, f"grant {index + 1}: {exc}",
                           {"reason": BAD_GRANT, "index": index}) from exc
    return out


#: Re-exported: the rules live in ``scheduler/access.py`` (security review 7, M-2).
REMOTE_LOGIN_ANY_JOB = access.REMOTE_LOGIN_ANY_JOB
REMOTE_LOGIN_JOB_CHANNELS = access.REMOTE_LOGIN_JOB_CHANNELS


async def job_schedule_handler(
    conn: RpcConnection, params: JobScheduleParams, core: Core
) -> JobScheduleResult:
    """``job.schedule`` — parse the spec, store the job, report its first firing.

    Who may schedule what is :func:`snowpea_core.scheduler.access.schedule_job`'s
    (the same entry point as ``/schedule`` and the ``schedule_create`` tool):
    the mode (a raise is a person's; a re-created job is a new job), whose
    browser the runs drive, a remote login's channel and ``createdBy``.  Every
    job is a prompt job: a jarvis-task target cannot be set over RPC and no job
    kind runs a shell line.
    """
    from snowpea_core.scheduler.jobs import iso
    from snowpea_core.server.person_gate import connection_kind, snapshot

    template = params.sessionTemplate
    # Security review 7, H-2: no job runs inside snowpea's own home by request
    # (a job with no workdir keeps running there, as before).
    from snowpea_core.server.project_gate import require_project

    require_project(core, params.workdir, dot=False)
    owner: str | None = None
    if params.meta is not None:
        owner = _owner_id(conn)
        _check_meta(params.meta)
    grants = None
    if params.grants:
        _grant_refusal(conn, params.actor, params.personConfirm, core, "job.schedule")
        grants = _built_grants(params.grants)
    explicit = template.mode if template and template.mode else params.mode
    if explicit and connection_kind(conn) == "browser":
        # Security review of 1.68.0, L1: a browser naming a mode above the
        # project's default confirms it like a grant (an unmarked one is
        # refused as before, mode_raise, in schedule_job).  The maker's
        # default (default_job_mode, below) is not judged here.
        from snowpea_core.server.settings_gate import acts_for_person
        from snowpea_core.session import mode_guard

        default = core.sessions.default_mode(params.workdir or str(core.paths.home))
        if mode_guard.raises(default, explicit) and acts_for_person(conn, params.actor):
            _grant_refusal(conn, params.actor, params.personConfirm, core, "job.schedule",
                           what=f"schedule a routine in {explicit} mode")
    try:
        job = await access.schedule_job(
            core,
            snapshot(conn, params.actor),
            params.spec,
            params.task,
            via="rpc",
            # An explicit mode wins; none: the routine default for this maker (1.68.0).
            mode=explicit or access.default_job_mode(core, snapshot(conn, params.actor)),
            channel=params.channel,
            agent=(template.agent if template and template.agent else params.agent),
            workdir=params.workdir,
            host_tools_from=template.hostToolsFrom if template else None,
            host_wait_sec=template.hostWaitSec if template else None,
            catch_up_window_min=template.catchUpWindowMinutes if template else None,
            meta=params.meta,
            owner_client_id=owner,
            title=params.title,
            grants=grants,
            approval_timeout_sec=params.approvalTimeoutSec,
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
        await _with_last_run(core, scheduler, job, info)
        rows.append(info)
    return JobListResult(jobs=rows)


async def _with_last_run(core: Core, scheduler: Any, job: Job, info: JobInfo) -> None:
    """``lastError``, ``lastSessionId`` and ``waitingApproval`` (1.68.0)."""
    from snowpea_core.session.events import call_summary

    seen_error = False
    for row in await scheduler.store.runs(job.id, limit=3):
        status = row.get("status")
        if status not in ("ok", "error", "denied_by_timeout", "missed"):
            continue
        if not seen_error:
            # 1.69.0 (jobMissedRuns): a missed occurrence's reason shows too.
            info.lastError = row.get("error") if status != "ok" else None
            seen_error = True
        if status != "missed":
            info.lastSessionId = row.get("session_id")
            break
    approvals = getattr(core, "approvals", None)
    waiting = approvals.for_job(job.id) if approvals is not None else []
    if waiting:
        request = waiting[0]
        summary = request.tool if request.argsEscaped else call_summary(request.args)
        info.waitingApproval = JobWaitingApproval(
            requestId=request.requestId, sessionId=request.sessionId, tool=request.tool,
            summary=summary, timeoutSec=request.timeoutSec)


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
    add_grants = None
    if params.addGrants:
        _grant_refusal(conn, params.actor, params.personConfirm, core, "job.update")
        add_grants = _built_grants(params.addGrants)
    from snowpea_core.session import mode_guard

    current = await services(core).get(params.jobId)
    if params.mode is not None and current is not None and current.mode != params.mode:
        # The mode later runs start in: a raise is a person's, as at job.schedule,
        # and confirmed like a grant (security review of 1.68.0, L1).
        from snowpea_core.server.person_gate import snapshot

        mode_guard.check(mode_guard.ModeCaller.from_conn(snapshot(conn, params.actor)),
                         None, current.mode, params.mode,
                         settings=getattr(core, "settings", None))
        if mode_guard.raises(current.mode, params.mode):
            _grant_refusal(conn, params.actor, params.personConfirm, core, "job.update",
                           what=f"raise a scheduled routine to {params.mode} mode")
    if (current is not None and params.task is not None and params.task != current.task
            and (current.grants or add_grants
                 or mode_guard.raises("accept", params.mode or current.mode))):
        # Security review of 1.68.0, M1: what a routine that runs unasked does
        # (its grants, a mode above accept) changes only by a person; the
        # grants are never dropped behind the person's back.  A time change
        # (``schedule``) changes nothing the agent does and stays open.
        _grant_refusal(conn, params.actor, params.personConfirm, core, "job.update",
                       what="change the task of a routine that runs with allowed "
                            "commands or above accept mode")
    try:
        job = await services(core).update(
            params.jobId,
            catch_up_window_min=template.catchUpWindowMinutes if template else None,
            host_wait_sec=template.hostWaitSec if template else None,
            enabled=params.enabled,
            schedule=params.schedule,
            task=params.task,
            title=params.title,
            mode=params.mode,
            add_grants=add_grants,
            remove_grants=params.removeGrants,
            approval_timeout_sec=params.approvalTimeoutSec,
        )
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    if job is None:
        raise RpcError(errors.INVALID_PARAMS, f"no such job: {params.jobId}")
    info = job.info(owner=_is_owner(conn, job))
    await _with_last_run(core, services(core), job, info)
    return info


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
