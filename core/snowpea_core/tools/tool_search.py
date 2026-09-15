"""``tool_search`` — turn a deferred tool's name into its schema.

The tools fragment names the deferred groups; this is how the model gets from
``browser (5)`` to a callable ``browser_navigate``.  Anything it returns is
added to ``session.loaded_tools``, so the next round's tool list carries those
schemas in full and the model never has to search for the same tool twice.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from snowpea_core.prompts.tool_descriptions import TOOL_SEARCH as DESCRIPTION
from snowpea_core.tools import deferred
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.providers.base import ToolSpec

#: Said when the query matched nothing, so the model stops guessing at it.
NO_MATCH = (
    "No tool matched {query!r}. Every tool you already have is in your tool list; "
    "the deferred groups are named on the last line of it."
)

#: Header above the matches.
FOUND = (
    "Loaded {count} tool(s) for this session. They are in your tool list from the "
    "next round on, and you can call them now."
)


def render(specs: list[ToolSpec]) -> str:
    """The matches, in the shape the tool list uses, plus each input schema."""
    lines: list[str] = []
    for spec in specs:
        lines.append(f"- {spec.name}: {spec.description}")
        try:
            schema = json.dumps(spec.input_schema, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):  # pragma: no cover - a schema that will not encode
            schema = "{}"
        lines.append(f"  input schema: {schema}")
    return "\n".join(lines)


async def tool_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Find deferred tools by exact name (``select:a,b``) or by keyword."""
    query = str(args.get("query", "") or "").strip()
    if not query:
        return ToolResult(ok=False, error="tool_search needs a query")
    registry = getattr(ctx.core, "tools", None)
    if registry is None:  # pragma: no cover - a Core with no registry
        return ToolResult(ok=False, error="no tool registry is available")
    limit = args.get("limit")
    try:
        count = int(limit) if limit is not None else deferred.MAX_RESULTS
    except (TypeError, ValueError):
        count = deferred.MAX_RESULTS
    catalogue = registry.all_specs(ctx.session)
    matches = deferred.search(catalogue, query, min(max(1, count), deferred.MAX_RESULTS))
    if not matches:
        return ToolResult(ok=True, output=NO_MATCH.format(query=query))
    deferred.load(ctx.session, [spec.name for spec in matches])
    invalidate(ctx.session)
    return ToolResult(
        ok=True,
        output=f"{FOUND.format(count=len(matches))}\n\n{render(matches)}",
        meta={"loaded": [spec.name for spec in matches]},
    )


def invalidate(session: Any) -> None:
    """Drop the cached tools fragment: this session's tool list just changed."""
    from snowpea_core.agent import agent as agent_mod

    agent_mod.invalidate_tools(session)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="tool_search",
        category="tools",
        description=DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        'Either "select:name,name" for exact tools, or a few keywords; '
                        'prefix a keyword with + to require it, e.g. "+browser click".'
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"How many tools to return, at most {deferred.MAX_RESULTS}."
                    ),
                },
            },
            "required": ["query"],
        },
        permission="read",
        run=tool_search,
    ),
)


__all__ = ["FOUND", "NO_MATCH", "TOOLS", "invalidate", "render", "tool_search"]
