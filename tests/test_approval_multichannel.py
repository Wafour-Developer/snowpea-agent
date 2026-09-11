"""US-016: the shared unattended approval queue (M5 contract §4, §5, AC-20).

An unattended turn — here a gateway message, the same path a scheduled job
takes — has no human on its own surface, so its approval goes two places at
once: an ``approval.pending`` notification to every authenticated client, and a
message with allow/deny buttons to the bound conversation.  Whoever answers
first wins; everybody else is told with ``approval.resolved``.

Interactive turns keep the old behaviour: the request is asked of the surface
that started the session and is invisible to any other client.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest_asyncio

from snowpea_core.gateway.base import parse_approval_callback
from snowpea_core.gateway.fake import FakeAdapter
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import PROTOCOL_VERSION

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "gateway.json"
TIMEOUT = 10.0
#: Prompt whose scripted reply calls ``shell``, which "accept" mode asks about.
SHELL_PROMPT = "deploy the thing"


class Client:
    """JSON-RPC client that records notifications and can answer approvals."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.notifications: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.approval_requests: list[dict[str, Any]] = []
        #: "allow", "deny" or "ignore" (never answer, so the daemon times out).
        self.approval_mode = "ignore"

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
            if frame.get("id") is not None:
                await self._server_request(frame)
                continue
            self.notifications.append(frame)
            if frame["method"] == "session.event":
                self.events.append(frame["params"])

    async def _server_request(self, frame: dict[str, Any]) -> None:
        if frame.get("method") == "approval.request":
            self.approval_requests.append(frame["params"])
            if self.approval_mode == "ignore":
                return
            await self._ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "result": {"decision": self.approval_mode, "scope": "once"},
                }
            )

    async def ok(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._ws.send_json(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        frame = await asyncio.wait_for(future, TIMEOUT)
        assert "error" not in frame or frame["error"] is None, frame.get("error")
        return dict(frame["result"])

    # -- waiting -------------------------------------------------------
    async def wait_notification(self, method: str, timeout: float = TIMEOUT) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for frame in self.notifications:
                if frame["method"] == method:
                    return dict(frame["params"])
            if loop.time() > deadline:
                seen = sorted({frame["method"] for frame in self.notifications})
                raise AssertionError(f"no {method}; saw {seen}")
            await asyncio.sleep(0.02)

    async def wait_turn(self, turn_id: str, timeout: float = TIMEOUT) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for event in self.events:
                if event["kind"] == "turn.done" and event["payload"]["turnId"] == turn_id:
                    return str(event["payload"]["reason"])
            if loop.time() > deadline:
                raise AssertionError("turn never finished")
            await asyncio.sleep(0.02)


@pytest_asyncio.fixture
async def gateway_env() -> AsyncIterator[None]:
    previous = {key: os.environ.get(key) for key in ("SNOWPEA_PROVIDER", "SNOWPEA_GATEWAY_FAKE")}
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    os.environ["SNOWPEA_GATEWAY_FAKE"] = "1"
    FakeAdapter.instances.clear()
    try:
        yield None
    finally:
        FakeAdapter.instances.clear()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest_asyncio.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def make_daemon(home: Path, timeout_sec: int = 30) -> Daemon:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"approvals": {"timeoutSec": timeout_sec}}), encoding="utf-8"
    )
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    return daemon


async def connect(http: aiohttp.ClientSession, daemon: Daemon) -> Client:
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    client = Client(ws)
    client.start()
    await client.ok(
        "system.hello",
        {"token": daemon.token, "clientVersion": "test", "protocolVersion": PROTOCOL_VERSION},
    )
    return client


async def bind_fake(client: Client, workdir: Path) -> FakeAdapter:
    """Bind a fake platform to a fresh session per chat and return the adapter."""
    await client.ok(
        "gateway.bind",
        {
            "platform": "telegram",
            "credentialsRef": "tg_test",
            "target": {"new_session": {"workdir": str(workdir), "mode": "accept"}},
            "channelId": "c1",
            "userId": "u1",
        },
    )
    return FakeAdapter.instances["tg_test"]


def approval_log(daemon: Daemon) -> list[dict[str, Any]]:
    path = daemon.paths.approvals_log
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# (a) one request, two deliveries
# ---------------------------------------------------------------------------


async def test_unattended_approval_reaches_both_the_tui_and_the_chat(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = await make_daemon(tmp_path / "home")
    try:
        binder = await connect(http, daemon)
        adapter = await bind_fake(binder, workdir)
        # A second surface that owns no session at all — a TUI on the side.
        watcher = await connect(http, daemon)

        await adapter.push(SHELL_PROMPT, channel_id="c1", user_id="u1")

        pending = await watcher.wait_notification("approval.pending")
        request_id = pending["request"]["requestId"]
        assert pending["request"]["tool"] == "shell"

        outbound = await adapter.wait_for_send(TIMEOUT, count=1)
        assert outbound.buttons, "the chat message must carry allow/deny buttons"
        decisions = {parse_approval_callback(data) for data in outbound.button_data()}
        assert decisions == {(request_id, "allow"), (request_id, "deny")}

        # It is in the shared queue for any authenticated client.
        listed = (await watcher.ok("approval.list", {}))["requests"]
        assert [r["requestId"] for r in listed] == [request_id]

        await adapter.press(f"apr:{request_id}:allow", channel_id="c1", user_id="u1")
        resolved = await watcher.wait_notification("approval.resolved")
        assert resolved["requestId"] == request_id
        assert resolved["decision"] == "allow"
        assert resolved["by"] == "gateway:telegram:u1"

        record = approval_log(daemon)[-1]
        assert record["decision"] == "allow"
        assert record["unattended"] is True
        assert record["by"] == "gateway:telegram:u1"

        await watcher.stop()
        await binder.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (b) first response wins, from either side
# ---------------------------------------------------------------------------


async def test_an_answer_from_the_tui_resolves_the_chat_request(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = await make_daemon(tmp_path / "home")
    try:
        binder = await connect(http, daemon)
        adapter = await bind_fake(binder, workdir)
        watcher = await connect(http, daemon)

        await adapter.push(SHELL_PROMPT, channel_id="c1", user_id="u1")
        pending = await watcher.wait_notification("approval.pending")
        request_id = pending["request"]["requestId"]
        await adapter.wait_for_send(TIMEOUT)

        await watcher.ok(
            "approval.respond", {"requestId": request_id, "decision": "deny", "scope": "once"}
        )

        # The chat that was asked is told how it ended.
        await adapter.wait_for_send(TIMEOUT, count=2)
        assert any(request_id in text and "deny" in text for text in adapter.texts())

        # A late button press changes nothing and raises nothing.
        await adapter.press(f"apr:{request_id}:allow", channel_id="c1", user_id="u1")
        assert [r["decision"] for r in approval_log(daemon)] == ["deny"]

        await watcher.stop()
        await binder.stop()
    finally:
        await daemon.stop()


async def test_only_the_bound_user_may_answer_from_chat(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = await make_daemon(tmp_path / "home")
    try:
        binder = await connect(http, daemon)
        adapter = await bind_fake(binder, workdir)
        watcher = await connect(http, daemon)

        await adapter.push(SHELL_PROMPT, channel_id="c1", user_id="u1")
        request_id = (await watcher.wait_notification("approval.pending"))["request"]["requestId"]
        await adapter.wait_for_send(TIMEOUT)

        # Someone else in the same chat presses allow (plan §6 risk 4).
        await adapter.press(f"apr:{request_id}:allow", channel_id="c1", user_id="intruder")
        await asyncio.sleep(0.2)
        assert approval_log(daemon) == []
        assert (await watcher.ok("approval.list", {}))["requests"][0]["requestId"] == request_id

        await watcher.ok(
            "approval.respond", {"requestId": request_id, "decision": "deny", "scope": "once"}
        )
        await watcher.wait_notification("approval.resolved")
        assert approval_log(daemon)[-1]["by"] != "gateway:telegram:intruder"

        await watcher.stop()
        await binder.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (c) nobody answers
# ---------------------------------------------------------------------------


async def test_an_unanswered_unattended_approval_times_out_as_a_denial(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = await make_daemon(tmp_path / "home", timeout_sec=1)
    try:
        binder = await connect(http, daemon)
        adapter = await bind_fake(binder, workdir)
        watcher = await connect(http, daemon)

        await adapter.push(SHELL_PROMPT, channel_id="c1", user_id="u1")
        await watcher.wait_notification("approval.pending")
        resolved = await watcher.wait_notification("approval.resolved", timeout=8.0)

        assert resolved["decision"] == "deny"
        assert resolved["by"] == "timeout"
        record = approval_log(daemon)[-1]
        assert record["decision"] == "deny"
        assert record["code"] == "approval_timeout"
        assert record["unattended"] is True

        await watcher.stop()
        await binder.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (d) AC-20: an interactive request stays with its own surface
# ---------------------------------------------------------------------------


async def test_an_interactive_request_is_invisible_to_other_clients(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = await make_daemon(tmp_path / "home", timeout_sec=10)
    try:
        owner = await connect(http, daemon)
        watcher = await connect(http, daemon)
        session_id = (
            await owner.ok("session.create", {"workdir": str(workdir), "mode": "accept"})
        )["sessionId"]
        turn = await owner.ok("session.prompt", {"sessionId": session_id, "text": SHELL_PROMPT})

        # The owning surface is asked directly...
        deadline = asyncio.get_running_loop().time() + TIMEOUT
        while not owner.approval_requests:
            assert asyncio.get_running_loop().time() < deadline, "no approval.request arrived"
            await asyncio.sleep(0.02)
        request_id = owner.approval_requests[0]["requestId"]

        # ...and nobody else hears about it, by notification or by query.
        assert (await watcher.ok("approval.list", {}))["requests"] == []
        heard = [f["method"] for f in watcher.notifications if f["method"] == "approval.pending"]
        assert heard == []

        await owner.ok(
            "approval.respond", {"requestId": request_id, "decision": "deny", "scope": "once"}
        )
        assert await owner.wait_turn(turn["turnId"]) == "denied"
        record = approval_log(daemon)[-1]
        assert record["unattended"] is False

        await watcher.stop()
        await owner.stop()
    finally:
        await daemon.stop()
