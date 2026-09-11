"""Mode, approval-queue and allowlist slash commands (contract §9, M4).

``/plan``, ``/accept`` and ``/auto`` switch the session mode; ``/mode`` shows,
switches or persists it; ``/approvals`` lists the unattended backlog; ``/allow``
and ``/allowlist`` edit the persistent allowlist that promotes ``ask`` to
``allow``.
"""

from __future__ import annotations

import shlex
from typing import get_args

from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.config.project import ProjectSettings
from snowpea_core.permissions.allowlist import (
    SHELL_TARGET,
    Allowlist,
    AllowlistItem,
    Scope,
    pattern_for_tool,
    tool_target,
)
from snowpea_core.server.protocol import Mode
from snowpea_core.session import events

MODES: tuple[str, ...] = get_args(Mode)

MODE_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": [*MODES, "save", "show"],
            "description": (
                "Mode to switch to, 'save' to make the current mode this project's "
                "default, or 'show' to print it."
            ),
        }
    },
}

ALLOW_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": (
                "Regex matched against a shell command, or 'tool:<name>' for a whole tool."
            ),
        },
        "global": {
            "type": "boolean",
            "description": "Store in $SNOWPEA_HOME/settings.json instead of this project.",
        },
    },
    "required": ["pattern"],
}

ALLOWLIST_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "remove"],
            "description": "'list' (default) prints the entries, 'remove <id>' deletes one.",
        }
    },
}


def _allowlist(ctx: CommandContext) -> Allowlist | None:
    return getattr(ctx.core, "allowlist", None)


async def _set_mode(ctx: CommandContext, mode: str) -> None:
    await ctx.core.sessions.set_mode(ctx.session, mode)  # type: ignore[arg-type]
    await ctx.emit(events.mode_changed(mode))


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------


async def cmd_mode(ctx: CommandContext, args: str) -> None:
    """Show, change or persist the session mode."""
    target = args.strip().lower()
    if not target or target == "show":
        await ctx.say(f"Mode: {ctx.session.mode} (one of {', '.join(MODES)}, or 'save' / 'show')")
        return
    if target == "save":
        project = ProjectSettings.load(ctx.session.workdir)
        project.defaultMode = ctx.session.mode  # type: ignore[assignment]
        path = project.save(ctx.session.workdir)
        await ctx.say(f"Default mode for this project is now '{ctx.session.mode}' ({path}).")
        return
    if target not in MODES:
        await ctx.say(f"Unknown mode '{target}'. Use one of: {', '.join(MODES)}, save, show.")
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


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


async def cmd_approvals(ctx: CommandContext, args: str) -> None:
    """List the unattended approvals still waiting for an answer."""
    scope_all = args.strip().lower() in {"all", "--all"}
    pending = ctx.core.approvals.unattended(None if scope_all else ctx.session.id)
    if not pending:
        await ctx.say("No unattended approvals are waiting.")
        return
    lines = [f"Unattended approvals ({len(pending)}):"]
    for request in pending:
        detail = request.args.get("command") or ""
        lines.append(
            f"  {request.requestId} · {request.tool} · risk={request.risk}"
            + (f" · {detail}" if detail else "")
        )
    lines.append("")
    lines.append("Answer with approval.respond(requestId, decision, scope).")
    await ctx.say("\n".join(lines))


# ---------------------------------------------------------------------------
# allowlist
# ---------------------------------------------------------------------------


def _describe(items: list[AllowlistItem]) -> list[str]:
    return [f"  {item.id} · [{item.scope}] {item.target} · {item.pattern}" for item in items]


async def cmd_allow(ctx: CommandContext, args: str) -> None:
    """Add an allowlist pattern: ``/allow <regex> [--global]``."""
    allowlist = _allowlist(ctx)
    if allowlist is None:
        await ctx.say("The allowlist is not available on this daemon.")
        return
    try:
        parts = shlex.split(args)
    except ValueError:
        parts = args.split()
    is_global = any(part in ("--global", "-g") for part in parts)
    rest = [part for part in parts if part not in ("--global", "-g")]
    if not rest:
        await ctx.say("Usage: /allow <regex> [--global]   (or /allow tool:<name>)")
        return
    raw = " ".join(rest)
    if raw.startswith("tool:"):
        name = raw[len("tool:") :].strip()
        if not name:
            await ctx.say("Usage: /allow tool:<name> [--global]")
            return
        pattern, target = pattern_for_tool(name), tool_target(name)
    else:
        pattern, target = raw, SHELL_TARGET
    scope: Scope = "global" if is_global else "project"
    try:
        pattern_id = allowlist.add(pattern, scope, target, workdir=ctx.session.workdir)
    except ValueError as exc:
        await ctx.say(f"Not a usable pattern: {exc}")
        return
    await ctx.say(f"Allowed [{scope}] {target} · {pattern} ({pattern_id})")


async def cmd_allowlist(ctx: CommandContext, args: str) -> None:
    """Show the allowlist, or ``/allowlist remove <id>``."""
    allowlist = _allowlist(ctx)
    if allowlist is None:
        await ctx.say("The allowlist is not available on this daemon.")
        return
    parts = args.split()
    if parts and parts[0] == "remove":
        if len(parts) < 2:
            await ctx.say("Usage: /allowlist remove <id>")
            return
        removed = allowlist.remove(parts[1], workdir=ctx.session.workdir)
        await ctx.say(
            f"Removed {parts[1]}." if removed else f"No allowlist entry with id {parts[1]}."
        )
        return
    items = allowlist.list(workdir=ctx.session.workdir)
    if not items:
        await ctx.say("The allowlist is empty. Add one with /allow <regex>.")
        return
    await ctx.say("\n".join([f"Allowlist ({len(items)}):", *_describe(items)]))


COMMANDS: tuple[Command, ...] = (
    _mode_command("plan"),
    _mode_command("accept"),
    _mode_command("auto"),
    Command(
        name="mode",
        summary="Show or change the mode: /mode [plan|accept|auto|save|show].",
        run=cmd_mode,
        args_schema=MODE_ARGS_SCHEMA,
    ),
    Command(
        name="approvals",
        summary="List the unattended approvals waiting for an answer.",
        run=cmd_approvals,
        args_schema={"type": "object", "properties": {}},
    ),
    Command(
        name="allow",
        summary="Allow a command without asking: /allow <regex> [--global].",
        run=cmd_allow,
        args_schema=ALLOW_ARGS_SCHEMA,
    ),
    Command(
        name="allowlist",
        summary="Show the allowlist, or /allowlist remove <id>.",
        run=cmd_allowlist,
        args_schema=ALLOWLIST_ARGS_SCHEMA,
    ),
)


__all__ = [
    "ALLOWLIST_ARGS_SCHEMA",
    "ALLOW_ARGS_SCHEMA",
    "COMMANDS",
    "MODES",
    "MODE_ARGS_SCHEMA",
    "cmd_allow",
    "cmd_allowlist",
    "cmd_approvals",
    "cmd_mode",
]
