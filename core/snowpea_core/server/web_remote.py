"""snowpea web from another machine: a second, password-protected listener.

The loopback listener hands the page its daemon token in the address
``snowpea web`` printed.  Another machine cannot be given that address, so
when ``web.remote.enabled`` is on the daemon also binds ``web.remote.host:port``
with a much smaller app:

* ``GET /web/...`` — the same static build;
* ``POST /web/login`` — ``{username, password}`` → a ``snowpea_web`` cookie;
* ``GET /web/session`` — with that cookie, this login's own capability (field
  ``token``) for ``system.hello``.  Never the daemon token (security review 5,
  H3): the capability is made per login, is accepted only on this listener's
  sockets of the same login, and dies with the login (logout, expiry, a
  password change, a token rotation, the listener stopping);
* ``POST /web/logout`` — drops the session and closes its sockets;
* ``GET /ws`` — the JSON-RPC socket, only with a valid cookie and a same-origin
  ``Origin``; ``system.hello`` there accepts only that login's capability.

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
from snowpea_core.server.transport_http import (
    CONTENT_SECURITY_POLICY,  # noqa: F401 - kept importable from here
    LOOPBACK_NAMES,
    SECURITY_HEADERS,
    WEB_PATH,
    web_static,
)
from snowpea_core.server.transport_ws import (
    CONNECTIONS_KEY,
    DISPATCHER_KEY,
    PENDING_KEY,
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
#: Security review 6, M-3/L-5: over a limit an attempt is not refused unchecked
#: but made to wait first — this long, doubling with every failure over the
#: limit up to the cap — and then checked, so a right credential still gets in.
PENALTY_BASE_SEC = 1.0
PENALTY_MAX_SEC = 30.0
#: scrypt checks running at once (each takes 32 MiB and a core for ~0.1 s).
MAX_PARALLEL_CHECKS = 2
#: Password attempts of one address (one /64) that may wait their turn behind
#: the one being checked; any beyond are answered 429 unchecked (review 8, H8-4).
MAX_WAITING_PER_IP = 4
#: Password attempts that may wait (or be checked) at once, every address
#: together; any beyond are answered 429 unchecked (review 8, H8-4).
MAX_WAITING_GLOBAL = 64
#: A login body is two short strings.
MAX_BODY_BYTES = 4096
SWEEP_INTERVAL_SEC = 60.0
#: Security review 9b, L1: failure stamps kept per queue.  Every reader looks at
#: the newest MAX_FAILURES_* entries or at how far past that limit the queue is,
#: and the penalty stops growing 16 over the limit, so this many keep every
#: answer the unbounded queues gave.
FAILURE_HISTORY_EXTRA = 17
#: Wrong-credential warnings: the first, then one summary per this many seconds.
FAILURE_LOG_INTERVAL_SEC = 60.0


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
    #: What this login's page presents in ``system.hello`` (security review 5, H3).
    capability: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    #: How the login was made: ``password`` (the sign-in page) or ``link`` (the
    #: access link).  Only a password login can re-authenticate (1.56.0).
    via: str = "link"
    #: M37: the password was entered again for terminals over the tunnel
    #: (``web.tunnel.unlock``); lasts as long as this sign-in session.
    terminal_unlocked: bool = False


def rate_key(ip: str) -> str:
    """What the per-address limit counts by: the address, or its /64 for IPv6
    (one subscriber holds a whole /64; security review 6, M-3)."""
    import ipaddress

    try:
        address = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


class _Track(set):  # type: ignore[type-arg]
    """What ``websocket_handler`` tracks a remote socket in: its login's set and
    the listener's set of every socket (security review 6, M-1)."""

    def __init__(self, *targets: set[web.WebSocketResponse]) -> None:
        super().__init__()
        self.targets = targets

    def add(self, ws: web.WebSocketResponse) -> None:
        for target in self.targets:
            target.add(ws)

    def discard(self, ws: web.WebSocketResponse) -> None:
        for target in self.targets:
            target.discard(ws)


class RemoteAuth:
    """In-memory login sessions and failure counters for one daemon run."""

    def __init__(self, username: str, session_hours: float, paths: Paths) -> None:
        self.username = username
        self.session_sec = max(1.0, float(session_hours) * 3600.0)
        self.paths = paths
        self.sessions: dict[str, _Session] = {}
        #: Every open socket of this listener, whichever login opened it and
        #: whether or not that login is still listed (M-1).
        self.sockets: set[web.WebSocketResponse] = set()
        self._closing: set[asyncio.Task[None]] = set()
        self.penalty_base = PENALTY_BASE_SEC
        self.penalty_max = PENALTY_MAX_SEC
        self.sleep = asyncio.sleep
        self._check_slots = asyncio.Semaphore(MAX_PARALLEL_CHECKS)
        self._waiting: dict[str, int] = {}
        self._waiting_total = 0
        self._turns: dict[str, asyncio.Lock] = {}
        self._ip_failures: dict[str, deque[float]] = {}
        self._ip_history = MAX_FAILURES_PER_IP + FAILURE_HISTORY_EXTRA
        self._global_failures: deque[float] = deque(
            maxlen=MAX_FAILURES_GLOBAL + FAILURE_HISTORY_EXTRA)
        #: Rate-limited warnings (L1): when the last one was logged, and how
        #: many failures went unlogged since.
        self._failure_logged_at: float | None = None
        self._failures_unlogged = 0
        #: ``(checked at, fingerprint)``: what :meth:`push_alive` compares (L3).
        self._push_print: tuple[float, str] | None = None
        self.failure_delay = FAILURE_DELAY_SEC
        self.clock = time.monotonic
        self._hash_stamp: tuple[int, int, int] | None = None
        self._hash_print = ""
        #: The listener's proxy and origin options (``None``: the app's keys); kept
        #: here so ``web.remote.listen`` on the same address updates them in place.
        self.trust_proxy: bool | None = None
        self.origins: frozenset[str] | None = None
        #: M37: the running :class:`~snowpea_core.server.tunnel.TunnelService`, if any
        #: (duck-typed: ``active``, ``origin``, ``host``, ``provider``).
        self.tunnel: Any = None

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

    def push_alive(self, token: str | None) -> bool:
        """Before every message pushed to a login's socket (review 8 Low, 9b L3):
        the login exists, has not expired, and its password or link has not
        changed since — the file is looked at no more than once a second."""
        found = self.sessions.get(token or "")
        now = self.clock()
        if found is None or found.expires <= now:
            return False
        cached = self._push_print
        # Re-read when the cached print is old, or differs (a login opened
        # after a change carries the new print).
        if cached is None or not 0 <= now - cached[0] < 1.0 or cached[1] != found.fingerprint:
            cached = (now, self._current_fingerprint())
            self._push_print = cached
        return bool(cached[1]) and cached[1] == found.fingerprint

    def log_failure(self, what: str, ip: str) -> None:
        """A wrong credential (L1): warn about the first, then at most one
        summary a minute with how many there were."""
        now = self.clock()
        last = self._failure_logged_at
        if last is not None and 0 <= now - last < FAILURE_LOG_INTERVAL_SEC:
            self._failures_unlogged += 1
            return
        if self._failures_unlogged:
            log.warning("%s from %s (and %d more failed attempts in the last %d s)", what, ip,
                        self._failures_unlogged, int(FAILURE_LOG_INTERVAL_SEC))
        else:
            log.warning("%s from %s", what, ip)
        self._failure_logged_at = now
        self._failures_unlogged = 0

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
    def open_session(self, via: str = "link") -> str:
        token = secrets.token_urlsafe(32)
        self.sessions[token] = _Session(
            self.username, self.clock() + self.session_sec, self._current_fingerprint(),
            via=via,
        )
        return token

    async def reauthenticate(self, cookie: str | None, ip: str, password: Any) -> Any:
        """The remote password again, for a delegation grant (1.56.0).

        ``"ok"``, ``"no_password"`` (a login made with the access link, or no
        password set now), ``"logged_out"``, ``("rate_limited", seconds)`` or
        ``"failed"``.  Checked exactly like a sign-in: :meth:`attempt` (the same
        per-address and global failure counters, queue bounds and penalties —
        a wrong password here counts toward the sign-in limits and the other way
        round) around :meth:`check_password` (scrypt, constant-time compares)
        with the login's own username.  The password is never logged.
        """
        found = self.session(cookie)
        if found is None:
            return "logged_out"
        if found.via != "password" or not self.password_mode:
            return "no_password"
        malformed = not isinstance(password, str) or not password
        username = found.username

        async def check() -> bool:
            return not malformed and await self.check_password(username, password)

        ok, wait = await self.attempt(ip, check)
        if ok:
            log.info("remote password confirmed again from %s (delegation grant)", ip)
            return "ok"
        self.log_failure("remote password re-check failed (delegation grant)", ip)
        if wait:
            return ("rate_limited", int(wait))
        return "failed"

    def terminal_unlocked(self, token: str | None) -> bool:
        found = self.session(token)
        return bool(found is not None and found.terminal_unlocked)

    def unlock_terminal(self, token: str | None) -> None:
        found = self.session(token)
        if found is not None:
            found.terminal_unlocked = True

    def session(self, token: str | None) -> _Session | None:
        if not token:
            return None
        found = self.sessions.get(token)
        if found is None:
            return None
        if found.expires <= self.clock():
            # Expired: its sockets go with it (security review 6, M-1).
            self._end(token, found)
            return None
        current = self._current_fingerprint()
        if not current or current != found.fingerprint:
            # The password was changed or cleared: end the session like a logout.
            self._end(token, found)
            return None
        return found

    def _end(self, token: str, found: _Session) -> None:
        """Forget a login and close its sockets (from sync code: as a task)."""
        self.sessions.pop(token, None)
        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(_close_all(found.sockets))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    async def end_all(self) -> None:
        """Log everyone out: no login stays, and every socket of the listener closes."""
        for token in list(self.sessions):
            await self.drop(token)
        await _close_all(self.sockets)

    def capability_ok(self, cookie: str | None, presented: str | None) -> bool:
        """``presented`` is the live login ``cookie``'s capability (constant time)."""
        found = self.session(cookie)
        if found is None or not presented:
            return False
        return hmac.compare_digest(
            hashlib.sha256(found.capability.encode("utf-8")).digest(),
            hashlib.sha256(presented.encode("utf-8")).digest(),
        )

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
        """Seconds until ``ip`` may try again, 0 when it may now (both buckets)."""
        return max(self.retry_after_ip(ip), self.retry_after_global())

    def retry_after_ip(self, ip: str) -> int:
        """Seconds until ``ip``'s own failures let it try again (0: now)."""
        queue = self._ip_failures.get(rate_key(ip))
        if queue is None:
            return 0
        self._trim(queue)
        if len(queue) < MAX_FAILURES_PER_IP:
            return 0
        return max(1, int(queue[-MAX_FAILURES_PER_IP] + RATE_WINDOW_SEC - self.clock() + 0.999))

    def retry_after_global(self) -> int:
        """Seconds until the everyone-together bucket empties (0: now).

        Security review 5, L1: this bucket only ever refuses a WRONG credential —
        the credential is checked first, so a flood of failures from elsewhere
        cannot lock the person out of their own correct link or password.
        """
        self._trim(self._global_failures)
        if len(self._global_failures) < MAX_FAILURES_GLOBAL:
            return 0
        return max(1, int(self._global_failures[-MAX_FAILURES_GLOBAL] + RATE_WINDOW_SEC
                          - self.clock() + 0.999))

    def record_failure(self, ip: str, *, everyone: bool = True) -> float:
        now = self.clock()
        self._ip_failures.setdefault(rate_key(ip), deque(maxlen=self._ip_history)).append(now)
        if everyone:
            self._global_failures.append(now)
        return now

    def forgive(self, ip: str, stamp: float) -> None:
        """Take back the failure :meth:`record_failure` counted for an attempt that passed."""
        for queue in (self._ip_failures.get(rate_key(ip)), self._global_failures):
            with contextlib.suppress(ValueError):
                if queue is not None:
                    queue.remove(stamp)

    def penalty(self, ip: str) -> float:
        """Seconds the next attempt from ``ip`` waits before it is checked: 0 under
        both limits, else ``penalty_base`` doubling per failure over a limit, capped."""
        self._trim(self._global_failures)
        queue = self._ip_failures.get(rate_key(ip))
        if queue is not None:
            self._trim(queue)
        over = max(len(queue or ()) - MAX_FAILURES_PER_IP,
                   len(self._global_failures) - MAX_FAILURES_GLOBAL)
        if over < 0:
            return 0.0
        return float(min(self.penalty_max, self.penalty_base * 2 ** min(over, 16)))

    async def attempt(self, ip: str, check: Any, *, fast: bool = False) -> tuple[bool, int]:
        """One credential attempt from ``ip`` under the limits: ``(passed, retry_after)``.

        Security review 6, M-3/L-5: within the bounds below the credential is
        checked, so a right one gets in whatever the buckets say; what the
        limits cost is time — over a limit an attempt waits :meth:`penalty`
        first.  A wrong one over a limit is answered ``retry_after`` > 0 (429).

        Security review 8, H8-4 (the bounds): a password check is scrypt, so
        per address (one /64) at most ONE check is in flight and at most
        :data:`MAX_WAITING_PER_IP` more wait their turn; in all at most
        :data:`MAX_WAITING_GLOBAL` attempts wait.  Beyond either an attempt is
        answered 429 at once, UNCHECKED, and counted against its address.  So a
        flood costs the daemon a bounded queue — and while it lasts, a right
        password from the SAME address (or, past the global bound, from
        anywhere) can be answered 429 too: someone sharing the address can
        delay a sign-in; nobody can guess faster through it.

        ``fast``: the access link's token — a constant-time compare, no scrypt —
        never queues and never waits a penalty (it cannot be held up behind
        password checks); it is still counted and answered 429 over a limit.
        """
        key = rate_key(ip)
        ip_wait = self.retry_after_ip(ip)
        global_wait = self.retry_after_global()
        if not fast and (
            self._waiting.get(key, 0) >= 1 + MAX_WAITING_PER_IP
            or self._waiting_total >= MAX_WAITING_GLOBAL
        ):
            self.record_failure(ip, everyone=False)
            return False, max(1, self.retry_after(ip))
        delay = 0.0 if fast else self.penalty(ip)
        # Counted at once (no await since the limits were read), so attempts sent
        # in parallel cannot all slip under a limit; a right credential takes it
        # back.  An address already over its own limit no longer adds to the
        # everyone-together bucket: one address cannot slow everybody down.
        stamp = self.record_failure(ip, everyone=not ip_wait)
        started = time.monotonic()
        if fast:
            passed = bool(await check())
        else:
            self._waiting[key] = self._waiting.get(key, 0) + 1
            self._waiting_total += 1
            turn = self._turns.setdefault(key, asyncio.Lock())
            try:
                async with turn:
                    if delay:
                        await self.sleep(delay)
                    passed = bool(await check())
            finally:
                self._waiting_total -= 1
                left = self._waiting.get(key, 1) - 1
                if left > 0:
                    self._waiting[key] = left
                else:
                    self._waiting.pop(key, None)
                    self._turns.pop(key, None)
        if passed:
            self.forgive(ip, stamp)
            return True, 0
        await asyncio.sleep(max(0.0, self.failure_delay - (time.monotonic() - started)))
        # Over a limit before this attempt: 429, with the wait as it stands now.
        return False, self.retry_after(ip) if (ip_wait or global_wait) else 0

    async def check_password(self, username: str, password: str) -> bool:
        """:meth:`check` off the event loop, at most :data:`MAX_PARALLEL_CHECKS` at once."""
        async with self._check_slots:
            return await asyncio.to_thread(self.check, username, password)

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
#: Set on the app of the tunnel's dedicated loopback listener (M37 security review H5).
TUNNEL_LISTENER_KEY: web.AppKey[bool] = web.AppKey("remote_tunnel_listener", bool)


def _tunnel(request: web.Request) -> Any:
    """The tunnel service while it runs (``active`` is literally ``True``), else ``None``."""
    tunnel = getattr(request.app[AUTH_KEY], "tunnel", None)
    return tunnel if tunnel is not None and getattr(tunnel, "active", False) is True else None


def _trust_proxy(request: web.Request) -> bool:
    """The listener's current ``trustProxy`` (kept on its auth so an update in place works).

    On the tunnel's own listener (:data:`TUNNEL_LISTENER_KEY`) the forwarded headers
    count always (M37): the callers still require the peer to be on this machine
    (:func:`_from_local_proxy`), which is where the tunnel program connects from.
    The ordinary listener never believes them just because a tunnel runs.
    """
    if _on_tunnel_listener(request):
        return True
    value = getattr(request.app[AUTH_KEY], "trust_proxy", None)
    return bool(request.app[TRUST_PROXY_KEY] if value is None else value)


def _origins(request: web.Request) -> frozenset[str]:
    value = getattr(request.app[AUTH_KEY], "origins", None)
    base = request.app[ORIGINS_KEY] if value is None else value
    tunnel = _tunnel(request) if _on_tunnel_listener(request) else None
    origin = getattr(tunnel, "origin", None) if tunnel is not None else None
    # The tunnel's own origin is allowed at runtime on its own listener, never written
    # to the settings.
    return base | {origin} if isinstance(origin, str) and origin else base


def _on_tunnel_listener(request: web.Request) -> bool:
    """The request came in on the tunnel's dedicated loopback listener."""
    return request.app.get(TUNNEL_LISTENER_KEY, False) is True


def _via_tunnel(request: web.Request) -> bool:
    """Everything on the tunnel's own listener is through the tunnel (decided by the
    path, never by the Host header, which the public side can set).  On the ordinary
    listener a Host that is the tunnel's is an additional marker, so a request that
    names the tunnel host but did not come through it only gets the stricter rules."""
    if _on_tunnel_listener(request):
        return True
    tunnel = _tunnel(request)
    if tunnel is None:
        return False
    checker = getattr(tunnel, "is_tunnel_host", None)
    return bool(callable(checker) and checker(request.host))


def _is_https(request: web.Request) -> bool:
    if request.secure:
        return True
    if _trust_proxy(request) and _from_local_proxy(request):
        proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        return proto == "https"
    return False


def _from_local_proxy(request: web.Request) -> bool:
    """The peer is a reverse proxy on this machine (security review 9b, L2): only
    then are its ``X-Forwarded-*`` headers believed under ``trustProxy``."""
    remote = str(request.remote or "").lower()
    if remote.startswith("::ffff:"):
        remote = remote[len("::ffff:"):]
    return remote in LOOPBACK_NAMES or remote.startswith("127.")


def _client_ip(request: web.Request) -> str:
    if _trust_proxy(request) and _from_local_proxy(request):
        tunnel = _tunnel(request) if _on_tunnel_listener(request) else None
        if tunnel is not None and str(getattr(tunnel, "provider", "")).startswith("cloudflare"):
            # Cloudflare's edge sets this and cloudflared passes it on.  Over ngrok it is
            # whatever the visitor sent, so only the cloudflare providers believe it.
            connecting = request.headers.get("CF-Connecting-IP", "").strip()
            if connecting:
                return connecting
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
    return origin == own or origin in _origins(request)


def _json(payload: dict[str, Any], status: int = 200, **headers: str) -> web.Response:
    return web.json_response(
        payload, status=status, headers={"Cache-Control": "no-store", **headers}
    )


async def login(request: web.Request) -> web.Response:
    auth = request.app[AUTH_KEY]
    ip = _client_ip(request)
    if request.headers.get("Origin") and not _origin_ok(request):
        return _json({"ok": False, "error": "bad_origin"}, 403)
    username: Any = None
    password: Any = None
    if request.content_type == "application/json":
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            body = None
        username = body.get("username") if isinstance(body, dict) else None
        password = body.get("password") if isinstance(body, dict) else None
    malformed = not isinstance(username, str) or not isinstance(password, str)

    async def check() -> bool:
        # A malformed body is an attempt too (it counts), and never passes.
        return not malformed and await auth.check_password(username, password)

    # Both limits are paid in time and judged after the check (review 5 L1,
    # review 6 M-3/L-5): they refuse wrong passwords only.
    ok, wait = await auth.attempt(ip, check)
    if malformed:
        return _json({"ok": False, "error": "bad_request"}, 400)
    if not ok:
        auth.log_failure("web login failed", ip)
        if wait:
            return _json({"ok": False, "error": "rate_limited"}, 429,
                         **{"Retry-After": str(wait)})
        return _json({"ok": False, "error": "invalid_credentials"}, 401)
    token = auth.open_session(via="password")
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
    # ``token`` is this login's capability, never the daemon token (H3); the
    # field keeps its name so the web page needs no change.
    return _json({"ok": True, "token": found.capability, "username": found.username,
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
    cookie = request.cookies.get(COOKIE_NAME)

    def capability_ok(presented: str | None) -> bool:
        return auth.capability_ok(cookie, presented)

    def alive() -> bool:
        # Security review 7, Low 3: the login behind this socket still stands
        # (not expired, its password or link unchanged) — a clock compare.
        return auth.session(cookie) is not None

    def push_alive() -> bool:
        # Security review 8, Low / 9b, L3: before every pushed message — expiry
        # and the credential fingerprint (cached for a second).
        return auth.push_alive(cookie)

    ip = _client_ip(request)
    via_tunnel = _via_tunnel(request)

    def configure(conn: RpcConnection) -> None:
        conn.via_tunnel = via_tunnel
        conn.tunnel_unlocked = lambda: auth.terminal_unlocked(cookie)
        conn.tunnel_mark_unlocked = lambda: auth.unlock_terminal(cookie)

    async def reauth(password: Any) -> Any:
        # 1.56.0: the password again for a delegation grant, counted with this
        # address's sign-in failures (the address as the sign-in sees it).
        return await auth.reauthenticate(cookie, ip, password)

    return await websocket_handler(request, track=_Track(found.sockets, auth.sockets),
                                   capability=capability_ok, alive=alive,
                                   push_alive=push_alive, reauth=reauth, configure=configure)


async def remote_web_redirect(request: web.Request) -> web.Response:
    # Keep ``?t=`` so the page handler can trade it for a cookie.
    query = request.query_string
    raise web.HTTPFound(WEB_PATH + (f"?{query}" if query else ""))


def _without_token(request: web.Request) -> str:
    """The request's path and query with the access token taken out."""
    from urllib.parse import urlencode

    kept = [(k, v) for k, v in request.query.items() if k != TOKEN_PARAM]
    return request.path + (f"?{urlencode(kept)}" if kept else "")


_STALE_LINK_TEXT: dict[str, tuple[str, str, str]] = {
    "en": ("Link expired", "This link no longer works",
           "The access link was changed or is wrong. Ask the person who runs snowpea for a new "
           "link."),
    "ko": ("링크 만료", "이 링크는 더 이상 쓸 수 없습니다",
           "접속 링크가 바뀌었거나 올바르지 않습니다. snowpea 를 실행하는 분께 새 링크를 "
           "받으세요."),
}


def _wants_html(request: web.Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("Accept", "")


def _language(request: web.Request) -> str:
    """``ko`` when Accept-Language prefers Korean over English, else ``en``."""
    best, best_q = "en", -1.0
    for part in request.headers.get("Accept-Language", "").split(","):
        tag, _, rest = part.strip().partition(";")
        lang = tag.strip().lower().split("-", 1)[0]
        q = 1.0
        if rest.strip().startswith("q="):
            with contextlib.suppress(ValueError):
                q = float(rest.strip()[2:])
        if lang in _STALE_LINK_TEXT and q > best_q:
            best, best_q = lang, q
    return best


def _stale_link_page(request: web.Request) -> web.Response:
    """A small self-contained page for a stale or wrong access link (status 401)."""
    import html

    lang = _language(request)
    title, heading, body = (html.escape(t) for t in _STALE_LINK_TEXT[lang])
    page = (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title><style>body{{font-family:system-ui,sans-serif;margin:0;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center;"
        "background:#f6f7f8;color:#222}main{max-width:28rem;padding:2rem;text-align:center}"
        "h1{font-size:1.3rem}p{line-height:1.5;color:#555}@media(prefers-color-scheme:dark)"
        "{body{background:#16181a;color:#eee}p{color:#aaa}}</style></head>"
        f"<body><main><h1>{heading}</h1><p>{body}</p></main></body></html>"
    )
    return web.Response(status=401, text=page, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


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

    async def check() -> bool:
        return auth.check_token(presented)

    # The limits refuse a wrong token only (review 5 L1, review 6 M-3/L-5); a
    # token check never waits behind password checks (review 8, H8-4).
    ok, wait = await auth.attempt(ip, check, fast=True)
    if not ok:
        auth.log_failure("web access link with a wrong token", ip)
        if wait:
            return _json({"ok": False, "error": "rate_limited"}, 429,
                         **{"Retry-After": str(wait)})
        if _wants_html(request):
            # A person opened a stale or wrong link: a page, never the token or why.
            return _stale_link_page(request)
        return _json({"ok": False, "error": "invalid_token"}, 401)
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
    tunnel_listener: bool = False,
) -> web.Application:
    """The app the remote listener serves; nothing beyond the page needs.

    ``tunnel_listener``: this is the dedicated loopback listener a tunnel program
    connects to; every connection on it is ``via_tunnel``.
    """
    app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[_security_headers])
    app[DISPATCHER_KEY] = dispatcher
    # Shared with the loopback app so shutdown closes these sockets too.
    app[CONNECTIONS_KEY] = connections if connections is not None else set()
    app[SOCKETS_KEY] = sockets if sockets is not None else set()
    app[PENDING_KEY] = {}
    app[AUTH_KEY] = auth
    app[TUNNEL_LISTENER_KEY] = bool(tunnel_listener)
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


#: What a remote sign-in is, in plain words (security review 7: the limits on it
#: are "not by a click", never a boundary against whoever holds the link).
REMOTE_ACCESS_WARNING = (
    "Anyone who has the link (or the username and password) can use snowpea as you, "
    "including running commands on this computer. Treat the link like an SSH key: share it "
    "with nobody, prefer the password mode, and put HTTPS (a tunnel or reverse proxy) in "
    "front of it."
)
#: The warning ``web.remote.status`` carries while the listener is open, which
#: every client shows: who can do what with the link, then the transport.
PLAIN_HTTP_WARNING = (
    f"{REMOTE_ACCESS_WARNING} This listener is plain HTTP: the access link, the password and "
    "everything on the page travel unencrypted; put it behind TLS (a reverse proxy) or a "
    "VPN/ssh tunnel when it is reachable beyond a trusted network"
)


class ListenError(OSError):
    """Why the listener could not bind: ``field`` (``host`` | ``port``) and ``reason``
    (``in_use`` | ``bad_address`` | ``permission`` | ``out_of_range``), for
    ``web.remote.listen``'s ``error.data.details``."""

    def __init__(self, field: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.reason = reason

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else self.reason

    @classmethod
    def of(cls, exc: OSError, host: str, port: int) -> ListenError:
        import errno
        import socket as socket_module

        text = f"could not listen on {host}:{port}: {exc}"
        if isinstance(exc, socket_module.gaierror) or exc.errno in (
            errno.EADDRNOTAVAIL, getattr(errno, "EAFNOSUPPORT", -1), errno.EINVAL,
        ):
            return cls("host", "bad_address", text)
        if exc.errno == errno.EADDRINUSE:
            return cls("port", "in_use", text)
        if exc.errno in (errno.EACCES, errno.EPERM):
            return cls("port", "permission", text)
        if isinstance(exc, OverflowError):  # pragma: no cover - checked before binding
            return cls("port", "out_of_range", text)
        return cls("host", "bad_address", text)


class RemoteListener:
    """The external listener, opened and closed at runtime (``web.remote.listen|stop``).

    Every connection through it is a remote web login (``remote_login``), with a
    password or with the access link alike; the loopback listener is unchanged.
    """

    def __init__(self, dispatcher: RpcDispatcher, paths: Paths, *,
                 connections: set[RpcConnection], sockets: set[web.WebSocketResponse]) -> None:
        #: M37: the tunnel service the daemon sets; handed to every auth made.
        self.tunnel: Any = None
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
        #: The tunnel's own loopback listener (M37): same logins, own port.
        self._tunnel_runner: web.AppRunner | None = None
        self._tunnel_port = 0

    async def open_tunnel_listener(self) -> int:
        """Bind (once) a listener on 127.0.0.1 for the tunnel program and return its port.

        It serves the same app and logins; every connection on it is ``via_tunnel``.  The
        tunnel is pointed here, not at the ordinary listener, so what came through it is
        known by path and not by a header (M37 security review H5)."""
        async with self._lock:
            if self._tunnel_runner is not None:
                return self._tunnel_port
            if self.auth is None or self.runner is None:
                raise ListenError("host", "bad_address", "remote web access is not listening")
            app = create_remote_app(self.dispatcher, self.auth, connections=self.connections,
                                    sockets=self.sockets, tunnel_listener=True)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, "127.0.0.1", 0).start()
            except OSError as exc:
                await runner.cleanup()
                raise ListenError.of(exc, "127.0.0.1", 0) from exc
            self._tunnel_runner = runner
            self._tunnel_port = _bound_port(runner, 0)
            return self._tunnel_port

    async def close_tunnel_listener(self) -> None:
        async with self._lock:
            await self._close_tunnel_locked()

    async def _close_tunnel_locked(self) -> None:
        runner, self._tunnel_runner, self._tunnel_port = self._tunnel_runner, None, 0
        if runner is not None:
            await runner.cleanup()

    @property
    def listening(self) -> bool:
        return self.runner is not None

    async def start(self, *, host: str, port: int, username: str = "", session_hours: float = 12,
                    trust_proxy: bool = False, allowed_origins: list[str] | tuple[str, ...] = ()
                    ) -> None:
        """(Re)bind ``host:port``; :class:`ListenError` and :class:`ValueError` say why not.

        The new address is bound FIRST and the old listener stopped only once
        it is (a move that fails leaves remote access where it was, with its
        sessions).  The address it already listens on (``port`` 0 included)
        is an update of the options in place: the sessions stay.
        """
        async with self._lock:
            remote = type("Remote", (), {"username": username})()
            problem = config_problem(remote, self.paths)
            if problem is not None:
                self.error = problem
                raise ValueError(problem)
            if not 0 <= int(port) <= 65535:
                raise ListenError("port", "out_of_range",
                                  f"could not listen on {host}:{port}: the port must be 0-65535")
            origins = frozenset(o.rstrip("/") for o in allowed_origins if o)
            if self.runner is not None and self.auth is not None and host == self.host \
                    and port in (0, self.port):
                auth = self.auth
                auth.username, self.username = username, username
                auth.session_sec = max(1.0, float(session_hours) * 3600.0)
                auth.trust_proxy, auth.origins = bool(trust_proxy), origins
                auth.tunnel = self.tunnel
                self.error = None
                return
            access_token(self.paths)  # made now, so the link exists before the first visit
            auth = RemoteAuth(username, session_hours, self.paths)
            auth.trust_proxy, auth.origins = bool(trust_proxy), origins
            auth.tunnel = self.tunnel
            app = create_remote_app(self.dispatcher, auth, connections=self.connections,
                                    sockets=self.sockets, trust_proxy=trust_proxy,
                                    allowed_origins=allowed_origins)
            # No access log (security review 5, M4): the access link's ``?t=`` would land in it.
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, host, port).start()
            except OSError as exc:
                await runner.cleanup()
                error = ListenError.of(exc, host, port)
                if self.runner is None:
                    self.error = str(error)
                raise error from exc
            await self._stop_locked()  # the old address, only now that the new one is bound
            self.runner, self.auth = runner, auth
            self.host, self.username, self.error = host, username, None
            self.port = _bound_port(runner, port)
            log.warning("remote web access listening on %s:%s (%s); %s", host, self.port,
                        "password" if auth.password_mode else "access link", PLAIN_HTTP_WARNING)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        await self._close_tunnel_locked()
        runner, auth = self.runner, self.auth
        self.runner = None
        self.auth = None
        if auth is not None:
            await auth.end_all()
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
    "REMOTE_ACCESS_WARNING",
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
