"""``/delegate <agent> <task>`` — hand one task to a named agent, in the open.

Before this, ``/delegate`` (and the ``$<agent> <task>`` shorthand parsed in
``session.prompt``) reached ``agent.spawn`` directly: a subagent started in
the background, and once it finished nobody turned that into a reply on the
parent session — the child's own transcript was the only place the answer
showed up.

Both paths now go through here instead, and here is a completely ordinary
main-agent turn: the user's task becomes a turn whose text also tells the
model to call ``delegate_task`` for the named agent, wait for the result, and
then answer. ``agent_loop.run_turn`` drives that turn exactly like any other
prompt — same history, same ``message.done``/``turn.done`` — so the model's
own reply *is* the reply the user sees, with whatever the subagent reported
folded into it. ``agent.spawn`` still exists for a client that really does
want fire-and-forget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.agent import loop as agent_loop
from snowpea_core.commands.agent_cmd import definitions_for
from snowpea_core.commands.registry import Command, CommandContext

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

USAGE = "Usage: /delegate <agent> <task>"

DELEGATE_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "agent": {"type": "string", "description": "Name of the agent to delegate to."},
        "task": {"type": "string", "description": "What that agent should do."},
    },
    "required": ["agent", "task"],
}


def known_agents(core: Core, session: Session) -> list[str]:
    """Agent names ``delegate_task`` would accept from this session right now."""
    names = [defn.name for defn in definitions_for(core, session.workdir)]
    if session.team_agents:
        allowed = set(session.team_agents)
        names = [name for name in names if name in allowed]
    return sorted(dict.fromkeys(names))


def compose_delegation(agent: str, task: str) -> str:
    """The turn's user text: the task, plus the instruction that delegates it.

    One string, because a turn only ever gets one user message — there is no
    separate per-turn system slot to put the instruction in (contract §8's
    tiers are stable/context/volatile, none of them "this one turn only").
    """
    return (
        f"{task}\n\n"
        "---\n"
        f"Delegate this to the '{agent}' agent: call "
        f'delegate_task(agent="{agent}", task="{task}"), wait for its result, then '
        "answer the user with the outcome — what was done, files changed, and "
        "anything left."
    )


async def cmd_delegate(ctx: CommandContext, args: str) -> None:
    """``/delegate <agent> <task>``: a normal turn that delegates and reports back."""
    agent, _, task = args.strip().partition(" ")
    agent = agent.strip()
    task = task.strip()
    if not agent or not task:
        await ctx.say(USAGE)
        return
    available = known_agents(ctx.core, ctx.session)
    if agent not in available:
        names = ", ".join(available) if available else "(none defined for this session)"
        await ctx.say(f"delegate: unknown agent '{agent}'. Available agents: {names}")
        return
    ctx.handled_turn = True
    await agent_loop.run_turn(
        ctx.core,
        ctx.session,
        compose_delegation(agent, task),
        turn_id=ctx.turn_id,
        unattended=ctx.session.origin_conn is None,
    )


COMMANDS: tuple[Command, ...] = (
    Command(
        name="delegate",
        summary="Delegate one task to a named agent, then report back: /delegate <agent> <task>.",
        run=cmd_delegate,
        args_schema=DELEGATE_ARGS_SCHEMA,
    ),
)


__all__ = [
    "COMMANDS",
    "DELEGATE_ARGS_SCHEMA",
    "USAGE",
    "cmd_delegate",
    "compose_delegation",
    "known_agents",
]
