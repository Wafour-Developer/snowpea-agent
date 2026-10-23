"""1.27.0: ``session.events`` reads without resuming; ``session.list`` says when it is complete.

A browser building a feedback report used ``session.resume`` to read a
thread, which subscribed it, could make it the thread's surface and reopened
a closed session.  And an empty ``session.list`` from a daemon still booting
looked exactly like a user with no chats, so the browser could not safely
forget its records of closed tabs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, make_daemon

from snowpea_core.server.app_server import Daemon

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()


@pytest_asyncio.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def _session_with_events(daemon: Daemon, workdir: Path, count: int) -> str:
    core = daemon.core
    assert core is not None
    workdir.mkdir(parents=True, exist_ok=True)
    session = await core.sessions.create(workdir, mode="accept")
    for n in range(count):
        await core.hub.emit(session.id, "message.delta", {"text": f"part {n}"})
    return session.id


async def test_events_are_read_in_pages(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path
) -> None:
    session_id = await _session_with_events(daemon, tmp_path / "w", 5)
    client = await connect(http, daemon)
    try:
        first = await client.ok("session.events", {"sessionId": session_id, "limit": 2})
        assert [e["payload"]["text"] for e in first["events"]] == ["part 0", "part 1"]
        assert first["more"] is True
        last_seq = first["lastSeq"]
        assert last_seq == max(e["seq"] for e in first["events"]) + 3

        rest = await client.ok(
            "session.events",
            {"sessionId": session_id, "afterSeq": first["events"][-1]["seq"], "limit": 10},
        )
        assert [e["payload"]["text"] for e in rest["events"]] == ["part 2", "part 3", "part 4"]
        assert rest["more"] is False
        seqs = [e["seq"] for e in first["events"] + rest["events"]]
        assert seqs == sorted(seqs) and seqs[-1] == last_seq
    finally:
        await client.stop()


async def test_reading_events_changes_nothing(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path
) -> None:
    core = daemon.core
    assert core is not None
    session_id = await _session_with_events(daemon, tmp_path / "w", 2)
    session = core.sessions.get(session_id)
    assert session is not None
    owner = session.origin_conn
    client = await connect(http, daemon)
    try:
        await client.ok("session.events", {"sessionId": session_id})
        # Not subscribed: a new event on the session does not reach the reader.
        await core.hub.emit(session_id, "message.delta", {"text": "later"})
        await asyncio.sleep(0.1)
        assert not [e for e in client.events if e.get("sessionId") == session_id]
        # Not the session's surface either.
        assert session.origin_conn is owner

        # A closed session is read from disk and stays closed.
        await core.sessions.close(session_id)
        closed = await client.ok("session.events", {"sessionId": session_id})
        assert [e["payload"]["text"] for e in closed["events"]][-1] == "later"
        assert core.sessions.get(session_id) is None
    finally:
        await client.stop()


async def test_an_unknown_session_is_not_found(http: aiohttp.ClientSession, daemon: Daemon) -> None:
    client = await connect(http, daemon)
    try:
        frame = await client.call("session.events", {"sessionId": "s-nope"})
        assert (frame["error"]["data"] or {}).get("code") == "not_found"
    finally:
        await client.stop()


async def test_session_list_says_when_it_is_complete(
    http: aiohttp.ClientSession, daemon: Daemon
) -> None:
    core = daemon.core
    assert core is not None
    client = await connect(http, daemon)
    try:
        listed = await client.ok("session.list", {"includeClosed": True})
        assert listed["sessions"] == [] and listed["complete"] is True

        # Before the boot restore finishes, an empty list proves nothing.
        core.sessions_loaded = False
        booting = await client.ok("session.list", {"includeClosed": True})
        assert booting["complete"] is False
    finally:
        await client.stop()


async def test_a_page_never_runs_past_its_last_seq(daemon: Daemon, tmp_path: Path) -> None:
    """An event saved after lastSeq was read is reported as more, not skipped."""
    core = daemon.core
    assert core is not None
    session_id = await _session_with_events(daemon, tmp_path / "w", 3)
    cap = await core.store.max_seq(session_id)
    await core.hub.emit(session_id, "message.delta", {"text": "written meanwhile"})

    events, more = await core.store.events_page(session_id, 0, 10, up_to=cap)
    assert [e["seq"] for e in events][-1] == cap
    assert more is False
    later, _ = await core.store.events_page(session_id, cap, 10, up_to=cap + 1)
    assert [e["payload"]["text"] for e in later] == ["written meanwhile"]
