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

from snowpea_core.config.settings import DEFAULT_REMEMBER_PATTERNS
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


def render_block(entries: list[MemoryEntry]) -> str:
    """The ``<memory …>`` block for a system prompt; ``""`` when there is none."""
    if not entries:
        return ""
    lines = [HEADER]
    for entry in entries:
        tags = " ".join(entry.tags)
        lines.append(f'<memory id="{entry.id}" tags="{tags}">{entry.text}</memory>')
    return "\n".join(lines)


class Retrieval:
    """Turns the user's message into the memory section of the system prompt."""

    def __init__(self, store: MemoryStore, *, top_k: int = 8) -> None:
        self.store = store
        self.top_k = top_k

    async def recall(
        self, query: str, *, namespace: str = "default", limit: int | None = None
    ) -> list[MemoryEntry]:
        return await self.store.search(query, namespace=namespace, limit=limit or self.top_k)

    async def context_block(self, session: Any, query: str) -> str:
        """Top-k memories for ``query`` in ``session``'s namespace, rendered."""
        namespace = namespace_of(session)
        return render_block(await self.recall(query, namespace=namespace))


def namespace_of(session: Session | Any) -> str:
    """``session.memory_namespace`` when it has one, else ``"default"``."""
    value = getattr(session, "memory_namespace", None)
    return str(value) if value else "default"


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
    "Retrieval",
    "compile_patterns",
    "namespace_of",
    "remember_candidate",
    "render_block",
]
