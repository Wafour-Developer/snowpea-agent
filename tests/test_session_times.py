"""Reopened sessions keep activity times; pending notices survive a restart."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server.app_server import Core, Daemon
from snowpea_core.server.protocol import SessionListParams
from snowpea_core.server.session_handlers import collect_sessions
from snowpea_core.session.store import Store

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "basic.json"
HUMAN = {"text": "fix the build", "origin": {"kind": "human"}}


async def _stored_chat(core: Core, sid: str, workdir: Path) -> None:
    await core.store.insert_session(
        sid, str(workdir), "accept", None, None, "browser", "2026-11-16T09:00:00Z"
    )
    await core.store.append_event(
        sid, 1, "message.user", HUMAN, "2026-11-16T09:01:00Z", None, None
    )
    await core.store.append_event(
        sid,
        2,
        "turn.done",
        {"turnId": "t1", "reason": "complete"},
        "2026-11-16T09:02:30Z",
        None,
        None,
    )
    await core.store.replace_messages(
        sid,
        [{"role": "user", "content": {"content": "fix the build"}}],
    )


async def test_reopened_live_session_carries_stored_times(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    work = tmp_path / "w"
    work.mkdir()
    core = Core(settings=Settings(), paths=paths, token="t")
    core.store = Store.open(paths)
    core.sessions.store = core.store
    core.hub.store = core.store
    core.hub.sessions = core.sessions
    try:
        await _stored_chat(core, "s-live", work)
        session = await core.sessions.restore("s-live")
        assert session is not None
        row = session.summary()
        assert row.lastActivityAt == "2026-11-16T09:02:30Z"
        assert row.lastUserAt == "2026-11-16T09:01:00Z"

        listed = {r.sessionId: r for r in await collect_sessions(core, SessionListParams())}
        assert listed["s-live"].lastActivityAt == "2026-11-16T09:02:30Z"
        assert listed["s-live"].lastUserAt == "2026-11-16T09:01:00Z"

        await core.hub.emit(
            "s-live", "message.user", {"text": "again", "origin": {"kind": "human"}}
        )
        after = core.sessions.get("s-live")
        assert after is not None
        assert after.last_user_at is not None and after.last_user_at > "2026-11-16T09:01:00Z"
    finally:
        core.store.close()


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    home = tmp_path / "home"
    with fake_provider(FIXTURE):
        instance = await make_daemon(home)
    try:
        yield instance
    finally:
        await instance.stop()


@pytest_asyncio.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def run_turn(client: RpcClient, session_id: str, text: str) -> None:
    turn = await client.ok("session.prompt", {"sessionId": session_id, "text": text})
    await client.wait_turn(turn["turnId"], timeout=20)


async def test_pending_notices_survive_restart(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path
) -> None:
    home = daemon.paths.home
    work = tmp_path / "w"
    work.mkdir()
    client = await connect(http, daemon)
    try:
        session_id = str(
            (await client.ok("session.create", {"workdir": str(work), "mode": "accept"}))[
                "sessionId"
            ]
        )
        await client.ok(
            "session.notice",
            {"sessionId": session_id, "text": "download finished: report.pdf"},
        )
        assert daemon.core.sessions.get(session_id).pending_notices == [
            "download finished: report.pdf"
        ]
    finally:
        await client.stop()
    await daemon.stop()

    with fake_provider(FIXTURE):
        second = await make_daemon(home)
    client = await connect(http, second)
    try:
        session = await second.core.sessions.restore(session_id)
        assert session is not None
        assert session.pending_notices == ["download finished: report.pdf"]
        await run_turn(client, session_id, "hello")
        notes = [m.content for m in session.history.messages if m.role == "user"]
        assert notes.count("[system] download finished: report.pdf") == 1
        assert session.pending_notices == []
    finally:
        await client.stop()
        await second.stop()