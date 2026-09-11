"""The ``memory_write`` / ``memory_search`` tools (M5 contract §1).

These replace the inactive M5 placeholders registered by
``tools/stubs.py``: registering a tool under an existing name overwrites it, so
``register_memory_tools`` is called after the builtin catalog.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.memory.retrieval import namespace_of
from snowpea_core.memory.store import MemoryEntry
from snowpea_core.tools.registry import Tool, ToolContext, ToolRegistry, ToolResult

WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "What to remember."},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Labels to file the note under, e.g. profile:deploy_target.",
        },
    },
    "required": ["text"],
}

SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to look for."},
        "limit": {"type": "integer", "description": "Maximum number of memories to return."},
    },
    "required": ["query"],
}


def _services(ctx: ToolContext) -> Any:
    from snowpea_core.memory import services

    return services(ctx.core)


def format_entry(entry: MemoryEntry) -> str:
    tags = f" tags={','.join(entry.tags)}" if entry.tags else ""
    return f"[mem:{entry.id}]{tags} {entry.text}"


async def memory_write(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Store one durable note for this session's namespace."""
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolResult(ok=False, error="memory_write needs a non-empty text")
    raw_tags = args.get("tags") or []
    tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
    memory = _services(ctx)
    entry = await memory.store.write(
        text,
        tags=tags,
        namespace=namespace_of(ctx.session),
        source_session=ctx.session.id,
    )
    return ToolResult(ok=True, output=f"remembered as [mem:{entry.id}]")


async def memory_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Recall memories matching a query, best first."""
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="memory_search needs a non-empty query")
    try:
        limit = int(args.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    memory = _services(ctx)
    entries = await memory.store.search(
        query,
        namespace=namespace_of(ctx.session),
        limit=limit or memory.settings.top_k,
    )
    if not entries:
        return ToolResult(ok=True, output="no memories matched")
    return ToolResult(ok=True, output="\n".join(format_entry(entry) for entry in entries))


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="memory_write",
        category="memory",
        description="Store a durable note the agent can recall in later sessions.",
        input_schema=WRITE_SCHEMA,
        permission="write",
        run=memory_write,
    ),
    Tool(
        name="memory_search",
        category="memory",
        description="Search stored memories and return the matching notes.",
        input_schema=SEARCH_SCHEMA,
        permission="read",
        run=memory_search,
    ),
)


def register_memory_tools(registry: ToolRegistry) -> ToolRegistry:
    """Install the active memory tools over the inactive M5 stubs."""
    for tool in TOOLS:
        registry.register(tool)
    return registry


__all__ = [
    "SEARCH_SCHEMA",
    "TOOLS",
    "WRITE_SCHEMA",
    "format_entry",
    "memory_search",
    "memory_write",
    "register_memory_tools",
]
