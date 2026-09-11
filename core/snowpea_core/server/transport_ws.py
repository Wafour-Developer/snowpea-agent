"""WebSocket transport.

The daemon serves a single aiohttp application on ``127.0.0.1:<port>``; the
JSON-RPC endpoint is ``ws://127.0.0.1:<port>/ws`` (contract §1 note).  One
:class:`~snowpea_core.server.rpc.RpcConnection` is created per socket.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from snowpea_core.server import errors
from snowpea_core.server.auth import token_matches
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    CAPABILITIES,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    HelloParams,
    HelloResult,
    protocol_major,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.ws")

WS_PATH = "/ws"

DISPATCHER_KEY: web.AppKey[RpcDispatcher] = web.AppKey("dispatcher", RpcDispatcher)
CONNECTIONS_KEY: web.AppKey[set[RpcConnection]] = web.AppKey("connections", set)
SOCKETS_KEY: web.AppKey[set[web.WebSocketResponse]] = web.AppKey("sockets", set)


async def hello_handler(conn: RpcConnection, params: HelloParams, core: Core) -> HelloResult:
    """``system.hello``: authenticate the socket and agree on the protocol."""
    if protocol_major(params.protocolVersion) != protocol_major(PROTOCOL_VERSION):
        raise RpcError(
            errors.PROTOCOL_INCOMPATIBLE,
            f"client protocol {params.protocolVersion} != server {PROTOCOL_VERSION}",
        )
    if not token_matches(core.token, params.token):
        raise RpcError(errors.UNAUTHORIZED, "invalid token")
    conn.authenticated = True
    conn.client_version = params.clientVersion
    log.info("client %s authenticated (surface %s)", params.clientVersion, conn.surface_id)
    return HelloResult(
        protocolVersion=PROTOCOL_VERSION,
        serverVersion=SERVER_VERSION,
        capabilities=list(CAPABILITIES),
    )


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """aiohttp route handler: bridge one socket to the RPC dispatcher."""
    dispatcher = request.app[DISPATCHER_KEY]
    connections = request.app[CONNECTIONS_KEY]
    sockets = request.app[SOCKETS_KEY]

    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    sockets.add(ws)

    async def send(obj: dict[str, Any]) -> None:
        if not ws.closed:
            await ws.send_json(obj)

    conn = RpcConnection(send, peer=str(request.remote or "local"))
    connections.add(conn)
    hub = getattr(dispatcher.core, "hub", None)
    try:
        async for msg in ws:
            if msg.type is WSMsgType.TEXT:
                await dispatcher.handle_message(conn, msg.data)
            elif msg.type is WSMsgType.BINARY:
                await dispatcher.handle_message(conn, msg.data.decode("utf-8", "replace"))
            elif msg.type is WSMsgType.ERROR:  # pragma: no cover - transport error
                log.warning("websocket error: %s", ws.exception())
    finally:
        connections.discard(conn)
        sockets.discard(ws)
        if hub is not None and hasattr(hub, "unsubscribe"):
            hub.unsubscribe(conn)
        await conn.close()
    return ws


__all__ = [
    "CONNECTIONS_KEY",
    "DISPATCHER_KEY",
    "SOCKETS_KEY",
    "WS_PATH",
    "hello_handler",
    "websocket_handler",
]
