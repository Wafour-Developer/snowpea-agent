"""The seam between the Jarvis service and the delegation engine (M19 WP2/WP3).

The engine is the security boundary: it decides, without any model, where an
approval or question goes (``auto`` — the engine allows it alone, ``jarvis`` —
Jarvis decides within what the engine permits, ``person``), which delegation
level holds for a session, and whether the person has taken a session back.
The service and the intake only ask; they never decide on their own.

:func:`build_engine` is the one place the daemon gets its engine from.  This
work package ships :class:`ConservativeDelegation`, which routes everything to
the person and allows nothing by itself, so Jarvis mode is safe before the
real engine (``jarvis/delegation.py``, WP2) is connected there.
"""

from __future__ import annotations

import itertools
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from snowpea_core.jarvis.clock import iso
from snowpea_core.jarvis.pause import PauseBook, PauseState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

#: Where the engine sends a request (``ApprovalRequest.route``).
Route = Literal["auto", "jarvis", "person"]
#: ``SessionJarvisState.level``.
Level = Literal["staged", "full"]


@dataclass(frozen=True)
class RouteRequest:
    """One approval or question raised in a supervised session."""

    kind: Literal["approval", "question"]
    request_id: str
    session_id: str
    #: Project root of the session (canonical workdir), when known.
    project: str | None
    #: The ``ApprovalRequest`` / ``QuestionRequest`` as it went on the wire.
    payload: dict[str, Any]
    #: True when an interactive surface was asked directly (``approval.request``).
    interactive: bool = False


@dataclass(frozen=True)
class RouteDecision:
    """The engine's verdict for one :class:`RouteRequest`."""

    route: Route
    #: ``JarvisClassification``: safe | risky | guarded | never.
    classification: str | None = None
    #: ``GuardedClass`` with classification ``guarded``.
    guarded_class: str | None = None
    #: The rule that matched, e.g. ``exec:git-push`` (ledger, escalation).
    rule: str | None = None
    grant_id: str | None = None
    reason: str = ""


@runtime_checkable
class DelegationPort(Protocol):
    """What the Jarvis service needs from the delegation engine.

    Every method is synchronous and cheap: the intake calls them from inside
    the event hub's fan-out and must never wait on a model or a network.
    """

    def route(self, request: RouteRequest) -> RouteDecision:
        """Where ``request`` goes.  Must answer ``person`` when in doubt."""
        ...

    def level(self, session_id: str, project: str | None) -> Level:
        """The delegation level in force for a session (most specific grant wins)."""
        ...

    def note_person_message(self, session_id: str, project: str | None) -> bool:
        """The person typed into ``session_id``; True when that started a pause."""
        ...

    def is_paused(self, session_id: str) -> bool:
        """True while the person holds the session (typing pause or takeover)."""
        ...

    def pause_state(self, session_id: str) -> PauseState | None:
        """The pause on ``session_id``, or ``None`` when Jarvis may decide."""
        ...

    def paused(self) -> list[PauseState]:
        """Every live pause (``JarvisStatusResult.paused``)."""
        ...

    def takeover(self, session_id: str, minutes: int | None, project: str | None) -> PauseState:
        """``jarvis.takeover``: the person takes the session back."""
        ...

    def handback(self, session_id: str) -> bool:
        """``jarvis.handback``: authority returns now; False when nothing was paused."""
        ...

    def ledger_append(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Record one decision (``JarvisDecision`` fields); returns it with ``id`` and ``ts``."""
        ...


class ConservativeDelegation:
    """The engine this package ships: everything to the person, nothing automatic.

    Pauses are real (the person's typing and ``jarvis.takeover`` must work in
    either case), and the ledger is a bounded in-memory list so decisions made
    through the seam stay visible to the process that made them.
    """

    LEDGER_LIMIT = 500

    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        quiet_minutes: Callable[[], int],
        default_level: Callable[[], str],
    ) -> None:
        self._now = now
        self._default_level = default_level
        self.pauses = PauseBook(now, quiet_minutes)
        self.entries: deque[dict[str, Any]] = deque(maxlen=self.LEDGER_LIMIT)
        self._ids = itertools.count(1)

    def route(self, request: RouteRequest) -> RouteDecision:
        return RouteDecision(
            route="person",
            classification="risky",
            rule="default:person",
            reason="no delegation engine: every request waits for the person",
        )

    def level(self, session_id: str, project: str | None) -> Level:
        return "full" if self._default_level() == "full" else "staged"

    def note_person_message(self, session_id: str, project: str | None) -> bool:
        return self.pauses.typed(session_id, project)

    def is_paused(self, session_id: str) -> bool:
        return self.pauses.is_paused(session_id)

    def pause_state(self, session_id: str) -> PauseState | None:
        return self.pauses.get(session_id)

    def paused(self) -> list[PauseState]:
        return self.pauses.all()

    def takeover(self, session_id: str, minutes: int | None, project: str | None) -> PauseState:
        return self.pauses.takeover(session_id, minutes, project)

    def handback(self, session_id: str) -> bool:
        return self.pauses.handback(session_id)

    def ledger_append(self, entry: dict[str, Any]) -> dict[str, Any]:
        stored = dict(entry)
        stored.setdefault("id", f"jd-{next(self._ids):06d}")
        stored.setdefault("ts", iso(self._now()))
        self.entries.append(stored)
        return stored


def build_engine(core: Core, *, now: Callable[[], datetime]) -> DelegationPort:
    """The daemon's delegation engine — the single place WP2's engine is connected."""

    def settings() -> Any:
        return core.settings.jarvis

    return ConservativeDelegation(
        now=now,
        quiet_minutes=lambda: int(settings().quietMinutes),
        default_level=lambda: str(settings().delegation.default),
    )


#: A delivery hook for escalations and decisions beyond the wire event
#: (``jarvis.notify.messenger``, WP7): ``hook(kind, payload)`` with kind
#: ``"escalation"`` or ``"decision"`` and the notification's params.
NotifyHook = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class Hooks:
    """Extension points later work packages attach to (see the service)."""

    #: Messenger delivery (WP7); called for ``high``/``urgent`` notices only,
    #: and only while ``jarvis.notify.messenger`` is on.
    notify: list[NotifyHook] = field(default_factory=list)
    #: Extra overview rows, e.g. external CLI sessions (WP5/6):
    #: ``source() -> list[JarvisOverviewRow]``; exposure is filtered after.
    overview_sources: list[Callable[[], list[Any]]] = field(default_factory=list)


__all__ = [
    "ConservativeDelegation",
    "DelegationPort",
    "Hooks",
    "Level",
    "NotifyHook",
    "Route",
    "RouteDecision",
    "RouteRequest",
    "build_engine",
]
