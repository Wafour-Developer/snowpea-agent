"""The ``save_memory`` / ``search_memory`` tools (M5 contract §1, §1b).

These replace the inactive M5 placeholders registered by
``tools/stubs.py``: registering a tool under an existing name overwrites it, so
``register_memory_tools`` is called after the builtin catalog.  They were
``memory_write`` / ``memory_search`` before; see ``tools/renames.py`` for why
and for the old names that still resolve.

"Remember this" is ambiguous — the same sentence means "for this repo" and
"for me, everywhere" depending on who says it.  ``save_memory`` used to stop
the turn and ask; that question waited up to ten minutes and twice timed out
in the middle of a task (feedback report).  Now, without a ``scope``, the note
is kept for the project at once (the narrower, safer guess), and the result
says so and how to make it global, which the model passes on in its answer.
The turn never waits.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.memory.browser import BROWSER_NAMESPACE
from snowpea_core.memory.retrieval import (
    namespace_of,
    namespaces_for,
    project_namespace_of,
)
from snowpea_core.memory.safety import refusal, sensitive_kind
from snowpea_core.memory.scopes import GLOBAL_NAMESPACE, label, scope_of
from snowpea_core.memory.store import MemoryEntry
from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools.registry import Tool, ToolContext, ToolRegistry, ToolResult

WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "The fact to remember, in the user's own words.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Labels to file the note under, e.g. profile:deploy_target.",
        },
        "scope": {
            "type": "string",
            "enum": ["project", "global"],
            "description": (
                "Where to keep it. 'project' is this repository only; 'global' is every "
                "project. Pass it ONLY when the user said which one they meant "
                "('프로젝트에 기억해', 'remember globally'). Otherwise leave it out: it is "
                "kept for this project and the result says how to make it global."
            ),
        },
    },
    "required": ["text"],
}

SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Words to look for, e.g. 'deploy target'.",
        },
        "limit": {"type": "integer", "description": "Maximum number of memories to return."},
        "namespace": {
            "type": "string",
            "enum": ["browser"],
            "description": (
                "Search only this namespace. 'browser' is the text of pages the "
                "browser saved; it is not in the automatic recall, so ask for it here."
            ),
        },
    },
    "required": ["query"],
}

#: Told with a project save the model chose no scope for (feedback: never block).
PROJECT_DEFAULT_NOTE = (
    "Kept for this project only. If it should apply to every project, tell the user "
    "so in your answer; if they say yes, save it again with scope 'global'."
)


#: What the memory tools answer in a session created with ``memory: false``.
INACTIVE = "tool_inactive: memory is turned off for this session"


def _off(ctx: ToolContext) -> bool:
    """``session.create {memory: false}`` (addendum 27, C1), or its root's (addendum 30, C4)."""
    from snowpea_core.session.session import inherited_override

    return inherited_override(ctx.core, ctx.session, "memory_override") is False


def _services(ctx: ToolContext) -> Any:
    from snowpea_core.memory import services

    return services(ctx.core)


def format_entry(entry: MemoryEntry) -> str:
    """One search hit, labelled with the scope it came from (§1b)."""
    tags = f" tags={','.join(entry.tags)}" if entry.tags else ""
    return f"[mem:{entry.id}] {label(entry.namespace)}{tags} {entry.text}"


def resolve_namespace(ctx: ToolContext, scope: str | None) -> tuple[str, str]:
    """Which namespace ``save_memory`` should use, without asking anyone.

    Returns ``(namespace, note)``; ``note`` is added to the result when the
    model gave no scope and the note went to the project.
    """
    session = ctx.session
    project = project_namespace_of(session)
    session_namespace = namespace_of(session)

    if scope == "global":
        return GLOBAL_NAMESPACE, ""
    if scope == "project":
        if not project:
            # The user asked for a project scope in a session that has no
            # project; global is the only place it can go, and saying so is
            # more useful than refusing the write.
            return GLOBAL_NAMESPACE, ""
        return project, ""

    # No scope passed by the model.
    if not project:
        return GLOBAL_NAMESPACE, ""
    if scope_of(session_namespace) == "agent":
        # A named agent's own memory stays its own (M7 §6); scopes do not
        # reach into it unless the model asked for one explicitly.
        return session_namespace, ""
    return project, PROJECT_DEFAULT_NOTE


async def save_memory(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Store one durable note: in the scope the user named, else for the project."""
    if _off(ctx):
        return ToolResult(ok=False, error=INACTIVE, meta={"written": False})
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolResult(ok=False, error="save_memory needs a non-empty text")
    kind = sensitive_kind(text)
    if kind is not None:
        return ToolResult(ok=False, error=refusal(kind), meta={"written": False})
    raw_scope = str(args.get("scope") or "").strip().lower() or None
    if raw_scope is not None and raw_scope not in {"project", "global"}:
        return ToolResult(
            ok=False,
            error="scope must be 'project' or 'global'; omit it to keep it for this project",
        )
    raw_tags = args.get("tags") or []
    tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []

    from snowpea_core.memory import access

    forced, denied = access.write_namespace(ctx.core, ctx.session, raw_scope)
    if denied:
        return ToolResult(ok=False, error=denied, meta={"written": False})
    if forced is not None:
        # Jarvis writes only to its own memory (M19 WP4).
        namespace, note = forced, ""
    else:
        namespace, note = resolve_namespace(ctx, raw_scope)

    memory = _services(ctx)
    entry = await memory.store.write(
        text,
        tags=tags,
        namespace=namespace,
        source_session=ctx.session.id,
        origin="model",
    )
    where = entry.project or "every project"
    output = f"remembered as [mem:{entry.id}] {label(namespace)} ({where})"
    return ToolResult(
        ok=True,
        output=f"{output}. {note}" if note else output,
        meta={"written": True, "id": entry.id, "scope": entry.scope, "project": entry.project},
    )


#: Candidates fetched per result wanted, before :func:`relevant` drops some.
SEARCH_OVERFETCH = 3

#: A word in at most this share of the memories searched (and in at least
#: one) is distinctive: matching it alone makes a hit relevant.
DISTINCTIVE_SHARE = 0.05


def query_words(query: str) -> list[str]:
    """The query's distinct words of two characters or more, lower-cased."""
    seen: dict[str, None] = {}
    for word in query.lower().split():
        word = word.strip(".,;:!?\"'()[]{}")
        if len(word) >= 2:
            seen.setdefault(word, None)
    return list(seen)


async def relevant(
    store: Any, query: str, entries: list[MemoryEntry], namespaces: list[str]
) -> list[MemoryEntry]:
    """``entries`` without the hits that only share the query's common words.

    The index matches any shared trigram, so "danawa 다나와 site flow" brought
    back every "Google Flow" note.  A hit stays when it contains more than half
    of the query's words, or one distinctive word (rare in the store).
    """
    words = query_words(query)
    if len(words) < 2 or not entries:
        return entries
    total = await store.count(namespaces=namespaces)
    cutoff = max(2, int(total * DISTINCTIVE_SHARE))
    distinctive = set()
    for word in words:
        if 0 < await store.containing(word, namespaces=namespaces) <= cutoff:
            distinctive.add(word)
    kept = []
    for entry in entries:
        text = entry.text.lower()
        matched = {word for word in words if word in text}
        if len(matched) * 2 > len(words) or matched & distinctive:
            kept.append(entry)
    return kept


async def search_memory(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Recall memories matching a query, best first, across every scope."""
    if _off(ctx):
        return ToolResult(ok=False, error=INACTIVE)
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="search_memory needs a non-empty query")
    try:
        limit = int(args.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    wanted = str(args.get("namespace") or "").strip()
    if wanted and wanted != BROWSER_NAMESPACE:
        return ToolResult(
            ok=False, error=f"unknown namespace {wanted!r}; use 'browser' or leave it out"
        )
    memory = _services(ctx)
    from snowpea_core.memory import access

    jarvis_reads = await access.read_namespaces(ctx.core, ctx.session)
    if jarvis_reads is not None:
        # Jarvis: global, its own, and every exposed project's (M19 WP4).
        if wanted:
            return ToolResult(ok=False, error="Jarvis does not search browser memory")
        namespaces = jarvis_reads
    else:
        namespaces = [wanted] if wanted else namespaces_for(ctx.session)
    limit = limit or memory.settings.top_k
    found = await memory.store.search_many(
        query, namespaces=namespaces, limit=limit * SEARCH_OVERFETCH
    )
    entries = (await relevant(memory.store, query, found, namespaces))[:limit]
    if not entries:
        return ToolResult(ok=True, output="no memories matched")
    output = "\n".join(format_entry(entry) for entry in entries)
    if jarvis_reads is not None:
        # Jarvis: memories hold text sessions wrote (or a fooled Jarvis saved):
        # data inside the fence, never instructions (security review L2).
        from snowpea_core.jarvis.tools import fence

        output = fence("memory", output)
    return ToolResult(ok=True, output=output)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="save_memory",
        category="memory",
        description=descriptions.MEMORY_WRITE,
        input_schema=WRITE_SCHEMA,
        permission="write",
        run=save_memory,
    ),
    Tool(
        name="search_memory",
        category="memory",
        description=descriptions.MEMORY_SEARCH,
        input_schema=SEARCH_SCHEMA,
        permission="read",
        run=search_memory,
    ),
)


def register_memory_tools(registry: ToolRegistry) -> ToolRegistry:
    """Install the active memory tools over the inactive M5 stubs."""
    for tool in TOOLS:
        registry.register(tool)
    return registry


__all__ = [
    "INACTIVE",
    "PROJECT_DEFAULT_NOTE",
    "SEARCH_SCHEMA",
    "TOOLS",
    "WRITE_SCHEMA",
    "format_entry",
    "search_memory",
    "save_memory",
    "register_memory_tools",
    "resolve_namespace",
]
