"""M1 US-005: sessions, the agent loop, tools, permissions and slash commands.

Every test drives a real in-process daemon over its WebSocket, with the
deterministic scripted provider standing in for the model, so the assertions
are about the wire behaviour a TUI or the SDK would see.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.config.paths import Paths
from snowpea_core.server.app_server import Daemon
from snowpea_core.session.store import Store

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "session.json"
TIMEOUT = 10.0


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            await instance.stop()


async def start_session(client: RpcClient, workdir: Path, mode: str = "accept") -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": mode})
    return str(result["sessionId"])


async def prompt(client: RpcClient, session_id: str, text: str) -> str:
    result = await client.ok("session.prompt", {"sessionId": session_id, "text": text})
    return str(result["turnId"])


# ---------------------------------------------------------------------------
# (a) accept mode: a write tool runs without asking
# ---------------------------------------------------------------------------


async def test_accept_mode_write_needs_no_approval(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir)

    turn_id = await prompt(client, session_id, "write a greeting")
    assert await client.wait_turn(turn_id) == "complete"

    assert (workdir / "greeting.txt").read_text(encoding="utf-8") == "hello\n"
    assert client.approval_requests == []
    calls = client.of_kind("tool.call")
    assert [call["payload"]["name"] for call in calls] == ["write_file"]
    results = client.of_kind("tool.result")
    assert results[0]["payload"]["ok"] is True
    diffs = client.of_kind("diff")
    assert diffs and diffs[0]["payload"]["path"] == "greeting.txt"
    assert "+hello" in diffs[0]["payload"]["patch"]
    assert client.of_kind("message.done")[-1]["payload"]["text"] == "wrote greeting.txt"
    assert [event["seq"] for event in client.events] == list(range(1, len(client.events) + 1))

    await client.stop()


async def test_accept_mode_edit_file_emits_a_diff(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    (workdir / "greeting.txt").write_text("hello\n", encoding="utf-8")
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir)

    turn_id = await prompt(client, session_id, "edit the greeting")
    assert await client.wait_turn(turn_id) == "complete"

    assert (workdir / "greeting.txt").read_text(encoding="utf-8") == "bonjour\n"
    patch = client.of_kind("diff")[0]["payload"]["patch"]
    assert "-hello" in patch and "+bonjour" in patch
    assert client.approval_requests == []

    await client.stop()


# ---------------------------------------------------------------------------
# (b) accept mode: shell asks, and the answer decides the turn
# ---------------------------------------------------------------------------


async def test_accept_mode_shell_asks_and_allow_continues(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    session_id = await start_session(client, workdir)

    turn_id = await prompt(client, session_id, "please run ls")
    assert await client.wait_turn(turn_id) == "complete"

    assert len(client.approval_requests) == 1
    request = client.approval_requests[0]
    assert request["tool"] == "shell"
    assert request["sessionId"] == session_id
    result = client.of_kind("tool.result")[0]["payload"]
    assert result["ok"] is True
    assert "listed" in result["output"]
    assert client.of_kind("message.done")[-1]["payload"]["text"] == "done"

    await client.stop()


async def test_accept_mode_shell_denied_is_fed_back_to_the_model(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    client.approval_mode = "deny"
    session_id = await start_session(client, workdir)

    turn_id = await prompt(client, session_id, "please run ls")
    # A refusal no longer ends the turn (CORE-prompts, gap 3): it comes back to
    # the model as a failed tool result so it can choose something else.
    assert await client.wait_turn(turn_id) == "complete"

    errors_seen = [event["payload"]["code"] for event in client.of_kind("error")]
    assert errors_seen == ["approval_denied"]
    results = client.of_kind("tool.result")
    assert [item["payload"]["ok"] for item in results] == [False]
    assert "do not retry the same call" in results[0]["payload"]["error"]

    await client.stop()


async def test_session_scope_allow_is_not_asked_twice(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    client.approval_scope = "session"
    session_id = await start_session(client, workdir)

    first = await prompt(client, session_id, "please run ls")
    assert await client.wait_turn(first) == "complete"
    second = await prompt(client, session_id, "please run ls")
    assert await client.wait_turn(second) == "complete"

    assert len(client.approval_requests) == 1

    await client.stop()


# ---------------------------------------------------------------------------
# (c) plan mode refuses writes outright
# ---------------------------------------------------------------------------


async def test_plan_mode_denies_writes(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir, mode="plan")

    turn_id = await prompt(client, session_id, "write a greeting")
    # A refusal no longer ends the turn (CORE-prompts, gap 3): it comes back to
    # the model as a failed tool result so it can choose something else.
    assert await client.wait_turn(turn_id) == "complete"

    assert [event["payload"]["code"] for event in client.of_kind("error")] == ["mode_denied"]
    assert not (workdir / "greeting.txt").exists()
    assert client.approval_requests == []
    denial = client.of_kind("tool.result")[0]["payload"]
    assert denial["ok"] is False
    # Plan mode says what to do instead of the refused call.
    assert "describing what you would do instead" in denial["error"]

    await client.stop()


# ---------------------------------------------------------------------------
# (d) auto mode runs everything
# ---------------------------------------------------------------------------


async def test_auto_mode_runs_shell_without_asking(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir, mode="auto")

    turn_id = await prompt(client, session_id, "please run ls")
    assert await client.wait_turn(turn_id) == "complete"

    assert client.approval_requests == []
    assert client.of_kind("tool.result")[0]["payload"]["ok"] is True

    await client.stop()


# ---------------------------------------------------------------------------
# (e) resume replays what a client missed
# ---------------------------------------------------------------------------


async def test_resume_returns_events_after_seq(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir, mode="auto")

    turn_id = await prompt(client, session_id, "please run ls")
    assert await client.wait_turn(turn_id) == "complete"

    cutoff = client.events[2]["seq"]
    expected = [event for event in client.events if event["seq"] > cutoff]
    resumed = await client.ok(
        "session.resume", {"sessionId": session_id, "afterSeq": cutoff}
    )
    assert resumed["sessionId"] == session_id
    assert [event["seq"] for event in resumed["events"]] == [e["seq"] for e in expected]
    assert [event["kind"] for event in resumed["events"]] == [e["kind"] for e in expected]

    everything = await client.ok("session.resume", {"sessionId": session_id})
    assert len(everything["events"]) == len(client.events)

    await client.stop()


async def test_resume_restores_a_persisted_session_after_daemon_restart(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workdir = tmp_path / "project"
    workdir.mkdir()
    with fake_provider(FIXTURE):
        first = await make_daemon(home)
        client = await connect(http, first)
        session_id = await start_session(client, workdir, mode="auto")
        turn_id = await prompt(client, session_id, "please run ls")
        assert await client.wait_turn(turn_id) == "complete"
        event_count = len(client.events)
        await client.stop()
        await first.stop()

        second = await make_daemon(home)
        resumed_client = await connect(http, second)
        try:
            resumed = await resumed_client.ok("session.resume", {"sessionId": session_id})
            assert len(resumed["events"]) == event_count
            restored = second.core.sessions.get(session_id)  # type: ignore[union-attr]
            assert restored is not None
            assert restored.mode == "auto"
            assert restored.seq == resumed["events"][-1]["seq"]
            assert len(restored.history) > 0
        finally:
            await resumed_client.stop()
            await second.stop()


async def test_session_list_and_close(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir)

    listed = await client.ok("session.list")
    assert [row["sessionId"] for row in listed["sessions"]] == [session_id]
    assert listed["sessions"][0]["workdir"] == str(workdir)

    assert (await client.ok("session.close", {"sessionId": session_id}))["ok"] is True
    assert (await client.ok("session.list"))["sessions"] == []
    missing = await client.call("session.close", {"sessionId": session_id})
    assert missing["error"]["data"]["code"] == "not_found"

    await client.stop()


# ---------------------------------------------------------------------------
# (f) slash commands
# ---------------------------------------------------------------------------


async def test_slash_help_and_mode(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir)

    turn_id = await prompt(client, session_id, "/help")
    assert await client.wait_turn(turn_id) == "complete"
    text = client.of_kind("message.done")[-1]["payload"]["text"]
    assert "help" in text and "/mode" in text
    assert client.of_kind("tool.call") == []

    mode_turn = await prompt(client, session_id, "/mode auto")
    assert await client.wait_turn(mode_turn) == "complete"
    assert client.of_kind("mode.changed")[-1]["payload"]["mode"] == "auto"
    assert (await client.ok("session.list"))["sessions"][0]["mode"] == "auto"

    unknown = await prompt(client, session_id, "/nope")
    assert await client.wait_turn(unknown) == "error"
    assert client.of_kind("error")[-1]["payload"]["code"] == "not_found"

    await client.stop()


async def test_command_and_tool_and_provider_listings(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir)

    commands = (await client.ok("command.list", {"sessionId": session_id}))["commands"]
    assert {command["name"] for command in commands} >= {
        "help",
        "plan",
        "accept",
        "auto",
        "mode",
        "tools",
    }

    tools = (await client.ok("tool.list", {"sessionId": session_id}))["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    # US-009 widened the catalog; the M1 tools must still all be there.
    assert set(by_name) >= {"read_file", "write_file", "edit_file", "list_dir", "shell"}
    assert by_name["shell"]["permissionTag"] == "exec"
    assert by_name["read_file"]["permissionTag"] == "read"

    providers = (await client.ok("provider.list"))["providers"]
    assert len(providers) == 11
    web_login = [
        provider["vendor"]
        for provider in providers
        if set(provider["authMethods"]) - {"api_key"}
    ]
    assert web_login == ["openai", "openrouter"]

    run = await client.ok(
        "command.run", {"sessionId": session_id, "name": "tools", "args": ""}
    )
    assert await client.wait_turn(run["turnId"]) == "complete"
    assert "shell" in client.of_kind("message.done")[-1]["payload"]["text"]

    await client.stop()


async def test_set_mode_emits_mode_changed(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir)

    result = await client.ok("session.setMode", {"sessionId": session_id, "mode": "plan"})
    assert result["mode"] == "plan"
    await client.wait(lambda e: e["kind"] == "mode.changed")
    assert client.of_kind("mode.changed")[-1]["payload"]["mode"] == "plan"

    await client.stop()


async def test_project_default_mode_is_used(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    (workdir / ".snowpea").mkdir(parents=True)
    (workdir / ".snowpea" / "settings.json").write_text(
        json.dumps({"defaultMode": "plan"}), encoding="utf-8"
    )
    client = await connect(http, daemon)
    result = await client.ok("session.create", {"workdir": str(workdir)})
    listed = await client.ok("session.list")
    assert listed["sessions"][0]["sessionId"] == result["sessionId"]
    assert listed["sessions"][0]["mode"] == "plan"

    await client.stop()


# ---------------------------------------------------------------------------
# (g) an unanswered approval times out and is denied
# ---------------------------------------------------------------------------


async def test_approval_timeout_denies_and_is_logged(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    home = tmp_path / "home"
    daemon = await make_daemon(home, {"approvals": {"timeoutSec": 1}})
    workdir = tmp_path / "project"
    workdir.mkdir()
    try:
        client = await connect(http, daemon)
        client.approval_mode = "ignore"
        session_id = await start_session(client, workdir)

        turn_id = await prompt(client, session_id, "please run ls")
        # The timeout denies the call and the model is told so; the turn goes on.
        assert await client.wait_turn(turn_id) == "complete"
        assert [event["payload"]["code"] for event in client.of_kind("error")] == [
            "approval_timeout"
        ]

        records = [
            json.loads(line)
            for line in (home / "logs" / "approvals.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(records) == 1
        assert records[0]["decision"] == "deny"
        assert records[0]["code"] == "approval_timeout"
        assert records[0]["tool"] == "shell"
        await client.stop()
    finally:
        await daemon.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


async def test_interrupt_ends_the_turn(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    client.approval_mode = "ignore"
    session_id = await start_session(client, workdir)

    turn_id = await prompt(client, session_id, "please run ls")
    while not client.approval_requests:
        await asyncio.sleep(0.02)
    assert (await client.ok("session.interrupt", {"sessionId": session_id}))["ok"] is True
    reason = await client.wait_turn(turn_id, timeout=TIMEOUT)
    assert reason in {"interrupted", "denied"}

    await client.stop()


# ---------------------------------------------------------------------------
# (h) a daemon stopped mid-turn does not race the session store (CORE-session-race)
# ---------------------------------------------------------------------------

SHUTDOWN_FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "shutdown_race.json"


async def _stop_mid_turn_is_clean(home: Path, http: aiohttp.ClientSession, caplog: Any) -> None:
    """Start a turn whose fake-provider step sleeps 2s, then stop immediately.

    Before CORE-session-race, ``Daemon.stop`` never cancelled or awaited the
    still-running turn task before closing the session store, so the turn's
    own final ``turn.done`` write could hit the already-closed SQLite
    connection and raise ``sqlite3.ProgrammingError: Cannot operate on a
    closed database`` out of a background task. It must now shut down with no
    exception and no error-level log record.
    """
    with fake_provider(SHUTDOWN_FIXTURE):
        daemon = await make_daemon(home)
        workdir = home / "project"
        workdir.mkdir(parents=True, exist_ok=True)
        client = await connect(http, daemon)
        session_id = await start_session(client, workdir)
        await prompt(client, session_id, "slow reply")

        await asyncio.sleep(0.05)  # let the turn task actually start and enter the delay
        with caplog.at_level(logging.ERROR):
            await daemon.stop()  # must not raise

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_records, [r.getMessage() for r in error_records]

    store = Store.open(Paths.create(home))
    try:
        rows = await store.list_sessions(include_closed=True)
        assert [row["id"] for row in rows] == [session_id]
        # closed (this story does not add turn resumption) but never corrupted:
        # reading it back at all proves the on-disk file is intact.
        assert rows[0]["closed_at"] is not None
    finally:
        store.close()


async def test_stop_mid_turn_is_clean(
    tmp_path: Path, http: aiohttp.ClientSession, caplog: Any
) -> None:
    await _stop_mid_turn_is_clean(tmp_path / "home", http, caplog)


async def test_stop_mid_turn_is_clean_repeated(
    tmp_path: Path, http: aiohttp.ClientSession, caplog: Any
) -> None:
    """The same race, run 5 times over fresh homes to catch flakiness."""
    for index in range(5):
        await _stop_mid_turn_is_clean(tmp_path / f"home-{index}", http, caplog)
        caplog.clear()
