"""M1 US-005: sessions, the agent loop, tools, permissions and slash commands.

Every test drives a real in-process daemon over its WebSocket, with the
deterministic scripted provider standing in for the model, so the assertions
are about the wire behaviour a TUI or the SDK would see.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio

from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import PROTOCOL_VERSION

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "session.json"
TIMEOUT = 10.0


class Client:
    """JSON-RPC client with a background reader, an event log and an approval policy."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.events: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.approval_requests: list[dict[str, Any]] = []
        #: "allow", "deny" or "ignore" (never answer, so the daemon times out).
        self.approval_mode = "allow"
        self.approval_scope = "once"

    def start(self) -> None:
        self._reader = asyncio.ensure_future(self._read())

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        await self._ws.close()

    async def _read(self) -> None:
        async for message in self._ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(message.data)
            if "method" not in frame:
                future = self._pending.pop(int(frame["id"]), None)
                if future is not None and not future.done():
                    future.set_result(frame)
                continue
            if frame.get("id") is None:
                self.notifications.append(frame)
                if frame["method"] == "session.event":
                    self.events.append(frame["params"])
                continue
            self.approval_requests.append(frame["params"])
            if self.approval_mode == "ignore":
                continue
            await self._ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "result": {"decision": self.approval_mode, "scope": self.approval_scope},
                }
            )

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and return the raw response frame."""
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._ws.send_json(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        return await asyncio.wait_for(future, TIMEOUT)

    async def ok(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a method and fail the test if it answered with an error."""
        frame = await self.call(method, params)
        assert "error" not in frame, frame["error"]
        return frame["result"]

    # -- event helpers -------------------------------------------------
    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.events]

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["kind"] == kind]

    async def wait(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = TIMEOUT
    ) -> dict[str, Any]:
        """Wait until one already-seen or incoming event satisfies ``predicate``."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for event in self.events:
                if predicate(event):
                    return event
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"timed out; saw {self.kinds()}")
            await asyncio.sleep(0.02)

    async def wait_turn(self, turn_id: str, timeout: float = TIMEOUT) -> str:
        """Wait for ``turn.done`` of ``turn_id`` and return its reason."""
        event = await self.wait(
            lambda e: e["kind"] == "turn.done" and e["payload"]["turnId"] == turn_id, timeout
        )
        return str(event["payload"]["reason"])


async def make_daemon(home: Path, settings: dict[str, Any] | None = None) -> Daemon:
    home.mkdir(parents=True, exist_ok=True)
    if settings:
        (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    return daemon


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


@pytest_asyncio.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def connect(http: aiohttp.ClientSession, daemon: Daemon) -> Client:
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    client = Client(ws)
    client.start()
    await client.ok(
        "system.hello",
        {
            "token": daemon.token,
            "clientVersion": "test-us005",
            "protocolVersion": PROTOCOL_VERSION,
        },
    )
    return client


async def start_session(client: Client, workdir: Path, mode: str = "accept") -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": mode})
    return str(result["sessionId"])


async def prompt(client: Client, session_id: str, text: str) -> str:
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


async def test_accept_mode_shell_denied_ends_the_turn(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    client.approval_mode = "deny"
    session_id = await start_session(client, workdir)

    turn_id = await prompt(client, session_id, "please run ls")
    assert await client.wait_turn(turn_id) == "denied"

    errors_seen = [event["payload"]["code"] for event in client.of_kind("error")]
    assert errors_seen == ["approval_denied"]
    assert client.of_kind("tool.result") == []

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
    assert await client.wait_turn(turn_id) == "denied"

    assert [event["payload"]["code"] for event in client.of_kind("error")] == ["mode_denied"]
    assert not (workdir / "greeting.txt").exists()
    assert client.approval_requests == []

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
    assert set(by_name) == {"read_file", "write_file", "edit_file", "list_dir", "shell"}
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
        assert await client.wait_turn(turn_id) == "denied"
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
