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


async def cmd_update(ctx: CommandContext, args: str) -> None:
    """Check for a newer snowpea and, unless asked only to check, install it.

    The same code path every surface uses: the TUI runs it behind its own
    confirmation, headless and IDE clients get it as ``/update``.
    """
    from snowpea_core import update as update_mod
    from snowpea_core.server.protocol import Empty
    from snowpea_core.server.update_handlers import update_handler

    core = ctx.core
    wants_check_only = args.strip().lower() in ("check", "--check")
    # An explicit /update always asks the network; the cached answer is for the
    # background nudge, not for someone who just typed the command.
    answer = await update_mod.check_update(core.paths, core.settings, force=True)
    current, latest = answer["current"], answer["latest"]
    if answer.get("error"):
        await ctx.say(f"Could not check for updates: {answer['error']} (current v{current})")
        return
    if not answer["available"]:
        await ctx.say(f"snowpea v{current} is up to date.")
        return
    if wants_check_only:
        await ctx.say(f"Update available: v{latest} (current v{current}). Run /update to install.")
        return

    result = await update_handler(ctx.conn, Empty(), core)
    if not result.started:
        await ctx.say(f"Could not start the update: {result.error}")
        return
    await ctx.say(
        f"Updating to v{latest} — running `{result.command}`.\n"
        f"Output: {result.log}. Restart snowpea when it finishes."
    )


async def cmd_compact(ctx: CommandContext, args: str) -> None:
    """Summarise the conversation so far and continue with the summary.

    The manual half of CORE-context; the automatic half fires from the agent
    loop at ``context.autoCompactPercent``.  Anything after ``/compact`` is
    passed to the summariser as extra instructions, so a user can say which
    parts matter.
    """
    from snowpea_core.session import compaction

    instructions = args.strip() or None
    result = await compaction.compact_session(ctx.core, ctx.session, instructions)
    if not result.compacted:
        await ctx.say("Nothing to compact yet — the conversation is still short.")
        return
    before = compaction.format_tokens(result.before)
    after = compaction.format_tokens(result.after)
    await ctx.say(
        f"Compacted the conversation: ~{before} → ~{after} tokens, "
        f"{result.kept} message(s) kept verbatim."
    )


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
    Command(
        name="compact",
        summary="Summarise the conversation so far and continue with the summary.",
        run=cmd_compact,
        args_schema={
            "type": "object",
            "properties": {
                "instructions": {
                    "type": "string",
                    "description": "What the summary must keep, e.g. 'the API design decisions'.",
                }
            },
        },
    ),
    Command(
        name="update",
        summary="Check for a newer snowpea and install it ('/update check' only reports).",
        run=cmd_update,
        args_schema={
            "type": "object",
            "properties": {
                "check": {"type": "boolean", "description": "Only report; install nothing."}
            },
        },
    ),
)


__all__ = [
    "COMMANDS",
    "MODES",
    "MODE_ARGS_SCHEMA",
    "cmd_allow",
    "cmd_allowlist",
    "cmd_approvals",
    "cmd_compact",
    "cmd_help",
    "cmd_mode",
    "cmd_tools",
    "cmd_update",
]
