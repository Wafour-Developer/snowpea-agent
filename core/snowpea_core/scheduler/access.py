"""Who may schedule, change and cancel a job: ONE entry point (security review 7, M-2).

``job.schedule`` (RPC), ``/schedule`` (slash command) and ``schedule_create``
(the model's tool) all register a job through :func:`schedule_job`, and
``job.cancel|update|delete|runNow``, ``/schedule cancel`` and
``schedule_cancel`` all pass :func:`guard_job` first.  Both take the caller
snapshot (``server.person_gate.CallerSnapshot``): for an RPC the connection's,
for a slash command the one taken when the command was accepted, for a tool the
snapshot of whoever started the turn (``agent.loop.turn_caller``).  So the
rules are the same whichever way a caller reaches the scheduler:

* ``createdBy`` is the caller's kind — a job made by a turn a remote web login
  prompted is ``remote_login``, like one it made with ``job.schedule``;
* a remote login's job delivers in the app or to the log only, never to a
  messenger chat, and is never above ``web.remote.maxMode``;
* a remote login changes, cancels and deletes only the jobs it created (it may
  run any job as saved, unless the job is above ``web.remote.maxMode``);
* a browser-owned job takes changes only from its owner's connection;
* a Jarvis task is a person's.

Refusals are ``RpcError`` (``jarvis_person_only`` with ``details.reason``
``local_only``/``mode_raise``/``person_only``, or ``unauthorized``); a command
or a tool reports the message instead of raising.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.scheduler.jobs import Job
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

#: Actions a remote web login may take on a job it did not create.
REMOTE_LOGIN_ANY_JOB: frozenset[str] = frozenset({"run"})
_ACTION_METHOD = {"run": "runNow"}
#: Delivery channels a remote login may give a job: none (in-app: the session
#: that made it, else the log) or the log.  A messenger chat leaves the machine.
REMOTE_LOGIN_JOB_CHANNELS: frozenset[str | None] = frozenset({None, "", "log"})


def created_by(who: Any) -> str | None:
    """``Job.created_by`` for a caller: its kind (``None`` for an in-process one)."""
    kind = getattr(who, "kind", "none")
    return None if kind == "none" else str(kind)


def _is_owner(who: Any, job: Job) -> bool:
    client_id = getattr(who, "client_id", None)
    return (
        job.owned
        and getattr(who, "client_kind", None) == "browser"
        and bool(client_id)
        and client_id == job.owner_client_id
    )


#: The mode a routine starts in when its maker names none and the checks are on,
#: or the maker is not a person (the model's schedule_create, a connector, a
#: remote web login, an in-process caller).
DEFAULT_JOB_MODE = "accept"


def default_job_mode(core: Any, who: Any) -> str:
    """The mode a routine gets when its maker names none (owner decision 2026-11-19).

    While the security checks are off (the 1.66.0 default), a routine a person
    makes directly runs in ``auto``: a local person's own connection (the
    desktop or TUI form, ``/schedule``), or a browser with ``actor: "person"``.
    Otherwise :data:`DEFAULT_JOB_MODE`.  An explicit mode always wins, and a
    raise still passes :mod:`~snowpea_core.session.mode_guard` in
    :func:`schedule_job`.
    """
    from snowpea_core.security import enforce
    from snowpea_core.server.settings_gate import ACTOR_PERSON

    if enforce.enabled(getattr(core, "settings", None)):
        return DEFAULT_JOB_MODE
    kind = getattr(who, "kind", "none")
    if kind == "person":
        return "auto"
    if kind == "browser" and getattr(who, "actor", None) == ACTOR_PERSON:
        return "auto"
    return DEFAULT_JOB_MODE


async def schedule_job(
    core: Any,
    who: Any,
    spec: str,
    task: str,
    *,
    via: str,
    mode: str = "accept",
    channel: str | None = None,
    host_tools_from: str | None = None,
    mode_judged: bool = False,
    **options: Any,
) -> Job:
    """Register a job for ``who`` (see the module doc); ``ValueError`` for a bad spec.

    ``mode_judged``: the caller already put the job's mode to a person (the
    ``schedule_create`` tool's approval); the ``web.remote.maxMode`` limit
    still applies.  ``options`` go to :meth:`Scheduler.schedule` unchanged.
    """
    from snowpea_core.scheduler import services
    from snowpea_core.server import settings_gate
    from snowpea_core.session import mode_guard

    kind = getattr(who, "kind", "none")
    settings_gate.refuse_closed(who, "schedule a job")
    # Whose browser the runs drive is judged now, for the job's creator
    # (security review 6, M-4/L-2); the scheduler's sessions inherit it.
    settings_gate.host_choice_refusal(who, host_tools_from)
    if kind == "remote_login" and channel not in REMOTE_LOGIN_JOB_CHANNELS:
        settings_gate.refuse_local_only(
            who, core, "schedule a job that delivers to a messenger",
            method="job.schedule", detail="channel")
    settings = getattr(core, "settings", None)
    workdir = options.get("workdir") or str(core.paths.home)
    default = core.sessions.default_mode(workdir)
    if kind != "none" and not mode_judged:
        # Every run starts in this mode with nobody asked: a mode wider than
        # the default is a raise, a person's (session/mode_guard.py).
        mode_guard.check(mode_guard.ModeCaller.from_conn(who), None, default, mode,
                         settings=settings)
    else:
        mode_guard.check_remote_max(kind, mode, settings, "scheduling a job")
    return await services(core).schedule(
        spec,
        task,
        mode=mode,  # type: ignore[arg-type]
        channel=channel,
        host_tools_from=host_tools_from,
        created_via=via,  # type: ignore[arg-type]
        created_by=created_by(who),
        **options,
    )


async def guard_job(core: Any, who: Any, job_id: str, action: str) -> Job | None:
    """Refuse ``who``'s ``action`` (``cancel``/``update``/``delete``/``run``) on a job.

    Returns the job (``None`` when there is none: the caller answers that).
    """
    from snowpea_core.scheduler import services
    from snowpea_core.server import settings_gate

    job = await services(core).get(job_id)
    if job is None:
        return None
    kind = getattr(who, "kind", "none")
    settings_gate.refuse_closed(who, f"{action} a scheduled job")
    if job.owned and not _is_owner(who, job):
        # An owned job takes changes only from its owner's connection (1.23.0).
        raise RpcError(
            errors.UNAUTHORIZED, f"job {job_id} is owned by its browser; cannot {action} it"
        )
    if job.target == "jarvis-task" and kind != "none":
        # A Jarvis task's prompt runs unattended and is handed to Jarvis: only a
        # person rewrites or fires it (security review M6/M9).
        from snowpea_core.server.person_gate import require_person

        require_person(who, f"{action} Jarvis's scheduled task {job_id}")
    if kind == "remote_login":
        if action not in REMOTE_LOGIN_ANY_JOB and job.created_by != "remote_login":
            # Security review 6, M-4: a remote login changes, cancels, re-enables
            # or deletes only the jobs it created; a local person's or a browser's
            # it may read and run as saved (the same class as prompting a session).
            settings_gate.refuse_local_only(
                who, core, f"{action} a scheduled job it did not create",
                method=f"job.{_ACTION_METHOD.get(action, action)}", detail="job")
        if action in ("run", "update"):
            from snowpea_core.session import mode_guard

            mode_guard.check_remote_max(kind, job.mode, getattr(core, "settings", None),
                                        f"{action if action == 'run' else 'changing'} a job")
    return job


__all__ = [
    "DEFAULT_JOB_MODE",
    "REMOTE_LOGIN_ANY_JOB",
    "REMOTE_LOGIN_JOB_CHANNELS",
    "created_by",
    "default_job_mode",
    "guard_job",
    "schedule_job",
]
