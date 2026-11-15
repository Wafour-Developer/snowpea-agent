"""The seam between the Jarvis service and the delegation engine (M19 WP2/WP3).

The engine (:class:`~snowpea_core.jarvis.delegation.DelegationEngine`) is the
security boundary: it decides, without any model, where an approval or a
question goes (``auto`` — the engine allows it alone, ``jarvis`` — Jarvis
decides within what the engine permits, ``person``), which delegation level
holds for a session, and whether the person has taken a session back.  Its
grants and pauses are persisted in ``$SNOWPEA_HOME/jarvis/delegation.db``, so a
daemon restart never silently hands a paused session back to Jarvis.  The
service, the intake and Jarvis's tools only ask it; they never decide alone.

:func:`build_engine` is the one place the service gets its engine from: the
daemon's single engine (:func:`~snowpea_core.jarvis.delegation.engine_for`),
the one the ``jarvis.delegation.*`` / ``jarvis.takeover`` handlers use too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from snowpea_core.jarvis.delegation import DelegationEngine, engine_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core


def build_engine(core: Core, *, now: Callable[[], datetime]) -> DelegationEngine:
    """The daemon's delegation engine, reading time from the service's clock."""
    engine = engine_for(core)
    engine._now = now
    return engine


#: A delivery hook for escalations and decisions beyond the wire event
#: (``jarvis.notify.messenger``, WP7): ``hook(kind, payload)`` with kind
#: ``"escalation"`` or ``"decision"`` and the notification's params, or
#: ``"briefing"`` with ``{"text", "report"}``: the briefing Jarvis wrote (or
#: the gathered facts when it wrote none) and the saved report's path in the
#: Jarvis folder (WP8).
NotifyHook = Callable[[str, dict[str, Any]], Awaitable[None]]


#: Told whenever the service announces ``jarvis.state``: ``hook(reason, running)``.
#: The messenger bridge (WP7) re-points the chats on it.
StateHook = Callable[[str, bool], Awaitable[None]]


#: ``Session.origin_surface`` (and the stored row's ``origin_surface``) of a
#: ``jarvis-task`` worker (M19 WP8, ``jarvis/tasks.py``).  Such a worker runs
#: only its schedule: the intake, the overview and Jarvis's session tools do
#: not see it, and its requests go to the person.
TASK_WORKER_SURFACE = "jarvis-task"


def is_task_worker(target: Any) -> bool:
    """``target`` (a session or a stored session row) is a ``jarvis-task`` worker."""
    if target is None:
        return False
    if isinstance(target, dict):
        return target.get("origin_surface") == TASK_WORKER_SURFACE
    return getattr(target, "origin_surface", None) == TASK_WORKER_SURFACE


#: See :attr:`Hooks.unrestricted`.
UnrestrictedHook = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass
class Hooks:
    """Extension points later work packages attach to (see the service)."""

    #: Messenger delivery (WP7); called for ``high``/``urgent`` notices only,
    #: and only while ``jarvis.notify.messenger`` is on.
    notify: list[NotifyHook] = field(default_factory=list)
    #: Called on every ``jarvis.state`` (enabled, disabled, settings, …).
    state: list[StateHook] = field(default_factory=list)
    #: Extra overview rows, e.g. external CLI sessions (WP5/6):
    #: ``source() -> list[JarvisOverviewRow]``; exposure is filtered after.
    overview_sources: list[Callable[[], list[Any]]] = field(default_factory=list)
    #: Extra Jarvis tools (WP5/6 ``external_list`` / ``external_read``, WP8
    #: ``briefing_set``): ``factory(service) -> list[Tool]``, added to the
    #: Jarvis session's own tool set when it is (re)built.
    tools: list[Callable[[Any], list[Any]]] = field(default_factory=list)
    #: A person's confirmation of ``unrestricted`` mode for a new external
    #: session, asked on the surface the current Jarvis turn came from (the
    #: messenger bridge, WP7): ``hook(turn_id, ask)`` with ``ask`` =
    #: ``{"cli", "project", "prompt"}`` returns a
    #: :class:`~snowpea_core.external.proc.PersonConfirmation`, a refusal
    #: reason, or ``None`` when that turn is not from its surface.
    unrestricted: list[UnrestrictedHook] = field(default_factory=list)


__all__ = [
    "TASK_WORKER_SURFACE",
    "Hooks",
    "NotifyHook",
    "StateHook",
    "UnrestrictedHook",
    "build_engine",
    "is_task_worker",
]
