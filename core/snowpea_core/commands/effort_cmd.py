"""``/effort`` — how hard the model may think, in one word.

Every vendor spells this differently (``reasoning_effort``, ``xhigh``, a
budget in tokens), so the command speaks one scale — ``low``, ``medium``,
``high``, ``max`` — and :mod:`snowpea_core.providers.effort` maps it.  Called
bare it answers the question a user actually has: what is in force right now,
and which rule decided it (CORE-effort).
"""

from __future__ import annotations

from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.providers import effort as effort_scale
from snowpea_core.session import events

USAGE = "usage: /effort [low|medium|high|max|auto]  (auto clears the session pin)"

#: Completion hint for the surfaces that offer the tiers as a picker.
ARGS_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "effort": {
            "type": "string",
            "enum": [*effort_scale.EFFORTS, effort_scale.AUTO],
            "description": (
                "How hard the model may think. 'auto' clears the session pin and "
                "lets agent.effortBy / agent.effort decide. Omit to show what is "
                "in force and which rule decided it."
            ),
        }
    },
}

#: What each source is called in the one line the bare command prints.
SOURCE_LABELS: dict[str, str] = {
    "session": "session pin",
    "model": "model rule",
    "vendor": "vendor rule",
    "default": "agent.effort",
}


def describe(effort: str, source: str) -> str:
    """``effort: high (session pin)`` — the tier and why it is that tier."""
    return f"effort: {effort} ({SOURCE_LABELS.get(source, source)})"


async def cmd_effort(ctx: CommandContext, args: str) -> None:
    """Show the effective effort, or pin this session to one."""
    from snowpea_core.server.session_handlers import effective_effort

    wanted = args.strip().lower()
    if not wanted:
        effort, source = effective_effort(ctx.core, ctx.session)
        await ctx.say(f"{describe(effort, source)}\n{USAGE}")
        return
    try:
        await ctx.core.sessions.set_effort(ctx.session, wanted)
    except ValueError as exc:
        await ctx.say(f"effort: {exc}\n{USAGE}")
        return
    effort, source = effective_effort(ctx.core, ctx.session)
    await ctx.emit(
        events.model_changed(ctx.session.provider, ctx.session.model, effort, source)
    )
    await ctx.say(describe(effort, source))


EFFORT_COMMAND = Command(
    name="effort",
    summary="Show or set how hard the model may think: /effort [low|medium|high|max|auto].",
    run=cmd_effort,
    args_schema=ARGS_SCHEMA,
)

COMMANDS: tuple[Command, ...] = (EFFORT_COMMAND,)

__all__ = [
    "ARGS_SCHEMA",
    "COMMANDS",
    "EFFORT_COMMAND",
    "SOURCE_LABELS",
    "USAGE",
    "cmd_effort",
    "describe",
]
