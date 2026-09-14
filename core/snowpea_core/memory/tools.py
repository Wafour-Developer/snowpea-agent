"""The ``memory_write`` / ``memory_search`` tools (M5 contract §1, §1b).

These replace the inactive M5 placeholders registered by
``tools/stubs.py``: registering a tool under an existing name overwrites it, so
``register_memory_tools`` is called after the builtin catalog.

``memory_write`` is the one tool that will stop and ask a human a question
before it does its job.  "Remember this" is ambiguous in a way most tool calls
are not — the same sentence means "for this repo" and "for me, everywhere"
depending on who says it — and guessing wrong is invisible until the memory
turns up in the wrong project months later.  So when the model does not pass a
``scope``, the tool puts the choice to the person through the questions queue
(the same one ``ask_user`` uses) and writes nothing if they cancel.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.memory.retrieval import (
    display_name,
    namespace_of,
    namespaces_for,
    project_namespace_of,
)
from snowpea_core.memory.scopes import GLOBAL_NAMESPACE, label, scope_of
from snowpea_core.memory.store import MemoryEntry
from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.server.protocol import QuestionItem, QuestionOption
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
        "scope": {
            "type": "string",
            "enum": ["project", "global"],
            "description": (
                "Where to keep it. 'project' is this repository only; 'global' is every "
                "project. Pass it ONLY when the user said which one they meant "
                "('프로젝트에 기억해', 'remember globally'). Otherwise leave it out and "
                "the daemon asks them."
            ),
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

#: Header of the scope question; short, because surfaces render it as a chip.
SCOPE_HEADER = "Where to keep it"
SCOPE_QUESTION = (
    "Save this memory for this project only ({project}) or for every project?"
)
OPTION_PROJECT = "Project ({project})"
OPTION_GLOBAL = "Global (every project)"
OPTION_CANCEL = "Cancel"

CANCELLED = (
    "nothing was remembered: the user cancelled the save. Do not retry without "
    "asking them what they want."
)
NO_ANSWER = (
    "nothing was remembered: nobody answered where to keep it. Say so, and ask "
    "again with an explicit scope when the user is back."
)


def _services(ctx: ToolContext) -> Any:
    from snowpea_core.memory import services

    return services(ctx.core)


def format_entry(entry: MemoryEntry) -> str:
    """One search hit, labelled with the scope it came from (§1b)."""
    tags = f" tags={','.join(entry.tags)}" if entry.tags else ""
    return f"[mem:{entry.id}] {label(entry.namespace)}{tags} {entry.text}"


def _unattended(session: Any) -> bool:
    """True when no human is watching this session, so nobody can be asked."""
    return bool(
        getattr(session, "unattended", False)
        or getattr(session, "is_subagent", False)
        or getattr(session, "kind", "chat") in {"subagent", "scheduled"}
    )


def scope_question(project: str) -> QuestionItem:
    """The single question ``memory_write`` asks when no scope was passed."""
    return QuestionItem(
        header=SCOPE_HEADER,
        question=SCOPE_QUESTION.format(project=project),
        options=[
            QuestionOption(
                label=OPTION_PROJECT.format(project=project),
                description="Only sessions opened in this project recall it.",
            ),
            QuestionOption(
                label=OPTION_GLOBAL,
                description="Every project recalls it.",
            ),
            QuestionOption(label=OPTION_CANCEL, description="Remember nothing."),
        ],
        multi=False,
        allowOther=False,
    )


def _chosen(selected: list[str]) -> str | None:
    """``"project"`` / ``"global"`` / ``None`` (cancel) from the answer labels."""
    first = (selected[0] if selected else "").strip().lower()
    if first.startswith("project"):
        return "project"
    if first.startswith("global"):
        return "global"
    return None


async def resolve_namespace(ctx: ToolContext, scope: str | None) -> tuple[str | None, str]:
    """Which namespace ``memory_write`` should use, asking the human if needed.

    Returns ``(namespace, reason)``.  ``namespace`` is ``None`` when nothing
    should be written, and ``reason`` is then the text the model is shown.
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
    if _unattended(session):
        return project, ""
    memory = _services(ctx)
    if not getattr(memory.settings, "askScope", True):
        return project, ""
    queue = getattr(ctx.core, "questions", None)
    if queue is None:
        return project, ""

    answers = await queue.ask(
        session,
        [scope_question(display_name(session))],
        cancel_event=getattr(session, "interrupt", None),
    )
    answer = answers[0]
    if answer.timed_out:
        return None, NO_ANSWER
    if answer.declined or not answer.selected:
        return None, CANCELLED
    picked = _chosen(answer.selected)
    if picked is None:
        return None, CANCELLED
    return (project if picked == "project" else GLOBAL_NAMESPACE), ""


async def memory_write(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Store one durable note, in the scope the user chose."""
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolResult(ok=False, error="memory_write needs a non-empty text")
    raw_scope = str(args.get("scope") or "").strip().lower() or None
    if raw_scope is not None and raw_scope not in {"project", "global"}:
        return ToolResult(
            ok=False,
            error="scope must be 'project' or 'global'; omit it to let the user choose",
        )
    raw_tags = args.get("tags") or []
    tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []

    namespace, reason = await resolve_namespace(ctx, raw_scope)
    if namespace is None:
        return ToolResult(ok=True, output=reason, meta={"written": False})

    memory = _services(ctx)
    entry = await memory.store.write(
        text,
        tags=tags,
        namespace=namespace,
        source_session=ctx.session.id,
    )
    where = entry.project or "every project"
    return ToolResult(
        ok=True,
        output=f"remembered as [mem:{entry.id}] {label(namespace)} ({where})",
        meta={"written": True, "id": entry.id, "scope": entry.scope, "project": entry.project},
    )


async def memory_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Recall memories matching a query, best first, across every scope."""
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="memory_search needs a non-empty query")
    try:
        limit = int(args.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    memory = _services(ctx)
    entries = await memory.store.search_many(
        query,
        namespaces=namespaces_for(ctx.session),
        limit=limit or memory.settings.top_k,
    )
    if not entries:
        return ToolResult(ok=True, output="no memories matched")
    return ToolResult(ok=True, output="\n".join(format_entry(entry) for entry in entries))


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="memory_write",
        category="memory",
        description=descriptions.MEMORY_WRITE,
        input_schema=WRITE_SCHEMA,
        permission="write",
        run=memory_write,
    ),
    Tool(
        name="memory_search",
        category="memory",
        description=descriptions.MEMORY_SEARCH,
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
    "CANCELLED",
    "NO_ANSWER",
    "OPTION_CANCEL",
    "OPTION_GLOBAL",
    "OPTION_PROJECT",
    "SCOPE_HEADER",
    "SCOPE_QUESTION",
    "SEARCH_SCHEMA",
    "TOOLS",
    "WRITE_SCHEMA",
    "format_entry",
    "memory_search",
    "memory_write",
    "register_memory_tools",
    "resolve_namespace",
    "scope_question",
]
