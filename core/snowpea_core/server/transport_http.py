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
from yarl import URL

from snowpea_core.server.auth import token_matches
from snowpea_core.server.protocol import PROTOCOL_VERSION, SERVER_VERSION, dump_schema
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.server.transport_ws import (
    CONNECTIONS_KEY,
    DISPATCHER_KEY,
    PENDING_KEY,
    SOCKETS_KEY,
    WS_PATH,
    websocket_handler,
)  # re-exported so app_server imports its app keys from one place

#: Set by ``app_server`` so routes can reach the daemon/core if they need to.
DAEMON_KEY: web.AppKey[Any] = web.AppKey("daemon", object)
CORE_KEY: web.AppKey[Any] = web.AppKey("core", object)
#: The address the listener is bound to (``127.0.0.1`` unless ``--host`` said otherwise).
BIND_HOST_KEY: web.AppKey[str] = web.AppKey("bind_host", str)

#: Names of this machine's loopback interface, as a ``Host`` or ``Origin`` spells them.
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})

#: The page's own meta CSP, with ``connect-src`` narrowed to this origin.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; frame-src data: blob:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
#: Every snowpea web response carries these, on the loopback and remote listeners alike.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}


def _host_port(text: str, scheme: str) -> tuple[str, int | None] | None:
    """``(host, port)`` of a ``Host`` value or an origin (``None``: not parseable)."""
    try:
        url = URL(text) if "://" in text else URL(f"{scheme}://{text}")
    except (ValueError, TypeError):
        return None
    if not url.host:
        return None
    return url.host.lower().rstrip("."), url.port


def peer_refusal(request: web.Request, bind_host: str) -> str | None:
    """Why a request to the daemon's own listener is refused (security review 9b, M1).

    ``"host"`` — DNS rebinding: bound to loopback, the ``Host`` must name the
    loopback interface (``127.0.0.1``, ``localhost`` or ``[::1]``, any port).
    A listener bound elsewhere with ``--host`` keeps the token as its only check.

    ``"origin"`` — a cross-site page: an ``http(s)`` ``Origin`` must be this
    listener itself — a loopback name at the daemon's own port, or the very
    address the request was sent to (the web UI the daemon serves, also through
    an ``ssh -L`` tunnel).  No ``Origin`` (the SDK, TUI, CLI, desktop app and
    browser host), ``null`` and non-web schemes (``file:``, ``app:``,
    ``chrome-extension:``, ``vscode-webview:``) are not a web page and pass.
    """
    raw_host = request.headers.get("Host")
    host = _host_port(raw_host, request.scheme) if raw_host else None
    if raw_host and bind_host in LOOPBACK_NAMES and (host is None or host[0] not in LOOPBACK_NAMES):
        return "host"
    origin = request.headers.get("Origin")
    if origin is None:
        return None
    scheme = origin.strip().split(":", 1)[0].lower()
    if scheme not in ("http", "https"):
        return None
    said = _host_port(origin.strip(), scheme)
    if said is None:
        return "origin"
    if host is not None and said == host:
        return None
    sockname = request.transport.get_extra_info("sockname") if request.transport else None
    own_port = sockname[1] if isinstance(sockname, tuple) and len(sockname) >= 2 else None
    if said[0] in LOOPBACK_NAMES and own_port is not None and said[1] == own_port:
        return None
    return "origin"


@web.middleware
async def _peer_guard(request: web.Request, handler: Any) -> web.StreamResponse:
    refusal = peer_refusal(request, request.app[BIND_HOST_KEY])
    if refusal is not None:
        what = "Host" if refusal == "host" else "Origin"
        return web.Response(status=403, text=f"snowpea: this {what} may not use the daemon",
                            headers={"Cache-Control": "no-store"})
    return await handler(request)


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
    # Security review 9b: no Referer from the page (its address carries the token).
    headers = dict(SECURITY_HEADERS)
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


async def web_not_here(_request: web.Request) -> web.Response:
    """The remote listener's login routes do not exist on loopback.

    Without this the client-route fallback would answer ``/web/session`` with
    the page, and a new path that hands out the token must never exist here.
    """
    raise web.HTTPNotFound()


async def web_remote_status(request: web.Request) -> web.Response:
    """``GET /web/remote/status`` (loopback, ``Authorization: Bearer <token>``).

    What ``snowpea web status`` shows: the remote listener's state as this
    daemon started it, and how many login sessions are open.
    """
    core = request.app[CORE_KEY]
    presented = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if core is None or not token_matches(core.token, presented):
        raise web.HTTPUnauthorized()
    daemon = request.app[DAEMON_KEY]
    status = dict(getattr(daemon, "remote_status", None) or {"enabled": False})
    auth = getattr(daemon, "remote_auth", None)
    status["sessions"] = auth.active() if auth is not None else 0
    return web.json_response(status, headers={"Cache-Control": "no-store"})


def create_app(
    dispatcher: RpcDispatcher, daemon: Any | None = None, core: Any | None = None,
    *, bind_host: str = "127.0.0.1",
) -> web.Application:
    """Build the aiohttp application that serves both HTTP and the WS endpoint."""
    app = web.Application(middlewares=[_peer_guard])
    app[DISPATCHER_KEY] = dispatcher
    app[CONNECTIONS_KEY] = set[RpcConnection]()
    app[SOCKETS_KEY] = set[web.WebSocketResponse]()
    app[PENDING_KEY] = {}
    app[BIND_HOST_KEY] = bind_host or "127.0.0.1"
    app[DAEMON_KEY] = daemon
    app[CORE_KEY] = core
    app.router.add_get("/health", health)
    app.router.add_get("/version", version)
    app.router.add_get("/protocol.json", protocol_json)
    app.router.add_get(WS_PATH, websocket_handler)
    app.router.add_get("/web", web_redirect)
    app.router.add_get(WEB_PATH + "remote/status", web_remote_status)
    for name in ("session", "login", "logout"):
        app.router.add_route("*", WEB_PATH + name, web_not_here)
    app.router.add_get(WEB_PATH + "{tail:.*}", web_static)
    return app


__all__ = [
    "BIND_HOST_KEY",
    "CORE_KEY",
    "LOOPBACK_NAMES",
    "SECURITY_HEADERS",
    "peer_refusal",
    "DAEMON_KEY",
    "create_app",
    "health",
    "protocol_json",
    "version",
    "web_root",
    "web_static",
]
