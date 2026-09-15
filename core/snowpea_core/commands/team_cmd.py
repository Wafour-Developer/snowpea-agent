"""``/team`` — run the project's team, by role (M7 §4, §9).

``/team`` is one idea now: *your team does this*.

* ``/team "<task>"`` — the active team's members, each in the role its name
  implies, staged plan → implement → test → review
  (:mod:`snowpea_core.agent.team_pipeline`).
* ``/team <name> "<task>"`` — the same pipeline on a *named* team, project or
  global, for this one run.  The project's ``activeTeam`` is not touched.
* ``/team create|use|list|delete`` — the project roster.

**N identical workers moved to** :mod:`~snowpea_core.commands.workers_cmd`
(``/workers <N> "<task>"``).  They were never a variation of a team: a team is
the people you assembled, workers are N copies of one anonymous agent racing
through a task list, and deciding between them by whether the first word was a
number was a puzzle rather than a grammar.  ``/team <N> …`` now says where the
mode went instead of quietly doing it — a silent fallback would leave a user
who typed the old spelling with no idea the grammar had changed.

The run is awaited, so ``turn.done`` really means the team is finished.
"""

from __future__ import annotations

import logging
import re
import shlex

from snowpea_core.agent.team_pipeline import (
    PipelineError,
    named_roster,
    parse_pipeline_args,
    run_pipeline,
    stage_line,
    stage_summary,
)
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.commands.workers_cmd import cmd_workers
from snowpea_core.server import errors
from snowpea_core.session import events

log = logging.getLogger("snowpea.commands.team")

USAGE = ('Usage: /team "<task>" | /team <name> "<task>" | '
         '/team create <name> <agent...> | /team use <name>|none | /team list | '
         '/team delete <name>')
WORKERS_COMPAT_NOTE = "`/workers N` is the current spelling; forwarding this `/team N` request."

#: ``/team <bareword> "<task>"`` — a leading name followed by a *quoted* task.
#: The quotes are what marks the first word as a team name: without them
#: ``/team add docstrings`` would read "add" as an unknown team rather than as
#: the task the user plainly meant.
NAMED_RUN = re.compile(r"""^(\S+)\s+(["'])(.+)\2\s*$""", re.DOTALL)

#: What ``/team use`` takes to mean "no team at all".
NONE_WORDS = frozenset({"none", "--none", "-"})

TEAM_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "team": {
            "type": "string",
            "description": (
                "Name of a project or global team to run this once; omit it to use "
                "the project's active team."
            ),
        },
        "task": {"type": "string", "description": "What the team should build."},
    },
    "required": ["task"],
}


async def cmd_team(ctx: CommandContext, args: str) -> None:
    """``/team "add docstrings"`` — the roster, by role, staged."""
    try:
        words = shlex.split(args)
    except ValueError:
        await ctx.say(USAGE)
        return
    if words and words[0] in {"create", "use", "list", "delete"}:
        await _configure_team(ctx, words)
        return
    if words and words[0].isdigit():
        await _run_workers_compat(ctx, args)
        return
    await _dispatch_pipeline(ctx, args)


async def _run_workers_compat(ctx: CommandContext, args: str) -> None:
    """Run the old ``/team N`` spelling through the ``/workers`` command."""
    original_say = ctx.say
    note_sent = False

    async def say_with_note(text: str) -> None:
        nonlocal note_sent
        if not note_sent:
            note_sent = True
            text = f"{WORKERS_COMPAT_NOTE}\n{text}"
        await original_say(text)

    ctx.say = say_with_note  # type: ignore[method-assign]
    try:
        await cmd_workers(ctx, args)
    finally:
        ctx.say = original_say  # type: ignore[method-assign]


async def _dispatch_pipeline(ctx: CommandContext, args: str) -> None:
    """Tell ``/team <name> "<task>"`` from ``/team "<task>"`` and run it."""
    from snowpea_core.agent.team_config import teams_with_source

    match = NAMED_RUN.match(args.strip())
    if match is not None and match.group(1).lower() != "run":
        name = match.group(1)
        roster = named_roster(ctx.core, ctx.session.workdir, name)
        if roster is None:
            known = sorted(teams_with_source(ctx.core.settings, ctx.session.workdir))
            await _fail(
                ctx,
                f"unknown team '{name}'; known teams: {', '.join(known) or 'none'} "
                "(/team create <name> <agent...> makes one)",
            )
            return
        await _run_pipeline(ctx, match.group(3).strip(), roster=roster, source=name)
        return
    await _run_pipeline(ctx, parse_pipeline_args(args))


async def _run_pipeline(
    ctx: CommandContext,
    task: str,
    *,
    roster: list[str] | None = None,
    source: str | None = None,
) -> None:
    """``/team "<task>"`` — a roster, by role, staged."""
    if not task:
        await ctx.say(USAGE)
        return
    if roster is None and not _has_a_roster(ctx):
        await _fail(
            ctx,
            'no active team — /team use <name>, or /team <name> "<task>" to run one '
            "just this once (/team list shows them)",
        )
        return
    try:
        report = await run_pipeline(ctx.core, ctx.session, task, ctx.say, roster, source=source)
    except PipelineError as exc:
        await _fail(ctx, str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - a broken pipeline ends the command
        log.exception("the team pipeline failed")
        await _fail(ctx, f"the pipeline failed: {type(exc).__name__}: {exc}")
        return
    await ctx.say(report)
    if _pipeline_report_failed(report):
        await _fail(ctx, "the team pipeline finished with incomplete evidence")


def _pipeline_report_failed(report: str) -> bool:
    """Whether a rendered pipeline report means the command should fail."""
    lines = [line.strip() for line in report.splitlines()]
    stages = _report_value(lines, "stages")
    tests = _report_value(lines, "tests")
    verify = _report_value(lines, "verify")
    review = _report_value(lines, "review")
    if any(line == "Left unfinished:" for line in lines):
        return True
    if "test=" in stages and tests != "TESTS: PASS":
        return True
    if "verify=" in stages and verify != "VERIFY: PASS":
        return True
    return "review=" in stages and review != "APPROVE"


def _report_value(lines: list[str], key: str) -> str:
    prefix = f"{key}:"
    return next((line.partition(":")[2].strip() for line in lines if line.startswith(prefix)), "")


def _has_a_roster(ctx: CommandContext) -> bool:
    """True when ``/team "<task>"`` has somebody to run: a team, or the default."""
    from snowpea_core.agent.team_pipeline import roster_for

    return bool(roster_for(ctx.core, ctx.session)[0])


async def _configure_team(ctx: CommandContext, words: list[str]) -> None:
    """Manage the project roster without disturbing legacy team execution."""
    from snowpea_core.agent.team_config import teams_for, teams_with_source
    from snowpea_core.commands.agent_cmd import definitions_for
    from snowpea_core.config.project import ProjectSettings

    action = words[0]
    project = ProjectSettings.load(ctx.session.workdir)
    all_teams = teams_for(ctx.core.settings, ctx.session.workdir)
    if action == "list":
        active = project.agents.activeTeam or ctx.core.settings.agents.default_team
        sourced = teams_with_source(ctx.core.settings, ctx.session.workdir)
        lines = ["Teams:"]
        for name, (members, origin) in sorted(sourced.items()):
            lines.append(
                f"  {'*' if name == active else ' '} {name} [{origin}]: {', '.join(members)}"
            )
            # A roster is a list of names until you know which stage of
            # `/team "<task>"` each one fills, so say it here.
            lines.append(f"      {stage_line(ctx.core, ctx.session.workdir, members)}")
        if not sourced:
            lines.append("  (none — /team create <name> <agent...> makes one)")
        lines.append("Pipeline stages for the active roster:")
        lines.extend(stage_summary(ctx.core, ctx.session))
        await ctx.say("\n".join(lines))
        return
    if len(words) < 2:
        await ctx.say(USAGE)
        return
    name = words[1]
    if action == "use" and name.lower() in NONE_WORDS:
        # Back to no team: delegation is unrestricted again and `/team
        # "<task>"` falls back to the global default roster.
        project.agents.activeTeam = None
        project.save(ctx.session.workdir)
        ctx.session.team = None
        ctx.session.team_agents = ()
        await ctx.say("Active team cleared. This project has no team now.")
        return
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
    if ctx.session.job_id:
        await ctx.emit(events.job_failed(ctx.session.job_id, ctx.session.id, text=message))
    ctx.handled_turn = True
    await ctx.emit(events.turn_done(ctx.turn_id, "error"))


COMMANDS: tuple[Command, ...] = (
    Command(
        name="team",
        summary=('Run the project team by role: /team "<task>" | /team <name> "<task>", '
                 'or manage the roster: /team create <name> <agent...> | /team use '
                 '<name>|none | /team list | /team delete <name>.'),
        run=cmd_team,
        args_schema=TEAM_ARGS_SCHEMA,
    ),
)


__all__ = ["COMMANDS", "TEAM_ARGS_SCHEMA", "USAGE", "WORKERS_COMPAT_NOTE", "cmd_team"]
