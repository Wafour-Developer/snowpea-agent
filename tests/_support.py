"""Helpers shared by the test modules.

Every test that drives a real in-process daemon needs the same three things: a
websocket JSON-RPC client, a way to point the daemon at a scripted fake
provider, and a pseudo-connection that records the events the hub publishes.
They lived in a handful of near-identical copies; this module is the one copy.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import aiohttp

from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import PROTOCOL_VERSION

#: Fallback request/wait budget; every helper takes an explicit override.
DEFAULT_TIMEOUT = 10.0

FIXTURES = Path(__file__).parent / "fixtures"
PROVIDER_FIXTURES = FIXTURES / "providers" / "fake"


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


@contextmanager
def env_vars(**values: str | None) -> Iterator[None]:
    """Set environment variables for the block and restore them afterwards."""
    previous = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield None
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def fake_provider(fixture: Path) -> Iterator[Path]:
    """Point ``SNOWPEA_PROVIDER`` at one scripted fake-provider script."""
    with env_vars(SNOWPEA_PROVIDER=f"fake:{fixture}"):
        yield fixture


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    """Run one git command in ``repo`` and return its stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def init_repo(root: Path, files: dict[str, str]) -> Path:
    """A git repository with one commit, so worktrees have something to branch."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "snowpea test")
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


# ---------------------------------------------------------------------------
# the hub side: a connection that only records
# ---------------------------------------------------------------------------


class Recorder:
    """A pseudo-connection that keeps every event the hub sends it."""

    closed = False

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "session.event":
            self.events.append(params)

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["kind"] == kind]

    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.events]

    def texts(self) -> str:
        return "\n".join(
            str(event["payload"].get("text", "")) for event in self.of_kind("message.done")
        )


class OriginConn:
    """The smallest thing ``_session_for`` will accept as a connection."""

    closed = False

    def __init__(self, session: Any) -> None:
        session.origin_conn = self


# ---------------------------------------------------------------------------
# the wire side: a JSON-RPC client over /ws
# ---------------------------------------------------------------------------


class RpcClient:
    """JSON-RPC client with a background reader, an event log and an approval policy."""

    def __init__(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        approval_mode: str = "allow",
        approval_scope: str = "once",
    ) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.timeout = timeout
        self.events: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.approval_requests: list[dict[str, Any]] = []
        #: "allow", "deny" or "ignore" (never answer, so the daemon times out).
        self.approval_mode = approval_mode
        self.approval_scope = approval_scope

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
        if frame.get("method") != "approval.request":
            return
        self.approval_requests.append(frame["params"])
        if self.approval_mode == "ignore":
            return
        await self._ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {"decision": self.approval_mode, "scope": self.approval_scope},
            }
        )

    # -- calling -------------------------------------------------------
    async def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send a request and return the raw JSON-RPC response frame."""
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._ws.send_json(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        return await asyncio.wait_for(future, self.timeout if timeout is None else timeout)

    async def ok(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        """Call a method and fail the test if it answered with an error."""
        frame = await self.call(method, params, timeout)
        assert "error" not in frame or frame["error"] is None, frame.get("error")
        return dict(frame["result"])

    # -- event helpers -------------------------------------------------
    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.events]

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["kind"] == kind]

    def of_method(self, method: str) -> list[dict[str, Any]]:
        return [frame["params"] for frame in self.notifications if frame["method"] == method]

    async def wait(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float | None = None
    ) -> dict[str, Any]:
        """Wait until one already-seen or incoming event satisfies ``predicate``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.timeout if timeout is None else timeout)
        while True:
            for event in self.events:
                if predicate(event):
                    return event
            if loop.time() > deadline:
                raise AssertionError(f"timed out; saw {self.kinds()}")
            await asyncio.sleep(0.02)

    async def wait_turn(self, turn_id: str, timeout: float | None = None) -> str:
        """Wait for ``turn.done`` of ``turn_id`` and return its reason."""
        event = await self.wait(
            lambda e: e["kind"] == "turn.done" and e["payload"]["turnId"] == turn_id, timeout
        )
        return str(event["payload"]["reason"])

    async def wait_notification(self, method: str, timeout: float | None = None) -> dict[str, Any]:
        """Wait for the first notification with ``method`` and return its params."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.timeout if timeout is None else timeout)
        while True:
            for frame in self.notifications:
                if frame["method"] == method:
                    return dict(frame["params"])
            if loop.time() > deadline:
                seen = sorted({frame["method"] for frame in self.notifications})
                raise AssertionError(f"no {method}; saw {seen}")
            await asyncio.sleep(0.02)


async def make_daemon(home: Path, settings: dict[str, Any] | None = None) -> Daemon:
    """Start a daemon on an ephemeral port, optionally with a settings file."""
    home.mkdir(parents=True, exist_ok=True)
    if settings:
        (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    return daemon


async def connect(
    http: aiohttp.ClientSession,
    daemon: Daemon,
    *,
    client_version: str = "test",
    timeout: float = DEFAULT_TIMEOUT,
    approval_mode: str = "allow",
    approval_scope: str = "once",
) -> RpcClient:
    """Open ``/ws``, complete ``system.hello`` and return the ready client."""
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    client = RpcClient(
        ws, timeout=timeout, approval_mode=approval_mode, approval_scope=approval_scope
    )
    client.start()
    await client.ok(
        "system.hello",
        {
            "token": daemon.token,
            "clientVersion": client_version,
            "protocolVersion": PROTOCOL_VERSION,
        },
    )
    return client
