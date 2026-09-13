"""``/schedule`` — register, list and cancel jobs from a chat surface (US-015).

``/schedule "<spec>" "<task>" [--channel X] [--mode M]`` registers a job,
``/schedule list`` prints them and ``/schedule cancel <id>`` stops one.  The
arguments are split with :mod:`shlex`, so the quotes in the contract's example
work and a task may contain spaces.

Typing the command is the user speaking, so it registers the job directly
rather than asking for approval; the ``schedule_create`` tool is the path that
asks, because there it is the model proposing the job.
"""

from __future__ import annotations

import shlex
from typing import Any

from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.scheduler import services
from snowpea_core.scheduler.tools import format_job

MODES = ("plan", "accept", "auto")

ARGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spec": {
            "type": "string",
            "description": "When to run: cron, 'in 10m', 'every 30m', '매일 09:00'.",
        },
        "task": {"type": "string", "description": "Prompt to run on each firing."},
        "channel": {"type": "string", "description": "Where the answer goes, e.g. telegram:123."},
        "mode": {"type": "string", "enum": list(MODES), "description": "Mode for the run."},
    },
}

USAGE = (
    'Usage: /schedule "<spec>" "<task>" [--channel <channel>] [--mode plan|accept|auto]\n'
    "       /schedule list\n"
    "       /schedule cancel <jobId>\n"
    '  /schedule "매일 09:00" "레포 상태 요약" --channel telegram:12345'
)


def parse_args(args: str) -> dict[str, Any]:
    """``'"in 60s" "echo hi" --mode auto'`` -> a kwargs dict for the scheduler."""
    try:
        parts = shlex.split(args)
    except ValueError as exc:
        raise ValueError(f"could not read the arguments: {exc}") from exc
    positional: list[str] = []
    options: dict[str, Any] = {}
    index = 0
    while index < len(parts):
        token = parts[index]
        if token.startswith("--"):
            name = token[2:]
            if name not in ("channel", "mode", "agent"):
                raise ValueError(f"unknown option --{name}")
            if index + 1 >= len(parts):
                raise ValueError(f"--{name} needs a value")
            options[name] = parts[index + 1]
            index += 2
            continue
        positional.append(token)
        index += 1
    if len(positional) < 2:
        raise ValueError("a schedule needs both a spec and a task")
    mode = options.get("mode", "accept")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; pick one of {', '.join(MODES)}")
    return {
        "spec": positional[0],
        "task": " ".join(positional[1:]),
        "mode": mode,
        "channel": options.get("channel"),
        "agent": options.get("agent"),
    }


async def cmd_schedule(ctx: CommandContext, args: str) -> None:
    """Register, list or cancel a scheduled job."""
    text = args.strip()
    if not text:
        await ctx.say(USAGE)
        return
    scheduler = services(ctx.core)

    head, _, rest = text.partition(" ")
    if head == "list":
        jobs = await scheduler.list()
        if not jobs:
            await ctx.say("no jobs are scheduled")
            return
        await ctx.say("\n".join(format_job(job) for job in jobs))
        return
    if head == "cancel":
        job_id = rest.strip()
        if not job_id:
            await ctx.say("Usage: /schedule cancel <jobId>")
            return
        cancelled = await scheduler.cancel(job_id)
        await ctx.say(f"cancelled {job_id}" if cancelled else f"no such job: {job_id}")
        return

    try:
        parsed = parse_args(text)
        job = await scheduler.schedule(
            parsed["spec"],
            parsed["task"],
            mode=parsed["mode"],
            channel=parsed["channel"],
            origin_session_id=ctx.session.id,
            agent=parsed["agent"],
            workdir=str(ctx.session.workdir),
        )
    except ValueError as exc:
        await ctx.say(f"{exc}\n\n{USAGE}")
        return
    await ctx.say(f"scheduled {format_job(job)}")


COMMANDS: tuple[Command, ...] = (
    Command(
        name="schedule",
        summary="Schedule a prompt to run unattended, or list and cancel jobs.",
        run=cmd_schedule,
        args_schema=ARGS_SCHEMA,
    ),
)


__all__ = ["ARGS_SCHEMA", "COMMANDS", "MODES", "USAGE", "cmd_schedule", "parse_args"]
