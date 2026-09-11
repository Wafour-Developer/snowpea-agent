"""M1 US-004: JSON-RPC handshake, dispatch and HTTP surface of the core daemon."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio

from snowpea_core import __version__
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import (
    IMPLEMENTED_METHODS,
    METHODS,
    PROTOCOL_VERSION,
    Empty,
)

pytestmark = pytest.mark.asyncio


class Client:
    """Tiny JSON-RPC client over the daemon's ``/ws`` endpoint."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._next_id = 0
        self.server_requests: list[dict[str, Any]] = []
        self.answer: dict[str, Any] = {"decision": "allow", "scope": "once"}

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and return the raw JSON-RPC response frame."""
        self._next_id += 1
        request_id = self._next_id
        await self._ws.send_json(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        while True:
            message = json.loads(await self._ws.receive_str())
            if message.get("id") == request_id and "method" not in message:
                return message
            if "method" in message and message.get("id") is not None:
                # Server-initiated request: answer it with the canned decision.
                self.server_requests.append(message)
                await self._ws.send_json(
                    {"jsonrpc": "2.0", "id": message["id"], "result": self.answer}
                )


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    os.environ["SNOWPEA_TEST"] = "1"
    instance = Daemon(port=0, home=tmp_path / "home")
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()
        os.environ.pop("SNOWPEA_TEST", None)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client_session:
        yield client_session


async def _connect(session: aiohttp.ClientSession, daemon: Daemon) -> Client:
    ws = await session.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    return Client(ws)


async def _hello(client: Client, token: str, protocol: str = PROTOCOL_VERSION) -> dict[str, Any]:
    return await client.call(
        "system.hello",
        {"token": token, "clientVersion": "test-1", "protocolVersion": protocol},
    )


async def test_call_before_hello_is_unauthorized(
    daemon: Daemon, session: aiohttp.ClientSession
) -> None:
    client = await _connect(session, daemon)
    response = await client.call("system.info")
    assert response["error"]["data"]["code"] == "unauthorized"


async def test_hello_with_wrong_token_is_unauthorized(
    daemon: Daemon, session: aiohttp.ClientSession
) -> None:
    client = await _connect(session, daemon)
    response = await _hello(client, "not-the-token")
    assert response["error"]["data"]["code"] == "unauthorized"


async def test_hello_with_bad_major_is_incompatible(
    daemon: Daemon, session: aiohttp.ClientSession
) -> None:
    client = await _connect(session, daemon)
    response = await _hello(client, daemon.token, protocol="9.0.0")
    assert response["error"]["data"]["code"] == "protocol_incompatible"


async def test_hello_then_info(daemon: Daemon, session: aiohttp.ClientSession) -> None:
    client = await _connect(session, daemon)
    hello = await _hello(client, daemon.token)
    assert hello["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert "sessions" in hello["result"]["capabilities"]

    info = (await client.call("system.info"))["result"]
    assert info["pid"] == os.getpid()
    assert info["port"] == daemon.port
    assert info["version"] == __version__
    assert info["home"] == str(daemon.paths.home)


async def test_unknown_method_is_not_found(daemon: Daemon, session: aiohttp.ClientSession) -> None:
    client = await _connect(session, daemon)
    await _hello(client, daemon.token)
    response = await client.call("nope.nothing")
    assert response["error"]["code"] == -32601
    assert response["error"]["data"]["code"] == "not_found"


def _pending_no_arg_method() -> str:
    """A registered method that is declared but not implemented yet.

    Chosen dynamically so this test keeps working as later stories implement
    more of the protocol; restricted to no-argument methods so the call needs
    no fabricated params.
    """
    for name, method in METHODS.items():
        if method.direction != "c2s" or name in IMPLEMENTED_METHODS:
            continue
        if method.params is Empty:
            return name
    raise AssertionError("every no-arg method is implemented; retire this test")


async def test_unimplemented_method_reports_not_implemented(
    daemon: Daemon, session: aiohttp.ClientSession
) -> None:
    client = await _connect(session, daemon)
    await _hello(client, daemon.token)
    response = await client.call(_pending_no_arg_method())
    assert response["error"]["data"]["code"] == "not_implemented"


async def test_server_initiated_request(daemon: Daemon, session: aiohttp.ClientSession) -> None:
    client = await _connect(session, daemon)
    await _hello(client, daemon.token)
    client.answer = {"decision": "deny", "scope": "session"}
    response = await client.call("system.echoRequest", {"tool": "shell", "args": {"command": "ls"}})
    assert response["result"] == {"decision": "deny", "scope": "session"}
    assert client.server_requests[0]["method"] == "approval.request"
    assert client.server_requests[0]["params"]["tool"] == "shell"


async def test_http_routes(daemon: Daemon, session: aiohttp.ClientSession) -> None:
    base = f"http://127.0.0.1:{daemon.port}"
    async with session.get(f"{base}/health") as response:
        assert await response.json() == {"status": "ok"}
    async with session.get(f"{base}/version") as response:
        assert await response.json() == {
            "version": __version__,
            "protocolVersion": PROTOCOL_VERSION,
        }
    async with session.get(f"{base}/protocol.json") as response:
        schema = await response.json()
    assert schema["version"] == PROTOCOL_VERSION
    assert schema["protocolVersion"] == PROTOCOL_VERSION
    assert schema["serverVersion"] == __version__
    assert schema["methods"]["session.create"]["direction"] == "c2s"
    assert schema["methods"]["session.create"]["params"]["type"] == "object"
    assert schema["methods"]["approval.request"]["direction"] == "s2c"
    assert schema["events"]["session.event"]["type"] == "object"
    assert schema["sessionEventKinds"]["turn.done"]["type"] == "object"
    assert schema["transport"]["ws"] == "/ws"
    assert "unauthorized" in schema["errorCodes"]


async def test_daemon_json_written_and_removed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    instance = Daemon(port=0, home=home)
    await instance.start()
    daemon_json = home / "daemon.json"
    try:
        payload = json.loads(daemon_json.read_text(encoding="utf-8"))
        assert payload["port"] == instance.port
        assert payload["pid"] == os.getpid()
        assert payload["token"] == instance.token
        assert payload["protocolVersion"] == PROTOCOL_VERSION
        token_file = home / "token"
        assert token_file.read_text(encoding="utf-8").strip() == instance.token
        assert oct(token_file.stat().st_mode & 0o777) == "0o600"
    finally:
        await instance.stop()
    assert not daemon_json.exists()


async def test_shutdown_closes_the_daemon(tmp_path: Path) -> None:
    instance = Daemon(port=0, home=tmp_path / "home")
    await instance.start()
    async with aiohttp.ClientSession() as session:
        client = await _connect(session, instance)
        await _hello(client, instance.token)
        assert (await client.call("system.shutdown"))["result"] == {"ok": True}
    await asyncio.wait_for(instance.wait_closed(), timeout=5)
    await instance.stop()
    assert instance.shutdown_reason == "rpc"
    assert not (tmp_path / "home" / "daemon.json").exists()
