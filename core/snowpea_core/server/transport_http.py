"""HTTP transport: health, version and the generated protocol schema.

aiohttp owns the listening socket, so the WebSocket endpoint is mounted on the
same port at :data:`~snowpea_core.server.transport_ws.WS_PATH` instead of
running a second ``websockets`` server.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
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


#: Where the snowpea web build is served from (M18 4.2).
WEB_PATH = "/web/"
#: Packaged copy of the web build, filled at release time.
PACKAGED_WEB = Path(__file__).resolve().parent.parent / "web_dist"


def web_root() -> Path | None:
    """The snowpea web build: ``SNOWPEA_WEB_DIST``, else the packaged copy."""
    override = os.environ.get("SNOWPEA_WEB_DIST", "").strip()
    for candidate in ([Path(override)] if override else []) + [PACKAGED_WEB]:
        if (candidate / "index.html").is_file():
            return candidate
    return None


async def web_static(request: web.Request) -> web.StreamResponse:
    """``GET /web/...`` — the snowpea web build (M18 4.2).

    The files carry no secrets: the page reads its token from the address the
    ``snowpea web`` command printed, and every WebSocket still has to say hello
    with it.  ``index.html`` is never cached, hashed assets are.
    """
    root = web_root()
    if root is None:
        return web.Response(
            status=404,
            text=(
                "snowpea web is not part of this build. Build it in snowpea-ide with "
                "`npm run build:web` and start the daemon with SNOWPEA_WEB_DIST=<out/web>."
            ),
        )
    tail = request.match_info.get("tail", "") or "index.html"
    target = (root / tail).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise web.HTTPNotFound()
    if not target.is_file():
        # A client-side route: hand back the app.
        target = root / "index.html"
    headers = {"X-Content-Type-Options": "nosniff"}
    if target.name == "index.html":
        headers["Cache-Control"] = "no-cache"
    elif "/assets/" in target.as_posix():
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    kind, _ = mimetypes.guess_type(target.name)
    headers["Content-Type"] = kind or "application/octet-stream"
    return web.FileResponse(target, headers=headers)


async def web_redirect(request: web.Request) -> web.Response:
    # Keep the query: it carries the token the page needs.
    query = request.query_string
    raise web.HTTPFound(WEB_PATH + (f"?{query}" if query else ""))


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
    app.router.add_get("/web", web_redirect)
    app.router.add_get(WEB_PATH + "{tail:.*}", web_static)
    return app


__all__ = [
    "CORE_KEY",
    "DAEMON_KEY",
    "create_app",
    "health",
    "protocol_json",
    "version",
    "web_root",
    "web_static",
]
