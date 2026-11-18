"""WebSocket transport.

The daemon serves a single aiohttp application on ``127.0.0.1:<port>``; the
JSON-RPC endpoint is ``ws://127.0.0.1:<port>/ws`` (contract §1 note).  One
:class:`~snowpea_core.server.rpc.RpcConnection` is created per socket.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from snowpea_core.machine import machine_of
from snowpea_core.server import errors
from snowpea_core.server.auth import token_matches
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    CAPABILITIES,
    MODEL_CATEGORIES,
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
#: This listener's sockets still without a successful ``system.hello``, oldest first.
PENDING_KEY: web.AppKey[dict[web.WebSocketResponse, RpcConnection]] = web.AppKey(
    "pending_sockets", dict
)


class PreAuthGate:
    """Stands in front of aiohttp's frame parser while the socket is unauthenticated.

    aiohttp buffers a whole frame (up to ``max_msg_size``) before the handler
    sees it, and that limit cannot be changed once the socket is open; so the
    bytes are counted HERE, as they arrive, and a socket that sends more than
    :data:`PRE_AUTH_MAX_BYTES` before its hello succeeds is closed (1009)
    before the rest reaches the parser.  Once authenticated it only forwards.
    """

    def __init__(self, inner: Any, conn: RpcConnection, transport: Any) -> None:
        self.inner = inner
        self._conn = conn
        self._transport = transport
        self._seen = 0
        self.overflowed = False

    def feed_data(self, data: Any) -> Any:
        if not self._conn.authenticated:
            if self.overflowed:
                return False, b""
            self._seen += len(data)
            if self._seen > PRE_AUTH_MAX_BYTES:
                self.overflowed = True
                _close_raw(self._transport, CLOSE_TOO_BIG, b"too much data before system.hello")
                return False, b""
        return self.inner.feed_data(data)

    def feed_eof(self) -> Any:
        return self.inner.feed_eof()


def _close_raw(transport: Any, code: int, reason: bytes) -> None:
    """Write a server close frame and drop the connection (from sync code)."""
    if transport is None or transport.is_closing():
        return
    payload = code.to_bytes(2, "big") + reason[:123]
    transport.write(bytes((0x88, len(payload))) + payload)
    transport.close()


def install_pre_auth_gate(request: web.Request, conn: RpcConnection) -> PreAuthGate | None:
    """Put a :class:`PreAuthGate` in front of the socket's frame parser.

    aiohttp keeps the parser at ``request.protocol._payload_parser`` (3.9-3.14);
    ``tests/test_security_review9b_peers.py`` fails if that changes, and the
    handler falls back to checking each frame's size after aiohttp read it.
    """
    protocol = request.protocol
    inner = getattr(protocol, "_payload_parser", None)
    if inner is None or not hasattr(inner, "feed_data") or not hasattr(inner, "feed_eof"):
        log.warning("cannot limit unauthenticated websocket input on this aiohttp version")
        return None
    gate = PreAuthGate(inner, conn, request.transport)
    protocol._payload_parser = gate
    return gate


#: Largest frame a client may send: a prompt carrying five 20 MB attachments
#: as base64 (~134 MB) fits; a bigger one closes the socket with 1009.
MAX_MESSAGE_BYTES = 160 * 1024 * 1024
#: Security review 9b, M1 — a socket that has not said ``system.hello`` yet is
#: nobody: everything it sends before then (a hello is a few hundred bytes) may
#: come to this many bytes in all; more closes it with 1009 before any of it is
#: buffered as a frame or parsed.
PRE_AUTH_MAX_BYTES = 64 * 1024
#: Seconds a socket has to authenticate; then it is closed (1008).
HELLO_DEADLINE_SEC = 10.0
#: Sockets of one listener that have not authenticated yet; a new one beyond
#: this closes the OLDEST (1008), so a flood cannot keep a person's own client out.
MAX_UNAUTHENTICATED = 64
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
#: Closes started from a timer or for another socket, held until they finish.
_CLOSING: set[asyncio.Task[Any]] = set()
#: What every connection through the remote listener is, whatever its hello says.
REMOTE_CLIENT_KIND = "web"


def _advertised_capabilities() -> list[str]:
    """``CAPABILITIES``, less 개선하기 until its server takes core's reports."""
    from snowpea_core.feedback.sender import advertised

    caps = list(CAPABILITIES)
    if not advertised():
        caps = [c for c in caps if c != "improvementFeedback"]
    return caps


async def hello_handler(conn: RpcConnection, params: HelloParams, core: Core) -> HelloResult:
    """``system.hello``: authenticate the socket and agree on the protocol."""
    if protocol_major(params.protocolVersion) != protocol_major(PROTOCOL_VERSION):
        raise RpcError(
            errors.PROTOCOL_INCOMPATIBLE,
            f"client protocol {params.protocolVersion} != server {PROTOCOL_VERSION}",
        )
    capability = getattr(conn, "remote_capability", None)
    if getattr(conn, "remote_login", False):
        # The remote listener (security review 5, H3): only this login's own
        # capability, never the daemon token, and nothing once the login ended.
        if capability is None or not capability(params.token):
            raise RpcError(errors.UNAUTHORIZED, "invalid token")
    elif not token_matches(core.token, params.token):
        raise RpcError(errors.UNAUTHORIZED, "invalid token")
    if getattr(conn, "remote_login", False):
        # Security review 5, M3: on the remote listener the connection is a
        # remote web login whatever hello declares — never a (legacy) browser
        # host, a connector or a local app — and its client id is its own
        # namespace, so it cannot re-bind another client's sessions.
        updates: dict[str, Any] = {"clientKind": REMOTE_CLIENT_KIND}
        if params.clientId:
            updates["clientId"] = f"remote:{params.clientId}"
        params = params.model_copy(update=updates)
    if conn.authenticated and params.clientKind != conn.client_kind:
        # What a connection is (a person's app, a browser host, the connector)
        # is decided once: a later hello may not turn it into something else.
        raise RpcError(
            errors.INVALID_PARAMS,
            f"clientKind is fixed for this connection ({conn.client_kind or 'none'}); "
            "open a new connection to be another client",
        )
    conn.authenticated = True
    conn.client_version = params.clientVersion
    from snowpea_core.server.host_handlers import adopt_hello

    adopt_hello(core, conn, params)
    # Authenticated surfaces share the unattended approval queue (M5 §4), so
    # they receive its notifications whether or not they own a session.
    hub = getattr(core, "hub", None)
    if hub is not None and hasattr(hub, "register_client"):
        hub.register_client(conn)
    log.info("client %s authenticated (surface %s)", params.clientVersion, conn.surface_id)
    from snowpea_core.server.person_gate import connection_kind

    # Security review 8, Low: a local person's surface hears at once that
    # settings.json did not validate (the sections in question run degraded).
    degraded = (
        list(getattr(getattr(core, "settings", None), "degraded_sections", list)())
        if connection_kind(conn) == "person" else []
    )
    return HelloResult(
        protocolVersion=PROTOCOL_VERSION,
        serverVersion=SERVER_VERSION,
        capabilities=_advertised_capabilities(),
        modelCategories=list(MODEL_CATEGORIES),
        machine=machine_of(core),
        settingsDegraded=degraded,
    )


async def websocket_handler(
    request: web.Request,
    *,
    track: set[web.WebSocketResponse] | None = None,
    capability: Callable[[str | None], bool] | None = None,
    alive: Callable[[], bool] | None = None,
    push_alive: Callable[[], bool] | None = None,
    reauth: Callable[[Any], Any] | None = None,
) -> web.WebSocketResponse:
    """aiohttp route handler: bridge one socket to the RPC dispatcher.

    ``track`` also holds the socket while it is open (the remote listener's
    login session, so logging out closes it); ``capability`` checks what its
    ``system.hello`` presents (that login's capability, never the daemon token).
    ``alive`` (security review 7, Low 3) says whether that login still stands:
    asked before every frame is dispatched, so an expired login's socket takes
    no further call — it used to keep working until the next sweep (up to 60 s).
    ``push_alive`` (security review 8, Low): a cheap expiry check asked before
    every message the daemon pushes, so an expired login also stops RECEIVING
    session events at once (the socket closes with 4401).
    """
    dispatcher = request.app[DISPATCHER_KEY]
    connections = request.app[CONNECTIONS_KEY]
    sockets = request.app[SOCKETS_KEY]

    ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=MAX_MESSAGE_BYTES)
    await ws.prepare(request)
    sockets.add(ws)
    if track is not None:
        track.add(ws)

    async def send(obj: dict[str, Any]) -> None:
        if ws.closed:
            return
        if push_alive is not None and not push_alive():
            await ws.close(code=4401, message=b"logged out")
            return
        await ws.send_json(obj)

    conn = RpcConnection(send, peer=str(request.remote or "local"))
    conn.remote_login = track is not None  # only the remote listener passes ``track``
    conn.remote_capability = capability if conn.remote_login else None
    # 1.56.0: the remote password checked again for this login (jarvis/remote_grant.py).
    conn.remote_reauth = reauth if conn.remote_login else None
    connections.add(conn)
    hub = getattr(dispatcher.core, "hub", None)
    # Security review 9b, M1: limits on a socket that has not authenticated.
    gate = install_pre_auth_gate(request, conn)

    def close_soon(target: web.WebSocketResponse, code: int, reason: bytes) -> None:
        task = asyncio.ensure_future(target.close(code=code, message=reason))
        _CLOSING.add(task)
        task.add_done_callback(_CLOSING.discard)

    def hello_overdue() -> None:
        if not conn.authenticated and not ws.closed:
            log.info("closing a websocket from %s: no system.hello in %.0f s",
                     conn.peer, HELLO_DEADLINE_SEC)
            close_soon(ws, CLOSE_POLICY, b"system.hello timed out")

    deadline = asyncio.get_running_loop().call_later(HELLO_DEADLINE_SEC, hello_overdue)
    pending = request.app.get(PENDING_KEY)
    if pending is not None:
        for other, other_conn in list(pending.items()):
            if other_conn.authenticated or other.closed:
                pending.pop(other, None)
        pending[ws] = conn
        while len(pending) > MAX_UNAUTHENTICATED:
            oldest = next(iter(pending))
            pending.pop(oldest)
            close_soon(oldest, CLOSE_POLICY, b"too many unauthenticated connections")
    try:
        async for msg in ws:
            if alive is not None and not alive():
                await ws.close(code=4401, message=b"logged out")
                break
            if not conn.authenticated and msg.type in (WSMsgType.TEXT, WSMsgType.BINARY) \
                    and len(msg.data) > PRE_AUTH_MAX_BYTES:
                # Only reached when the gate could not be installed.
                await ws.close(code=CLOSE_TOO_BIG, message=b"too much data before system.hello")
                break
            if msg.type is WSMsgType.TEXT:
                await dispatcher.handle_message(conn, msg.data)
            elif msg.type is WSMsgType.BINARY:
                await dispatcher.handle_message(conn, msg.data.decode("utf-8", "replace"))
            elif msg.type is WSMsgType.ERROR:  # pragma: no cover - transport error
                log.warning("websocket error: %s", ws.exception())
    finally:
        deadline.cancel()
        if pending is not None:
            pending.pop(ws, None)
        if gate is not None and gate.overflowed:
            log.info("closed a websocket from %s: over %d bytes before system.hello",
                     conn.peer, PRE_AUTH_MAX_BYTES)
        connections.discard(conn)
        sockets.discard(ws)
        if track is not None:
            track.discard(ws)
        if hub is not None and hasattr(hub, "unsubscribe"):
            hub.unsubscribe(conn)
        await conn.close()
    return ws


__all__ = [
    "CONNECTIONS_KEY",
    "DISPATCHER_KEY",
    "PENDING_KEY",
    "PRE_AUTH_MAX_BYTES",
    "SOCKETS_KEY",
    "WS_PATH",
    "hello_handler",
    "websocket_handler",
]
