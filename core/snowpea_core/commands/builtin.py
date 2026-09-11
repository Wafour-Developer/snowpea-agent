"""The M1 built-in slash commands (contract §9)."""

from __future__ import annotations

from typing import get_args

from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.config.project import ProjectSettings
from snowpea_core.server.protocol import Mode
from snowpea_core.session import events

MODES: tuple[str, ...] = get_args(Mode)

MODE_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": [*MODES, "save"],
            "description": "Mode to switch to, or 'save' to make the current mode the default.",
        }
    },
}


async def _set_mode(ctx: CommandContext, mode: str) -> None:
    await ctx.core.sessions.set_mode(ctx.session, mode)  # type: ignore[arg-type]
    await ctx.emit(events.mode_changed(mode))


async def cmd_help(ctx: CommandContext, args: str) -> None:
    """List the available commands."""
    lines = ["Commands:"]
    for command in ctx.core.commands.commands():
        lines.append(f"  /{command.name} — {command.summary}")
    lines.append("")
    lines.append("Anything that does not start with '/' is sent to the model.")
    await ctx.say("\n".join(lines))


async def cmd_mode(ctx: CommandContext, args: str) -> None:
    """Show, change or persist the session mode."""
    target = args.strip().lower()
    if not target:
        await ctx.say(f"Mode: {ctx.session.mode} (one of {', '.join(MODES)}, or 'save')")
        return
    if target == "save":
        project = ProjectSettings.load(ctx.session.workdir)
        project.defaultMode = ctx.session.mode  # type: ignore[assignment]
        path = project.save(ctx.session.workdir)
        await ctx.say(f"Default mode for this project is now '{ctx.session.mode}' ({path}).")
        return
    if target not in MODES:
        await ctx.say(f"Unknown mode '{target}'. Use one of: {', '.join(MODES)}, save.")
        return
    await _set_mode(ctx, target)
    await ctx.say(f"Mode: {target}")


def _mode_command(mode: str) -> Command:
    async def run(ctx: CommandContext, args: str) -> None:
        await _set_mode(ctx, mode)
        await ctx.say(f"Mode: {mode}")

    return Command(
        name=mode,
        summary=f"Switch the session to {mode} mode.",
        run=run,
        args_schema={"type": "object", "properties": {}},
    )


async def cmd_tools(ctx: CommandContext, args: str) -> None:
    """List the registered tools and their permission tags."""
    infos = ctx.core.tools.list(ctx.session)
    if not infos:
        await ctx.say("No tools are registered.")
        return
    lines = ["Tools:"]
    for info in infos:
        lines.append(
            f"  {info.name} [{info.category}/{info.permissionTag}] "
            f"({info.state}) — {info.description}"
        )
    await ctx.say("\n".join(lines))


COMMANDS: tuple[Command, ...] = (
    Command(
        name="help",
        summary="List the available commands.",
        run=cmd_help,
        args_schema={"type": "object", "properties": {}},
    ),
    _mode_command("plan"),
    _mode_command("accept"),
    _mode_command("auto"),
    Command(
        name="mode",
        summary="Show or change the mode: /mode [plan|accept|auto|save].",
        run=cmd_mode,
        args_schema=MODE_ARGS_SCHEMA,
    ),
    Command(
        name="tools",
        summary="List the registered tools.",
        run=cmd_tools,
        args_schema={"type": "object", "properties": {}},
    ),
)


__all__ = ["COMMANDS", "MODES", "cmd_help", "cmd_mode", "cmd_tools"]
