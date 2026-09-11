"""Session bookkeeping — M1 stub.

US-005 replaces the bodies; the class names and signatures come from
``docs/design/m1-core-contract.md`` §4 and must stay stable.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.server.protocol import SessionSummary


class SessionManager:
    """Owns live sessions. Stub: holds nothing and lists nothing."""

    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}

    def get(self, session_id: str) -> Any | None:
        return self._sessions.get(session_id)

    def list(self) -> list[SessionSummary]:
        return []

    def __len__(self) -> int:
        return len(self._sessions)


class EventHub:
    """Broadcasts ``session.event`` to subscribed connections. Stub."""

    def __init__(self) -> None:
        self._subscribers: list[tuple[Any, str | None]] = []

    def subscribe(self, conn: Any, session_id: str | None = None) -> None:
        self._subscribers.append((conn, session_id))

    def unsubscribe(self, conn: Any) -> None:
        self._subscribers = [entry for entry in self._subscribers if entry[0] is not conn]


__all__ = ["EventHub", "SessionManager"]
