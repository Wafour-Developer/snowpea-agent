"""``/team N <task>`` — the slash command in front of team mode (M7 §4, §5).

The command is a thin wrapper: it parses ``N`` and the quoted task, hands both
to :class:`~snowpea_core.agent.team.TeamManager`, and waits for the run so that
``turn.done`` really means the team is finished and its worktrees are gone.
"""

from __future__ import annotations

import logging
import shlex

from snowpea_core.agent import team_store
from snowpea_core.agent.team import TeamError, get_manager_for
from snowpea_core.agent.team import parse_team_args as _parse_args
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.server import errors
from snowpea_core.session import events

log = logging.getLogger("snowpea.commands.team")

USAGE = ('Usage: /team <N> "<task>" | /team create <name> <agent...> | '
         '/team use <name> | /team list | /team delete <name>')

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
        words = shlex.split(args)
    except ValueError:
        await ctx.say(USAGE)
        return
    if words and words[0] in {"create", "use", "list", "delete"}:
        await _configure_team(ctx, words)
        return
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


async def _configure_team(ctx: CommandContext, words: list[str]) -> None:
    """Manage the project roster without disturbing legacy team execution."""
    from snowpea_core.agent.team_config import teams_for
    from snowpea_core.commands.agent_cmd import definitions_for
    from snowpea_core.config.project import ProjectSettings

    action = words[0]
    project = ProjectSettings.load(ctx.session.workdir)
    all_teams = teams_for(ctx.core.settings, ctx.session.workdir)
    if action == "list":
        active = project.agents.activeTeam or ctx.core.settings.agents.default_team
        lines = ["Teams:"]
        for name, members in sorted(all_teams.items()):
            lines.append(f"  {'*' if name == active else ' '} {name}: {', '.join(members)}")
        await ctx.say("\n".join(lines))
        return
    if len(words) < 2:
        await ctx.say(USAGE)
        return
    name = words[1]
    if action == "create":
        members = list(dict.fromkeys(words[2:]))
        known = {definition.name for definition in definitions_for(ctx.core, ctx.session.workdir)}
        missing = [member for member in members if member not in known]
        if not members or missing:
            detail = (
                f" unknown agents: {', '.join(missing)}"
                if missing
                else " choose at least one agent"
            )
            await ctx.say(f"team:{detail}; available: {', '.join(sorted(known))}")
            return
        project.agents.teams[name] = members
        project.agents.activeTeam = name
    elif action == "use":
        if name not in all_teams:
            await ctx.say(f"team: unknown team {name}; use /team list")
            return
        project.agents.activeTeam = name
    elif action == "delete":
        if name not in project.agents.teams:
            await ctx.say(f"team: {name} is not a project team")
            return
        project.agents.teams.pop(name)
        if project.agents.activeTeam == name:
            project.agents.activeTeam = None
    project.save(ctx.session.workdir)
    from snowpea_core.agent.team_config import active_team
    selected = active_team(ctx.core.settings, ctx.session.workdir)
    ctx.session.team = selected.name if selected else None
    ctx.session.team_agents = selected.agents if selected else ()
    if action == "delete":
        await ctx.say(f"Deleted team '{name}'. Active team: {ctx.session.team or 'none'}.")
    else:
        await ctx.say(f"Active team '{ctx.session.team}': {', '.join(ctx.session.team_agents)}")


async def _fail(ctx: CommandContext, message: str) -> None:
    """End the team turn unsuccessfully."""
    await ctx.say(f"team: {message}")
    await ctx.emit(events.error(errors.INTERNAL, message))
    ctx.handled_turn = True
    await ctx.emit(events.turn_done(ctx.turn_id, "error"))


COMMANDS: tuple[Command, ...] = (
    Command(
        name="team",
        summary=('Run a worktree team or manage the project roster: '
                 '/team create <name> <agent...> | /team use <name> | /team list.'),
        run=cmd_team,
        args_schema=TEAM_ARGS_SCHEMA,
    ),
)


__all__ = ["COMMANDS", "TEAM_ARGS_SCHEMA", "USAGE", "cmd_team"]
