"""The ``schedule_create`` / ``schedule_list`` / ``schedule_cancel`` tools.

These replace the inactive M5 placeholders from ``tools/stubs.py``: registering
a tool under an existing name overwrites it, so ``register_schedule_tools`` is
called after the builtin catalog, exactly like the memory tools.  The names,
category and ``send`` permission tag are the ones the stubs promised.

``send`` means the mode matrix already asks before the model may register a job
in ``accept`` mode.  One case is stricter than the matrix: a job whose *own*
mode is ``auto`` runs unattended with every permission granted, so
``schedule_create`` asks for approval for that even in an ``auto`` session
(M5 contract §2).  A job registered over RPC or from ``/schedule`` is the user
speaking directly and is never prompted.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.scheduler.jobs import Job, iso
from snowpea_core.tools.registry import Tool, ToolContext, ToolRegistry, ToolResult

CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spec": {
            "type": "string",
            "description": (
                "When to run: a cron expression, 'in 10m', 'every 30m', "
                "'every day at 9am' or '매일 09:00'."
            ),
        },
        "task": {"type": "string", "description": "Prompt to run on each firing."},
        "mode": {
            "type": "string",
            "enum": ["plan", "accept", "auto"],
            "description": "Permission mode for the unattended run.",
        },
        "channel": {
            "type": "string",
            "description": "Where the answer goes, e.g. 'telegram:12345' or 'log'.",
        },
    },
    "required": ["spec", "task"],
}

LIST_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

CANCEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string", "description": "Job id from schedule_list."}},
    "required": ["id"],
}


def _scheduler(ctx: ToolContext) -> Any:
    from snowpea_core.scheduler import services

    return services(ctx.core)


def format_job(job: Job) -> str:
    """One line per job, the same wording the CLI prints."""
    parts = [job.id, f"spec={job.spec!r}", f"next={iso(job.next_run) or '-'}", f"mode={job.mode}"]
    if job.channel:
        parts.append(f"channel={job.channel}")
    if job.last_status:
        parts.append(f"last={iso(job.last_run) or '-'}:{job.last_status}")
    if not job.enabled:
        parts.append("disabled")
    return " ".join(parts)


async def _needs_approval(ctx: ToolContext, args: dict[str, Any], mode: str) -> str | None:
    """Ask before registering an ``auto`` job; returns an error string on a denial."""
    if mode != "auto":
        return None
    session = ctx.session
    unattended = getattr(session, "origin_conn", None) is None
    decision = await ctx.core.approvals.request(
        session,
        "schedule_create",
        args,
        risk="high",
        unattended=unattended,
        cancel_event=getattr(session, "interrupt", None),
    )
    if decision.allowed:
        return None
    return f"registering an auto-mode job was not approved ({decision.by})"


async def schedule_create(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Register a job that runs a prompt unattended."""
    spec = str(args.get("spec") or args.get("cron") or "").strip()
    task = str(args.get("task") or args.get("prompt") or "").strip()
    if not spec or not task:
        return ToolResult(ok=False, error="schedule_create needs a spec and a task")
    mode = str(args.get("mode") or "accept")
    if mode not in ("plan", "accept", "auto"):
        return ToolResult(ok=False, error=f"unknown mode {mode!r}")
    denied = await _needs_approval(ctx, dict(args), mode)
    if denied is not None:
        return ToolResult(ok=False, error=denied)
    channel = args.get("channel")
    try:
        job = await _scheduler(ctx).schedule(
            spec,
            task,
            mode=mode,  # type: ignore[arg-type]
            channel=str(channel) if channel else None,
            workdir=str(ctx.session.workdir),
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(ok=True, output=f"scheduled {format_job(job)}")


async def schedule_list(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    """List every job this daemon knows about."""
    jobs = await _scheduler(ctx).list()
    if not jobs:
        return ToolResult(ok=True, output="no jobs are scheduled")
    return ToolResult(ok=True, output="\n".join(format_job(job) for job in jobs))


async def schedule_cancel(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Cancel a job by id."""
    job_id = str(args.get("id") or args.get("jobId") or "").strip()
    if not job_id:
        return ToolResult(ok=False, error="schedule_cancel needs an id")
    cancelled = await _scheduler(ctx).cancel(job_id)
    if not cancelled:
        return ToolResult(ok=False, error=f"no such job: {job_id}")
    return ToolResult(ok=True, output=f"cancelled {job_id}")


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="schedule_create",
        category="schedule",
        description="Schedule a prompt to run unattended on a cron, interval or one-shot spec.",
        input_schema=CREATE_SCHEMA,
        permission="send",
        run=schedule_create,
    ),
    Tool(
        name="schedule_list",
        category="schedule",
        description="List the schedules this daemon knows about.",
        input_schema=LIST_SCHEMA,
        permission="send",
        run=schedule_list,
    ),
    Tool(
        name="schedule_cancel",
        category="schedule",
        description="Cancel a schedule by id.",
        input_schema=CANCEL_SCHEMA,
        permission="send",
        run=schedule_cancel,
    ),
)


def register_schedule_tools(registry: ToolRegistry) -> ToolRegistry:
    """Activate the three schedule tools, replacing the M5 stubs."""
    for tool in TOOLS:
        registry.register(tool)
    return registry


__all__ = [
    "CANCEL_SCHEMA",
    "CREATE_SCHEMA",
    "LIST_SCHEMA",
    "TOOLS",
    "format_job",
    "register_schedule_tools",
    "schedule_cancel",
    "schedule_create",
    "schedule_list",
]
