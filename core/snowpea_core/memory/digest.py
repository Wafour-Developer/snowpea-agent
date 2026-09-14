"""The memory digest: what a session already knows before anyone speaks.

Query-based recall (``Retrieval.recall_many``) answers "what is relevant to
*this* message".  That is the wrong shape for the first turn of a session,
where there is no message yet worth matching against and the user reasonably
expects the agent to already know the project it just opened.

So every turn's memory block starts with a digest — the standing facts, listed
rather than searched:

* ``## Project memory (<name>)`` — the newest ``memory.digestEntries``
  project-scope memories, newest first, trimmed to ``memory.digestChars``.
* ``## About the user`` — every ``profile:<key>`` fact, as ``key: value``.
* ``## Global memory`` — the newest :data:`GLOBAL_ENTRIES` global memories that
  are not already profile facts.

Empty sections are dropped, and a digest with no sections at all is ``""``.

Each line carries its ``[mem:<id>]`` so the model can cite a digest fact the
same way it cites a recall hit — the block's instruction to cite applies to
everything in it, and a line nobody can name is a line nobody can attribute.

Building it is two queries, so it is cached per namespace against
:attr:`~snowpea_core.memory.store.MemoryStore.revision`: a write or a delete
anywhere in the store bumps the revision and the next turn rebuilds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from snowpea_core.memory.profile import PROFILE_TAG, key_of
from snowpea_core.memory.scopes import GLOBAL_NAMESPACE, project_name

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.memory.store import MemoryEntry, MemoryStore

#: Global memories listed in the digest.  Small on purpose: the global scope is
#: standing preferences, and a long list there crowds out the project.
GLOBAL_ENTRIES = 10

PREAMBLE = (
    "What you already know about this user and this project, from earlier "
    "sessions. Treat it as background you start the conversation with, not "
    "something to recite back. memory_search finds anything not listed here."
)
PROJECT_HEADING = "## Project memory ({name})"
PROFILE_HEADING = "## About the user"
GLOBAL_HEADING = "## Global memory"
MORE = "… and {count} more — memory_search finds the rest"
#: Appended to the preamble so the model can see how full the project scope is
#: and consolidate before the budget starts dropping entries (M15 §D3).  The
#: two ceilings are ``memory.digestEntries`` and ``memory.digestChars``, which
#: govern the project section only — so that is what the numbers count.
USAGE = "Project memory in use: {entries}/{max_entries} entries · {chars}/{max_chars} chars."


@dataclass
class Digest:
    """The rendered digest and the ids it already accounts for."""

    text: str = ""
    #: Ids rendered in :attr:`text`, so recall can drop a hit the reader can
    #: already see and spend the slot on something new.
    ids: set[str] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.text)


def render_line(entry: MemoryEntry) -> str:
    """One digest line: ``- [date] text #tags [mem:<id>]``."""
    date = (entry.created_at or "")[:10]
    stamp = f"[{date}] " if date else ""
    tags = "".join(f" #{tag}" for tag in entry.tags)
    text = " ".join(entry.text.split())
    return f"- {stamp}{text}{tags} [mem:{entry.id}]"


def _is_profile(entry: MemoryEntry) -> bool:
    return any(tag.startswith(f"{PROFILE_TAG}:") for tag in entry.tags)


def _trim(entries: list[MemoryEntry], budget: int, extra: int) -> tuple[list[str], int]:
    """Render as many entries as ``budget`` characters allow.

    Returns the lines and how many memories are *not* shown — those trimmed
    here plus ``extra``, the ones the entry limit already left out — so the
    "… and K more" line is an honest count of the whole namespace.
    """
    lines: list[str] = []
    used = 0
    for index, entry in enumerate(entries):
        line = render_line(entry)
        if lines and used + len(line) + 1 > budget:
            return lines, len(entries) - index + extra
        lines.append(line)
        used += len(line) + 1
    return lines, extra


async def build(
    store: MemoryStore,
    *,
    project_namespace: str = "",
    entries: int = 30,
    chars: int = 6000,
) -> Digest:
    """The digest for a session rooted in ``project_namespace``."""
    sections: list[str] = []
    seen: set[str] = set()

    used_entries = 0
    used_chars = 0
    if project_namespace:
        limit = max(1, entries)
        newest = await store.list_many(namespaces=[project_namespace], limit=limit)
        if newest:
            total = await store.count(namespaces=[project_namespace])
            lines, missing = _trim(newest, max(1, chars), max(0, total - len(newest)))
            seen.update(entry.id for entry in newest[: len(lines)])
            used_entries = len(lines)
            used_chars = sum(len(line) + 1 for line in lines)
            body = [PROJECT_HEADING.format(name=project_name(project_namespace)), *lines]
            if missing > 0:
                body.append(MORE.format(count=missing))
            sections.append("\n".join(body))

    global_entries = await store.list_many(
        namespaces=[GLOBAL_NAMESPACE], limit=max(GLOBAL_ENTRIES, 200)
    )
    facts: dict[str, MemoryEntry] = {}
    for entry in reversed(global_entries):
        key = key_of(entry)
        if key:
            facts[key] = entry
    if facts:
        lines = [
            f"- {key}: {' '.join(item.text.split())} [mem:{item.id}]"
            for key, item in facts.items()
        ]
        seen.update(item.id for item in facts.values())
        sections.append("\n".join([PROFILE_HEADING, *lines]))

    plain = [entry for entry in global_entries if not _is_profile(entry)][:GLOBAL_ENTRIES]
    if plain:
        seen.update(entry.id for entry in plain)
        sections.append("\n".join([GLOBAL_HEADING, *(render_line(entry) for entry in plain)]))

    if not sections:
        return Digest()
    header = "\n".join(
        [
            PREAMBLE,
            USAGE.format(
                entries=used_entries,
                max_entries=max(1, entries),
                chars=used_chars,
                max_chars=max(1, chars),
            ),
        ]
    )
    return Digest(text="\n\n".join([header, *sections]), ids=seen)


__all__ = [
    "GLOBAL_ENTRIES",
    "GLOBAL_HEADING",
    "MORE",
    "PREAMBLE",
    "PROFILE_HEADING",
    "PROJECT_HEADING",
    "USAGE",
    "Digest",
    "build",
    "render_line",
]
