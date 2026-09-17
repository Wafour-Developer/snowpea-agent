"""CORE-dangling-turns: a turn a restart or a crash left open must still close.

The daemon was restarted while a turn was running.  The stored event log kept
``turn.started`` (and the reasoning and deltas it had produced) with no
``turn.done`` after it, so every surface that rebuilds a thread by replaying
its history — ``session.resume``, the IDE's hydrate — showed a turn that would
never finish.  Two halves fix it: a graceful stop closes the turns it is about
to cancel, and a daemon that starts on a log left by a ``kill -9`` repairs it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.config.paths import Paths
from snowpea_core.server.app_server import Daemon
from snowpea_core.session.store import Store

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "shutdown_race.json"
TIMEOUT = 20.0


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            if instance.core is not None:
                await instance.stop()


def stored_events(home: Path, session_id: str) -> list[dict[str, object]]:
    """Read one session's event log straight off disk."""
    connection = sqlite3.connect(Paths.create(home).state_db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT kind, payload_json FROM events WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        return [{"kind": row["kind"], "payload": json.loads(row["payload_json"])} for row in rows]
    finally:
        connection.close()


async def start_session(client: RpcClient, workdir: Path) -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept"})
    return str(result["sessionId"])


# ---------------------------------------------------------------------------
# (1) a graceful stop closes the turn it cancels
# ---------------------------------------------------------------------------


async def test_stopping_mid_turn_writes_exactly_one_interrupted_turn_done(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    home = tmp_path / "home"
    with fake_provider(FIXTURE):
        daemon = await make_daemon(home)
        workdir = home / "project"
        workdir.mkdir(parents=True, exist_ok=True)
        client = await connect(http, daemon, timeout=TIMEOUT)
        session_id = await start_session(client, workdir)
        turn = await client.ok("session.prompt", {"sessionId": session_id, "text": "slow reply"})
        turn_id = str(turn["turnId"])
        # Let the turn actually start; its fake step then sleeps for 2s.
        await client.wait(
            lambda event: event["kind"] == "turn.started"
            and event["payload"]["turnId"] == turn_id
        )
        await daemon.stop()

    events = stored_events(home, session_id)
    done = [e for e in events if e["kind"] == "turn.done"]
    assert len(done) == 1, events
    assert done[0]["payload"]["turnId"] == turn_id
    assert done[0]["payload"]["reason"] == "interrupted"
    assert done[0]["payload"]["synthetic"] is True
    # The log ends idle: the turn is closed and nothing reopens it.  Not
    # "turn.done is the very last event" — the cancelled turn task can still
    # land the `message.user` it was about to write before it is torn down,
    # and an event after a terminal one is the client's cue to ignore it.
    kinds = [event["kind"] for event in events]
    assert "turn.started" not in kinds[kinds.index("turn.done") :][1:]


async def test_a_restart_after_a_stop_mid_turn_adds_nothing(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    """The repair is idempotent: the closed turn is not closed a second time."""
    home = tmp_path / "home"
    with fake_provider(FIXTURE):
        daemon = await make_daemon(home)
        workdir = home / "project"
        workdir.mkdir(parents=True, exist_ok=True)
        client = await connect(http, daemon, timeout=TIMEOUT)
        session_id = await start_session(client, workdir)
        turn = await client.ok("session.prompt", {"sessionId": session_id, "text": "slow reply"})
        await client.wait(
            lambda event: event["kind"] == "turn.started"
            and event["payload"]["turnId"] == turn["turnId"]
        )
        await daemon.stop()
        before = stored_events(home, session_id)

        restarted = await make_daemon(home)
        try:
            assert stored_events(home, session_id) == before
        finally:
            await restarted.stop()


# ---------------------------------------------------------------------------
# (2) a log left by a kill -9 is repaired when the store opens
# ---------------------------------------------------------------------------


def seed_dangling(store: Store, session_id: str, turn_id: str) -> None:
    """Write the log a crash leaves behind: a turn that starts and says nothing more."""
    connection = store._conn  # noqa: SLF001 - the test writes the pre-fix shape by hand
    connection.execute(
        "INSERT OR REPLACE INTO sessions (id, workdir, mode, created_at) VALUES (?, ?, ?, ?)",
        (session_id, "/tmp", "accept", "2026-09-01T00:00:00Z"),
    )
    connection.execute(
        "INSERT OR REPLACE INTO events (session_id, seq, kind, payload_json, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        (session_id, 1, "message.user", json.dumps({"text": "hi"}), "2026-09-01T00:00:00Z"),
    )
    connection.execute(
        "INSERT OR REPLACE INTO events (session_id, seq, kind, payload_json, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            session_id,
            2,
            "turn.started",
            json.dumps({"turnId": turn_id, "prompt": "hi", "queued": False}),
            "2026-09-01T00:00:00Z",
        ),
    )
    connection.execute(
        "INSERT OR REPLACE INTO events (session_id, seq, kind, payload_json, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            session_id,
            3,
            "message.reasoning",
            json.dumps({"text": "thinking"}),
            "2026-09-01T00:00:00Z",
        ),
    )
    connection.commit()


async def test_a_dangling_turn_is_repaired(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = Store(path)
    try:
        seed_dangling(store, "s-crash", "t-open")
        written = await store.repair_dangling_turns()
        assert [(e["sessionId"], e["kind"], e["seq"]) for e in written] == [
            ("s-crash", "turn.done", 4)
        ]
        assert written[0]["payload"] == {
            "turnId": "t-open",
            "reason": "interrupted",
            "synthetic": True,
        }
        # Replaying the thread now ends idle.
        events = await store.events_after("s-crash", 0)
        assert events[-1]["kind"] == "turn.done"
        assert await store.max_seq("s-crash") == 4
        # Idempotent: a second pass has nothing left to close.
        assert await store.repair_dangling_turns() == []
    finally:
        store.close()


async def test_a_turn_done_for_a_dropped_prompt_does_not_hide_the_running_one(
    tmp_path: Path,
) -> None:
    """Matching is by turn id, not by "what was the last marker".

    ``flush_queued_turns`` writes ``turn.done`` for a dropped prompt while the
    real turn is still running, so the last marker in the log is not always the
    running turn's.
    """
    path = tmp_path / "state.db"
    store = Store(path)
    try:
        seed_dangling(store, "s-crash", "t-open")
        await store.append_event(
            "s-crash", 4, "turn.done", {"turnId": "t-dropped", "reason": "interrupted"}, "t"
        )
        written = await store.repair_dangling_turns()
        assert [e["payload"]["turnId"] for e in written] == ["t-open"]
        assert [e["seq"] for e in written] == [5]
    finally:
        store.close()


async def test_a_daemon_repairs_the_log_it_starts_on(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    seed = Store.open(Paths.create(home))
    seed_dangling(seed, "s-crash", "t-open")
    seed.close()

    with fake_provider(FIXTURE):
        daemon = await make_daemon(home)
        try:
            client = await connect(http, daemon, timeout=TIMEOUT)
            replay = await client.ok("session.resume", {"sessionId": "s-crash"})
            kinds = [event["kind"] for event in replay["events"]]
            assert kinds[-1] == "turn.done"
            last = replay["events"][-1]["payload"]
            assert last == {"turnId": "t-open", "reason": "interrupted", "synthetic": True}
            await client.stop()
        finally:
            await daemon.stop()


async def test_restore_repairs_a_session_written_after_the_startup_scan(
    daemon: Daemon, http: aiohttp.ClientSession
) -> None:
    """A row that appeared after the scan is repaired the moment it is restored."""
    assert daemon.core is not None
    seed_dangling(daemon.core.store, "s-late", "t-late")

    session = await daemon.core.sessions.restore("s-late")
    assert session is not None
    # The synthetic close is in the log, and the restored session's seq counts it.
    events = await daemon.core.store.events_after("s-late", 0)
    assert events[-1]["kind"] == "turn.done"
    assert events[-1]["payload"]["synthetic"] is True
    assert session.seq == 4
    assert session.current_turn is None


# ---------------------------------------------------------------------------
# (3) session.list says what is really running
# ---------------------------------------------------------------------------


async def test_session_list_reports_the_running_turn(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    home = tmp_path / "home"
    with fake_provider(FIXTURE):
        daemon = await make_daemon(home)
        workdir = home / "project"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            client = await connect(http, daemon, timeout=TIMEOUT)
            idle_id = await start_session(client, workdir)
            listing = await client.ok("session.list", {"workdir": str(workdir)})
            assert [row["running"] for row in listing["sessions"]] == [False]

            turn = await client.ok(
                "session.prompt", {"sessionId": idle_id, "text": "slow reply"}
            )
            await client.wait(
                lambda event: event["kind"] == "turn.started"
                and event["payload"]["turnId"] == turn["turnId"]
            )
            busy = await client.ok("session.list", {"workdir": str(workdir)})
            assert [row["running"] for row in busy["sessions"]] == [True]

            await client.ok("session.interrupt", {"sessionId": idle_id})
            await client.wait_turn(str(turn["turnId"]))
            await asyncio.sleep(0.05)
            after = await client.ok("session.list", {"workdir": str(workdir)})
            assert [row["running"] for row in after["sessions"]] == [False]
            await client.stop()
        finally:
            await daemon.stop()


async def test_a_stored_row_is_never_running(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    seed = Store.open(Paths.create(home))
    seed_dangling(seed, "s-crash", "t-open")
    seed.close()

    with fake_provider(FIXTURE):
        daemon = await make_daemon(home)
        try:
            client = await connect(http, daemon, timeout=TIMEOUT)
            listing = await client.ok("session.list", {"includeClosed": True})
            row = next(r for r in listing["sessions"] if r["sessionId"] == "s-crash")
            assert row["running"] is False
            await client.stop()
        finally:
            await daemon.stop()


def stored_messages(home: Path, session_id: str) -> list[dict[str, object]]:
    connection = sqlite3.connect(Paths.create(home).state_db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT role, content_json FROM messages WHERE session_id = ? ORDER BY idx",
            (session_id,),
        )
        return [{"role": row["role"], "content": json.loads(row["content_json"])} for row in rows]
    finally:
        connection.close()


async def test_an_interrupted_turn_keeps_its_prompt_for_resume(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    """Stop a turn mid-way: the prompt it began with must be in the stored
    history, or a resumed session answers as if it was never asked."""
    home = tmp_path / "home"
    with fake_provider(FIXTURE):
        daemon = await make_daemon(home)
        workdir = home / "project"
        workdir.mkdir(parents=True, exist_ok=True)
        client = await connect(http, daemon, timeout=TIMEOUT)
        session_id = await start_session(client, workdir)
        turn = await client.ok("session.prompt", {"sessionId": session_id, "text": "slow reply"})
        turn_id = str(turn["turnId"])
        await client.wait(
            lambda event: event["kind"] == "turn.started"
            and event["payload"]["turnId"] == turn_id
        )
        await client.ok("session.interrupt", {"sessionId": session_id})
        await client.wait(
            lambda event: event["kind"] == "turn.done" and event["payload"]["turnId"] == turn_id
        )
        messages = stored_messages(home, session_id)
        assert [m["role"] for m in messages][-1:] == ["user"], messages
        assert "slow reply" in json.dumps(messages[-1]["content"], ensure_ascii=False)
        await daemon.stop()


async def test_session_resume_after_daemon_died_mid_turn_has_synthetic_interrupted_turn_done(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    """session.resume for a session whose daemon died mid-turn has synthetic
    interrupted turn.done in replay.
    """
    home = tmp_path / "home"
    workdir = home / "project"
    workdir.mkdir(parents=True, exist_ok=True)

    with fake_provider(FIXTURE):
        daemon1 = await make_daemon(home)
        client1 = await connect(http, daemon1, timeout=TIMEOUT)
        session_id = await start_session(client1, workdir)
        turn = await client1.ok("session.prompt", {"sessionId": session_id, "text": "slow reply"})
        turn_id = str(turn["turnId"])

        # Wait until turn has started
        await client1.wait(
            lambda event: event["kind"] == "turn.started"
            and event["payload"]["turnId"] == turn_id
        )
        after_seq = max(e.get("seq", 0) for e in client1.events if e.get("sessionId") == session_id)

        # Abrupt stop/death mid-turn
        await client1.stop()
        await daemon1.stop()

        # Restart daemon on same home and connect fresh client
        daemon2 = await make_daemon(home)
        try:
            client2 = await connect(http, daemon2, timeout=TIMEOUT)
            replay = await client2.ok(
                "session.resume", {"sessionId": session_id, "afterSeq": after_seq}
            )
            events = replay.get("events", [])
            dones = [e for e in events if e.get("kind") == "turn.done"]
            assert len(dones) == 1, events
            assert dones[0]["payload"]["turnId"] == turn_id
            assert dones[0]["payload"]["reason"] == "interrupted"
            assert dones[0]["payload"]["synthetic"] is True
            await client2.stop()
        finally:
            await daemon2.stop()

