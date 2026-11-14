"""``/goal`` — work toward an objective in rounds until it is proven done (M16 P1.6).

``/goal`` shows the goal; ``/goal <objective> [--rounds N]`` sets it and starts
the first round; ``/goal pause``, ``/goal resume [N]`` (N more rounds) and
``/goal clear`` control it.  The rules are in :mod:`snowpea_core.agent.goals`.
"""

from __future__ import annotations

import re
from typing import Any

from snowpea_core.agent import goal_driver, goals
from snowpea_core.commands.registry import Command, CommandContext

USAGE = "Usage: /goal [<objective> [--rounds N]] | /goal pause | /goal resume [N] | /goal clear"

ARGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "objective": {
            "type": "string",
            "description": (
                "What has to be true when done, optionally followed by --rounds N; or one of "
                "pause, resume [N], clear. Omit to show the current goal."
            ),
        }
    },
}

_ROUNDS = re.compile(r"\s--rounds[= ](\d+)\s*$")


def describe(session: Any) -> str:
    goal = getattr(session, "goal", None)
    if goal is None:
        return f"No goal. {USAGE}"
    lines = [
        f"Goal: {goal.objective}",
        f"Phase: {goal.phase}{' (armed)' if getattr(session, 'goal_armed', False) else ''}"
        f" — round {goal.rounds}/{goal.max_rounds}, revision {goal.revision}",
    ]
    if goal.phase == "blocked" and goal.blocked_code:
        lines.append(f"Blocked: {goal.blocked_code} — {goal.blocked_message or ''}".rstrip(" —"))
    if goal.phase == "complete" and goal.evidence:
        lines.append(f"Evidence: {goal.evidence}")
    return "\n".join(lines)


def _start_after(ctx: CommandContext) -> None:
    """Start the first round once this command's own turn has finished."""
    core, session = ctx.core, ctx.session
    task = getattr(session, "turn_task", None)

    def go(_task: Any = None) -> None:
        if goals.due(session):
            goal_driver.start_round(core, session)

    if task is not None and not task.done():
        task.add_done_callback(go)
    else:
        go()


async def cmd_goal(ctx: CommandContext, args: str) -> None:
    session = ctx.session
    text = (args or "").strip()
    if getattr(session, "is_subagent", False):
        await ctx.say("A delegated child does not take a goal.")
        return
    if not text:
        await ctx.say(describe(session))
        return
    word, _, rest = text.partition(" ")
    keyword = word.lower()
    if keyword == "clear":
        goals.clear(session)
        await goal_driver.publish(ctx.core, session)
        await ctx.say("Goal cleared.")
        return
    if keyword == "pause":
        if getattr(session, "goal", None) is None:
            await ctx.say("No goal to pause.")
            return
        goals.pause(session)
        await goal_driver.publish(ctx.core, session)
        await ctx.say(describe(session))
        return
    if keyword == "resume":
        if getattr(session, "goal", None) is None:
            await ctx.say("No goal to resume.")
            return
        extra = int(rest) if rest.strip().isdigit() else 0
        goals.resume(session, extra)
        await goal_driver.publish(ctx.core, session)
        await ctx.say(describe(session))
        _start_after(ctx)
        return
    rounds: int | None = None
    found = _ROUNDS.search(" " + text)
    if found:
        rounds = int(found.group(1))
        text = (" " + text)[: found.start()].strip()
    try:
        goals.start(session, text, rounds)
    except ValueError:
        await ctx.say(USAGE)
        return
    await goal_driver.publish(ctx.core, session)
    await ctx.say(describe(session))
    _start_after(ctx)


GOAL_COMMAND = Command(
    name="goal",
    summary="Work toward an objective in rounds until proven done: /goal <objective> [--rounds N].",
    run=cmd_goal,
    args_schema=ARGS_SCHEMA,
)

COMMANDS: tuple[Command, ...] = (GOAL_COMMAND,)

__all__ = ["ARGS_SCHEMA", "COMMANDS", "GOAL_COMMAND", "USAGE", "cmd_goal", "describe"]
