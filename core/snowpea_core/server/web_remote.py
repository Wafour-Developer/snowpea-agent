"""snowpea web from another machine: a second, password-protected listener.

The loopback listener hands the page its daemon token in the address
``snowpea web`` printed.  Another machine cannot be given that address, so
when ``web.remote.enabled`` is on the daemon also binds ``web.remote.host:port``
with a much smaller app:

* ``GET /web/...`` — the same static build;
* ``POST /web/login`` — ``{username, password}`` → a ``snowpea_web`` cookie;
* ``GET /web/session`` — with that cookie, the daemon token for ``system.hello``;
* ``POST /web/logout`` — drops the session and closes its sockets;
* ``GET /ws`` — the JSON-RPC socket, only with a valid cookie and a same-origin
  ``Origin``; ``system.hello`` still checks the token.

The password lives in ``credentials.json`` as a salted scrypt hash under
:data:`PASSWORD_KEY`.  Sessions are kept in memory, so a daemon restart logs
everyone out.
Changing or clearing the password also ends every open session.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from snowpea_core.config.credentials import CredentialStore
from snowpea_core.config.paths import Paths
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.server.transport_http import WEB_PATH, web_static
from snowpea_core.server.transport_ws import (
    CONNECTIONS_KEY,
    DISPATCHER_KEY,
    SOCKETS_KEY,
    WS_PATH,
    websocket_handler,
)

log = logging.getLogger("snowpea.web.remote")

#: ``credentials.json`` key of the password hash (never in settings.json).
PASSWORD_KEY = "web.remote.passwordHash"
COOKIE_NAME = "snowpea_web"

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
#: n=2**15, r=8 needs 32 MiB, exactly hashlib's default ceiling, which it refuses.
SCRYPT_MAXMEM = 64 * 1024 * 1024

#: Every failed login answers after about this long, right or wrong username.
FAILURE_DELAY_SEC = 0.3
RATE_WINDOW_SEC = 300.0
MAX_FAILURES_PER_IP = 5
MAX_FAILURES_GLOBAL = 20
#: A login body is two short strings.
MAX_BODY_BYTES = 4096
SWEEP_INTERVAL_SEC = 60.0

#: The built page's own meta CSP, with ``connect-src`` narrowed to this origin.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; frame-src data: blob:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}


# ---------------------------------------------------------------------------
# password hash
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32
    )


def hash_password(password: str) -> str:
    """``scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>``."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against :func:`hash_password` output."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def stored_hash(paths: Paths) -> str:
    value = CredentialStore(paths).load().get(PASSWORD_KEY)
    return value if isinstance(value, str) else ""


def _fingerprint(stored: str) -> str:
    return hashlib.sha256(stored.encode("utf-8")).hexdigest() if stored else ""


def set_password(paths: Paths, password: str) -> None:
    CredentialStore(paths).set(PASSWORD_KEY, hash_password(password))


def clear_password(paths: Paths) -> bool:
    store = CredentialStore(paths)
    data = store.load()
    if PASSWORD_KEY not in data:
        return False
    del data[PASSWORD_KEY]
    store.save(data)
    return True


def config_problem(remote: Any, paths: Paths) -> str | None:
    """Why remote access cannot start, or ``None`` when it can.

    Owner decision 2b (2026-11-16): a password is no longer required — without
    one the listener admits the web access token's link.  A password without a
    username is the one broken shape.
    """
    if stored_hash(paths) and not str(remote.username or "").strip():
        return "a password is set but web.remote.username is empty"
    return None


# ---------------------------------------------------------------------------
# the web access token (owner decision 2b)
# ---------------------------------------------------------------------------

#: ``credentials.json`` key of the web access token (never the daemon token).
ACCESS_TOKEN_KEY = "web.remote.accessToken"
#: Query parameter the access link carries; the first request trades it for a cookie.
TOKEN_PARAM = "t"


def access_token(paths: Paths, *, create: bool = True) -> str:
    """The web access token (32 random bytes, urlsafe), made on first use."""
    store = CredentialStore(paths)
    value = store.load().get(ACCESS_TOKEN_KEY)
    if isinstance(value, str) and len(value) >= 43:
        return value
    if not create:
        return ""
    return rotate_access_token(paths)


def rotate_access_token(paths: Paths) -> str:
    """A new access token; every session opened with any credential ends (fingerprint)."""
    token = secrets.token_urlsafe(32)
    CredentialStore(paths).set(ACCESS_TOKEN_KEY, token)
    return token


def password_mode(remote_username: str, paths: Paths) -> bool:
    """A username and a password are set: the login page, never the token link alone."""
    return bool(str(remote_username or "").strip()) and bool(stored_hash(paths))


# ---------------------------------------------------------------------------
# sessions and rate limits
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    username: str
    expires: float
    fingerprint: str = ""
    sockets: set[web.WebSocketResponse] = field(default_factory=set)


class RemoteAuth:
    """In-memory login sessions and failure counters for one daemon run."""

    def __init__(self, username: str, session_hours: float, paths: Paths) -> None:
        self.username = username
        self.session_sec = max(1.0, float(session_hours) * 3600.0)
        self.paths = paths
        self.sessions: dict[str, _Session] = {}
        self._ip_failures: dict[str, deque[float]] = {}
        self._global_failures: deque[float] = deque()
        self.failure_delay = FAILURE_DELAY_SEC
        self.clock = time.monotonic
        self._hash_stamp: tuple[int, int, int] | None = None
        self._hash_print = ""

    def _current_fingerprint(self) -> str:
        """Fingerprint of the stored hash and the access token (a change of either ends
        every session); the file is re-read only when it changed."""
        try:
            st = CredentialStore(self.paths).path.stat()
            stamp: tuple[int, int, int] | None = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            stamp = None
        if stamp is None or stamp != self._hash_stamp:
            stored = stored_hash(self.paths)
            token = access_token(self.paths, create=False)
            self._hash_print = _fingerprint(f"{stored}|{token}") if (stored or token) else ""
            self._hash_stamp = stamp
        return self._hash_print

    @property
    def password_mode(self) -> bool:
        return password_mode(self.username, self.paths)

    def check_token(self, presented: str) -> bool:
        """Constant-time check of the access link's token (token mode only)."""
        expected = access_token(self.paths, create=False)
        given = hashlib.sha256(presented.encode("utf-8")).digest()
        wanted = hashlib.sha256((expected or secrets.token_urlsafe(32)).encode("utf-8")).digest()
        return hmac.compare_digest(given, wanted) and bool(expected) and not self.password_mode

    # -- sessions --------------------------------------------------------
    def open_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions[token] = _Session(
            self.username, self.clock() + self.session_sec, self._current_fingerprint()
        )
        return token

    def session(self, token: str | None) -> _Session | None:
        if not token:
            return None
        found = self.sessions.get(token)
        if found is None:
            return None
        if found.expires <= self.clock():
            self.sessions.pop(token, None)
            return None
        current = self._current_fingerprint()
        if not current or current != found.fingerprint:
            # The password was changed or cleared: end the session like a logout.
            self.sessions.pop(token, None)
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(_close_all(found.sockets))
            return None
        return found

    async def drop(self, token: str) -> None:
        found = self.sessions.pop(token, None)
        if found is not None:
            await _close_all(found.sockets)

    async def sweep(self) -> None:
        now = self.clock()
        for token in [t for t, s in self.sessions.items() if s.expires <= now]:
            await self.drop(token)
        for token in list(self.sessions):
            self.session(token)
        # A client that stopped failing leaves an empty queue; drop it.
        for ip, queue in list(self._ip_failures.items()):
            self._trim(queue)
            if not queue:
                del self._ip_failures[ip]

    def active(self) -> int:
        return sum(1 for token in list(self.sessions) if self.session(token) is not None)

    # -- rate limits -----------------------------------------------------
    def _trim(self, queue: deque[float]) -> None:
        cutoff = self.clock() - RATE_WINDOW_SEC
        while queue and queue[0] <= cutoff:
            queue.popleft()

    def retry_after(self, ip: str) -> int:
        """Seconds until ``ip`` may try again, 0 when it may now."""
        waits: list[float] = []
        queue = self._ip_failures.get(ip)
        if queue is not None:
            self._trim(queue)
            if len(queue) >= MAX_FAILURES_PER_IP:
                waits.append(queue[-MAX_FAILURES_PER_IP] + RATE_WINDOW_SEC - self.clock())
        self._trim(self._global_failures)
        if len(self._global_failures) >= MAX_FAILURES_GLOBAL:
            waits.append(
                self._global_failures[-MAX_FAILURES_GLOBAL] + RATE_WINDOW_SEC - self.clock()
            )
        return max(1, int(max(waits) + 0.999)) if waits else 0

    def record_failure(self, ip: str) -> float:
        now = self.clock()
        self._ip_failures.setdefault(ip, deque()).append(now)
        self._global_failures.append(now)
        return now

    def forgive(self, ip: str, stamp: float) -> None:
        """Take back the failure :meth:`record_failure` counted for an attempt that passed."""
        for queue in (self._ip_failures.get(ip), self._global_failures):
            with contextlib.suppress(ValueError):
                if queue is not None:
                    queue.remove(stamp)

    def check(self, username: str, password: str) -> bool:
        """Constant-time username and password check; never says which failed."""
        # Digests first, so not even the length of the username shows in the timing.
        user_ok = hmac.compare_digest(
            hashlib.sha256(username.encode("utf-8")).digest(),
            hashlib.sha256(self.username.encode("utf-8")).digest(),
        )
        stored = stored_hash(self.paths)
        # Hash even with a wrong username or no password so the timing is the same.
        pass_ok = verify_password(password, stored or hash_placeholder())
        return user_ok and pass_ok and bool(stored)


_PLACEHOLDER: list[str] = []


def hash_placeholder() -> str:
    if not _PLACEHOLDER:
        _PLACEHOLDER.append(hash_password(secrets.token_urlsafe(16)))
    return _PLACEHOLDER[0]


async def _close_all(sockets: set[web.WebSocketResponse]) -> None:
    for ws in list(sockets):
        with contextlib.suppress(Exception):
            await ws.close(code=4401, message=b"logged out")


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

AUTH_KEY: web.AppKey[RemoteAuth] = web.AppKey("remote_auth", RemoteAuth)
TRUST_PROXY_KEY: web.AppKey[bool] = web.AppKey("remote_trust_proxy", bool)
ORIGINS_KEY: web.AppKey[frozenset[str]] = web.AppKey("remote_origins", frozenset)


def _is_https(request: web.Request) -> bool:
    if request.secure:
        return True
    if request.app[TRUST_PROXY_KEY]:
        proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        return proto == "https"
    return False


def _client_ip(request: web.Request) -> str:
    if request.app[TRUST_PROXY_KEY]:
        forwarded = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",")]
        forwarded = [p for p in forwarded if p]
        if forwarded:
            # The proxy appends the address it saw; earlier entries are the client's say-so.
            return forwarded[-1]
    return str(request.remote or "unknown")


def _origin_ok(request: web.Request) -> bool:
    origin = request.headers.get("Origin", "").rstrip("/")
    if not origin:
        return False
    own = f"{'https' if _is_https(request) else 'http'}://{request.host}"
    return origin == own or origin in request.app[ORIGINS_KEY]


def _json(payload: dict[str, Any], status: int = 200, **headers: str) -> web.Response:
    return web.json_response(
        payload, status=status, headers={"Cache-Control": "no-store", **headers}
    )


async def login(request: web.Request) -> web.Response:
    auth = request.app[AUTH_KEY]
    ip = _client_ip(request)
    if request.headers.get("Origin") and not _origin_ok(request):
        return _json({"ok": False, "error": "bad_origin"}, 403)
    wait = auth.retry_after(ip)
    if wait:
        log.warning("web login refused from %s: too many failures (retry in %ds)", ip, wait)
        return _json({"ok": False, "error": "rate_limited"}, 429, **{"Retry-After": str(wait)})
    # Counted at once (no await since the check above), so attempts sent in
    # parallel cannot all slip under the limit; a good password takes it back.
    stamp = auth.record_failure(ip)
    started = time.monotonic()
    if request.content_type != "application/json":
        return _json({"ok": False, "error": "bad_request"}, 400)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        body = None
    username = body.get("username") if isinstance(body, dict) else None
    password = body.get("password") if isinstance(body, dict) else None
    if not isinstance(username, str) or not isinstance(password, str):
        return _json({"ok": False, "error": "bad_request"}, 400)
    ok = await asyncio.to_thread(auth.check, username, password)
    if not ok:
        log.warning("web login failed from %s", ip)
        await asyncio.sleep(max(0.0, auth.failure_delay - (time.monotonic() - started)))
        return _json({"ok": False, "error": "invalid_credentials"}, 401)
    auth.forgive(ip, stamp)
    token = auth.open_session()
    log.info("web login succeeded from %s (user %s)", ip, auth.username)
    response = _json({"ok": True})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(auth.session_sec),
        path="/",
        httponly=True,
        samesite="Strict",
        secure=_is_https(request),
    )
    return response


async def session(request: web.Request) -> web.Response:
    auth = request.app[AUTH_KEY]
    found = auth.session(request.cookies.get(COOKIE_NAME))
    if found is None:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    core = request.app[DISPATCHER_KEY].core
    return _json({"ok": True, "token": core.token, "username": found.username,
                  "mode": "password" if auth.password_mode else "token"})


async def logout(request: web.Request) -> web.Response:
    if request.headers.get("Origin") and not _origin_ok(request):
        return _json({"ok": False, "error": "bad_origin"}, 403)
    auth = request.app[AUTH_KEY]
    token = request.cookies.get(COOKIE_NAME)
    if token and auth.session(token) is not None:
        await auth.drop(token)
        log.info("web logout from %s", _client_ip(request))
    response = _json({"ok": True})
    response.del_cookie(COOKIE_NAME, path="/")
    return response


async def remote_websocket(request: web.Request) -> web.StreamResponse:
    auth = request.app[AUTH_KEY]
    found = auth.session(request.cookies.get(COOKIE_NAME))
    if found is None:
        return _json({"ok": False, "error": "unauthorized"}, 401)
    if not _origin_ok(request):
        log.warning(
            "web socket refused from %s: origin %r",
            _client_ip(request),
            request.headers.get("Origin"),
        )
        return _json({"ok": False, "error": "bad_origin"}, 403)
    return await websocket_handler(request, track=found.sockets)


async def remote_web_redirect(request: web.Request) -> web.Response:
    # Keep ``?t=`` so the page handler can trade it for a cookie.
    query = request.query_string
    raise web.HTTPFound(WEB_PATH + (f"?{query}" if query else ""))


def _without_token(request: web.Request) -> str:
    """The request's path and query with the access token taken out."""
    from urllib.parse import urlencode

    kept = [(k, v) for k, v in request.query.items() if k != TOKEN_PARAM]
    return request.path + (f"?{urlencode(kept)}" if kept else "")


async def remote_page(request: web.Request) -> web.StreamResponse:
    """The page, and the access link: ``?t=<token>`` becomes a session cookie.

    The first request with a right token gets the cookie and a redirect to the
    same address without the token, so it stays out of the history and the
    ``Referer``.  A wrong one counts toward the same limit as a wrong password.
    With a password set the link alone is not enough: the token is dropped and
    the login page shows.
    """
    presented = request.query.get(TOKEN_PARAM)
    if presented is None:
        return await web_static(request)
    auth = request.app[AUTH_KEY]
    target = _without_token(request)
    if auth.password_mode:
        raise web.HTTPFound(target)
    ip = _client_ip(request)
    wait = auth.retry_after(ip)
    if wait:
        log.warning("web access link refused from %s: too many failures", ip)
        return _json({"ok": False, "error": "rate_limited"}, 429, **{"Retry-After": str(wait)})
    stamp = auth.record_failure(ip)
    started = time.monotonic()
    if not auth.check_token(presented):
        log.warning("web access link with a wrong token from %s", ip)
        await asyncio.sleep(max(0.0, auth.failure_delay - (time.monotonic() - started)))
        return _json({"ok": False, "error": "invalid_token"}, 401)
    auth.forgive(ip, stamp)
    session_token = auth.open_session()
    log.info("web access link used from %s", ip)
    response = web.Response(status=302, headers={"Location": target, "Cache-Control": "no-store"})
    response.set_cookie(COOKIE_NAME, session_token, max_age=int(auth.session_sec), path="/",
                        httponly=True, samesite="Strict", secure=_is_https(request))
    return response


@web.middleware
async def _security_headers(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        exc.headers.update({k: v for k, v in SECURITY_HEADERS.items() if k not in exc.headers})
        raise
    if not response.prepared:
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
    return response


def create_remote_app(
    dispatcher: RpcDispatcher,
    auth: RemoteAuth,
    *,
    connections: set[RpcConnection] | None = None,
    sockets: set[web.WebSocketResponse] | None = None,
    trust_proxy: bool = False,
    allowed_origins: list[str] | tuple[str, ...] = (),
) -> web.Application:
    """The app the remote listener serves; nothing beyond the page needs."""
    app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[_security_headers])
    app[DISPATCHER_KEY] = dispatcher
    # Shared with the loopback app so shutdown closes these sockets too.
    app[CONNECTIONS_KEY] = connections if connections is not None else set()
    app[SOCKETS_KEY] = sockets if sockets is not None else set()
    app[AUTH_KEY] = auth
    app[TRUST_PROXY_KEY] = bool(trust_proxy)
    app[ORIGINS_KEY] = frozenset(o.rstrip("/") for o in allowed_origins if o)
    app.router.add_post(WEB_PATH + "login", login)
    app.router.add_post(WEB_PATH + "logout", logout)
    app.router.add_get(WEB_PATH + "session", session)
    app.router.add_get(WS_PATH, remote_websocket)
    app.router.add_get("/web", remote_web_redirect)
    app.router.add_get(WEB_PATH + "{tail:.*}", remote_page)

    async def _sweeper(app: web.Application) -> Any:
        async def loop() -> None:
            while True:
                await asyncio.sleep(SWEEP_INTERVAL_SEC)
                await auth.sweep()

        task = asyncio.ensure_future(loop())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(_sweeper)
    return app


PLAIN_HTTP_WARNING = (
    "plain HTTP: the access link, the password and everything on the page travel "
    "unencrypted; put it behind TLS (a reverse proxy) or a VPN/ssh tunnel when it is "
    "reachable beyond a trusted network"
)


class RemoteListener:
    """The external listener, opened and closed at runtime (``web.remote.listen|stop``).

    Every connection through it is a remote web login (``remote_login``), with a
    password or with the access link alike; the loopback listener is unchanged.
    """

    def __init__(self, dispatcher: RpcDispatcher, paths: Paths, *,
                 connections: set[RpcConnection], sockets: set[web.WebSocketResponse]) -> None:
        self.dispatcher = dispatcher
        self.paths = paths
        self.connections = connections
        self.sockets = sockets
        self.runner: web.AppRunner | None = None
        self.auth: RemoteAuth | None = None
        self.host = ""
        self.port = 0
        self.username = ""
        self.error: str | None = None
        self._lock = asyncio.Lock()

    @property
    def listening(self) -> bool:
        return self.runner is not None

    async def start(self, *, host: str, port: int, username: str = "", session_hours: float = 12,
                    trust_proxy: bool = False, allowed_origins: list[str] | tuple[str, ...] = ()
                    ) -> None:
        """(Re)bind ``host:port``; :class:`OSError` and :class:`ValueError` say why not."""
        async with self._lock:
            remote = type("Remote", (), {"username": username})()
            problem = config_problem(remote, self.paths)
            if problem is not None:
                self.error = problem
                raise ValueError(problem)
            await self._stop_locked()
            access_token(self.paths)  # made now, so the link exists before the first visit
            auth = RemoteAuth(username, session_hours, self.paths)
            app = create_remote_app(self.dispatcher, auth, connections=self.connections,
                                    sockets=self.sockets, trust_proxy=trust_proxy,
                                    allowed_origins=allowed_origins)
            runner = web.AppRunner(app)
            await runner.setup()
            try:
                await web.TCPSite(runner, host, port).start()
            except OSError as exc:
                await runner.cleanup()
                self.error = f"could not listen on {host}:{port}: {exc}"
                raise
            self.runner, self.auth = runner, auth
            self.host, self.username, self.error = host, username, None
            self.port = _bound_port(runner, port)
            log.warning("remote web access listening on %s:%s (%s); %s", host, self.port,
                        "password" if auth.password_mode else "access link", PLAIN_HTTP_WARNING)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        runner, auth = self.runner, self.auth
        self.runner = None
        self.auth = None
        if auth is not None:
            for token in list(auth.sessions):
                await auth.drop(token)
        if runner is not None:
            await runner.cleanup()

    def sessions(self) -> int:
        return self.auth.active() if self.auth is not None else 0


def _bound_port(runner: web.AppRunner, requested: int) -> int:
    for address in runner.addresses:
        if isinstance(address, tuple) and len(address) >= 2:
            return int(address[1])
    return requested


__all__ = [
    "ACCESS_TOKEN_KEY",
    "PLAIN_HTTP_WARNING",
    "RemoteListener",
    "access_token",
    "rotate_access_token",
    "COOKIE_NAME",
    "PASSWORD_KEY",
    "RemoteAuth",
    "clear_password",
    "config_problem",
    "create_remote_app",
    "hash_password",
    "set_password",
    "stored_hash",
    "verify_password",
]
