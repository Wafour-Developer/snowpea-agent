"""The ``delegate_task`` tool (M7 contract §3).

Hand a self-contained task to a child agent and get its final answer back as
the tool result.  Everything else — the concurrency limit, the child session,
the ``subagent.*`` events — is :mod:`snowpea_core.agent.subagent`'s job.

This replaces the inactive M1 stub of the same name; the name, category and
permission tag are unchanged, so a client that read ``tool.list`` before M7
sees the same tool simply become ``active``.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.agent.definition import builtin_agent_definitions
from snowpea_core.agent.subagent import get_manager
from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

#: Upper bound on ``timeout``, mirroring the ``shell`` tool's own ceiling.
MAX_TIMEOUT = 3600.0


def _builtin_agent_names() -> str:
    return ", ".join(defn.name for defn in builtin_agent_definitions())


BUILTIN_AGENT_NAMES = _builtin_agent_names()
BUILTIN_AGENT_HINT = (
    f" Built-in agents available by name: {BUILTIN_AGENT_NAMES}." if BUILTIN_AGENT_NAMES else ""
)


def _tool_list(value: Any) -> list[str] | None:
    if isinstance(value, str):
        names = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        names = [str(part).strip() for part in value]
    else:
        return None
    filtered = [name for name in names if name]
    return filtered or None


def _timeout(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(MAX_TIMEOUT, seconds)


async def delegate_task(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Run one subagent and report what it answered."""
    task = str(args.get("task", "") or "").strip()
    if not task:
        return ToolResult(ok=False, error="task is required")
    agent = str(args.get("agent", "") or "").strip() or None
    result = await get_manager(ctx.core).run(
        ctx.session,
        task,
        agent=agent,
        tools=_tool_list(args.get("tools")),
        timeout=_timeout(args.get("timeout")),
    )
    if not result.ok:
        return ToolResult(
            ok=False,
            output=result.summary,
            error=result.error or "the subagent did not finish",
        )
    return ToolResult(ok=True, output=result.summary or "(the subagent returned no text)")


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="delegate_task",
        category="delegate",
        description=descriptions.DELEGATE_TASK + BUILTIN_AGENT_HINT,
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "What the sub-agent should do, written so it needs no "
                        "other context than the project itself."
                    ),
                },
                "agent": {
                    "type": "string",
                    "description": (
                        "Name of an agent definition to run it as."
                        + BUILTIN_AGENT_HINT
                        + " Project, global, and plugin custom agent names also resolve "
                        "when present."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict the sub-agent to these tool names.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Seconds to wait before giving up on the sub-agent.",
                },
            },
            "required": ["task"],
        },
        permission="exec",
        run=delegate_task,
    ),
)


__all__ = ["BUILTIN_AGENT_HINT", "BUILTIN_AGENT_NAMES", "MAX_TIMEOUT", "TOOLS", "delegate_task"]
