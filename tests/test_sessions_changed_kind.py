"""``sessions.changed`` carries kind / parentSessionId; lastActivityAt tracks a running turn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _support import make_daemon

pytestmark = pytest.mark.asyncio


class Recorder:
    closed = False

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.sent.append((method, params))

    def changed(self, session_id: str) -> list[dict[str, Any]]:
        return [
            p for m, p in self.sent if m == "sessions.changed" and p.get("sessionId") == session_id
        ]


async def test_sessions_changed_carries_kind_and_parent(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        assert core is not None
        workdir = tmp_path / "project"
        workdir.mkdir()
        rec = Recorder()
        core.hub.subscribe(rec, None)
        parent = await core.sessions.create(workdir, mode="auto")
        child = await core.sessions.create(
            workdir, mode="auto", kind="subagent", parent_session_id=parent.id
        )
        created = rec.changed(child.id)[0]
        assert created["reason"] == "created"
        assert created["kind"] == "subagent"
        assert created["parentSessionId"] == parent.id
        root = rec.changed(parent.id)[0]
        assert root["kind"] == "chat"
        assert root["parentSessionId"] is None

        # The approval / question queues announce through the hub: enriched there.
        await core.hub.notify(
            "sessions.changed",
            {"reason": "approval", "sessionId": child.id, "status": "awaiting_approval"},
        )
        assert rec.changed(child.id)[-1]["kind"] == "subagent"
        assert rec.changed(child.id)[-1]["parentSessionId"] == parent.id

        # A closed session keeps its kind (it is no longer in the live table).
        await core.sessions.close(child.id)
        closed = rec.changed(child.id)[-1]
        assert closed["reason"] == "closed"
        assert closed["kind"] == "subagent"
        assert closed["parentSessionId"] == parent.id
    finally:
        await daemon.stop()


async def test_last_activity_advances_during_a_running_turn(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        assert core is not None
        workdir = tmp_path / "project"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        old = "2020-01-01T00:00:00+00:00"
        session.turn_started_at = old
        session.last_activity_at = old
        await core.hub.emit(session.id, "message.assistant", {"text": "working"})
        assert session.summary().lastActivityAt != old
        assert session.summary().lastActivityAt > old

        # Idle sessions are untouched by stray events.
        session.turn_started_at = None
        session.last_activity_at = old
        await core.hub.emit(session.id, "message.assistant", {"text": "late"})
        assert session.summary().lastActivityAt == old
    finally:
        await daemon.stop()
