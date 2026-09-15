"""``/workers N "<task>"`` — N identical workers, one git worktree each (M7 §5).

This is the mode ``/team <N>`` used to be.  It moved to a name of its own
because the two things ``/team`` carried were not variations of one idea: a
*team* is the people you assembled, each doing the job their role implies, and
*workers* are N copies of one anonymous agent racing through a task list.
Choosing between them by whether the first word happened to be a number was a
puzzle rather than a grammar.

The work itself is unchanged — :class:`~snowpea_core.agent.team.TeamManager`
splits the task onto a board, gives each worker a worktree, and merges the
branches as tasks finish.  Only the spelling moved.
"""

from __future__ import annotations

import logging

from snowpea_core.agent import team_store
from snowpea_core.agent.team import TeamError, get_manager_for
from snowpea_core.agent.team import parse_team_args as _parse_args
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.server import errors
from snowpea_core.session import events

log = logging.getLogger("snowpea.commands.workers")

USAGE = 'Usage: /workers <N> "<task>"'

#: What ``/team <N> …`` now says instead of quietly doing this.
MOVED_HINT = 'worker mode is /workers <N> "<task>"'

WORKERS_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "n": {
            "type": "integer",
            "description": "How many identical workers to run, each in its own git worktree.",
        },
        "task": {"type": "string", "description": "What the workers should build."},
    },
    "required": ["n", "task"],
}


async def cmd_workers(ctx: CommandContext, args: str) -> None:
    """``/workers 3 "add docstrings"`` — split, work in worktrees, merge."""
    try:
        workers, task = _parse_args(args)
    except TeamError:
        await ctx.say(USAGE)
        return

    manager = get_manager_for(ctx.core)
    try:
        team_id = await manager.start(ctx.session, workers, task)
    except TeamError as exc:
        await _fail(ctx, str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - a broken start ends the command
        log.exception("could not start the workers")
        await _fail(ctx, f"could not start the workers: {type(exc).__name__}: {exc}")
        return

    status = await manager.status(team_id)
    await ctx.say(
        "\n".join(
            [
                f"{team_id}: {len(status.tasks)} tasks across {workers} worktrees.",
                *(f"  {row.taskId} {row.title}" for row in status.tasks),
            ]
        )
    )

    await manager.wait(team_id)
    final = await manager.status(team_id)
    merged = [row for row in final.tasks if row.status == team_store.MERGED]
    failed = [row for row in final.tasks if row.status == team_store.FAILED]
    await ctx.say(
        "\n".join(
            [
                f"{team_id} finished: {len(merged)} merged, {len(failed)} failed.",
                *(
                    f"  {row.taskId} {row.status}"
                    + (f" (retries {row.retries})" if row.retries else "")
                    + (f" — {row.conflictSummary}" if row.conflictSummary else "")
                    for row in final.tasks
                ),
            ]
        )
    )


async def _fail(ctx: CommandContext, message: str) -> None:
    """End the turn unsuccessfully."""
    await ctx.say(f"workers: {message}")
    await ctx.emit(events.error(errors.INTERNAL, message))
    ctx.handled_turn = True
    await ctx.emit(events.turn_done(ctx.turn_id, "error"))


COMMANDS: tuple[Command, ...] = (
    Command(
        name="workers",
        summary='Run N identical workers, one git worktree each: /workers <N> "<task>".',
        run=cmd_workers,
        args_schema=WORKERS_ARGS_SCHEMA,
    ),
    Command(
        name="worker",
        summary='Alias of /workers: /worker <N> "<task>".',
        run=cmd_workers,
        args_schema=WORKERS_ARGS_SCHEMA,
    ),
)


__all__ = [
    "COMMANDS",
    "MOVED_HINT",
    "USAGE",
    "WORKERS_ARGS_SCHEMA",
    "cmd_workers",
]
