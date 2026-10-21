"""``mcp_add`` — register an MCP server the user asked for, globally by default.

Asked to "add this MCP" with a URL, an agent used to dig through
``settings.json``, the installed sources and ``snowpea mcp --help`` for twenty
rounds, then wrote the server into the project's ``.mcp.json`` (the CLI's old
default) or its own client script — so the next project could not see it.
This is the direct path: the same ``mcp.add`` the desktop's sheet calls, into
``$SNOWPEA_HOME/.mcp.json`` unless the user said "for this project", probed
before it is saved, and usable from the very next round of this turn.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import McpAddParams
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

_FIELDS = ("type", "command", "args", "env", "url", "headers", "toolTimeoutSec")


async def mcp_add(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from snowpea_core.server.mcp_handlers import mcp_add_handler

    fields = {key: args[key] for key in _FIELDS if args.get(key) not in (None, "", [], {})}
    try:
        params = McpAddParams(
            name=str(args.get("name") or "").strip(),
            scope=str(args.get("scope") or "global"),  # type: ignore[arg-type]
            workdir=str(ctx.session.workdir),
            force=bool(args.get("force")),
            **fields,
        )
    except ValidationError as exc:
        return ToolResult(ok=False, error=f"mcp_add: {exc.errors(include_url=False)}")
    try:
        result = await mcp_add_handler(None, params, ctx.core)
    except RpcError as exc:
        return ToolResult(ok=False, error=f"mcp_add ({exc.code}): {exc}")
    names = ", ".join(f"mcp__{params.name}__{tool.name}" for tool in result.tools)
    lines = [
        f"added '{params.name}' to {result.path} ({params.scope}); state {result.state}",
        f"tools ({len(result.tools)}): {names}" if names else "no tools reported",
        "They are callable from your next step in this turn (tool_search finds their schemas).",
    ]
    if result.warnings:
        lines.append("accepted with force: " + "; ".join(result.warnings))
    if result.error:
        lines.append(f"start error: {result.error}")
    return ToolResult(ok=True, output="\n".join(lines))


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="mcp_add",
        category="settings",
        description=(
            "Register an MCP server the user asked you to add (a URL, or a command for a "
            "stdio server). Saves to the global $SNOWPEA_HOME/.mcp.json so every project "
            "can use it; pass scope 'project' only when the user asked for this project "
            "alone. Tests the server first and makes its tools usable in this turn. Do not "
            "write .mcp.json or a client script yourself."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short server name, letters, digits, '_' or '-'.",
                },
                "url": {"type": "string", "description": "Endpoint of an http or sse server."},
                "type": {
                    "type": "string",
                    "enum": ["http", "sse", "stdio"],
                    "description": "Transport; inferred from url or command when omitted.",
                },
                "headers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "HTTP headers, e.g. {\"Authorization\": \"Bearer …\"}.",
                },
                "command": {"type": "string", "description": "Executable of a stdio server."},
                "args": {"type": "array", "items": {"type": "string"}},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
                "toolTimeoutSec": {"type": "number", "description": "Per-call cap, seconds."},
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "global (default) or project.",
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "Accept a security finding the first attempt reported (e.g. a plain "
                        "http URL the user gave you)."
                    ),
                },
            },
            "required": ["name"],
        },
        permission="config",
        run=mcp_add,
    ),
)


__all__ = ["TOOLS", "mcp_add"]
