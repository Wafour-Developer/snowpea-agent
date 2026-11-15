"""Who holds authority over a session right now: Jarvis, or the person.

Two ways the person takes a session back (design Part 1, "Hand-back"):

* *typing* — they sent a message into it themselves; Jarvis's authority
  returns after ``jarvis.quietMinutes`` without further input, each new
  message pushing that time out again;
* *takeover* — ``jarvis.takeover``, for a number of minutes or, without a
  limit, until ``jarvis.handback``.

``jarvis.handback`` ends either at once.  The book is in memory: a daemon
restart hands every session back, which is the safe direction because the
delegation engine still decides what Jarvis may do.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from snowpea_core.jarvis.clock import iso

PauseReason = Literal["typing", "takeover"]


@dataclass(frozen=True)
class PauseState:
    """One session's pause as the wire shows it (``SessionJarvisState``, ``JarvisPause``)."""

    session_id: str
    reason: PauseReason
    #: When authority returns; ``None`` for a takeover without a limit.
    until: datetime | None
    project: str | None = None

    @property
    def taken_over(self) -> bool:
        """``SessionJarvisState.takenOver``: a takeover with no limit."""
        return self.reason == "takeover" and self.until is None

    @property
    def paused_until(self) -> str | None:
        """``SessionJarvisState.pausedUntil``: null while ``taken_over`` holds it."""
        return iso(self.until) if self.until is not None else None


class PauseBook:
    """Typing pauses and takeovers, keyed by session id (or ``ext:<cli>:<id>``)."""

    def __init__(self, now: Callable[[], datetime], quiet_minutes: Callable[[], int]) -> None:
        self._now = now
        self._quiet_minutes = quiet_minutes
        self._pauses: dict[str, PauseState] = {}

    def _live(self, session_id: str) -> PauseState | None:
        state = self._pauses.get(session_id)
        if state is None:
            return None
        if state.until is not None and state.until <= self._now():
            del self._pauses[session_id]
            return None
        return state

    def get(self, session_id: str) -> PauseState | None:
        return self._live(session_id)

    def is_paused(self, session_id: str) -> bool:
        return self._live(session_id) is not None

    def typed(self, session_id: str, project: str | None = None) -> bool:
        """The person sent input; True when this started a pause (it was not paused)."""
        current = self._live(session_id)
        if current is not None and current.reason == "takeover":
            until = current.until
            quiet = self._now() + timedelta(minutes=max(1, int(self._quiet_minutes())))
            if until is not None and until < quiet:
                # A takeover ending sooner than the quiet time would hand the
                # session back while the person is still typing in it.
                self._pauses[session_id] = PauseState(session_id, "typing", quiet, project)
            return False
        until = self._now() + timedelta(minutes=max(1, int(self._quiet_minutes())))
        self._pauses[session_id] = PauseState(session_id, "typing", until, project)
        return current is None

    def takeover(
        self, session_id: str, minutes: int | None = None, project: str | None = None
    ) -> PauseState:
        until = self._now() + timedelta(minutes=minutes) if minutes else None
        state = PauseState(session_id, "takeover", until, project)
        self._pauses[session_id] = state
        return state

    def handback(self, session_id: str) -> bool:
        """End any pause at once; False when there was none."""
        live = self._live(session_id) is not None
        self._pauses.pop(session_id, None)
        return live

    def forget(self, session_id: str) -> None:
        self._pauses.pop(session_id, None)

    def all(self) -> list[PauseState]:
        return [state for key in list(self._pauses) if (state := self._live(key)) is not None]


__all__ = ["PauseBook", "PauseReason", "PauseState"]
