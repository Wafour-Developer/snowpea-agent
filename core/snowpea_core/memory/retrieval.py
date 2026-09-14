"""Recall: what goes into the system prompt, and what comes back out of a turn.

Two halves of the same loop (M5 contract §1):

* :class:`Retrieval` renders the top-k memories for the user's message as
  ``<memory>`` elements, with the instruction to cite ``[mem:<id>]``.
* :func:`remember_candidate` is the "memory nudge": after a turn completes, an
  explicit "기억해" / "remember that …" in the user's message is stored on its
  own, so the user never has to name a tool.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from snowpea_core.config.settings import DEFAULT_REMEMBER_PATTERNS, MemorySettings
from snowpea_core.memory.digest import Digest
from snowpea_core.memory.digest import build as build_digest
from snowpea_core.memory.scopes import (
    AGENT_PREFIX,
    GLOBAL_NAMESPACE,
    label,
    project_name,
)
from snowpea_core.memory.store import MemoryEntry, MemoryStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.session import Session

#: Leading "please remember:" style prefixes stripped before storing.
_PREFIXES = re.compile(
    r"^\s*(기억해\s*줘|기억해라|기억해|remember\s+that|remember|note\s+that|please\s+remember)"
    r"\s*[:：,،.\-]?\s*",
    re.IGNORECASE,
)

HEADER = (
    "Things you remember about this user from earlier sessions. Treat them as "
    "facts the user told you, and when one of them shapes your answer cite it "
    "inline as [mem:<id>] using the id below."
)

#: Appended to :data:`HEADER` once scopes are in play, so the model knows the
#: leading ``[project]`` / ``[global]`` / ``[agent]`` tag is a label and not
#: part of the remembered sentence (M5 §1b).
SCOPE_NOTE = (
    "Each memory is labelled with where it is kept: [project] only applies to "
    "{project}, [global] applies everywhere, [agent] belongs to this agent. "
    "The label is not part of the fact; never quote it back."
)
SCOPE_NOTE_NO_PROJECT = (
    "Each memory is labelled with where it is kept: [global] applies "
    "everywhere, [agent] belongs to this agent. The label is not part of the "
    "fact; never quote it back."
)


#: Heading over the query-based hits, so they read as an answer to *this*
#: message rather than as more of the standing digest above them.
RELEVANT_HEADING = "## Relevant to this message"


def block_header(entries: list[MemoryEntry], project: str = "") -> str:
    """The instruction line above the whole memory block."""
    if any(entry.scope != "global" for entry in entries) or project:
        note = SCOPE_NOTE.format(project=project) if project else SCOPE_NOTE_NO_PROJECT
        return f"{HEADER} {note}"
    return HEADER


def render_entries(entries: list[MemoryEntry]) -> str:
    """The ``<memory …>`` elements alone, one per line."""
    lines = []
    for entry in entries:
        tags = " ".join(entry.tags)
        mark = label(entry.namespace)
        lines.append(f'<memory id="{entry.id}" tags="{tags}">{mark} {entry.text}</memory>')
    return "\n".join(lines)


def render_block(entries: list[MemoryEntry], *, project: str = "") -> str:
    """The ``<memory …>`` block for a system prompt; ``""`` when there is none.

    ``project`` is the project root the session is in, named in the header so
    the model can tell *which* project a ``[project]`` memory belongs to.
    """
    if not entries:
        return ""
    return f"{block_header(entries, project)}\n{render_entries(entries)}"


def compose_block(
    digest: Digest, entries: list[MemoryEntry], *, project: str = ""
) -> str:
    """The whole memory block: the standing digest, then this message's hits.

    One header covers both halves, because the instruction to cite
    ``[mem:<id>]`` applies to everything in the block and repeating it once per
    section only makes the prompt longer.
    """
    if not digest and not entries:
        return ""
    parts = [block_header(entries, project)]
    if digest:
        parts.append(digest.text)
    if entries:
        parts.append(f"{RELEVANT_HEADING}\n{render_entries(entries)}")
    return "\n\n".join(parts)


class Retrieval:
    """Turns the user's message into the memory section of the system prompt."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        top_k: int = 8,
        settings: MemorySettings | None = None,
    ) -> None:
        self.store = store
        self.top_k = top_k
        self.settings = settings or MemorySettings()
        #: ``project namespace -> (store revision, digest)``.  The store bumps
        #: its revision on every write and delete, so a memory written this
        #: turn is in the next turn's digest and nothing else rebuilds.
        self._digests: dict[str, tuple[int, Digest]] = {}

    async def digest(self, session: Any) -> Digest:
        """The standing digest for ``session``, cached until the store changes."""
        namespace = project_namespace_of(session)
        cached = self._digests.get(namespace)
        if cached is not None and cached[0] == self.store.revision:
            return cached[1]
        built = await build_digest(
            self.store,
            project_namespace=namespace,
            entries=self.settings.digestEntries,
            chars=self.settings.digestChars,
        )
        self._digests[namespace] = (self.store.revision, built)
        return built

    async def recall(
        self, query: str, *, namespace: str = "default", limit: int | None = None
    ) -> list[MemoryEntry]:
        return await self.store.search(query, namespace=namespace, limit=limit or self.top_k)

    async def recall_many(
        self,
        query: str,
        *,
        namespaces: list[str],
        limit: int | None = None,
    ) -> list[MemoryEntry]:
        """Top-k memories across every namespace the session can see."""
        return await self.store.search_many(
            query, namespaces=namespaces, limit=limit or self.top_k
        )

    async def context_block(self, session: Any, query: str) -> str:
        """The session's whole memory block: the digest, then this turn's hits.

        The digest is there from the first turn, before the user has said
        anything worth matching against.  The query-based half searches
        project, global and (for a named agent) the agent namespace together
        so they are ranked against each other, and drops any hit the digest
        already shows (M5 §1b).
        """
        digest = await self.digest(session)
        entries = []
        if query.strip():
            found = await self.recall_many(query, namespaces=namespaces_for(session))
            entries = [entry for entry in found if entry.id not in digest.ids]
        return compose_block(digest, entries, project=project_root_of(session))


def namespace_of(session: Session | Any) -> str:
    """``session.memory_namespace`` when it has one, else ``"default"``."""
    value = getattr(session, "memory_namespace", None)
    return str(value) if value else GLOBAL_NAMESPACE


def project_namespace_of(session: Session | Any) -> str:
    """``session.project_namespace``; ``""`` when the session is not in a project."""
    value = getattr(session, "project_namespace", None)
    return str(value) if value else ""


def project_root_of(session: Session | Any) -> str:
    """The session's project root as a path string, or ``""``."""
    from snowpea_core.memory.scopes import project_of

    return project_of(project_namespace_of(session))


def agent_namespace_of(session: Session | Any) -> str:
    """The ``agent:<name>`` namespace of a named agent's session, or ``""``."""
    namespace = namespace_of(session)
    return namespace if namespace.startswith(AGENT_PREFIX) else ""


def namespaces_for(session: Session | Any) -> list[str]:
    """Every namespace a session recalls from: project, global, then agent.

    Order is only a tiebreak for equal relevance; the actual ranking is bm25
    over all of them at once.
    """
    found = [project_namespace_of(session), GLOBAL_NAMESPACE, agent_namespace_of(session)]
    return [namespace for namespace in found if namespace]


def display_name(session: Session | Any) -> str:
    """Short name of the session's project, for a question or a header."""
    return project_name(project_namespace_of(session))


def compile_patterns(patterns: list[str] | tuple[str, ...]) -> list[re.Pattern[str]]:
    """Compile the configured patterns, skipping any that do not parse."""
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            continue
    return compiled


def remember_candidate(
    text: str, patterns: list[str] | tuple[str, ...] = DEFAULT_REMEMBER_PATTERNS
) -> str | None:
    """The fact to store from ``text``, or ``None`` when it asks for nothing.

    ``"기억해: 내 배포 대상은 duho 서버다"`` yields ``"내 배포 대상은 duho 서버다"``.
    """
    stripped = " ".join(text.split())
    if not stripped:
        return None
    if not any(pattern.search(stripped) for pattern in compile_patterns(patterns)):
        return None
    fact = _PREFIXES.sub("", stripped).strip()
    # A bare "기억해" carries no fact; anything shorter than a few characters
    # is noise rather than something worth recalling later.
    return fact if len(fact) >= 4 else None


__all__ = [
    "DEFAULT_REMEMBER_PATTERNS",
    "HEADER",
    "RELEVANT_HEADING",
    "SCOPE_NOTE",
    "SCOPE_NOTE_NO_PROJECT",
    "Retrieval",
    "agent_namespace_of",
    "block_header",
    "compile_patterns",
    "compose_block",
    "display_name",
    "namespace_of",
    "namespaces_for",
    "project_namespace_of",
    "project_root_of",
    "remember_candidate",
    "render_block",
    "render_entries",
]
