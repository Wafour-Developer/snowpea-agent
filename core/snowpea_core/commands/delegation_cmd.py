"""``/delegation`` — whether the lead hands the work to its team.

Off by default: the agent works itself and delegates when it judges a task
worth it.  ``/delegation on`` makes this session an orchestrator — plans are
carried out by the team's agents (executor, test-engineer, verifier, …) and
the lead briefs, sequences and relays.  ``auto`` drops the pin and follows
``agents.delegateByDefault`` again.  Called bare it says what is in force and
why.
"""

from __future__ import annotations

from snowpea_core.agent.delegation import delegation_roster, effective_delegation
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.session import events

USAGE = "usage: /delegation [on|off|auto]  (auto follows agents.delegateByDefault)"

ARGS_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "delegation": {
            "type": "string",
            "enum": ["on", "off", "auto"],
            "description": (
                "on: hand implementation, tests and verification to the team's agents; "
                "off: do the work yourself; auto: follow agents.delegateByDefault. "
                "Omit to show what is in force."
            ),
        }
    },
}

SOURCE_LABELS: dict[str, str] = {"session": "session pin", "default": "agents.delegateByDefault"}


def describe(on: bool, source: str, roster: tuple[str, ...]) -> str:
    """``delegation: on (session pin) — team: executor, verifier``."""
    state = "on" if on else "off"
    line = f"delegation: {state} ({SOURCE_LABELS.get(source, source)})"
    if on:
        line += f" — team: {', '.join(roster)}"
    return line


async def cmd_delegation(ctx: CommandContext, args: str) -> None:
    """Show delegation mode, or pin this session on or off."""
    wanted = args.strip().lower()
    if wanted:
        try:
            await ctx.core.sessions.set_delegation(ctx.session, wanted)
        except ValueError as exc:
            await ctx.say(f"delegation: {exc}\n{USAGE}")
            return
    on, source = effective_delegation(ctx.core, ctx.session)
    text = describe(on, source, delegation_roster(ctx.core, ctx.session))
    if not wanted:
        await ctx.say(f"{text}\n{USAGE}")
        return
    await ctx.emit(events.mode_changed(ctx.session.mode, delegation=on))
    await ctx.say(text)


DELEGATION_COMMAND = Command(
    name="delegation",
    summary="Hand the work to the team by default: /delegation [on|off|auto].",
    run=cmd_delegation,
    args_schema=ARGS_SCHEMA,
)

COMMANDS: tuple[Command, ...] = (DELEGATION_COMMAND,)

__all__ = ["ARGS_SCHEMA", "COMMANDS", "DELEGATION_COMMAND", "USAGE", "cmd_delegation", "describe"]
