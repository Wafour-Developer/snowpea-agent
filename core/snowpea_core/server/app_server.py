"""Daemon assembly: core singletons, handler registration, process lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web
from pydantic import Field

from snowpea_core import __version__
from snowpea_core.commands.registry import CommandRegistry
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.registry import ProviderRegistry
from snowpea_core.server import errors
from snowpea_core.server.auth import ensure_token
from snowpea_core.server.errors import RpcError
from snowpea_core.server.lifecycle import Lifecycle
from snowpea_core.server.protocol import (
    METHODS as PROTOCOL_METHODS,
)
from snowpea_core.server.protocol import (
    PROTOCOL_VERSION,
    SERVER_VERSION,
    ApprovalRequest,
    Empty,
    HealthResult,
    InfoResult,
    LifecycleStatus,
    Ok,
    Payload,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.server.session_handlers import (
    register_session_handlers,
    wire_core,
)
from snowpea_core.server.transport_http import (
    CONNECTIONS_KEY,
    SOCKETS_KEY,
    create_app,
)
from snowpea_core.server.transport_ws import hello_handler
from snowpea_core.session.manager import EventHub, SessionManager
from snowpea_core.tools.registry import ToolRegistry

log = logging.getLogger("snowpea.server")

HOST = "127.0.0.1"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Core:
    """The daemon's singletons. Later stories replace the stub collaborators."""

    settings: Settings
    paths: Paths
    token: str
    store: Any = None
    sessions: SessionManager = field(default_factory=SessionManager)
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    commands: CommandRegistry = field(default_factory=CommandRegistry)
    providers: ProviderRegistry = field(default_factory=ProviderRegistry)
    approvals: ApprovalQueue = field(default_factory=ApprovalQueue)
    policy: PermissionPolicy = field(default_factory=PermissionPolicy)
    hub: EventHub = field(default_factory=EventHub)
    lifecycle: Lifecycle = field(default_factory=Lifecycle)
    pid: int = field(default_factory=os.getpid)
    port: int = 0
    started_at: str = field(default_factory=_utc_now)
    request_shutdown: Any = None


class EchoRequestParams(Payload):
    """Test-only params for ``system.echoRequest`` (``SNOWPEA_TEST=1``)."""

    sessionId: str = "test-session"
    tool: str = "shell"
    args: dict[str, Any] = Field(default_factory=dict)
    risk: str = "low"
    timeoutSec: int = 10
    scopeHint: str = "once"


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


async def info_handler(_conn: RpcConnection, _params: Empty, core: Core) -> InfoResult:
    """``system.info``."""
    status = core.lifecycle.status()
    return InfoResult(
        version=SERVER_VERSION,
        protocolVersion=PROTOCOL_VERSION,
        pid=core.pid,
        port=core.port,
        startedAt=core.started_at,
        home=str(core.paths.home),
        counters=dict(status["counters"]),
        lifecycle=LifecycleStatus(
            willExit=bool(status["willExit"]),
            reason=str(status["reason"]),
            secondsUntilExit=status["secondsUntilExit"],
        ),
    )


async def health_handler(_conn: RpcConnection, _params: Empty, _core: Core) -> HealthResult:
    """``system.health``."""
    return HealthResult()


async def shutdown_handler(_conn: RpcConnection, _params: Empty, core: Core) -> Ok:
    """``system.shutdown`` — answer first, then tear the daemon down."""
    if core.request_shutdown is not None:
        core.request_shutdown("rpc")
    return Ok(ok=True)


def _not_implemented(name: str) -> Any:
    async def handler(_conn: RpcConnection, _params: Any, _core: Core) -> dict[str, Any]:
        raise RpcError(errors.NOT_IMPLEMENTED, f"{name} is not implemented yet")

    return handler


async def echo_request_handler(
    conn: RpcConnection, params: EchoRequestParams, _core: Core
) -> dict[str, Any]:
    """Test-only: make a server->client ``approval.request`` and return the answer."""
    request = ApprovalRequest(
        requestId=f"req-{int(time.time() * 1000)}",
        sessionId=params.sessionId,
        tool=params.tool,
        args=params.args,
        risk=params.risk,
        timeoutSec=params.timeoutSec,
        scopeHint="once",
    )
    return await conn.call(
        "approval.request", request.model_dump(mode="json"), timeout=float(params.timeoutSec)
    )


def build_dispatcher(core: Core) -> RpcDispatcher:
    """Register ``system.*`` plus a ``not_implemented`` stub for every other method."""
    dispatcher = RpcDispatcher(core)
    dispatcher.register("system.hello", hello_handler)
    dispatcher.register("system.info", info_handler)
    dispatcher.register("system.health", health_handler)
    dispatcher.register("system.shutdown", shutdown_handler)
    register_session_handlers(dispatcher)
    for name, method in PROTOCOL_METHODS.items():
        if method.direction != "c2s" or dispatcher.has(name):
            continue
        dispatcher.register(name, _not_implemented(name))
    if os.environ.get("SNOWPEA_TEST") == "1":
        dispatcher.register("system.echoRequest", echo_request_handler, EchoRequestParams)
    return dispatcher


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


class Daemon:
    """Owns the listening socket, ``daemon.json`` and the shutdown signal."""

    def __init__(self, port: int = 0, home: Path | str | None = None) -> None:
        self._requested_port = port
        self._home = home
        self._runner: web.AppRunner | None = None
        self._closed = asyncio.Event()
        self.core: Core | None = None
        self.app: web.Application | None = None
        self.shutdown_reason: str | None = None

    @property
    def port(self) -> int:
        return self.core.port if self.core else 0

    @property
    def token(self) -> str:
        return self.core.token if self.core else ""

    @property
    def paths(self) -> Paths:
        if self.core is None:
            raise RuntimeError("daemon not started")
        return self.core.paths

    async def start(self) -> Core:
        """Bind the socket, publish ``daemon.json`` and start the idle timer."""
        paths = Paths.create(self._home)
        _configure_logging(paths)
        settings = Settings.load(paths)
        token = ensure_token(paths)
        core = Core(settings=settings, paths=paths, token=token)
        wire_core(core)
        core.lifecycle = Lifecycle(
            idle_timeout_sec=settings.daemon.idleTimeoutSec,
            on_idle=self._on_idle,
        )
        core.request_shutdown = self.request_shutdown
        self.core = core

        dispatcher = build_dispatcher(core)
        self.app = create_app(dispatcher, daemon=self, core=core)
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, HOST, self._requested_port)
        await site.start()
        self._runner = runner
        core.port = _resolve_port(runner, self._requested_port)
        self._write_daemon_json()
        core.lifecycle.start()
        log.info(
            "snowpea daemon listening on http://%s:%s (ws ws://%s:%s/ws)",
            HOST,
            core.port,
            HOST,
            core.port,
        )
        return core

    def _write_daemon_json(self) -> None:
        assert self.core is not None
        payload = {
            "port": self.core.port,
            "pid": self.core.pid,
            "token": self.core.token,
            "startedAt": self.core.started_at,
            "protocolVersion": PROTOCOL_VERSION,
            "version": __version__,
        }
        path = self.core.paths.daemon_json
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _remove_daemon_json(self) -> None:
        if self.core is None:
            return
        with contextlib.suppress(OSError):
            self.core.paths.daemon_json.unlink()

    async def _on_idle(self) -> None:
        self.request_shutdown("idle")

    def request_shutdown(self, reason: str = "requested") -> None:
        """Ask the daemon to stop; safe to call from a handler."""
        if self.shutdown_reason is None:
            self.shutdown_reason = reason
        self._closed.set()

    async def wait_closed(self) -> None:
        """Block until :meth:`request_shutdown` is called."""
        await self._closed.wait()

    async def stop(self) -> None:
        """Close sockets, drop ``daemon.json`` and release the port."""
        self.request_shutdown(self.shutdown_reason or "requested")
        if self.core is not None:
            await self.core.lifecycle.stop()
        if self.app is not None:
            sockets: set[web.WebSocketResponse] = self.app[SOCKETS_KEY]
            connections: set[RpcConnection] = self.app[CONNECTIONS_KEY]
            for ws in list(sockets):
                with contextlib.suppress(Exception):
                    await ws.close()
            for conn in list(connections):
                await conn.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self.core is not None and self.core.store is not None:
            with contextlib.suppress(Exception):
                self.core.store.close()
        self._remove_daemon_json()
        log.info("snowpea daemon stopped (%s)", self.shutdown_reason)


def _resolve_port(runner: web.AppRunner, requested: int) -> int:
    for address in runner.addresses:
        if isinstance(address, tuple) and len(address) >= 2:
            return int(address[1])
    return requested


def _configure_logging(paths: Paths) -> None:
    logger = logging.getLogger("snowpea")
    target = str(paths.daemon_log)
    for handler in logger.handlers:
        if getattr(handler, "baseFilename", None) == target:
            return
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _install_signal_handlers(daemon: Daemon) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, daemon.request_shutdown, f"signal:{sig.name}")


async def run_daemon(port: int = 0, home: Path | str | None = None) -> None:
    """Run the daemon until a signal, ``system.shutdown`` or the idle timeout."""
    daemon = Daemon(port=port, home=home)
    await daemon.start()
    _install_signal_handlers(daemon)
    try:
        await daemon.wait_closed()
    finally:
        await daemon.stop()


__all__ = ["HOST", "Core", "Daemon", "build_dispatcher", "run_daemon"]
