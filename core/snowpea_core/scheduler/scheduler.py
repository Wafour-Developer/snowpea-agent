"""The scheduler loop: ticks, fires jobs, delivers their answer (M5 contract §2).

A job fires *inside the daemon process*: the scheduler opens an unattended
session (``originSurface="scheduler"``, no origin connection), runs the job's
task through the ordinary agent loop, and hands the final assistant message to
the job's channel.  Nothing is forked, so AC-08's "parent pid == daemon pid"
holds by construction and a job sees the same tools, backends and approval
queue an interactive turn does.

Three details are worth knowing:

*catch-up*
    On start, every job whose ``next_run`` is already in the past is fired once
    if it is less than ``scheduler.catchUpSec`` late; an older ``once`` job is
    marked missed and disabled rather than firing at a time nobody wants.

*double fire*
    Each firing claims the occurrence key ``(job_id, scheduled_ts)`` in
    ``job_runs`` before doing anything.  A duplicate tick loses the claim and
    returns without running, so a restart during a tick cannot run a job twice.

*unattended approvals*
    The session has no origin connection, so ``unattended=True`` flows into the
    agent loop and any approval goes to the shared queue.  An approval that
    times out ends the turn with ``error{code:"approval_timeout"}``, which is
    what turns ``last_status`` into ``denied_by_timeout`` (AC-20).  Since
    1.68.0 (``jobApprovals``) the request names its job (``ApprovalRequest.job``,
    ``job.event approval_needed``), waits ``scheduler.approvalTimeoutSec`` (or
    the job's own), and the run records what it waited for ("approval needed:
    shell `git fetch`") as its ``error``.  A person's "always in this routine"
    (scope ``task``) and ``job.update addGrants`` are the job's *grants*
    (``scheduler/grants.py``), handed to each run's session.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

from snowpea_core.config.settings import SchedulerSettings
from snowpea_core.scheduler.grants import MAX_GRANTS
from snowpea_core.scheduler.jobs import (
    CreatedVia,
    Job,
    Jobs,
    JobStatus,
    JobStore,
    iso,
    parse_iso,
    utc_now,
)
from snowpea_core.scheduler.nl_parse import Spec, first_run, next_run, parse_spec
from snowpea_core.server import errors
from snowpea_core.server.protocol import Mode
from snowpea_core.session.mode_guard import ModeCaller

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

#: How often a routine waiting for its host looks again.
HOST_POLL_SEC = 2.0

log = logging.getLogger("snowpea.scheduler")

#: Channel that means "write it to ``$SNOWPEA_HOME/logs/jobs.log``".
LOG_CHANNEL = "log"

#: ``job.event`` kind for each way a run can end.
#: ``job_runs.status`` and ``job.event`` kind of an occurrence held for its
#: host, and of one whose host never came within the catch-up window.
WAITING_FOR_HOST = "waiting_for_host"
MISSED = "missed"

#: ``job.event`` kind that asks an owning host to notify its user (1.23.0).
NOTIFY = "notify"

Trigger = Literal["schedule", "runNow"]


def _zone(name: str) -> str:
    """``name`` as an IANA zone, or ``ValueError``."""
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(name.strip())
    except Exception as exc:  # noqa: BLE001 - ZoneInfo raises several kinds
        raise ValueError(f"unknown timezone {name!r}; use an IANA name such as Asia/Seoul") from exc
    return name.strip()


def _span(delta: timedelta) -> str:
    """``3 h 20 min`` / ``45 min`` / ``30 s``: a duration for a person."""
    seconds = max(0, int(delta.total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} d {hours} h" if hours else f"{days} d"
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{minutes} min" if minutes else f"{seconds} s"


def run_context(job: Job, scheduled_ts: datetime, trigger: Trigger) -> dict[str, Any]:
    """``tool.invoke`` ``job`` for one run of ``job`` (1.23.0)."""
    return {
        "id": job.id,
        "runId": uuid.uuid4().hex,
        "scheduledAt": iso(scheduled_ts),
        "trigger": trigger,
        "createdVia": job.created_via,
        "ownerClientId": job.owner_client_id,
        "fingerprint": job.fingerprint(),
        "meta": job.meta,
    }


#: ``approval.answered`` ``by`` of a request nobody answered.
UNANSWERED_BY: frozenset[str] = frozenset({"timeout", "origin-timeout"})

#: ``job.event`` kind of a run waiting for a person (1.68.0).
APPROVAL_NEEDED = "approval_needed"

FINISH_KINDS: dict[str, str] = {
    "ok": "finished",
    "denied_by_timeout": "denied",
    "error": "failed",
}

#: How much of a failed run's last words ``job.runs`` keeps as its ``error``.
RUN_ERROR_CHARS = 500


def _run_error(status: str, text: str) -> str | None:
    """What ``job.runs`` shows for a run that did not end ``ok`` (1.33.0)."""
    if status == "ok":
        return None
    clipped = (text or "").strip()
    if len(clipped) > RUN_ERROR_CHARS:
        clipped = clipped[: RUN_ERROR_CHARS - 1] + "…"
    return clipped or status


def _job_title(job: Job) -> str:
    """The job's name for a card and a notification: its title, else its task."""
    if job.title:
        return job.title
    task = " ".join(job.task.split())
    return task if len(task) <= 60 else task[:59] + "…"


def _merged(kept: list[dict[str, Any]], added: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``kept`` plus ``added`` without repeats, at most ``MAX_GRANTS`` (newest kept)."""
    from snowpea_core.scheduler.grants import same

    out = list(kept)
    for grant in added:
        if not any(same(grant, old) for old in out):
            out.append(grant)
    return out[-MAX_GRANTS:]


#: Consecutive failed runs after which a repeating job pauses itself.
PAUSE_AFTER_FAILURES = 3


class _Collector:
    """A pseudo-connection that records one unattended session's events.

    :class:`~snowpea_core.session.manager.EventHub` fans events out to anything
    with ``notify`` and ``closed``, which is all a scheduled run needs: the last
    assistant message to deliver, and enough of the rest to classify the run.
    """

    closed = False

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.error_codes: list[str] = []
        self.reason: str | None = None
        #: requestId -> ``(tool, summary)`` of each approval the run asked (1.68.0).
        self.asked: dict[str, tuple[str, str]] = {}
        #: Approvals nobody answered: ``(tool, summary, waited seconds)``.
        self.unanswered: list[tuple[str, str, int]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method != "session.event":
            return
        kind = params.get("kind")
        payload = params.get("payload") or {}
        if kind == "message.done":
            text = str(payload.get("text") or "")
            if text.strip():
                self.texts.append(text)
        elif kind == "error":
            self.error_codes.append(str(payload.get("code") or ""))
        elif kind == "turn.done":
            self.reason = str(payload.get("reason") or "")
        elif kind == "approval.asked":
            self.asked[str(payload.get("requestId") or "")] = (
                str(payload.get("tool") or ""), str(payload.get("summary") or ""))
        elif kind == "approval.answered" and payload.get("by") in UNANSWERED_BY:
            tool, summary = self.asked.get(
                str(payload.get("requestId") or ""), (str(payload.get("tool") or ""), ""))
            waited = int(payload.get("waitedMs") or 0) // 1000
            self.unanswered.append((tool, summary, waited))

    def approval_reason(self) -> str | None:
        """``approval needed: shell `git fetch` …`` for a run nobody answered (1.68.0)."""
        if not self.unanswered:
            return None
        tool, summary, waited = self.unanswered[0]
        what = f"{tool} `{summary}`" if summary else tool
        minutes = max(1, round(waited / 60)) if waited else None
        within = f" within {minutes} min" if minutes else ""
        return (
            f"approval needed: {what} — nobody answered{within}, so it was not run. "
            "Open the run to see it; allow it \"always in this routine\" on the card of "
            "the next run (Run now), add it to the routine's allowed commands, or set "
            "the routine's mode to auto."
        )

    @property
    def final_text(self) -> str:
        return self.texts[-1] if self.texts else ""

    def status(self) -> JobStatus:
        """``ok`` / ``error`` / ``denied_by_timeout`` for this run."""
        if errors.APPROVAL_TIMEOUT in self.error_codes:
            return "denied_by_timeout"
        if self.reason == "complete" and not self.error_codes:
            return "ok"
        return "error"


class Scheduler:
    """Owns the job table and the asyncio task that ticks it."""

    def __init__(
        self,
        core: Core,
        store: JobStore,
        settings: SchedulerSettings | None = None,
    ) -> None:
        self.core = core
        self.store = store
        self.settings = settings or SchedulerSettings()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        #: Jobs currently executing, so a tick never stacks runs of one job.
        self._running: set[str] = set()
        #: Job id -> why its finishing run waited in vain (1.68.0).
        self._approval_reasons: dict[str, str] = {}

    # -- lifecycle -----------------------------------------------------
    @property
    def tick_sec(self) -> float:
        return max(0.05, float(self.settings.tickSec))

    async def start(self) -> None:
        """Refresh the lifecycle counter, catch up, then begin ticking."""
        await self.refresh_counter()
        if not self.settings.enabled:
            log.info("scheduler is disabled in settings")
            return
        await self.catch_up()
        if self._task is None:
            self._stopping = False
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self.tick_sec)
                await self.tick()
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                raise
            except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                log.exception("scheduler tick failed")

    # -- registration --------------------------------------------------
    async def schedule(
        self,
        spec: str,
        task: str,
        *,
        mode: Mode = "accept",
        channel: str | None = None,
        origin_session_id: str | None = None,
        agent: str | None = None,
        workdir: str | None = None,
        host_tools_from: str | None = None,
        host_wait_sec: int | None = None,
        catch_up_window_min: int | None = None,
        created_via: CreatedVia = "tool",
        meta: dict[str, Any] | None = None,
        owner_client_id: str | None = None,
        title: str | None = None,
        target: str | None = None,
        target_data: dict[str, Any] | None = None,
        timezone: str | None = None,
        created_by: str | None = None,
        grants: list[dict[str, Any]] | None = None,
        approval_timeout_sec: int | None = None,
    ) -> Job:
        """Parse ``spec``, store the job and return it. ``ValueError`` if unparseable.

        ``grants`` (1.68.0) are already-built grants (``scheduler/grants.py``):
        whoever passes them judged that a person gave them.
        """
        if not task.strip():
            raise ValueError("a scheduled job needs a task to run")
        parsed: Spec = parse_spec(spec)
        if timezone:
            parsed = dataclasses.replace(parsed, tz=_zone(timezone))
        job = Job.from_spec(
            parsed,
            task.strip(),
            mode=mode,
            channel=channel,
            origin_session_id=origin_session_id,
            agent=agent,
            workdir=workdir,
            next_run=first_run(parsed),
        )
        if job.next_run is None:
            raise ValueError(f"the schedule {spec!r} has no future firing time")
        job.created_via = created_via
        job.created_by = created_by
        job.title = (title or "").strip() or None
        job.target, job.target_data = target, target_data
        job.timezone = parsed.tz
        if grants:
            job.grants = list(grants)[-MAX_GRANTS:]
        if approval_timeout_sec:
            job.approval_timeout_sec = int(approval_timeout_sec)
        if meta is not None:
            job.meta, job.owner_client_id = dict(meta), owner_client_id
        if host_tools_from:
            job.host_tools_from = host_tools_from
            job.host_wait_sec = max(0, int(host_wait_sec if host_wait_sec is not None else 300))
            job.catch_up_window_min = max(
                0, int(catch_up_window_min if catch_up_window_min is not None else 60)
            )
        await self.store.insert(job)
        await self.refresh_counter()
        log.info("job %s scheduled (%s) next at %s", job.id, job.spec, iso(job.next_run))
        return job

    async def list(self) -> Jobs:
        return await self.store.list()

    async def get(self, job_id: str) -> Job | None:
        return await self.store.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        """Disable a job and mark it cancelled; its run history is kept."""
        job = await self.store.get(job_id)
        if job is None:
            return False
        job.enabled = False
        job.state = "cancelled"
        job.next_run = None
        await self.store.update(job)
        await self.refresh_counter()
        log.info("job %s cancelled", job_id)
        return True

    async def delete(self, job_id: str) -> list[str] | None:
        """Remove a job, its schedule and run history.

        Returns the session ids its runs opened (the caller decides whether
        those go too), or ``None`` for an unknown job.
        """
        job = await self.store.get(job_id)
        if job is None:
            return None
        session_ids = [
            str(row["session_id"]) for row in await self.store.runs(job_id) if row["session_id"]
        ]
        await self.store.delete_runs(job_id)
        await self.store.delete(job_id)
        await self.refresh_counter()
        await self._emit(job_id, "deleted", {})
        log.info("job %s deleted", job_id)
        return session_ids

    async def run_now(self, job_id: str) -> Job | None:
        """Fire a job immediately, in this process, without touching its schedule."""
        job = await self.store.get(job_id)
        if job is None:
            return None
        await self._fire(job, utc_now(), advance=False, trigger="runNow")
        return await self.store.get(job_id)

    # -- ticking -------------------------------------------------------
    async def tick(self, moment: datetime | None = None) -> int:
        """Fire everything that is due; returns how many runs started."""
        now = moment or utc_now()
        fired = await self.resume_waiting(now)
        for job in await self.store.due(now):
            if job.id in self._running:
                continue
            fired += 1
            await self._fire(job, job.next_run or now)
        return fired

    async def catch_up(self, moment: datetime | None = None) -> int:
        """Run jobs the daemon slept through; drop the ones that are too old."""
        now = moment or utc_now()
        window = timedelta(seconds=max(0, int(self.settings.catchUpSec)))
        caught = 0
        for job in await self.store.due(now):
            scheduled = job.next_run or now
            if now - scheduled <= window:
                caught += 1
                await self._fire(job, scheduled)
                continue
            log.info(
                "job %s missed its %s firing by more than the catch-up window",
                job.id,
                iso(scheduled),
            )
            # Feedback: a weekly routine missed this way said last=ok, next=next
            # week, and nobody knew.  The occurrence is now a run row, an event
            # and a notice (1.69.0, jobMissedRuns).
            claimed = await self.store.claim(job.id, scheduled, trigger="schedule")
            await self._advance(job, scheduled, status=None, missed=True)
            if claimed:
                reason = (
                    "snowpea was not running when it was due (the computer was off or "
                    f"asleep, or the daemon was stopped) and started {_span(now - scheduled)} "
                    f"later, past the {_span(window)} catch-up window"
                )
                await self._missed(job, scheduled, "daemon_down", reason)
        return caught

    async def _missed(self, job: Job, scheduled: datetime, code: str, reason: str) -> None:
        """Record a claimed occurrence as ``missed`` and tell the person (1.69.0)."""
        error = f"missed: {reason}"
        await self.store.finish(job.id, scheduled, MISSED, error=error)
        fresh = await self.store.get(job.id) or job
        upcoming = iso(fresh.next_run) if fresh.next_run else None
        await self._emit(
            job.id,
            MISSED,
            {"scheduledAt": iso(scheduled), "reason": code, "error": error,
             "title": job.title, "nextRunAt": upcoming},
        )
        name = job.title or job.id
        text = f"Routine {name} did not run at {iso(scheduled)}: {reason}."
        text += f" Next run: {upcoming}." if upcoming else " It will not run again."
        try:
            await self.deliver(fresh, text)
            if fresh.channel is None:
                gateway = getattr(self.core, "gateway", None)
                share = getattr(gateway, "share_routine_notice", None)
                if share is not None:
                    await share(name, text)
        except Exception:  # noqa: BLE001 - a notice must never break the scheduler
            log.warning("could not tell anyone job %s missed a run", job.id, exc_info=True)

    # -- execution -----------------------------------------------------
    async def _fire(
        self,
        job: Job,
        scheduled_ts: datetime,
        *,
        advance: bool = True,
        trigger: Trigger = "schedule",
    ) -> None:
        """Claim the occurrence and run the job, unless someone claimed it first."""
        if not await self.store.claim(job.id, scheduled_ts, trigger=trigger):
            log.info("job %s occurrence %s already ran; skipping", job.id, iso(scheduled_ts))
            return
        if job.host_tools_from and not self._host_ready(job):
            # The browser stops its host when idle (addendum 11): hold the run
            # until that host connects instead of failing it.
            await self.store.set_run_status(job.id, scheduled_ts, WAITING_FOR_HOST)
            await self._emit(
                job.id,
                WAITING_FOR_HOST,
                {"scheduledAt": iso(scheduled_ts), "hostToolsFrom": job.host_tools_from},
            )
            if advance:
                await self._advance(job, scheduled_ts, status=None)
            log.info("job %s waits for host %s", job.id, job.host_tools_from)
            return
        await self._run_claimed(job, scheduled_ts, advance=advance, trigger=trigger)

    def _host_ready(self, job: Job) -> bool:
        from snowpea_core.tools.host_tools import HOST_TOOLS

        probe = SimpleNamespace(host_tools_from=job.host_tools_from, origin_conn=None)
        return bool(HOST_TOOLS.for_session(probe))

    async def resume_waiting(self, moment: datetime | None = None) -> int:
        """Run held occurrences whose host is back; record stale ones as missed.

        Called on every tick and whenever a client registers host tools, so a
        routine starts as soon as its browser profile wakes up.
        """
        now = moment or utc_now()
        started = 0
        for row in await self.store.waiting_runs():
            job = await self.store.get(str(row["job_id"]))
            scheduled = parse_iso(row["scheduled_ts"])
            if job is None or scheduled is None:
                continue
            if now - scheduled > timedelta(minutes=job.catch_up_window_min):
                reason = (
                    f"its browser host ({job.host_tools_from}) did not connect within "
                    f"{_span(timedelta(minutes=job.catch_up_window_min))}"
                )
                await self._missed(job, scheduled, "host_absent", reason)
                log.info("job %s missed %s: its host never came", job.id, iso(scheduled))
                continue
            if job.id in self._running or not self._host_ready(job):
                continue
            await self.store.set_run_status(job.id, scheduled, "running")
            started += 1
            # A held job.runNow stays a runNow (1.23.0); a row without one
            # (an older core's) is not taken for a schedule firing.
            trigger: Trigger = "schedule" if row.get("run_trigger") == "schedule" else "runNow"
            await self._run_claimed(job, scheduled, advance=False, trigger=trigger)
        return started

    async def update(
        self,
        job_id: str,
        *,
        catch_up_window_min: int | None = None,
        host_wait_sec: int | None = None,
        enabled: bool | None = None,
        schedule: str | None = None,
        task: str | None = None,
        title: str | None = None,
        timezone: str | None = None,
        target_data: dict[str, Any] | None = None,
        mode: Mode | None = None,
        add_grants: list[dict[str, Any]] | None = None,
        remove_grants: list[str] | None = None,
        approval_timeout_sec: int | None = None,
    ) -> Job | None:
        """Patch a job; ``None`` for an unknown one, ``ValueError`` for a bad spec.

        ``mode``, ``add_grants``, ``remove_grants`` and ``approval_timeout_sec``
        (1.68.0): the caller judged who may (``job_handlers``); ``0`` for the
        timeout goes back to the setting.

        ``task`` replaces the prompt later runs get and ``title`` the job's short
        name, ``""`` removing it (1.33.0).
        """
        job = await self.store.get(job_id)
        if job is None:
            return None
        if task is not None:
            if not task.strip():
                raise ValueError("a scheduled job needs a task to run")
            job.task = task.strip()
        if title is not None:
            job.title = title.strip() or None
        if target_data is not None:
            job.target_data = target_data
        if timezone is not None:
            job.timezone = _zone(timezone) if timezone.strip() else None
            if schedule is None and job.kind == "cron":
                schedule = job.cron
        if schedule is not None:
            parsed: Spec = parse_spec(schedule)
            if job.timezone:
                parsed = dataclasses.replace(parsed, tz=job.timezone)
            upcoming = first_run(parsed)
            if upcoming is None:
                raise ValueError(f"the schedule {schedule!r} has no future firing time")
            job.spec = parsed.display
            job.kind = parsed.kind  # type: ignore[assignment]
            job.cron = parsed.cron
            job.interval_sec = parsed.interval_sec
            job.next_run = upcoming
        if catch_up_window_min is not None:
            job.catch_up_window_min = max(0, int(catch_up_window_min))
        if host_wait_sec is not None:
            job.host_wait_sec = max(0, int(host_wait_sec))
        if mode is not None:
            job.mode = mode
        if remove_grants:
            gone = set(remove_grants)
            job.grants = [g for g in job.grants if g.get("id") not in gone]
        if add_grants:
            job.grants = _merged(job.grants, add_grants)
        if approval_timeout_sec is not None:
            job.approval_timeout_sec = int(approval_timeout_sec) or None
        if enabled is False:
            job.enabled = False
            job.state = "cancelled"
            job.next_run = None
        elif enabled is True or (schedule is not None and job.enabled):
            job.enabled = True
            if job.next_run is None or (enabled is True and schedule is None):
                job.next_run = next_run(job.to_spec(), utc_now())
            if job.next_run is None:
                raise ValueError("the job's schedule has no future firing time to resume at")
            job.state = "scheduled"
        await self.store.update(job)
        await self.refresh_counter()
        if add_grants is not None or remove_grants is not None:
            self._share_grants(job)
        await self._emit(job.id, "updated", {"nextRunAt": iso(job.next_run)})
        return job

    async def add_grant(self, job_id: str, grant: dict[str, Any]) -> Job | None:
        """Keep a grant a person gave on a run's card ("always in this routine")."""
        job = await self.store.get(job_id)
        if job is None:
            return None
        job.grants = _merged(job.grants, [grant])
        await self.store.update(job)
        self._share_grants(job)
        await self._emit(job.id, "updated", {"nextRunAt": iso(job.next_run)})
        log.info("job %s: %s allowed for the routine (%s)", job.id, grant.get("display"),
                 grant.get("by"))
        return job

    def _share_grants(self, job: Job) -> None:
        """A live run of ``job`` follows its grants at once (its next call)."""
        sessions = getattr(self.core, "sessions", None)
        live = sessions.all() if sessions is not None and hasattr(sessions, "all") else ()
        for session in live:
            ref = getattr(session, "job_ref", None)
            if isinstance(ref, dict) and ref.get("id") == job.id:
                session.job_grants = list(job.grants)

    def approval_timeout(self, job: Job) -> int:
        """How long a run of ``job`` waits for a person's approval (1.68.0)."""
        own = job.approval_timeout_sec
        return max(1, int(own if own else getattr(self.settings, "approvalTimeoutSec", 900)))

    async def next_run_for_client(self, client_id: str) -> datetime | None:
        """When a host should be up: its earliest held or upcoming run (addendum 11)."""
        times: list[datetime] = []
        for job in await self.store.list():
            if job.host_tools_from != client_id:
                continue
            if job.enabled and job.next_run is not None:
                times.append(job.next_run)
        for row in await self.store.waiting_runs():
            job = await self.store.get(str(row["job_id"]))
            scheduled = parse_iso(row["scheduled_ts"])
            if job is not None and job.host_tools_from == client_id and scheduled is not None:
                times.append(scheduled)
        return min(times) if times else None

    async def _run_claimed(
        self, job: Job, scheduled_ts: datetime, *, advance: bool, trigger: Trigger = "schedule"
    ) -> None:
        """Run an occurrence this process has claimed."""
        self._running.add(job.id)
        job.state = "running"
        await self.store.update(job)
        await self._emit(job.id, "started", {"scheduledAt": iso(scheduled_ts)})
        status: JobStatus = "error"
        text = ""
        session_id: str | None = None
        try:
            status, text, session_id = await self._execute(
                job, run_context(job, scheduled_ts, trigger)
            )
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # noqa: BLE001 - a broken job never kills the loop
            # Security review 8, Low: the text goes to the job's channel and to
            # every client's job.event — a short classified reason and an id;
            # the exception (an httpx error can quote a URL with a key) is logged.
            error_id = errors.new_error_id()
            log.exception("job %s failed (error id %s)", job.id, error_id)
            text = f"{errors.failure_class(exc)} (id {error_id})"
        finally:
            self._running.discard(job.id)
        if status != "ok":
            # What a failed run says can quote a URL with a key in its query.
            text = errors.redact_url_queries(text)
        waited = self._approval_reasons.pop(job.id, None)
        if waited and status != "ok":
            # 1.68.0: what the run waited for, before what the agent said of it.
            waited = errors.redact_url_queries(waited)
            text = f"{waited}\n\n{text}".strip() if text else waited
        await self.store.finish(
            job.id, scheduled_ts, status, session_id,
            error=_run_error(status, waited or text) if waited and status != "ok"
            else _run_error(status, text),
        )
        await self.deliver(job, text or f"(job {job.id} produced no output)")
        await self._advance(job, scheduled_ts, status=status, advance=advance)
        kind = FINISH_KINDS[status]
        await self._emit(job.id, kind, {"status": status, "sessionId": session_id, "text": text})
        if job.owned:
            # The host shows its own notification for a job it owns (1.23.0).
            await self._emit(
                job.id, NOTIFY, {"status": status, "sessionId": session_id, "text": text}
            )
        await self._notify_origin_session(job, status, session_id, text)

    async def _notify_origin_session(
        self, job: Job, status: JobStatus, session_id: str | None, text: str
    ) -> None:
        """Tell the thread that scheduled the job that its run has finished.

        ``job.event`` is a plain notification every client sees; this is the
        per-session counterpart, so the originating thread can render "the
        scheduled run finished — open <sessionId>" in place (CORE-session-kind).
        Only a *live* origin session is notified: restoring one just to append
        an event would resurrect a thread nobody is watching.
        """
        origin_id = job.origin_session_id
        if not origin_id or origin_id == session_id:
            return
        if self.core.sessions.get(origin_id) is None:
            return
        from snowpea_core.session import events

        builder = events.job_done if status == "ok" else events.job_failed
        with contextlib.suppress(Exception):
            await self.core.hub.emit_event(
                origin_id, builder(job.id, session_id, status, text.strip())
            )

    async def _execute(
        self, job: Job, context: dict[str, Any] | None = None
    ) -> tuple[JobStatus, str, str | None]:
        """Open the unattended session, run the task, return what it said."""
        from snowpea_core.agent import loop as agent_loop
        from snowpea_core.agent.named import session_for_job

        core = self.core
        if job.target == "jarvis-task":
            from snowpea_core.jarvis import tasks as jarvis_tasks

            return await jarvis_tasks.run_job(core, job, context)
        if job.host_tools_from and not await self._wait_for_host(job):
            return (
                "error",
                f"host_unavailable: no {job.host_tools_from} host connected within "
                f"{job.host_wait_sec}s, so the routine did not run",
                None,
            )
        # A job that names a persistent agent runs *inside* that agent's
        # session, so it sees the agent's memory namespace (M7 contract §6).
        # An owned job never borrows a live named session: another turn there
        # would run alongside it under its job (1.23.0).
        remote_job = job.created_by == "remote_login"
        if remote_job:
            # web.remote.maxMode (security review 7): what a remote sign-in
            # scheduled never runs unattended above it, also when the limit was
            # lowered after the job was made.
            from snowpea_core.session import mode_guard

            limit = mode_guard.above_remote_max(
                "remote_login", job.mode, getattr(core, "settings", None))
            if limit is not None:
                return (
                    "error",
                    f"mode_above_remote_max: this job was scheduled from a remote sign-in in "
                    f"{job.mode} mode, above web.remote.maxMode ({limit}); it did not run",
                    None,
                )
        session = None if job.owned else session_for_job(core, job.agent)
        owned = session is None
        if session is None:
            workdir = Path(job.workdir) if job.workdir else core.paths.home
            session = await core.sessions.create(
                workdir=workdir,
                mode=job.mode,
                agent=job.agent,
                origin_surface="scheduler",
                origin_conn=None,
                parent_session_id=job.origin_session_id,
                kind="scheduled",
                job_id=job.id,
                # The job's stored mode, which only a person may have raised
                # (job.schedule / job.update, session/mode_guard.py).
                caller=ModeCaller.internal("scheduled-job"),
                owned_by=job.owner_client_id if job.owned else None,
            )
        # The contract calls these sessions unattended; the flag is what the
        # gateway and approval code read when they need to know (contract §2).
        session.unattended = True
        if owned:
            # 1.68.0 (jobApprovals): the approval queue names the job on the
            # card, waits the job's time, and the policy reads its grants.
            session.job_ref = {"id": job.id, "title": _job_title(job),
                               "timeoutSec": self.approval_timeout(job)}
            session.job_grants = list(job.grants)
        if job.host_tools_from:
            session.host_tools_from = job.host_tools_from
        collector = _Collector()
        core.hub.subscribe(collector, session.id)
        core.lifecycle.set_counter("sessions", len(core.sessions))
        turn_id = agent_loop.new_turn_id()
        if context is not None:
            session.turn_jobs[turn_id] = context
        # Only this turn runs with the job's grants and mode (review of 1.68.0, L3).
        session.scheduler_turns.add(turn_id)
        if remote_job:
            # What the run does (a job it schedules, an MCP server it adds) is
            # still a remote sign-in's (security review 7, M-2).
            from snowpea_core.server.person_gate import CallerSnapshot

            agent_loop.note_caller(
                session, turn_id, CallerSnapshot(kind="remote_login", origin="schedule"))
        try:
            await agent_loop.run_turn(
                core, session, job.task, turn_id=turn_id, unattended=True, origin="schedule"
            )
        finally:
            session.turn_jobs.pop(turn_id, None)
            session.scheduler_turns.discard(turn_id)
            core.hub.unsubscribe(collector)
            # A named agent's session outlives the run that borrowed it.
            if owned:
                with contextlib.suppress(Exception):
                    await core.sessions.close(session.id)
            core.lifecycle.set_counter("sessions", len(core.sessions))
        reason = collector.approval_reason()
        if reason:
            self._approval_reasons[job.id] = reason
        return collector.status(), collector.final_text, session.id

    async def _wait_for_host(self, job: Job) -> bool:
        """Wait up to ``job.host_wait_sec`` for a host that can serve the job."""
        from snowpea_core.tools.host_tools import HOST_TOOLS

        probe = SimpleNamespace(host_tools_from=job.host_tools_from, origin_conn=None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + job.host_wait_sec
        while True:
            if HOST_TOOLS.for_session(probe):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(min(HOST_POLL_SEC, max(0.05, deadline - loop.time())))

    async def _advance(
        self,
        job: Job,
        scheduled_ts: datetime,
        *,
        status: JobStatus | None,
        advance: bool = True,
        missed: bool = False,
    ) -> None:
        """Record the outcome and move ``next_run`` on (or retire the job)."""
        fresh = await self.store.get(job.id) or job
        if status is not None:
            fresh.last_run = scheduled_ts
            fresh.last_status = status
        if advance:
            upcoming = next_run(fresh.to_spec(), max(scheduled_ts, utc_now()))
            fresh.next_run = upcoming
            if upcoming is None:
                fresh.enabled = False
                fresh.state = "cancelled" if missed else "scheduled"
            else:
                fresh.state = "scheduled"
        else:
            fresh.state = "cancelled" if fresh.next_run is None else "scheduled"
        paused = False
        if status in ("error", "denied_by_timeout") and fresh.kind != "once" and fresh.enabled:
            recent = await self.store.runs(fresh.id, limit=PAUSE_AFTER_FAILURES)
            if len(recent) >= PAUSE_AFTER_FAILURES and all(
                row.get("status") in ("error", "denied_by_timeout") for row in recent
            ):
                fresh.enabled = False
                paused = True
        await self.store.update(fresh)
        await self.refresh_counter()
        if paused:
            await self.deliver(
                fresh,
                f"Schedule {fresh.id} paused: its last {PAUSE_AFTER_FAILURES} runs failed. "
                "Fix the cause and enable it again, or cancel it.",
            )

    # -- delivery ------------------------------------------------------
    async def deliver(self, job: Job, text: str) -> None:
        """Notify the creating session and any configured external channel."""
        session_delivered = await self._deliver_to_session(job, text)
        channel = job.channel or LOG_CHANNEL
        if channel != LOG_CHANNEL:
            gateway = getattr(self.core, "gateway", None)
            deliver = getattr(gateway, "deliver", None) if gateway is not None else None
            if deliver is not None:
                try:
                    await deliver(channel, text)
                    return
                except Exception:  # noqa: BLE001 - a dead gateway falls back to the log
                    log.warning("could not deliver job %s to %s", job.id, channel, exc_info=True)
            else:
                log.info("no gateway is bound; job %s output goes to the log", job.id)
        if not session_delivered or job.channel is not None:
            self._append_log(job, channel, text)

    async def _deliver_to_session(self, job: Job, text: str) -> bool:
        """Persist and broadcast a reminder in the TUI session that created it."""
        session_id = job.origin_session_id
        if not session_id:
            return False
        session = self.core.sessions.get(session_id)
        restored = session is None
        if session is None:
            session = await self.core.sessions.restore(session_id)
        if session is None:
            log.warning("job %s refers to missing session %s", job.id, session_id)
            return False
        from snowpea_core.session import events

        try:
            await self.core.hub.emit_event(
                session_id,
                events.message_done(f"⏰ Scheduled reminder ({job.id})\n\n{text.strip()}"),
            )
            return True
        finally:
            if restored:
                with contextlib.suppress(Exception):
                    await self.core.sessions.close(session_id)

    def _append_log(self, job: Job, channel: str, text: str) -> None:
        path = self.core.paths.jobs_log
        line = f"{iso(utc_now())} {job.id} [{channel}] {text.strip()}\n"
        try:
            self.core.paths.ensure()
            # Security review 9, F4: owner-only, never through a link.
            from snowpea_core.util.atomic import append_text

            append_text(path, line)
        except OSError:  # pragma: no cover - the log must never break a run
            log.warning("could not append to %s", path, exc_info=True)

    # -- notifications and counters ------------------------------------
    async def _emit(self, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        hub = getattr(self.core, "hub", None)
        if hub is None:
            return
        body = {key: value for key, value in payload.items() if value is not None}
        with contextlib.suppress(Exception):
            await hub.notify("job.event", {"jobId": job_id, "kind": kind, "payload": body})

    async def refresh_counter(self) -> int:
        """Keep ``lifecycle.jobs`` equal to the number of enabled jobs (plan §2.6)."""
        count = await self.store.enabled_count()
        lifecycle = getattr(self.core, "lifecycle", None)
        if lifecycle is not None:
            lifecycle.set_counter("jobs", count)
        return count

    def close(self) -> None:
        self.store.close()


__all__ = ["APPROVAL_NEEDED", "FINISH_KINDS", "LOG_CHANNEL", "NOTIFY", "Scheduler",
           "run_context"]
