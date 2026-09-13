"""Daemon discovery, spawning and the CLI's JSON-RPC client.

``ensure_daemon`` reuses a live daemon advertised by ``$SNOWPEA_HOME/daemon.json``
and otherwise spawns a detached ``python -m snowpea_core --port 0 --home <home>``.
``DaemonClient`` speaks the protocol of ``docs/design/m1-core-contract.md`` over
``ws://127.0.0.1:<port>/ws`` (§13.1 deviation: aiohttp, single port).

Every failure to reach a daemon raises :class:`DaemonError`, which the CLI maps
onto exit code ``3``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from snowpea_core import __version__
from snowpea_core.config.paths import Paths, resolve_home
from snowpea_core.server.protocol import PROTOCOL_VERSION

#: Seconds to wait for a freshly spawned daemon to publish ``daemon.json``.
SPAWN_TIMEOUT_SEC = 15.0
#: Poll interval while waiting for the spawned daemon.
POLL_INTERVAL_SEC = 0.1
#: Default timeout for a single JSON-RPC call.
CALL_TIMEOUT_SEC = 60.0


class DaemonError(RuntimeError):
    """The daemon could not be reached, started or handshaken with."""


class RpcCallError(RuntimeError):
    """A JSON-RPC error frame came back for one of our requests."""

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class DaemonInfo:
    """The contents of ``daemon.json``."""

    port: int
    pid: int
    token: str
    startedAt: str = ""
    protocolVersion: str = PROTOCOL_VERSION
    version: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DaemonInfo:
        return cls(
            port=int(payload["port"]),
            pid=int(payload["pid"]),
            token=str(payload["token"]),
            startedAt=str(payload.get("startedAt", "")),
            protocolVersion=str(payload.get("protocolVersion", PROTOCOL_VERSION)),
            version=str(payload.get("version", "")),
        )


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def read_daemon_json(home: Path | str | None = None) -> DaemonInfo | None:
    """Return the advertised daemon, or ``None`` when the file is absent/corrupt."""
    path = Paths(home=resolve_home(home)).daemon_json
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return DaemonInfo.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` exists (POSIX ``kill -0``)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


async def health_ok(port: int, *, timeout: float = 2.0) -> bool:
    """True when ``GET /health`` on ``port`` answers ``{"status": "ok"}``."""
    url = f"http://127.0.0.1:{port}/health"
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session,
            session.get(url) as response,
        ):
            if response.status != 200:
                return False
            payload = await response.json()
    except Exception:
        return False
    return bool(isinstance(payload, dict) and payload.get("status") == "ok")


def _spawn_command(home: Path) -> list[str]:
    """Command used to start the daemon; ``SNOWPEA_DAEMON_CMD`` overrides it (tests)."""
    override = os.environ.get("SNOWPEA_DAEMON_CMD")
    base = shlex.split(override) if override else [sys.executable, "-m", "snowpea_core"]
    return [*base, "--port", "0", "--home", str(home)]


def _spawn_daemon(home: Path) -> subprocess.Popen[bytes]:
    """Start a detached daemon whose output lands in ``$SNOWPEA_HOME/logs/daemon.out``."""
    paths = Paths(home=home)
    paths.ensure()
    log_file = paths.logs_dir / "daemon.out"
    command = _spawn_command(home)
    handle = log_file.open("ab")
    try:
        return subprocess.Popen(  # noqa: S603 - command is ours or a test override
            command,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(home),
        )
    except OSError as exc:
        handle.close()
        raise DaemonError(f"could not start the daemon ({command[0]}): {exc}") from exc


async def ensure_daemon(home: Path | str | None = None) -> DaemonInfo:
    """Return a live daemon, starting one if needed.

    Raises :class:`DaemonError` (→ exit code 3) when no daemon can be reached
    within :data:`SPAWN_TIMEOUT_SEC`.
    """
    resolved = resolve_home(home)
    existing = read_daemon_json(resolved)
    if existing is not None and pid_alive(existing.pid) and await health_ok(existing.port):
        return existing

    # Stale advert: drop it so we can tell the new daemon's file apart.
    stale = Paths(home=resolved).daemon_json
    with contextlib.suppress(OSError):
        stale.unlink()

    process = _spawn_daemon(resolved)
    deadline = time.monotonic() + SPAWN_TIMEOUT_SEC
    while time.monotonic() < deadline:
        info = read_daemon_json(resolved)
        if info is not None and await health_ok(info.port):
            return info
        if process.poll() is not None:
            raise DaemonError(
                f"the daemon exited immediately with status {process.returncode}; "
                f"see {Paths(home=resolved).logs_dir / 'daemon.out'}"
            )
        await asyncio.sleep(POLL_INTERVAL_SEC)
    with contextlib.suppress(Exception):
        process.terminate()
    raise DaemonError(f"the daemon did not come up within {SPAWN_TIMEOUT_SEC:.0f}s")


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

ApprovalHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
#: Answers a server->client ``question.request``; headless declines every one.
QuestionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class DaemonClient:
    """Minimal JSON-RPC 2.0 client over the daemon's ``/ws`` endpoint."""

    def __init__(
        self,
        info: DaemonInfo,
        *,
        client_version: str = f"snowpea-cli {__version__}",
        approval_handler: ApprovalHandler | None = None,
        question_handler: QuestionHandler | None = None,
    ) -> None:
        self.info = info
        self.client_version = client_version
        self.approval_handler = approval_handler
        self.question_handler = question_handler
        self.server_version = ""
        self.capabilities: list[str] = []
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._notifications: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._next_id = 0
        self._closed = False

    # -- lifecycle -----------------------------------------------------
    async def connect(self) -> None:
        """Open the socket and complete ``system.hello``."""
        url = f"http://127.0.0.1:{self.info.port}/ws"
        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(url, heartbeat=30.0)
        except Exception as exc:
            await session.close()
            raise DaemonError(f"could not connect to the daemon at {url}: {exc}") from exc
        self._session = session
        self._ws = ws
        self._reader = asyncio.ensure_future(self._read_loop())
        try:
            hello = await self.call(
                "system.hello",
                {
                    "token": self.info.token,
                    "clientVersion": self.client_version,
                    "protocolVersion": PROTOCOL_VERSION,
                },
                timeout=10.0,
            )
        except RpcCallError as exc:
            await self.close()
            raise DaemonError(
                f"handshake rejected by the daemon ({exc.code}): {exc.message}"
            ) from exc
        except DaemonError:
            await self.close()
            raise
        self.server_version = str(hello.get("serverVersion", ""))
        self.capabilities = list(hello.get("capabilities", []))

    async def close(self) -> None:
        """Close the socket and cancel every in-flight task."""
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None

    async def __aenter__(self) -> DaemonClient:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # -- requests ------------------------------------------------------
    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = CALL_TIMEOUT_SEC,
    ) -> dict[str, Any]:
        """Send a request and return its ``result`` (raises :class:`RpcCallError`)."""
        if self._ws is None or self._ws.closed:
            raise DaemonError(f"the connection is closed (while calling {method})")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        try:
            await self._ws.send_str(json.dumps(frame))
            message = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise DaemonError(f"{method} timed out after {timeout}s") from exc
        finally:
            self._pending.pop(request_id, None)
        error = message.get("error")
        if error is not None:
            data = error.get("data") or {}
            raise RpcCallError(
                str(data.get("code", "internal")),
                str(error.get("message", "")),
                data.get("details"),
            )
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification (no response expected)."""
        if self._ws is None or self._ws.closed:
            raise DaemonError(f"the connection is closed (while notifying {method})")
        await self._ws.send_str(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        )

    # -- notifications -------------------------------------------------
    async def notifications(self) -> AsyncIterator[dict[str, Any]]:
        """Yield every server notification frame until the socket closes."""
        while True:
            item = await self._notifications.get()
            if item is None:
                return
            yield item

    # -- internals -----------------------------------------------------
    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(message.data)
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                self._dispatch(frame)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # pragma: no cover - transport hiccup
            pass
        finally:
            self._fail_pending()
            await self._notifications.put(None)

    def _dispatch(self, frame: dict[str, Any]) -> None:
        if "method" in frame:
            if frame.get("id") is None:
                self._notifications.put_nowait(frame)
            else:
                self._spawn(self._answer_server_request(frame))
            return
        future = self._pending.pop(int(frame.get("id", -1)), None)
        if future is not None and not future.done():
            future.set_result(frame)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer_server_request(self, frame: dict[str, Any]) -> None:
        """Answer a s2c request: ``approval.request``, and ``question.request``.

        A headless run has no picker to draw, so a question is declined at
        once — an empty answer, which the tool reports as declined — rather
        than left to burn its ten-minute timeout with nobody watching.
        """
        method = str(frame.get("method"))
        params = frame.get("params") or {}
        if method == "question.request":
            if self.question_handler is not None:
                try:
                    reply: dict[str, Any] = await self.question_handler(params)
                except Exception:
                    reply = {"answers": []}
            else:
                reply = {"answers": []}
            await self._respond(frame["id"], result=reply)
            return
        if method == "approval.request" and self.approval_handler is not None:
            try:
                answer: dict[str, Any] = await self.approval_handler(params)
            except Exception:
                answer = {"decision": "deny", "scope": "once"}
            await self._respond(frame["id"], result=answer)
            return
        if method == "approval.request":
            await self._respond(frame["id"], result={"decision": "deny", "scope": "once"})
            return
        await self._respond(
            frame["id"],
            error={
                "code": -32601,
                "message": f"{method} is not supported by this client",
                "data": {"code": "not_found"},
            },
        )

    async def _respond(
        self,
        request_id: Any,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if self._ws is None or self._ws.closed:
            return
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            frame["error"] = error
        else:
            frame["result"] = result or {}
        with contextlib.suppress(Exception):
            await self._ws.send_str(json.dumps(frame))

    def _fail_pending(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(DaemonError("the daemon closed the connection"))
        self._pending.clear()


async def connect(home: Path | str | None = None, **kwargs: Any) -> DaemonClient:
    """``ensure_daemon`` + :meth:`DaemonClient.connect` in one call."""
    info = await ensure_daemon(home)
    client = DaemonClient(info, **kwargs)
    await client.connect()
    return client


__all__ = [
    "CALL_TIMEOUT_SEC",
    "POLL_INTERVAL_SEC",
    "SPAWN_TIMEOUT_SEC",
    "DaemonClient",
    "DaemonError",
    "DaemonInfo",
    "RpcCallError",
    "connect",
    "ensure_daemon",
    "health_ok",
    "pid_alive",
    "read_daemon_json",
]
