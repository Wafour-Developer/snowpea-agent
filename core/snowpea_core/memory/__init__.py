"""Long-term memory (M5 contract §1).

:class:`MemoryServices` is what the daemon holds: one store, the profile view
over it, and the retrieval that feeds the system prompt.  ``wire_core`` builds
it; the agent loop reaches it through :func:`context_for_turn` and
:func:`nudge_after_turn`, which are the only two entry points the loop needs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import MemorySettings, Settings
from snowpea_core.memory.profile import UserProfile, profile_tag
from snowpea_core.memory.retrieval import (
    DEFAULT_REMEMBER_PATTERNS,
    Retrieval,
    namespace_of,
    remember_candidate,
    render_block,
)
from snowpea_core.memory.store import MemoryEntry, MemoryStore
from snowpea_core.memory.tools import register_memory_tools

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.memory")


@dataclass
class MemoryServices:
    """The memory singletons a daemon owns."""

    store: MemoryStore
    profile: UserProfile
    retrieval: Retrieval
    settings: MemorySettings

    @classmethod
    def open(cls, paths: Paths, settings: Settings | None = None) -> MemoryServices:
        """Open the store under ``$SNOWPEA_HOME`` and build the views over it."""
        config = (settings or Settings()).memory
        store = MemoryStore.open(paths)
        if store.tokenizer != "trigram":
            log.warning(
                "fts5 trigram tokenizer unavailable; memory search fell back to %s,"
                " which only matches whole space-delimited words",
                store.tokenizer,
            )
        return cls(
            store=store,
            profile=UserProfile(store),
            retrieval=Retrieval(store, top_k=config.top_k),
            settings=config,
        )

    def close(self) -> None:
        self.store.close()


def services(core: Core) -> MemoryServices:
    """The daemon's :class:`MemoryServices`, raising when it was never wired."""
    memory = getattr(core, "memory", None)
    if memory is None:
        raise RuntimeError("memory services are not wired; call wire_core first")
    return memory  # type: ignore[no-any-return]


def enabled(core: Core) -> bool:
    return bool(getattr(core, "memory", None)) and bool(core.settings.memory.enabled)


async def context_for_turn(core: Core, session: Session, text: str) -> str:
    """The ``<memory>`` block for this turn's system prompt, or ``""``.

    Never raises: a memory that cannot be read must not cost the user a turn.
    """
    if not enabled(core) or not text.strip():
        return ""
    try:
        return await services(core).retrieval.context_block(session, text)
    except Exception:  # noqa: BLE001 - recall is best-effort
        log.exception("memory recall failed for session %s", session.id)
        return ""


async def nudge_after_turn(core: Core, session: Session, text: str) -> MemoryEntry | None:
    """Store ``text`` when the user explicitly asked to be remembered.

    Runs after ``turn.done{complete}``; returns the entry it wrote, if any.
    """
    if not enabled(core) or not text.strip():
        return None
    memory = services(core)
    fact = remember_candidate(text, memory.settings.auto_remember_patterns)
    if fact is None:
        return None
    namespace = namespace_of(session)
    try:
        if await memory.store.exists(fact, namespace=namespace):
            return None
        return await memory.store.write(
            fact, tags=["auto"], namespace=namespace, source_session=session.id
        )
    except Exception:  # noqa: BLE001 - the turn already succeeded
        log.exception("auto-remember failed for session %s", session.id)
        return None


def wire_memory(core: Core) -> MemoryServices:
    """Build the services, hang them off ``Core`` and activate the tools."""
    memory = MemoryServices.open(core.paths, core.settings)
    core.memory = memory
    register_memory_tools(core.tools)
    return memory


__all__: list[str] = [
    "DEFAULT_REMEMBER_PATTERNS",
    "MemoryEntry",
    "MemoryServices",
    "MemoryStore",
    "Retrieval",
    "UserProfile",
    "context_for_turn",
    "enabled",
    "namespace_of",
    "nudge_after_turn",
    "profile_tag",
    "register_memory_tools",
    "remember_candidate",
    "render_block",
    "services",
    "wire_memory",
]
