"""Jarvis's assistant tools: ``briefing_set``, ``schedule_list|create|update|remove|run`` (WP8).

Attached to the Jarvis session through ``service.hooks.tools`` (see
:func:`factory`), so they exist only there like the rest of its tools.

The scheduler is the existing job system; what these tools add is the rules:

* changing what runs unattended in the person's name (a briefing, a recurring
  task) needs the person's own request: they refuse in a turn that a wake
  message started, because anything a wake carries may have been written by
  someone else;
* a task Jarvis creates is a ``jarvis-task`` job: every firing opens its own
  worker session, never Jarvis's, and hands Jarvis only a fenced result
  (:mod:`snowpea_core.jarvis.tasks`);
* a task runs in a project the person exposed to Jarvis, or in Jarvis's folder;
  it has the worker's ordinary permissions (mode ``accept``: what it may not do
  alone waits for the person), never more.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.jarvis import briefing
from snowpea_core.jarvis import tools as jt
from snowpea_core.jarvis.service import JarvisError
from snowpea_core.scheduler import services
from snowpea_core.scheduler.jobs import Job, iso
from snowpea_core.scheduler.nl_parse import parse_spec
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.jarvis.service import JarvisService

TASK_LIMIT = 30
TASK_CHARS = 4000

_PERSON_ONLY = (
    "change what runs on a schedule only while handling the person's own request; "
    "tell the person what you would set up instead"
)


def _require_person(ctx: ToolContext) -> None:
    if not jt.human_turn(ctx.session):
        raise jt.Refused(_PERSON_ONLY)


def _jarvis_jobs(jobs: list[Job]) -> list[Job]:
    return [j for j in jobs if j.target == "jarvis-task" and j.state != "cancelled"]


def _line(service: JarvisService, job: Job) -> str:
    data = job.target_data or {}
    kind = str(data.get("kind") or "task")
    where = ""
    if kind == "task" and job.workdir:
        where = (
            f" in {service.projects.name_of(job.workdir)}"
            if (Path(job.workdir) != service.folder())
            else ""
        )
    zone = f" [{job.timezone}]" if job.timezone else ""
    last = f"; last {job.last_status} at {iso(job.last_run)}" if job.last_status else ""
    state = "" if job.enabled else " (paused)"
    return (
        f"{job.id} {kind}{state}: {job.title or job.task[:60]!r} - {job.spec}{zone}{where}; "
        f"next {iso(job.next_run) or '-'}{last}"
    )


async def _find(ctx: ToolContext, job_id: str) -> Job:
    job = await services(ctx.core).get(job_id)
    if job is None or job.target != "jarvis-task":
        raise jt.Refused(f"no assistant task {job_id!r}; see schedule_list")
    return job


# -- briefing ------------------------------------------------------------------------


async def briefing_set(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = jt._service(ctx)
    keys = {"enabled", "time", "days", "schedule", "timezone", "sections", "instructions"}
    if not any(args.get(k) is not None for k in keys):
        current = await briefing.get(service.core)
        return jt._result(_briefing_text(current))
    _require_person(ctx)
    schedule = args.get("schedule")
    if schedule is None and (args.get("time") is not None or args.get("days") is not None):
        current = await briefing.get(service.core)
        parts = current.schedule.split()
        time = args.get("time")
        if time is None and len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
            time = f"{int(parts[1]):02d}:{int(parts[0]):02d}"
        try:
            schedule = briefing.build_cron(
                time, args.get("days"), keep_days=parts[4] if len(parts) == 5 else None
            )
        except JarvisError as exc:
            raise jt.Refused(str(exc)) from exc
    sections = args.get("sections")
    try:
        result = await briefing.set_briefing(
            service.core,
            enabled=args.get("enabled"),
            schedule=schedule,
            instructions=args.get("instructions"),
            timezone=args.get("timezone"),
            sections=list(sections) if sections is not None else None,
        )
    except JarvisError as exc:
        raise jt.Refused(str(exc)) from exc
    return jt._result(_briefing_text(result))


def _briefing_text(b: Any) -> str:
    if not b.enabled:
        return "The briefing is off. (briefing_set enabled=true with time and days turns it on.)"
    zone = f" ({b.timezone})" if b.timezone else ""
    return (
        f"The briefing is on: {b.schedule}{zone}, next {b.nextRunAt or '-'}; "
        f"covers {', '.join(b.sections)}."
        + (f" Instructions: {b.instructions}" if b.instructions else "")
    )


# -- schedules -----------------------------------------------------------------------


async def schedule_list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = jt._service(ctx)
    jobs = _jarvis_jobs(await services(ctx.core).list())
    if not jobs:
        return jt._result("no assistant tasks are scheduled")
    return jt._result("\n".join(_line(service, j) for j in jobs), count=len(jobs))


async def schedule_create(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = jt._service(ctx)
    _require_person(ctx)
    spec = jt._text(args, "spec", limit=200)
    task = jt._text(args, "task", limit=TASK_CHARS)
    title = jt._text(args, "title", required=False, limit=80) or None
    zone = jt._text(args, "timezone", required=False, limit=60) or None
    try:
        parsed = parse_spec(spec)
    except ValueError as exc:
        raise jt.Refused(str(exc)) from exc
    scheduler = services(ctx.core)
    if len(_jarvis_jobs(await scheduler.list())) >= TASK_LIMIT:
        raise jt.Refused(f"at most {TASK_LIMIT} assistant tasks; remove one first")
    workdir = service.folder()
    project = jt._text(args, "project", required=False)
    if project:
        root = service.projects.resolve(project)
        if root is None or not service.projects.exposed(root) or not Path(root).is_dir():
            raise jt.Refused(f"no exposed project called {project!r}; see project_list")
        workdir = Path(root)
    try:
        job = await scheduler.schedule(
            spec,
            task,
            mode="accept",
            workdir=str(workdir),
            title=title,
            created_via="tool",
            target="jarvis-task",
            target_data={"kind": "task", "title": title or task[:60]},
            timezone=zone,
        )
    except ValueError as exc:
        raise jt.Refused(str(exc)) from exc
    service.record_decision(
        kind="prompt",
        decided_by="jarvis",
        summary=f"scheduled {parsed.display!r}: {task[:120]}",
        importance="normal",
    )
    return jt._result(f"scheduled {_line(service, job)}", id=job.id)


async def schedule_update(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = jt._service(ctx)
    _require_person(ctx)
    job = await _find(ctx, jt._text(args, "id", limit=40))
    kind = str((job.target_data or {}).get("kind") or "task")
    if kind == "briefing":
        raise jt.Refused("the briefing is changed with briefing_set")
    task = jt._text(args, "task", required=False, limit=TASK_CHARS) or None
    if task is not None and kind != "task":
        raise jt.Refused("only the schedule of a mail or calendar digest can change here")
    enabled = args.get("enabled")
    try:
        updated = await services(ctx.core).update(
            job.id,
            schedule=jt._text(args, "spec", required=False, limit=200) or None,
            task=task,
            title=jt._text(args, "title", required=False, limit=80) or None,
            timezone=(
                jt._text(args, "timezone", required=False, limit=60)
                if args.get("timezone") is not None
                else None
            ),
            enabled=bool(enabled) if enabled is not None else None,
        )
    except ValueError as exc:
        raise jt.Refused(str(exc)) from exc
    assert updated is not None
    return jt._result(f"updated {_line(service, updated)}", id=updated.id)


async def schedule_remove(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    jt._service(ctx)
    _require_person(ctx)
    job = await _find(ctx, jt._text(args, "id", limit=40))
    if (job.target_data or {}).get("kind") == "briefing":
        raise jt.Refused("the briefing is turned off with briefing_set enabled=false")
    await services(ctx.core).delete(job.id)
    return jt._result(f"removed {job.id}")


async def schedule_run(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Run one task now, in its own worker; the result arrives as a wake message."""
    service = jt._service(ctx)
    _require_person(ctx)
    job = await _find(ctx, jt._text(args, "id", limit=40))
    scheduler = services(ctx.core)
    task = asyncio.ensure_future(scheduler.run_now(job.id))
    service._tasks.add(task)
    task.add_done_callback(service._tasks.discard)
    with contextlib.suppress(Exception):
        await asyncio.sleep(0)
    return jt._result(
        f"started {job.id} in its own worker session; you will be told when it is done",
        id=job.id,
    )


def factory(service: JarvisService) -> list[Tool]:
    return [
        jt._tool(
            "briefing_set",
            "Read or change the periodic briefing (off until the person asks for it). With no "
            "arguments it reports the current one. Use time 'HH:MM' and days ('weekdays', "
            "'daily', 'weekends' or a list like ['mon','thu']) for when; sections from "
            "sessions, waiting, todos, mail, calendar; instructions in the person's words. "
            "Only while handling the person's own request.",
            {
                "enabled": {"type": "boolean"},
                "time": {"type": "string", "description": "HH:MM, 24-hour, in the timezone."},
                "days": {"type": "array", "items": {"type": "string"}},
                "schedule": {"type": "string", "description": "A cron expression instead."},
                "timezone": {"type": "string", "description": "IANA, e.g. Asia/Seoul."},
                "sections": {"type": "array", "items": {"type": "string"}},
                "instructions": {"type": "string"},
            },
            [],
            briefing_set,
            tag="write",
        ),
        jt._tool(
            "schedule_list",
            "The assistant tasks scheduled for the person (recurring digests and reports, "
            "not the briefing): id, kind, schedule, next and last run.",
            {},
            [],
            schedule_list,
        ),
        jt._tool(
            "schedule_create",
            "Schedule a recurring or one-time assistant task. Each run is its own worker "
            "session (never yours); you receive only its result. Only while handling the "
            "person's own request. spec is a cron expression or 'every day at 9am' / "
            "'매주 금요일 18:00'; project (a name from project_list) picks where it works, "
            "else your folder.",
            {
                "spec": {"type": "string"},
                "task": {"type": "string", "description": "What the worker should do."},
                "title": {"type": "string"},
                "project": {"type": "string"},
                "timezone": {"type": "string", "description": "IANA zone for clock times."},
            },
            ["spec", "task"],
            schedule_create,
            tag="write",
        ),
        jt._tool(
            "schedule_update",
            "Change an assistant task: spec, task, title, timezone, or enabled "
            "(false pauses, true resumes). Only while handling the person's own request.",
            {
                "id": {"type": "string"},
                "spec": {"type": "string"},
                "task": {"type": "string"},
                "title": {"type": "string"},
                "timezone": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            ["id"],
            schedule_update,
            tag="write",
        ),
        jt._tool(
            "schedule_remove",
            "Remove an assistant task and its history. Only while handling the person's own "
            "request.",
            {"id": {"type": "string"}},
            ["id"],
            schedule_remove,
            tag="write",
        ),
        jt._tool(
            "schedule_run",
            "Run an assistant task now (for example the mail digest the person asked to see "
            "right away). It runs in its own worker; the result reaches you as a message.",
            {"id": {"type": "string"}},
            ["id"],
            schedule_run,
            tag="write",
        ),
    ]


__all__ = ["factory"]
