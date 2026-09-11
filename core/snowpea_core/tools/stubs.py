"""Tools whose implementation lands in a later milestone.

They are registered now, at ``state="inactive"``, so ``tool.list`` describes
the full surface the protocol promises and a caller gets
``error{code:"not_implemented"}`` instead of an unknown-tool error.  Delegation
arrives with M7, scheduling and memory with M5.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.server.protocol import PermissionTag
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

NOT_IMPLEMENTED = "not_implemented"


def _stub(name: str, milestone: str) -> Any:
    async def run(_ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=False, error=f"{NOT_IMPLEMENTED}: {name} arrives in {milestone}")

    return run


def _tool(
    name: str,
    category: str,
    permission: PermissionTag,
    description: str,
    milestone: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> Tool:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return Tool(
        name=name,
        category=category,
        description=description,
        input_schema=schema,
        permission=permission,
        run=_stub(name, milestone),
        state="inactive",
    )


TOOLS: tuple[Tool, ...] = (
    _tool(
        "delegate_task",
        "delegate",
        "exec",
        "Hand a self-contained task to a sub-agent and return its report.",
        "M7",
        {
            "task": {"type": "string", "description": "What the sub-agent should do."},
            "agent": {"type": "string", "description": "Named agent to run it."},
        },
        ["task"],
    ),
    _tool(
        "schedule_create",
        "schedule",
        "send",
        "Schedule a prompt to run on a cron expression.",
        "M5",
        {
            "cron": {"type": "string", "description": "Five-field cron expression."},
            "prompt": {"type": "string", "description": "Prompt to run on each tick."},
        },
        ["cron", "prompt"],
    ),
    _tool(
        "schedule_list",
        "schedule",
        "send",
        "List the schedules this daemon knows about.",
        "M5",
        {},
    ),
    _tool(
        "schedule_cancel",
        "schedule",
        "send",
        "Cancel a schedule by id.",
        "M5",
        {"id": {"type": "string", "description": "Schedule id from schedule_list."}},
        ["id"],
    ),
    _tool(
        "memory_write",
        "memory",
        "write",
        "Store a durable note the agent can recall in later sessions.",
        "M5",
        {
            "text": {"type": "string", "description": "What to remember."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Labels to file the note under.",
            },
        },
        ["text"],
    ),
    _tool(
        "memory_search",
        "memory",
        "read",
        "Search stored memories and return the matching notes.",
        "M5",
        {"query": {"type": "string", "description": "What to look for."}},
        ["query"],
    ),
)


__all__ = ["NOT_IMPLEMENTED", "TOOLS"]
