"""HTTP transport: health, version and the generated protocol schema.

aiohttp owns the listening socket, so the WebSocket endpoint is mounted on the
same port at :data:`~snowpea_core.server.transport_ws.WS_PATH` instead of
running a second ``websockets`` server.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from snowpea_core.server.protocol import PROTOCOL_VERSION, SERVER_VERSION, dump_schema
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.server.transport_ws import (
    CONNECTIONS_KEY,
    DISPATCHER_KEY,
    SOCKETS_KEY,
    WS_PATH,
    websocket_handler,
)  # re-exported so app_server imports its app keys from one place

#: Set by ``app_server`` so routes can reach the daemon/core if they need to.
DAEMON_KEY: web.AppKey[Any] = web.AppKey("daemon", object)
CORE_KEY: web.AppKey[Any] = web.AppKey("core", object)


async def health(_request: web.Request) -> web.Response:
    """``GET /health``."""
    return web.json_response({"status": "ok"})


async def version(_request: web.Request) -> web.Response:
    """``GET /version``."""
    return web.json_response({"version": SERVER_VERSION, "protocolVersion": PROTOCOL_VERSION})


async def protocol_json(_request: web.Request) -> web.Response:
    """``GET /protocol.json`` — the full schema dump."""
    return web.json_response(dump_schema())


def create_app(
    dispatcher: RpcDispatcher, daemon: Any | None = None, core: Any | None = None
) -> web.Application:
    """Build the aiohttp application that serves both HTTP and the WS endpoint."""
    app = web.Application()
    app[DISPATCHER_KEY] = dispatcher
    app[CONNECTIONS_KEY] = set[RpcConnection]()
    app[SOCKETS_KEY] = set[web.WebSocketResponse]()
    app[DAEMON_KEY] = daemon
    app[CORE_KEY] = core
    app.router.add_get("/health", health)
    app.router.add_get("/version", version)
    app.router.add_get("/protocol.json", protocol_json)
    app.router.add_get(WS_PATH, websocket_handler)
    return app


__all__ = [
    "CORE_KEY",
    "DAEMON_KEY",
    "create_app",
    "health",
    "protocol_json",
    "version",
]
