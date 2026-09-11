"""``/team N <task>`` — the slash command in front of team mode (M7 §4, §5).

The command is a thin wrapper: it parses ``N`` and the quoted task, hands both
to :class:`~snowpea_core.agent.team.TeamManager`, and waits for the run so that
``turn.done`` really means the team is finished and its worktrees are gone.
"""

from __future__ import annotations

import logging

from snowpea_core.agent import team_store
from snowpea_core.agent.team import TeamError, get_manager_for
from snowpea_core.agent.team import parse_team_args as _parse_args
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.server import errors
from snowpea_core.session import events

log = logging.getLogger("snowpea.commands.team")

USAGE = 'Usage: /team <N> "<task>"'

TEAM_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "n": {"type": "integer", "description": "How many workers to run in parallel."},
        "task": {"type": "string", "description": "What the team should build."},
    },
    "required": ["n", "task"],
}


async def cmd_team(ctx: CommandContext, args: str) -> None:
    """``/team 3 "add docstrings"`` — split, work in worktrees, merge."""
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
        log.exception("could not start a team")
        await _fail(ctx, f"could not start the team: {type(exc).__name__}: {exc}")
        return

    status = await manager.status(team_id)
    await ctx.say(
        "\n".join(
            [
                f"team {team_id}: {len(status.tasks)} tasks across {workers} worktrees.",
                *(f"  {row.taskId} {row.title}" for row in status.tasks),
            ]
        )
    )

    await manager.wait(team_id)
    final = await manager.status(team_id)
    merged = [row for row in final.tasks if row.status == team_store.MERGED]
    failed = [row for row in final.tasks if row.status == team_store.FAILED]
    lines = [
        f"team {team_id} finished: {len(merged)} merged, {len(failed)} failed.",
        *(
            f"  {row.taskId} {row.status}"
            + (f" (retries {row.retries})" if row.retries else "")
            + (f" — {row.conflictSummary}" if row.conflictSummary else "")
            for row in final.tasks
        ),
    ]
    await ctx.say("\n".join(lines))


async def _fail(ctx: CommandContext, message: str) -> None:
    """End the team turn unsuccessfully."""
    await ctx.say(f"team: {message}")
    await ctx.emit(events.error(errors.INTERNAL, message))
    ctx.handled_turn = True
    await ctx.emit(events.turn_done(ctx.turn_id, "error"))


COMMANDS: tuple[Command, ...] = (
    Command(
        name="team",
        summary='Run N agents in parallel git worktrees: /team 3 "<task>".',
        run=cmd_team,
        args_schema=TEAM_ARGS_SCHEMA,
    ),
)


__all__ = ["COMMANDS", "TEAM_ARGS_SCHEMA", "USAGE", "cmd_team"]
