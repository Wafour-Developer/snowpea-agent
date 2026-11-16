"""``session.list {includeClosed: true}``: stored rows carry what live rows show (1.55.0).

A finished scheduled run and an ordinary closed chat, written straight into a
tmp store; nothing runs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server.app_server import Core
from snowpea_core.server.protocol import SessionListParams
from snowpea_core.server.session_handlers import collect_sessions
from snowpea_core.session.store import Store

HUMAN = {"text": "fix the build", "origin": {"kind": "human"}}


async def _row(store: Store, sid: str, *, kind: str = "chat", job: str | None = None,
               events: list[tuple[str, dict[str, Any], str]]) -> None:
    await store.insert_session(sid, "/work", "accept", None, None, "tui",
                               "2026-11-16T09:00:00Z", kind=kind, job_id=job)
    for seq, (event_kind, payload, ts) in enumerate(events, start=1):
        await store.append_event(sid, seq, event_kind, payload, ts)
    await store.close_session(sid, "2026-11-16T10:00:00Z")


async def test_closed_rows_have_activity_status_title_and_last_user(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    core = Core(settings=Settings(), paths=paths, token="t")
    core.store = Store.open(paths)
    try:
        await _row(core.store, "s-chat", events=[
            ("message.user", HUMAN, "2026-11-16T09:01:00Z"),
            ("turn.done", {"turnId": "t1", "reason": "complete"}, "2026-11-16T09:02:30Z"),
        ])
        await core.store.update_title("s-chat", "Build fix")
        await _row(core.store, "s-job", kind="scheduled", job="job-1", events=[
            ("message.user", {"text": "daily report", "origin": {"kind": "schedule"}},
             "2026-11-16T08:00:00Z"),
            ("turn.done", {"turnId": "t2", "reason": "complete"}, "2026-11-16T08:05:00Z"),
        ])

        async def jobs() -> list[Any]:
            return [SimpleNamespace(id="job-1", title=None, task="Daily report\nmore")]

        core.scheduler = SimpleNamespace(list=jobs)  # type: ignore[assignment]
        rows = {r.sessionId: r for r in await collect_sessions(
            core, SessionListParams(includeClosed=True))}
        chat, job = rows["s-chat"], rows["s-job"]
        assert chat.running is False and chat.status == "idle"
        assert chat.title == "Build fix"
        assert chat.lastActivityAt == "2026-11-16T09:02:30Z" and chat.seq == 2
        assert chat.lastUserAt == "2026-11-16T09:01:00Z"
        assert job.title == "Daily report"  # no title of its own: the job's name
        assert job.lastActivityAt == "2026-11-16T08:05:00Z"
        assert job.lastUserAt is None  # a scheduled prompt is not a person's
        assert job.running is False and job.status == "idle"
    finally:
        core.store.close()
