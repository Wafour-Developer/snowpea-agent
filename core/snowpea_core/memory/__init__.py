"""Long-term memory (M5 contract §1).

:class:`MemoryServices` is what the daemon holds: one store, the profile view
over it, and the retrieval that feeds the system prompt.  ``wire_core`` builds
it; the agent loop reaches it through :func:`context_for_turn` and
:func:`nudge_after_turn`, which are the only two entry points the loop needs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import MemorySettings, Settings
from snowpea_core.memory.extract import Candidate, Candidates, extract_preference
from snowpea_core.memory.profile import UserProfile, profile_tag
from snowpea_core.memory.retrieval import (
    DEFAULT_REMEMBER_PATTERNS,
    Retrieval,
    namespace_of,
    namespaces_for,
    project_namespace_of,
    remember_candidate,
    render_block,
)
from snowpea_core.memory.safety import sensitive_kind
from snowpea_core.memory.scopes import (
    GLOBAL_NAMESPACE,
    project_namespace,
    project_root,
    scope_of,
)
from snowpea_core.memory.store import MemoryClosed, MemoryEntry, MemoryStore
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
    #: In-flight background work spawned by :func:`context_for_turn` and
    #: :func:`nudge_after_turn` (recall / the auto-remember nudge), tracked so
    #: :meth:`close` can cancel and await it before the store underneath it
    #: closes (CORE-memory-race).
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False, compare=False)
    #: Preferences proposed with ``memory.autoExtract: "suggest"``, waiting
    #: for ``memory.confirm`` (addendum 27, C7).
    candidates: Candidates = field(default_factory=Candidates, repr=False, compare=False)
    preference_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )

    def _track(self, coro: Any) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

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
            retrieval=Retrieval(store, top_k=config.top_k, settings=config),
            settings=config,
        )

    async def close(self) -> None:
        """Cancel and await any in-flight recall/nudge work, then close the store.

        Order matters: a background task still running when the store closed
        underneath it used to surface as a raw ``sqlite3.ProgrammingError``
        (CORE-memory-race). Draining ``_tasks`` first means anything still
        running either finishes (against a live store) or is cancelled
        cleanly, so the ``store.close()`` below never races a writer.
        """
        tasks = [task for task in list(self._tasks) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()


def services(core: Core) -> MemoryServices:
    """The daemon's :class:`MemoryServices`, raising when it was never wired."""
    memory = getattr(core, "memory", None)
    if memory is None:
        raise RuntimeError("memory services are not wired; call wire_core first")
    return memory  # type: ignore[no-any-return]


def enabled(core: Core, session: Any = None) -> bool:
    """Whether memory runs, for ``session`` when one is given.

    A session's ``memory_override`` (``session.create {memory}``) wins over
    ``memory.enabled``; without the services nothing runs either way.
    """
    if not getattr(core, "memory", None):
        return False
    override = getattr(session, "memory_override", None)
    if override is not None:
        return bool(override)
    return bool(core.settings.memory.enabled)


async def context_for_turn(core: Core, session: Session, text: str) -> str:
    """The ``<memory>`` block for this turn's system prompt, or ``""``.

    Never raises: a memory that cannot be read must not cost the user a turn.
    The actual lookup runs as a task tracked on :class:`MemoryServices`, so a
    daemon shutdown that lands mid-lookup cancels it cleanly instead of racing
    :meth:`MemoryServices.close` (CORE-memory-race).
    """
    if not enabled(core, session):
        return ""
    # An empty prompt still gets the digest: what the session already knows
    # does not depend on what was just typed (M5 §1b).
    memory = services(core)
    task = memory._track(memory.retrieval.context_block(session, text))
    try:
        return await task
    except (MemoryClosed, asyncio.CancelledError):
        # The daemon is shutting down and closed out from under this lookup;
        # the turn already has nothing useful to add to the prompt.
        return ""
    except Exception:  # noqa: BLE001 - recall is best-effort
        log.exception("memory recall failed for session %s", session.id)
        return ""


async def nudge_after_turn(core: Core, session: Session, text: str) -> MemoryEntry | None:
    """Store ``text`` when the user explicitly asked to be remembered.

    Runs after ``turn.done{complete}``; returns the entry it wrote, if any.
    Declines to even start once :attr:`Core.stopping` is set, and otherwise
    runs the write as a task tracked on :class:`MemoryServices` so a shutdown
    that lands mid-write cancels or fails it cleanly instead of racing
    :meth:`MemoryServices.close` (CORE-memory-race).
    """
    if not enabled(core, session) or not text.strip():
        return None
    if getattr(core, "stopping", False):
        return None
    memory = services(core)
    fact = remember_candidate(text, memory.settings.auto_remember_patterns)
    if fact is None:
        return None
    kind = sensitive_kind(fact)
    if kind is not None:
        # The user asked, but a remembered value would be repeated in every
        # later prompt; the kind is logged, never the text (addendum 27, C6).
        log.debug("auto-remember skipped for session %s: %s", session.id, kind)
        return None
    namespace = namespace_of(session)
    task = memory._track(
        _write_nudge(memory, fact, namespace, session.id, namespaces_for(session))
    )
    try:
        return await task
    except (MemoryClosed, asyncio.CancelledError):
        # The daemon closed the store out from under this write (or cancelled
        # it on shutdown); the turn already succeeded, so this is a no-op.
        return None
    except Exception:  # noqa: BLE001 - the turn already succeeded
        log.exception("auto-remember failed for session %s", session.id)
        return None


async def _write_nudge(
    memory: MemoryServices,
    fact: str,
    namespace: str,
    session_id: str,
    seen_in: list[str] | None = None,
) -> MemoryEntry | None:
    """Write the nudged fact, unless some scope already holds it verbatim.

    The dedupe spans every scope the session recalls from, not just the one
    being written to: a fact the model already filed under the project through
    ``save_memory`` must not reappear as a global copy (M5 §1b).
    """
    if await memory.store.exists_any(fact, namespaces=seen_in or [namespace]):
        return None
    return await memory.store.write(
        fact, tags=["auto"], namespace=namespace, source_session=session_id, origin="nudge"
    )


def extract_after_turn(
    core: Core, session: Session, turn_id: str, text: str, *, nudged: bool = False
) -> None:
    """Start the preference extractor for a completed turn (addendum 27, C7).

    Only for ``memory.autoExtract`` ``"suggest"`` or ``"on"``, a turn the
    person typed (origin ``human``) in a top-level session, with memory on for
    it, and not when the auto-remember nudge already stored this message.  It
    runs as a tracked task: the turn is already done and must not wait for a
    second model call.
    """
    mode = getattr(core.settings.memory, "autoExtract", "off")
    if mode == "off" or nudged or not text.strip() or not enabled(core, session):
        return
    if getattr(core, "stopping", False) or getattr(session, "is_subagent", False):
        return
    if session.turn_origins.get(turn_id, "human") != "human":
        return
    memory = services(core)
    memory._track(_extract(core, session, text, mode))


async def _extract(core: Core, session: Session, text: str, mode: str) -> None:
    from snowpea_core.session import events

    memory = services(core)
    try:
        provider = core.providers.get(session.provider, session.model)
        found = await extract_preference(provider, text)
        if found is None:
            return
        preference, category = found
        if mode == "on":
            await write_preference(memory, preference, category, session.id, confirmed=False)
            return
        if await memory.store.exists_any(preference, namespaces=namespaces_for(session)):
            return
        candidate_id = memory.candidates.add(Candidate(preference, category, session.id))
        if candidate_id is None:
            return
        await core.hub.emit_event(
            session.id, events.memory_candidate(candidate_id, preference, category)
        )
    except (MemoryClosed, asyncio.CancelledError):
        return
    except Exception:  # noqa: BLE001 - the turn already succeeded
        log.warning("preference extraction failed for session %s", session.id, exc_info=True)


async def write_preference(
    memory: MemoryServices, text: str, category: str, session_id: str, *, confirmed: bool
) -> MemoryEntry | None:
    """Store an extracted preference globally, once: two turns (or a confirm and
    an ``on`` write) racing for the same text store it a single time."""
    async with memory.preference_lock:
        if await memory.store.exists_any(text, namespaces=[GLOBAL_NAMESPACE]):
            return None
        return await memory.store.write(
            text,
            tags=["auto-pref", f"category:{category}"],
            namespace=GLOBAL_NAMESPACE,
            source_session=session_id,
            origin="auto-pref",
            confirmed=confirmed,
        )


def wire_memory(core: Core) -> MemoryServices:
    """Build the services, hang them off ``Core`` and activate the tools."""
    memory = MemoryServices.open(core.paths, core.settings)
    core.memory = memory
    register_memory_tools(core.tools)
    return memory


__all__: list[str] = [
    "DEFAULT_REMEMBER_PATTERNS",
    "MemoryClosed",
    "MemoryEntry",
    "MemoryServices",
    "GLOBAL_NAMESPACE",
    "MemoryStore",
    "Retrieval",
    "UserProfile",
    "context_for_turn",
    "enabled",
    "extract_after_turn",
    "write_preference",
    "namespace_of",
    "namespaces_for",
    "nudge_after_turn",
    "project_namespace",
    "project_namespace_of",
    "project_root",
    "scope_of",
    "profile_tag",
    "register_memory_tools",
    "remember_candidate",
    "render_block",
    "services",
    "wire_memory",
]
