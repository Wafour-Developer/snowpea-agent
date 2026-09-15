"""``/delegate <agent> <task>`` — hand one task to a named agent, in the open.

Before this, ``/delegate`` (and the ``$<agent> <task>`` shorthand parsed in
``session.prompt``) reached ``agent.spawn`` directly: a subagent started in
the background, and once it finished nobody turned that into a reply on the
parent session — the child's own transcript was the only place the answer
showed up.

Both paths now go through here instead.  The command calls the real
``delegate_task`` tool directly, emits the same visible tool/subagent events a
model-initiated delegation would, and then writes the child report back to the
parent session.  ``agent.spawn`` still exists for a client that really does
want fire-and-forget.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from snowpea_core.commands.agent_cmd import definitions_for
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.session import events
from snowpea_core.tools.delegate import delegate_task
from snowpea_core.tools.registry import ProgressEmitter, ToolContext

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
        registry = getattr(core, "named_agents", None)
        names = [
            name
            for name in names
            if name in allowed or (registry is not None and registry.get(name) is not None)
        ]
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
    """``/delegate <agent> <task>``: delegate deterministically and report back."""
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

    call_id = f"call-{uuid.uuid4().hex[:12]}"
    tool_args = {"agent": agent, "task": task, "title": f"{agent} delegation"}
    await ctx.emit(events.tool_call(call_id, "delegate_task", tool_args))
    result = await delegate_task(
        ToolContext(
            session=ctx.session,
            core=ctx.core,
            backend=None,  # type: ignore[arg-type]  # delegate_task does not use a backend
            call_id=call_id,
            progress=ProgressEmitter(ctx.core, ctx.session.id, call_id, "delegate_task"),
        ),
        tool_args,
    )
    await ctx.emit(
        events.tool_result(
            call_id,
            "delegate_task",
            result.ok,
            output=result.output,
            error=result.error,
        )
    )
    await ctx.say(result.output or result.error or "delegate_task returned no report")


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
