"""Runtime context: facts that change, sent only when they changed (M16 P1.2).

The system prompt holds what is stable; facts that move during a session — the
current plan, the plan-then-auto note, context pressure — are *contexts*.  Each
provider renders its own text; together they form one ordered ``(name, text)``
list.  That list goes to the model as a single ``<system-reminder>`` snapshot,
and only when it differs from the last one sent, or after a compaction summarised
the last one away.  The snapshot says it replaces earlier versions, so the model
never weighs two plan lines against each other (dsh
``packages/core/agent-loop/src/runtime-context.ts``).

What the user's turn brings each time — memory recall, the browser host's note —
is not a snapshot: it rides the turn's own message as before.

Where it goes: at the start of a turn the snapshot joins that turn's message
(``agent.turn_context``); in the middle of a turn it is appended as its own user
message before the next model call, the same channel session notices use.  Both
are stored, so every later request replays them byte for byte and the cached
prefix stays warm.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

#: ``ChatMessage.name`` of a mid-turn runtime-context message.
MESSAGE_NAME = "runtime_context"

#: Where each built-in provider sits in the snapshot.
CONTEXT_ORDERS: dict[str, int] = {
    "plan": 100,
    "plan-auto": 110,
    "context-pressure": 900,
}

OPEN_TAG = "<system-reminder>"
CLOSE_TAG = "</system-reminder>"
HEADING = (
    "Current runtime context. It replaces any earlier version of this context, "
    "including one kept in a conversation summary."
)
CLEARED = "Current runtime context: none. Earlier runtime context no longer applies."

Render = Callable[[Any, Any], str]


@dataclass(frozen=True)
class ContextProvider:
    """One named runtime fact."""

    name: str
    order: int
    #: ``render(core, session)`` -> text, or ``""`` when the fact does not apply.
    render: Render
    #: Kept when a caller suppresses optional context (a lean child).
    required: bool = False
    source: str = "builtin"


class ContextRegistry:
    """Named providers, sorted by order then name; registration returns a disposer."""

    def __init__(self) -> None:
        self._providers: dict[str, ContextProvider] = {}
        self._lock = threading.Lock()

    def register(self, provider: ContextProvider) -> Callable[[], None]:
        with self._lock:
            previous = self._providers.get(provider.name)
            self._providers[provider.name] = provider

        def dispose() -> None:
            with self._lock:
                if self._providers.get(provider.name) is provider:
                    if previous is not None:
                        self._providers[provider.name] = previous
                    else:
                        del self._providers[provider.name]

        return dispose

    def providers(self) -> list[ContextProvider]:
        with self._lock:
            items = list(self._providers.values())
        return sorted(items, key=lambda item: (item.order, item.name))

    def collect(
        self, core: Core | None, session: Session, *, required_only: bool = False
    ) -> tuple[tuple[str, str], ...]:
        """The ordered ``(name, text)`` list; a provider that raises is skipped."""
        out: list[tuple[str, str]] = []
        for provider in self.providers():
            if required_only and not provider.required:
                continue
            try:
                text = (provider.render(core, session) or "").strip()
            except Exception:  # noqa: BLE001 - one broken provider must not cost the turn
                import logging

                logging.getLogger("snowpea.context").debug(
                    "runtime context provider %s failed", provider.name, exc_info=True
                )
                continue
            if text:
                out.append((provider.name, text))
        return tuple(out)


# ---------------------------------------------------------------------------
# built-in providers (what ``agent.turn_context`` used to repeat every turn)
# ---------------------------------------------------------------------------


def _plan(core: Any, session: Any) -> str:
    # Only the main agent works a plan (CORE-plan-continuity).
    if getattr(session, "is_subagent", False):
        return ""
    from snowpea_core.agent.agent import plan_prompt_line

    return plan_prompt_line(session)


def _plan_auto(core: Any, session: Any) -> str:
    if getattr(session, "is_subagent", False):
        return ""
    if session.mode != "plan" or not getattr(session, "auto_after_plan", False):
        return ""
    from snowpea_core.agent.agent import PLAN_AUTO_NOTE

    return PLAN_AUTO_NOTE


def _context_pressure(core: Any, session: Any) -> str:
    from snowpea_core.agent.agent import context_fill
    from snowpea_core.prompts import compose

    fill = context_fill(session)
    if fill is None or fill < compose.CONTEXT_PRESSURE_THRESHOLD:
        return ""
    return compose.load("fragments/context-pressure", session.workdir)


BUILTIN_PROVIDERS: tuple[ContextProvider, ...] = (
    ContextProvider("plan", CONTEXT_ORDERS["plan"], _plan),
    ContextProvider("plan-auto", CONTEXT_ORDERS["plan-auto"], _plan_auto),
    ContextProvider("context-pressure", CONTEXT_ORDERS["context-pressure"], _context_pressure),
)


def default_registry() -> ContextRegistry:
    registry = ContextRegistry()
    for provider in BUILTIN_PROVIDERS:
        registry.register(provider)
    return registry


#: The daemon's registry: built-ins plus whatever plugins add.
REGISTRY = default_registry()


# ---------------------------------------------------------------------------
# the snapshot
# ---------------------------------------------------------------------------


def render(items: tuple[tuple[str, str], ...]) -> str:
    """The ``<system-reminder>`` text for ``items``; the cleared form when empty."""
    if not items:
        return f"{OPEN_TAG}\n{CLEARED}\n{CLOSE_TAG}"
    body = "\n\n".join(text for _name, text in items)
    return f"{OPEN_TAG}\n{HEADING}\n\n{body}\n{CLOSE_TAG}"


def _epoch(session: Any) -> int:
    return int(getattr(getattr(session, "history", None), "compactions", 0) or 0)


def take_due(
    core: Core | None, session: Session, *, registry: ContextRegistry | None = None
) -> str:
    """The snapshot to send now, or ``""`` when the model already has it.

    Due when the ordered ``(name, text)`` list differs from the last one sent,
    or a compaction happened since (the last one is inside the summary now).
    Nothing is sent for an empty list the model never saw; an empty list after a
    non-empty one is sent as the cleared form, so a finished plan stops counting.
    Marks what it returns as sent.
    """
    from snowpea_core.agent.agent import is_lean_child

    lean = is_lean_child(session, core)
    items = (registry or REGISTRY).collect(core, session, required_only=lean)
    epoch = _epoch(session)
    last = getattr(session, "context_snapshot", None)
    if getattr(session, "context_snapshot_epoch", -1) != epoch:
        last = None
    if last is None and not items:
        session.context_snapshot = None
        session.context_snapshot_epoch = epoch
        return ""
    if last is not None and tuple(last) == items:
        return ""
    session.context_snapshot = items
    session.context_snapshot_epoch = epoch
    return render(items)


def forget(session: Any) -> None:
    """Make the next :func:`take_due` send the snapshot again (a resumed session)."""
    session.context_snapshot = None
    session.context_snapshot_epoch = -1


__all__ = [
    "BUILTIN_PROVIDERS",
    "CLEARED",
    "CONTEXT_ORDERS",
    "HEADING",
    "MESSAGE_NAME",
    "OPEN_TAG",
    "REGISTRY",
    "ContextProvider",
    "ContextRegistry",
    "default_registry",
    "forget",
    "render",
    "take_due",
]
