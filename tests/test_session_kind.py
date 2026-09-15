"""CORE-session-kind: what opened a session, and which thread it belongs to.

A scheduled run used to look exactly like a thread the user had started: the
same shape of row in ``session.list``, with nothing tying it back to the
conversation that asked for it.  These tests pin the additive fields that fix
that — ``kind``, ``parentSessionId``, ``jobId``, ``agent`` — the store
migration that lets an old ``state.db`` carry them, the ``kinds`` filter, and
the ``job.done`` event the originating thread now receives.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, fake_provider, make_daemon

from snowpea_core.server.app_server import Daemon
from snowpea_core.session.store import SCHEMA, SESSION_COLUMNS, Store

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "scheduler.json"
TIMEOUT = 20.0


@pytest_asyncio.fixture
async def provider_env() -> AsyncIterator[None]:
    with fake_provider(FIXTURE):
        yield


@pytest_asyncio.fixture
async def daemon(tmp_path: Path, provider_env: None) -> AsyncIterator[Daemon]:
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()


# ---------------------------------------------------------------------------
# (1) the store migration
# ---------------------------------------------------------------------------


async def test_an_old_sessions_table_gains_the_new_columns(tmp_path: Path) -> None:
    """A state.db from before CORE-session-kind opens and reads back as chat."""
    path = tmp_path / "legacy.db"
    # Strip every column the migration is responsible for, whatever its
    # padding or position: hard-coding the declarations meant that adding one
    # more migrated column silently left it in the "legacy" table, and the
    # migration was then never exercised.
    added = {name for name, _decl in SESSION_COLUMNS}
    kept = [line for line in SCHEMA.splitlines() if line.strip().split(" ")[0] not in added]
    legacy = "\n".join(kept) + "\n"
    # The last column of a CREATE TABLE cannot keep its comma.
    legacy = legacy.replace("    closed_at      TEXT,\n", "    closed_at      TEXT\n")
    for name in added:
        assert name not in legacy
    assert "closed_at      TEXT\n);" in legacy
    connection = sqlite3.connect(path)
    connection.executescript(legacy)
    connection.execute(
        "INSERT INTO sessions (id, workdir, mode, created_at) VALUES (?, ?, ?, ?)",
        ("s-old", "/tmp", "accept", "2026-09-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    store = Store(path)
    try:
        columns = {
            str(row["name"])
            for row in store._query("PRAGMA table_info(sessions)")  # noqa: SLF001
        }
        assert added <= columns
        rows = await store.list_sessions(include_closed=True)
        assert [(row["id"], row["kind"], row["parent_session_id"]) for row in rows] == [
            ("s-old", "chat", None)
        ]
        # Idempotent: opening the same file again must not fail on a second ALTER.
        second = Store(path)
        second.close()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# (2) create -> list round trip
# ---------------------------------------------------------------------------


async def test_created_kinds_survive_the_store_and_reach_session_list(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    assert daemon.core is not None
    client = await connect(http, daemon, timeout=TIMEOUT)
    chat = await client.ok("session.create", {"workdir": str(tmp_path)})
    parent_id = chat["sessionId"]

    child = await daemon.core.sessions.create(
        workdir=tmp_path,
        kind="subagent",
        parent_session_id=parent_id,
        agent="researcher",
    )
    run = await daemon.core.sessions.create(
        workdir=tmp_path,
        kind="scheduled",
        parent_session_id=parent_id,
        job_id="j-123",
        origin_surface="scheduler",
    )

    listing = await client.ok("session.list", {"workdir": str(tmp_path)})
    rows = {row["sessionId"]: row for row in listing["sessions"]}
    assert rows[parent_id]["kind"] == "chat"
    assert rows[parent_id]["parentSessionId"] is None
    assert rows[child.id]["kind"] == "subagent"
    assert rows[child.id]["parentSessionId"] == parent_id
    assert rows[child.id]["agent"] == "researcher"
    assert rows[run.id]["kind"] == "scheduled"
    assert rows[run.id]["jobId"] == "j-123"

    # The same three fields come back off disk, not just off the live objects.
    stored = {row["id"]: row for row in await daemon.core.store.list_sessions(include_closed=True)}
    assert stored[run.id]["kind"] == "scheduled"
    assert stored[run.id]["parent_session_id"] == parent_id
    assert stored[run.id]["job_id"] == "j-123"

    await daemon.core.sessions.close(child.id)
    await daemon.core.sessions.close(run.id)
    closed = await client.ok("session.list", {"workdir": str(tmp_path), "includeClosed": True})
    closed_rows = {row["sessionId"]: row for row in closed["sessions"]}
    assert closed_rows[run.id]["kind"] == "scheduled"
    assert closed_rows[run.id]["parentSessionId"] == parent_id
    assert closed_rows[run.id]["jobId"] == "j-123"
    await client.stop()


# ---------------------------------------------------------------------------
# (3) the kinds filter
# ---------------------------------------------------------------------------


async def test_kinds_filters_the_listing(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    assert daemon.core is not None
    client = await connect(http, daemon, timeout=TIMEOUT)
    chat = await client.ok("session.create", {"workdir": str(tmp_path)})
    run = await daemon.core.sessions.create(
        workdir=tmp_path, kind="scheduled", parent_session_id=chat["sessionId"], job_id="j-9"
    )

    only_chat = await client.ok("session.list", {"workdir": str(tmp_path), "kinds": ["chat"]})
    assert [row["sessionId"] for row in only_chat["sessions"]] == [chat["sessionId"]]

    only_scheduled = await client.ok(
        "session.list", {"workdir": str(tmp_path), "kinds": ["scheduled"]}
    )
    assert [row["sessionId"] for row in only_scheduled["sessions"]] == [run.id]

    everything = await client.ok("session.list", {"workdir": str(tmp_path)})
    assert len(everything["sessions"]) == 2
    await client.stop()


# ---------------------------------------------------------------------------
# (4) a scheduled run, end to end
# ---------------------------------------------------------------------------


async def test_a_scheduled_run_is_tagged_and_tells_the_thread_that_asked(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    assert daemon.core is not None
    client = await connect(http, daemon, timeout=TIMEOUT)
    origin = await client.ok("session.create", {"workdir": str(tmp_path)})
    origin_id = origin["sessionId"]

    job = await daemon.core.scheduler.schedule(
        "0 9 * * *",
        "summarise the repo",
        channel="log",
        origin_session_id=origin_id,
        workdir=str(tmp_path),
    )
    await daemon.core.scheduler.run_now(job.id)

    stored = await daemon.core.store.list_sessions(include_closed=True)
    runs = [row for row in stored if row["job_id"] == job.id]
    assert len(runs) == 1
    assert runs[0]["kind"] == "scheduled"
    assert runs[0]["parent_session_id"] == origin_id
    assert runs[0]["origin_surface"] == "scheduler"

    # The thread that asked for the job hears that it finished, and is told
    # which session to open.
    done = await client.wait(
        lambda event: event["kind"] == "job.done" and event["sessionId"] == origin_id
    )
    assert done["payload"]["jobId"] == job.id
    assert done["payload"]["sessionId"] == runs[0]["id"]
    assert done["payload"]["status"] == "ok"
    await client.stop()
