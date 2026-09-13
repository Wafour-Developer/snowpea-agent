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
    what turns ``last_status`` into ``denied_by_timeout`` (AC-20).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.config.settings import SchedulerSettings
from snowpea_core.scheduler.jobs import Job, Jobs, JobStatus, JobStore, iso, utc_now
from snowpea_core.scheduler.nl_parse import Spec, first_run, next_run, parse_spec
from snowpea_core.server import errors
from snowpea_core.server.protocol import Mode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.scheduler")

#: Channel that means "write it to ``$SNOWPEA_HOME/logs/jobs.log``".
LOG_CHANNEL = "log"

#: ``job.event`` kind for each way a run can end.
FINISH_KINDS: dict[str, str] = {
    "ok": "finished",
    "denied_by_timeout": "denied",
    "error": "failed",
}


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
    ) -> Job:
        """Parse ``spec``, store the job and return it. ``ValueError`` if unparseable."""
        if not task.strip():
            raise ValueError("a scheduled job needs a task to run")
        parsed: Spec = parse_spec(spec)
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

    async def run_now(self, job_id: str) -> Job | None:
        """Fire a job immediately, in this process, without touching its schedule."""
        job = await self.store.get(job_id)
        if job is None:
            return None
        await self._fire(job, utc_now(), advance=False)
        return await self.store.get(job_id)

    # -- ticking -------------------------------------------------------
    async def tick(self, moment: datetime | None = None) -> int:
        """Fire everything that is due; returns how many runs started."""
        now = moment or utc_now()
        fired = 0
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
            await self._advance(job, scheduled, status=None, missed=True)
        return caught

    # -- execution -----------------------------------------------------
    async def _fire(self, job: Job, scheduled_ts: datetime, *, advance: bool = True) -> None:
        """Claim the occurrence and run the job, unless someone claimed it first."""
        if not await self.store.claim(job.id, scheduled_ts):
            log.info("job %s occurrence %s already ran; skipping", job.id, iso(scheduled_ts))
            return
        self._running.add(job.id)
        job.state = "running"
        await self.store.update(job)
        await self._emit(job.id, "started", {"scheduledAt": iso(scheduled_ts)})
        status: JobStatus = "error"
        text = ""
        session_id: str | None = None
        try:
            status, text, session_id = await self._execute(job)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # noqa: BLE001 - a broken job never kills the loop
            log.exception("job %s failed", job.id)
            text = f"{type(exc).__name__}: {exc}"
        finally:
            self._running.discard(job.id)
        await self.store.finish(job.id, scheduled_ts, status, session_id)
        await self.deliver(job, text or f"(job {job.id} produced no output)")
        await self._advance(job, scheduled_ts, status=status, advance=advance)
        kind = FINISH_KINDS[status]
        await self._emit(job.id, kind, {"status": status, "sessionId": session_id, "text": text})

    async def _execute(self, job: Job) -> tuple[JobStatus, str, str | None]:
        """Open the unattended session, run the task, return what it said."""
        from snowpea_core.agent import loop as agent_loop
        from snowpea_core.agent.named import session_for_job

        core = self.core
        # A job that names a persistent agent runs *inside* that agent's
        # session, so it sees the agent's memory namespace (M7 contract §6).
        session = session_for_job(core, job.agent)
        owned = session is None
        if session is None:
            workdir = Path(job.workdir) if job.workdir else core.paths.home
            session = await core.sessions.create(
                workdir=workdir,
                mode=job.mode,
                agent=job.agent,
                origin_surface="scheduler",
                origin_conn=None,
            )
        # The contract calls these sessions unattended; the flag is what the
        # gateway and approval code read when they need to know (contract §2).
        session.unattended = True
        collector = _Collector()
        core.hub.subscribe(collector, session.id)
        core.lifecycle.set_counter("sessions", len(core.sessions))
        try:
            await agent_loop.run_turn(core, session, job.task, unattended=True)
        finally:
            core.hub.unsubscribe(collector)
            # A named agent's session outlives the run that borrowed it.
            if owned:
                with contextlib.suppress(Exception):
                    await core.sessions.close(session.id)
            core.lifecycle.set_counter("sessions", len(core.sessions))
        return collector.status(), collector.final_text, session.id

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
        await self.store.update(fresh)
        await self.refresh_counter()

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
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
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


__all__ = ["FINISH_KINDS", "LOG_CHANNEL", "Scheduler"]
