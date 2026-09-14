"""``/memory list|search|forget`` — see and prune long-term memory (M5 §1b).

The agent writes memories; this is how a person checks what it wrote.  Three
actions, one scope flag:

    /memory                       list everything this session can recall
    /memory list --project        only this project's memories
    /memory search duho --global  search the global scope
    /memory forget m-abc123       delete one memory by id

Scope defaults to ``--all`` (project + global + this agent's own), which is the
same set recall itself draws on, so what the list shows is what the model sees.
Output is plain text: a command answers with a message, not a payload.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.memory.retrieval import (
    agent_namespace_of,
    namespaces_for,
    project_namespace_of,
)
from snowpea_core.memory.scopes import GLOBAL_NAMESPACE, label

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.memory.store import MemoryEntry

log = logging.getLogger("snowpea.commands.memory")

USAGE = (
    "Usage: /memory [list | search <query> | forget <id>] [--project | --global | --all]"
)

#: Most rows one ``/memory list`` prints before it starts truncating.
LIST_LIMIT = 50

ACTIONS = ("list", "search", "forget")
SCOPE_FLAGS = {"--project": "project", "--global": "global", "--all": "all"}


def parse(args: str) -> tuple[str, str, str]:
    """``(action, query, scope)`` from the raw argument string.

    ``action`` defaults to ``"list"``, ``scope`` to ``"all"``.  A bare
    ``/memory duho`` is a search, because that is the only thing a lone word
    after ``/memory`` can sensibly mean.
    """
    scope = "all"
    words: list[str] = []
    for word in args.split():
        if word in SCOPE_FLAGS:
            scope = SCOPE_FLAGS[word]
            continue
        words.append(word)
    if not words:
        return "list", "", scope
    head = words[0].lower()
    if head in ACTIONS:
        return head, " ".join(words[1:]).strip(), scope
    return "search", " ".join(words).strip(), scope


def namespaces(session: object, scope: str) -> list[str]:
    """The namespaces ``scope`` names for this session."""
    if scope == "global":
        return [GLOBAL_NAMESPACE]
    if scope == "project":
        project = project_namespace_of(session)
        return [project] if project else []
    found = namespaces_for(session)
    agent = agent_namespace_of(session)
    if agent and agent not in found:
        found.append(agent)
    return found


def render(entry: MemoryEntry) -> str:
    """One line per memory: id, scope, date, text, tags."""
    date = (entry.created_at or "")[:10]
    stamp = f" {date}" if date else ""
    tags = f"  #{' #'.join(entry.tags)}" if entry.tags else ""
    text = " ".join(entry.text.split())
    return f"{entry.id}  {label(entry.namespace)}{stamp}  {text}{tags}"


def _services(ctx: CommandContext) -> object | None:
    from snowpea_core.memory import services

    try:
        return services(ctx.core)
    except RuntimeError:
        return None


async def cmd_memory(ctx: CommandContext, args: str) -> None:
    """``/memory [list|search <q>|forget <id>] [--project|--global|--all]``."""
    action, query, scope = parse(args)
    memory = _services(ctx)
    if memory is None:
        await ctx.say("Long-term memory is not wired in this daemon.")
        return
    store = memory.store  # type: ignore[attr-defined]

    if action == "forget":
        if not query:
            await ctx.say(USAGE)
            return
        removed = await store.delete(query.split()[0])
        await ctx.say(f"forgot {query}" if removed else f"no memory with id {query}")
        return

    wanted = namespaces(ctx.session, scope)
    if not wanted:
        await ctx.say(
            "This session is not inside a project, so it has no project memories. "
            "Try /memory list --global."
        )
        return

    if action == "search":
        if not query:
            await ctx.say(USAGE)
            return
        entries = await store.search_many(query, namespaces=wanted, limit=LIST_LIMIT)
        if not entries:
            await ctx.say(f"no memories matched {query!r}")
            return
        header = f"{len(entries)} memories matching {query!r}:"
    else:
        entries = await store.list_many(namespaces=wanted, limit=LIST_LIMIT)
        if not entries:
            await ctx.say("nothing is remembered in this scope yet.")
            return
        header = f"{len(entries)} memories ({scope}):"

    lines = [header, *(render(entry) for entry in entries)]
    lines.append("Forget one with /memory forget <id>.")
    await ctx.say("\n".join(lines))


MEMORY_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "search", "forget"],
            "description": (
                "'list' shows what is remembered, 'search <query>' filters it, "
                "'forget <id>' deletes one."
            ),
        },
        "scope": {
            "type": "string",
            "enum": ["project", "global", "all"],
            "description": "--project, --global or --all (default).",
        },
    },
}


COMMANDS: tuple[Command, ...] = (
    Command(
        name="memory",
        summary="Show, search or forget long-term memories: /memory [list|search <q>|forget <id>].",
        run=cmd_memory,
        args_schema=MEMORY_ARGS_SCHEMA,
    ),
)


__all__ = [
    "ACTIONS",
    "COMMANDS",
    "LIST_LIMIT",
    "MEMORY_ARGS_SCHEMA",
    "SCOPE_FLAGS",
    "USAGE",
    "cmd_memory",
    "namespaces",
    "parse",
    "render",
]
