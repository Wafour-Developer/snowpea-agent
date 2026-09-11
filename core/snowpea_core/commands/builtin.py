"""The built-in slash commands that are not about permissions (contract §9).

The mode, approval-queue and allowlist commands live in
:mod:`snowpea_core.commands.mode_cmd`; they are re-exported here so older
imports keep working.
"""

from __future__ import annotations

from snowpea_core.commands.mode_cmd import (
    MODE_ARGS_SCHEMA,
    MODES,
    cmd_allow,
    cmd_allowlist,
    cmd_approvals,
    cmd_mode,
)
from snowpea_core.commands.registry import Command, CommandContext


async def cmd_help(ctx: CommandContext, args: str) -> None:
    """List the available commands."""
    lines = ["Commands:"]
    for command in ctx.core.commands.commands():
        lines.append(f"  /{command.name} — {command.summary}")
    lines.append("")
    lines.append("Anything that does not start with '/' is sent to the model.")
    await ctx.say("\n".join(lines))


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
    Command(
        name="tools",
        summary="List the registered tools.",
        run=cmd_tools,
        args_schema={"type": "object", "properties": {}},
    ),
)


__all__ = [
    "COMMANDS",
    "MODES",
    "MODE_ARGS_SCHEMA",
    "cmd_allow",
    "cmd_allowlist",
    "cmd_approvals",
    "cmd_help",
    "cmd_mode",
    "cmd_tools",
]
