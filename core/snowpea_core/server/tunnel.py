"""The web tunnel (M37, design §2.10): a public HTTPS address in front of ``snowpea web``.

A :class:`TunnelService` runs one tunnel program next to the daemon and points it
at the remote listener (``web.remote``), on this machine's loopback:

* ``cloudflare-quick``: ``cloudflared tunnel --no-autoupdate --url http://<loopback>``; the
  address (``https://*.trycloudflare.com``) is parsed from its log.
* ``cloudflare``: ``cloudflared tunnel --no-autoupdate run`` with the token in the
  environment (never on the command line); the address is ``https://<hostname>``.
* ``ngrok``: ``ngrok http <loopback> --log stdout --log-format json`` with the authtoken in
  the environment; the address is ``url`` of the JSON log, else ``127.0.0.1:4040/api/tunnels``.

What the rest of the daemon sees:

* :attr:`TunnelService.active` — the tunnel runs, so forwarded headers
  (``CF-Connecting-IP``, ``X-Forwarded-*``) are believed from a peer on this
  machine, and :attr:`TunnelService.origin` is an allowed WebSocket origin
  (``server/web_remote.py``).
* A remote connection whose Host is the tunnel's is ``viaTunnel``.  Terminal calls
  on it need a **terminal unlock** (``web.tunnel.unlock {password}``): terminal code
  calls :func:`require_terminal_unlock`.

Secrets (the ngrok authtoken, the Cloudflare tunnel token) live in ``credentials.json``
(:data:`NGROK_TOKEN_KEY`, :data:`CLOUDFLARE_TOKEN_KEY`); they reach the program through
its environment and are removed from every line the service keeps or shows.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.request
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from snowpea_core.config.credentials import CredentialStore
from snowpea_core.config.paths import Paths
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.rpc import RpcConnection

log = logging.getLogger("snowpea.web.tunnel")

PROVIDERS: tuple[str, ...] = ("cloudflare-quick", "cloudflare", "ngrok")
#: ``credentials.json`` keys of the tokens (never in settings.json).
NGROK_TOKEN_KEY = "web.tunnel.ngrokAuthtoken"
CLOUDFLARE_TOKEN_KEY = "web.tunnel.cloudflareToken"
TOKEN_KEYS: dict[str, str] = {"ngrok": NGROK_TOKEN_KEY, "cloudflare": CLOUDFLARE_TOKEN_KEY}

#: ``details.reason`` of the error a locked terminal call answers with.
TERMINAL_LOCKED = "terminal_locked"

#: Restart delays (seconds): doubling from the base up to the cap.
BACKOFF_BASE_SEC = 2.0
BACKOFF_MAX_SEC = 60.0
#: A run that lasted this long counts as stable: the failure count starts again.
STABLE_RUN_SEC = 30.0
#: Consecutive runs that exit before they ever showed an address, then it gives up.
MAX_EARLY_FAILURES = 5
#: How long a process may take to show its address before it is stopped and retried.
URL_TIMEOUT_SEC = 90.0
#: ngrok: ask its local API when the log has not shown the address after this long.
NGROK_API_AFTER_SEC = 4.0
NGROK_API_EVERY_SEC = 2.0
NGROK_API_URL = "http://127.0.0.1:4040/api/tunnels"
#: How long :meth:`TunnelService.start` waits for the address before it answers.
START_WAIT_SEC = 25.0
STOP_WAIT_SEC = 5.0
LOG_TAIL_LINES = 30
MAX_LINE_CHARS = 2000
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

# ---------------------------------------------------------------------------
# log parsing (fixtures of each provider's real log format in the tests)
# ---------------------------------------------------------------------------

_QUICK_URL = re.compile(r"https://([a-z0-9][a-z0-9-]*)\.trycloudflare\.com\b", re.IGNORECASE)
#: Not a tunnel: cloudflared's own API host appears in its error lines.
_QUICK_NOT_TUNNEL = frozenset({"api"})
_REGISTERED = re.compile(r"Registered tunnel connection", re.IGNORECASE)


def parse_quick_url(line: str) -> str | None:
    """The ``https://<words>.trycloudflare.com`` a quick tunnel's log line shows."""
    for match in _QUICK_URL.finditer(line):
        if match.group(1).lower() not in _QUICK_NOT_TUNNEL:
            return f"https://{match.group(1).lower()}.trycloudflare.com"
    return None


def is_registered(line: str) -> bool:
    """cloudflared connected a named tunnel (``INF Registered tunnel connection …``)."""
    return bool(_REGISTERED.search(line))


def parse_ngrok_url(line: str) -> str | None:
    """``url`` of ngrok's JSON log line ``{"msg":"started tunnel", …, "url":"https://…"}``."""
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    url = record.get("url")
    if isinstance(url, str) and url.startswith("https://") and "started tunnel" in str(
            record.get("msg", "")).lower():
        return url.rstrip("/")
    return None


def parse_ngrok_api(payload: Any, port: int | None = None) -> str | None:
    """The https address in ``GET 127.0.0.1:4040/api/tunnels``; the one that forwards to
    ``port`` when several tunnels run."""
    tunnels = payload.get("tunnels") if isinstance(payload, dict) else None
    if not isinstance(tunnels, list):
        return None
    found: list[tuple[bool, str]] = []
    for item in tunnels:
        if not isinstance(item, dict):
            continue
        public = item.get("public_url")
        if not isinstance(public, str) or not public.startswith("https://"):
            continue
        config = item.get("config")
        addr = str(config.get("addr", "")) if isinstance(config, dict) else ""
        found.append((port is not None and addr.endswith(f":{port}"), public.rstrip("/")))
    if not found:
        return None
    found.sort(key=lambda pair: not pair[0])
    return found[0][1]


def redact(text: str, secrets: list[str]) -> str:
    """``text`` with every secret removed; the line a status or a log may show."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


# ---------------------------------------------------------------------------
# errors and preconditions
# ---------------------------------------------------------------------------


class TunnelError(Exception):
    """The tunnel cannot start (or install); ``code`` is stable, ``message`` is for a person."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_rpc(self) -> RpcError:
        return RpcError(errors.UNAVAILABLE, self.message, {"reason": self.code, **self.details})


def binary_name(provider: str) -> str:
    name = "ngrok" if provider == "ngrok" else "cloudflared"
    return name + (".exe" if sys.platform == "win32" else "")


def _loopback_reachable(host: str) -> bool:
    return host in {"", "0.0.0.0", "::", "localhost", "::1"} or host.startswith("127.")


def target_address(host: str, port: int) -> str:
    """Where the tunnel program connects: this machine's loopback on the listener's port."""
    if host == "::1":
        return f"[::1]:{port}"
    if host.startswith("127."):
        return f"{host}:{port}"
    return f"127.0.0.1:{port}"


def check_preconditions(remote: Any, listener: Any, paths: Paths) -> None:
    """Remote access on, a web password set and the listener up on a loopback-reachable
    address; :class:`TunnelError` says which is missing."""
    from snowpea_core.server import web_remote

    if not getattr(remote, "enabled", False) and not (
            listener is not None and getattr(listener, "listening", False)):
        raise TunnelError(
            "remote_disabled",
            "the tunnel needs remote web access: turn it on first "
            "(`snowpea web --host … --port …`)")
    if not web_remote.stored_hash(paths):
        raise TunnelError(
            "no_password",
            "the tunnel needs a web password: set one first (`snowpea web password`); "
            "without it anyone with the address could use snowpea")
    if listener is None or not getattr(listener, "listening", False):
        raise TunnelError("not_listening", "remote web access is not listening yet")
    if not _loopback_reachable(str(getattr(listener, "host", ""))):
        raise TunnelError(
            "bad_listen_host",
            f"the remote listener is bound to {listener.host}, which this machine's loopback "
            "cannot reach; listen on 0.0.0.0 or 127.0.0.1 to use a tunnel")


def find_binary(provider: str, configured: str | None, paths: Paths) -> str | None:
    """``binary`` of the settings, then ``PATH``, then ``$SNOWPEA_HOME/bin``."""
    name = binary_name(provider)
    candidates: list[str | None] = []
    if configured:
        candidates.append(configured)
    candidates.append(shutil.which(name))
    candidates.append(str(bin_dir(paths) / name))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def bin_dir(paths: Paths) -> Path:
    return paths.home / "bin"


# ---------------------------------------------------------------------------
# installer: pinned version, pinned sha256, never anything unverified
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PinnedAsset:
    url: str
    sha256: str


CLOUDFLARED_VERSION = "2026.11.2"
_CF = f"https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}"
#: ``(os, arch)`` -> the file to download.  The sha256 values are the ones the official
#: release (github.com/cloudflare/cloudflared, tag 2026.11.2) lists in its notes AND as the
#: asset digest; the two agree for exactly these.  The macOS archives are left out: the
#: notes and the asset digest disagree there, and ngrok has no versioned official URL, so
#: ``install`` refuses for them and shows the manual command.
PINNED: dict[str, dict[tuple[str, str], PinnedAsset]] = {
    "cloudflared": {
        ("linux", "amd64"): PinnedAsset(
            f"{_CF}/cloudflared-linux-amd64",
            "56f13335143ec0e22772963290683a84a04114a35ec64cbc794a4d39ff9bb8cf"),
        ("linux", "arm64"): PinnedAsset(
            f"{_CF}/cloudflared-linux-arm64",
            "04a7b7f08bce85d56b26ffe1fdbfbb21a074495a363d16f0783c40e25b5c5281"),
        ("windows", "amd64"): PinnedAsset(
            f"{_CF}/cloudflared-windows-amd64.exe",
            "555b46c85582461d189cac30d14ad05480d612613801be9f110b8ce08c5b2113"),
    },
    "ngrok": {},
}

#: What to run by hand where no checksum is pinned.
MANUAL_INSTALL: dict[str, dict[str, str]] = {
    "cloudflared": {
        "darwin": "brew install cloudflared",
        "linux": "see https://developers.cloudflare.com/cloudflare-one/"
                 "connections/connect-networks/downloads/",
        "windows": "winget install --id Cloudflare.cloudflared",
    },
    "ngrok": {
        "darwin": "brew install ngrok",
        "linux": "snap install ngrok   (or see https://ngrok.com/download)",
        "windows": "winget install --id Ngrok.Ngrok",
    },
}


def platform_key() -> tuple[str, str]:
    os_name = {"linux": "linux", "win32": "windows", "darwin": "darwin"}.get(
        sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, machine)
    return os_name, arch


def pinned_asset(provider: str, key: tuple[str, str] | None = None) -> PinnedAsset | None:
    program = "ngrok" if provider == "ngrok" else "cloudflared"
    return PINNED.get(program, {}).get(key or platform_key())


def manual_command(provider: str) -> str:
    program = "ngrok" if provider == "ngrok" else "cloudflared"
    os_name = platform_key()[0]
    return MANUAL_INSTALL[program].get(os_name, f"install {program} and put it on PATH")


def _fetch_bytes(url: str) -> bytes:
    """Download ``url`` (https only, size capped); tests replace it, nothing is fetched there."""
    if not url.startswith("https://"):
        raise TunnelError("install_failed", "refusing a download that is not https")
    request = urllib.request.Request(url, headers={"User-Agent": "snowpea-tunnel-installer"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - https, pinned
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise TunnelError("install_failed", "the download is larger than expected")
            chunks.append(chunk)
    return b"".join(chunks)


async def install_binary(
    provider: str, paths: Paths, *,
    fetch: Callable[[str], bytes] | None = None,
    key: tuple[str, str] | None = None,
) -> Path:
    """Download the pinned program of ``provider`` into ``$SNOWPEA_HOME/bin``.

    Refuses (:class:`TunnelError`) when no checksum is pinned for this platform, and when
    the download's sha256 is not the pinned one; in both cases nothing is written.
    """
    program = "ngrok" if provider == "ngrok" else "cloudflared"
    asset = pinned_asset(provider, key)
    if asset is None:
        raise TunnelError(
            "install_unpinned",
            f"no verified download of {program} is pinned for this platform; install it "
            f"yourself with: {manual_command(provider)}",
            command=manual_command(provider))
    getter = fetch or _fetch_bytes
    try:
        data = await asyncio.to_thread(getter, asset.url)
    except TunnelError:
        raise
    except Exception as exc:  # noqa: BLE001 - network failures vary
        raise TunnelError("install_failed", f"could not download {program}: {exc}") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != asset.sha256:
        raise TunnelError(
            "checksum_mismatch",
            f"the downloaded {program} does not match the pinned checksum; it was not installed",
            expected=asset.sha256, actual=digest)
    target_dir = bin_dir(paths)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / binary_name(provider)
    fd, temp = tempfile.mkstemp(prefix=f".{program}-", dir=target_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp, stat.S_IRWXU)
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    log.info("installed %s %s into %s", program, CLOUDFLARED_VERSION, target_dir)
    return target


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------

Notify = Callable[[str, dict[str, Any]], Awaitable[None]]
HttpGetJson = Callable[[str], Awaitable[Any]]


async def _http_get_json(url: str) -> Any:
    """Loopback only (ngrok's local API)."""
    host = urlsplit(url).hostname or ""
    if host != "127.0.0.1":
        raise ValueError("only 127.0.0.1 is asked")

    def get() -> Any:
        with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - loopback
            return json.loads(response.read(1 << 20).decode("utf-8", "replace"))

    return await asyncio.to_thread(get)


async def _spawn(argv: list[str], env: dict[str, str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env)


class TunnelService:
    """Runs, watches and restarts one tunnel program; reports its address.

    ``settings`` is a callable returning the current ``WebRemoteSettings`` and
    ``listener`` one returning the ``RemoteListener`` (both read at every start, so a
    change applies on the next one).  ``notify(method, params)`` pushes
    ``web.tunnel.changed``.
    """

    def __init__(
        self, paths: Paths, *, settings: Callable[[], Any],
        listener: Callable[[], Any], notify: Notify | None = None,
        spawn: Callable[[list[str], dict[str, str]], Awaitable[Any]] | None = None,
        http_get_json: HttpGetJson | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        fetch: Callable[[str], bytes] | None = None,
    ) -> None:
        self.paths = paths
        self._settings = settings
        self._listener = listener
        self._notify = notify
        self._spawn = spawn or _spawn
        self._http_get_json = http_get_json or _http_get_json
        self.sleep = sleep or asyncio.sleep
        self.clock = clock
        self._fetch = fetch
        self.provider = "off"
        self.state = "off"  # off | starting | running | restarting | error
        self.url: str | None = None
        self.error: str | None = None
        self.restarts = 0
        self._wanted = False
        self._proc: Any = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._secrets: list[str] = []
        self._tail: deque[str] = deque(maxlen=LOG_TAIL_LINES)
        self._lock = asyncio.Lock()
        self.delays: list[float] = []  # the restart delays chosen, in order (tests, status)

    # -- what the rest of the daemon reads --------------------------------
    @property
    def active(self) -> bool:
        """The tunnel program is meant to run (starting, up or between restarts)."""
        return self._wanted and self.state in {"starting", "running", "restarting"}

    @property
    def origin(self) -> str | None:
        return self.url if self.state == "running" else None

    @property
    def host(self) -> str | None:
        return (urlsplit(self.url).hostname or None) if self.url else None

    def is_tunnel_host(self, host: str | None) -> bool:
        """``host`` (a Host header, port allowed) is the tunnel's own address."""
        mine = self.host
        if not mine or not host:
            return False
        text = host.strip().lower()
        name = text[1:text.index("]")] if text.startswith("[") and "]" in text \
            else text.rsplit(":", 1)[0] if text.count(":") == 1 else text
        return name == mine.lower()

    def status(self) -> dict[str, Any]:
        remote = self._remote_settings()
        provider = self.provider if self.provider != "off" else str(
            getattr(remote, "provider", "off"))
        found = find_binary(provider, getattr(remote, "binary", None), self.paths) \
            if provider in PROVIDERS else None
        asset = pinned_asset(provider) if provider in PROVIDERS else None
        return {
            "state": self.state, "provider": provider, "url": self.url, "error": self.error,
            "binary": found, "installable": asset is not None,
            "installCommand": manual_command(provider) if provider in PROVIDERS else None,
            "tokenSet": bool(self._token(provider)) if provider in TOKEN_KEYS else None,
            "restarts": self.restarts,
        }

    # -- start / stop -------------------------------------------------------
    async def start(self, provider: str | None = None, *, wait: float = START_WAIT_SEC
                    ) -> dict[str, Any]:
        """Start the tunnel and wait (up to ``wait`` s) for its address.

        :class:`TunnelError` when a precondition fails; the process failing later shows
        in :meth:`status` and in the ``web.tunnel.changed`` event.
        """
        async with self._lock:
            remote = self._remote_settings()
            chosen = provider or str(getattr(remote, "provider", "off"))
            if chosen not in PROVIDERS:
                raise TunnelError("bad_provider",
                                  "choose a tunnel provider: " + ", ".join(PROVIDERS))
            check_preconditions(self._web(), self._listener(), self.paths)
            if self._wanted:
                await self._stop_locked()
            self._prepare(chosen, remote)  # raises for a missing binary, token or hostname
            self.provider = chosen
            self._wanted = True
            self.error = None
            self.restarts = 0
            self.url = self._fixed_url(chosen, remote)
            await self._set_state("starting")
            self._ready = asyncio.Event()
            self._task = asyncio.ensure_future(self._supervise())
            ready = self._ready
        if wait > 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ready.wait(), wait)
        return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            await self._stop_locked()
        return self.status()

    async def _stop_locked(self) -> None:
        self._wanted = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._kill()
        self.url = None
        self.error = None
        self._ready.set()  # whoever waits on start() stops waiting
        if self.state != "off":
            await self._set_state("off")

    async def install(self, provider: str | None = None) -> dict[str, Any]:
        chosen = provider or str(getattr(self._remote_settings(), "provider", "off"))
        if chosen not in PROVIDERS:
            chosen = "cloudflare-quick"
        path = await install_binary(chosen, self.paths, fetch=self._fetch)
        return {**self.status(), "installedPath": str(path)}

    async def autostart(self) -> None:
        """At daemon start: run the tunnel when the provider is set, ``autoStart`` is on and
        the preconditions hold; otherwise stay off, logging why."""
        remote = self._remote_settings()
        provider = str(getattr(remote, "provider", "off"))
        if provider not in PROVIDERS or not getattr(remote, "autoStart", True):
            return
        try:
            await self.start(provider, wait=0)
        except TunnelError as exc:
            log.warning("web tunnel not started: %s", exc.message)
            self.error = exc.message

    # -- internals -------------------------------------------------------
    def _web(self) -> Any:
        """``web.remote`` of the settings (enabled, host, port…)."""
        return self._settings()

    def _remote_settings(self) -> Any:
        """``web.remote.tunnel`` (provider, autoStart, hostname, binary)."""
        remote = self._settings()
        return getattr(remote, "tunnel", remote)

    def _token(self, provider: str) -> str:
        key = TOKEN_KEYS.get(provider)
        if key is None:
            return ""
        value = CredentialStore(self.paths).load().get(key)
        if isinstance(value, dict):
            value = value.get("token")
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _fixed_url(provider: str, remote: Any) -> str | None:
        if provider == "cloudflare":
            host = str(getattr(remote, "hostname", "") or "").strip()
            return f"https://{host}" if host else None
        return None

    def _prepare(self, provider: str, remote: Any) -> str:
        """Check what the program needs and return its path."""
        binary = find_binary(provider, getattr(remote, "binary", None), self.paths)
        if binary is None:
            raise TunnelError(
                "no_binary",
                f"{binary_name(provider)} was not found; run `snowpea web tunnel install` "
                f"or install it yourself ({manual_command(provider)})",
                command=manual_command(provider))
        if provider in TOKEN_KEYS and not self._token(provider):
            raise TunnelError(
                "no_token", "no token is stored for this provider; set it with "
                f"`snowpea web tunnel token --provider {provider}`")
        if provider == "cloudflare":
            host = str(getattr(remote, "hostname", "") or "").strip()
            if not host or "/" in host or ":" in host:
                raise TunnelError(
                    "no_hostname", "set the tunnel's public hostname (web.remote.tunnel.hostname)")
        return binary

    def _command(self, provider: str, binary: str, port_host: str
                 ) -> tuple[list[str], dict[str, str]]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SNOWPEA_")}
        if provider == "cloudflare-quick":
            return [binary, "tunnel", "--no-autoupdate", "--url", f"http://{port_host}"], env
        if provider == "cloudflare":
            env["TUNNEL_TOKEN"] = self._token(provider)
            return [binary, "tunnel", "--no-autoupdate", "run"], env
        env["NGROK_AUTHTOKEN"] = self._token("ngrok")
        return [binary, "http", port_host, "--log", "stdout", "--log-format", "json"], env

    async def _set_state(self, state: str, error: str | None = None) -> None:
        before = (self.state, self.error)
        self.state = state
        if state in {"running", "off"}:
            self.error = None
        elif error is not None:
            self.error = error  # the last problem stays visible until the tunnel is up
        if before != (self.state, self.error):
            await self._emit()

    async def _emit(self) -> None:
        if self._notify is None:
            return
        params = {"state": self.state, "provider": self.provider, "url": self.url,
                  "error": self.error}
        with contextlib.suppress(Exception):
            await self._notify("web.tunnel.changed", params)

    async def _supervise(self) -> None:
        failures = 0  # consecutive runs that ended
        early = 0  # of those, runs that never showed an address
        try:
            while self._wanted:
                started = self.clock()
                reached = await self._run_once()
                if not self._wanted:
                    return
                ran = self.clock() - started
                if reached and ran >= STABLE_RUN_SEC:
                    failures = early = 0
                failures += 1
                early = 0 if reached else early + 1
                if early >= MAX_EARLY_FAILURES:
                    self._wanted = False
                    await self._set_state(
                        "error", self.error or "the tunnel program keeps exiting before it "
                        "shows an address")
                    self._ready.set()
                    return
                delay = min(BACKOFF_MAX_SEC, BACKOFF_BASE_SEC * (2 ** (failures - 1)))
                self.delays.append(delay)
                self.restarts += 1
                if self._fixed_url(self.provider, self._remote_settings()) is None:
                    self.url = None  # a new quick or ngrok tunnel has a new address
                await self._set_state("restarting", self.error)
                await self.sleep(delay)
                if not self._wanted:
                    return
                await self._set_state("starting")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leave a dead supervisor silently
            log.exception("web tunnel supervisor failed")
            self._wanted = False
            await self._set_state("error", f"the tunnel stopped: {exc}")
            self._ready.set()

    async def _run_once(self) -> bool:
        """One run of the program; ``True`` when it showed an address."""
        remote = self._remote_settings()
        listener = self._listener()
        provider = self.provider
        try:
            binary = self._prepare(provider, remote)
            check_preconditions(self._web(), listener, self.paths)
        except TunnelError as exc:
            self.error = exc.message
            return False
        port_host = target_address(str(listener.host), int(listener.port))
        argv, env = self._command(provider, binary, port_host)
        self._secrets = [self._token(provider)] if provider in TOKEN_KEYS else []
        self._tail.clear()
        try:
            proc = await self._spawn(argv, env)
        except OSError as exc:
            self.error = f"could not start {binary}: {exc.strerror or exc}"
            return False
        self._proc = proc
        reached = False
        api_task: asyncio.Task[None] | None = None
        if provider == "ngrok":
            api_task = asyncio.ensure_future(self._poll_ngrok_api(int(listener.port)))
        deadline = self.clock() + URL_TIMEOUT_SEC
        try:
            while True:
                timeout = max(0.1, deadline - self.clock()) if not reached else None
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout)
                except TimeoutError:
                    self.error = "the tunnel program did not show an address in time"
                    await self._kill()
                    break
                except ValueError:  # a line over the reader's limit: skip it
                    continue
                if not raw:
                    break
                line = redact(raw.decode("utf-8", "replace").rstrip("\r\n")[:MAX_LINE_CHARS],
                              self._secrets)
                self._tail.append(line)
                if await self._on_line(provider, line):
                    reached = True
            code = await self._wait_exit(proc)
            if not reached and self.error is None:
                tail = next((ln for ln in reversed(self._tail) if ln.strip()), "")
                self.error = f"{os.path.basename(binary)} exited ({code})" + (
                    f": {tail[:300]}" if tail else "")
            elif reached:
                self.error = f"{os.path.basename(binary)} exited ({code}); restarting"
            return reached
        finally:
            if api_task is not None:
                api_task.cancel()
                await asyncio.gather(api_task, return_exceptions=True)
            await self._kill()

    async def _on_line(self, provider: str, line: str) -> bool:
        """Feed one log line; ``True`` once the tunnel is up."""
        if provider == "cloudflare-quick":
            url = parse_quick_url(line)
            if url is not None:
                await self._up(url)
                return True
        elif provider == "cloudflare":
            if is_registered(line):
                await self._up(self.url)
                return True
        else:
            url = parse_ngrok_url(line)
            if url is not None:
                await self._up(url)
                return True
        return self.state == "running"

    async def _up(self, url: str | None) -> None:
        if url:
            self.url = url
        if self.url is None:
            return
        await self._set_state("running")
        self._ready.set()

    async def _poll_ngrok_api(self, port: int) -> None:
        await self.sleep(NGROK_API_AFTER_SEC)
        while self.state != "running":
            try:
                url = parse_ngrok_api(await self._http_get_json(NGROK_API_URL), port)
            except Exception:  # noqa: BLE001 - not up yet
                url = None
            if url:
                await self._up(url)
                return
            await self.sleep(NGROK_API_EVERY_SEC)

    async def _wait_exit(self, proc: Any) -> int | None:
        try:
            return await asyncio.wait_for(proc.wait(), STOP_WAIT_SEC)
        except TimeoutError:
            return None

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or getattr(proc, "returncode", 1) is not None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), STOP_WAIT_SEC)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(proc.wait(), STOP_WAIT_SEC)


# ---------------------------------------------------------------------------
# terminal unlock (called by the terminal handlers, T1/T3)
# ---------------------------------------------------------------------------


def is_via_tunnel(conn: RpcConnection) -> bool:
    return getattr(conn, "via_tunnel", False) is True


def terminal_unlocked(conn: RpcConnection) -> bool:
    """Not over the tunnel, or over it with the web password entered again."""
    if not is_via_tunnel(conn):
        return True
    check = getattr(conn, "tunnel_unlocked", None)
    return bool(callable(check) and check() is True)


def require_terminal_unlock(conn: RpcConnection, *, dangerous: bool = False,
                            core: Any = None) -> None:
    """Gate for terminal create, interactive attach, write and the like.

    Over the tunnel it raises :class:`RpcError` (``unavailable``, ``details.reason``
    :data:`TERMINAL_LOCKED`; a client answers with ``web.tunnel.unlock {password}``)
    unless this sign-in session is unlocked.  With ``dangerous=True`` it also refuses,
    over the tunnel, when ``web.remote.maxMode`` is not ``auto`` (pass ``core`` so
    that setting can be read; without it a dangerous call over the tunnel is refused).
    Off the tunnel nothing changes: it returns.
    """
    if not is_via_tunnel(conn):
        return
    if not terminal_unlocked(conn):
        raise RpcError(
            errors.UNAVAILABLE,
            "this connection comes through the web tunnel: enter the web password again to "
            "use terminals (web.tunnel.unlock)",
            {"reason": TERMINAL_LOCKED, "unlock": "web.tunnel.unlock"})
    if dangerous:
        mode = getattr(getattr(getattr(getattr(core, "settings", None), "web", None),
                               "remote", None), "maxMode", None)
        if mode != "auto":
            raise RpcError(
                errors.UNAVAILABLE,
                "dangerous terminal options are not allowed over the web tunnel unless "
                "web.remote.maxMode is auto",
                {"reason": "tunnel_dangerous", "maxMode": mode})


__all__ = [
    "CLOUDFLARE_TOKEN_KEY", "NGROK_TOKEN_KEY", "PINNED", "PROVIDERS", "TERMINAL_LOCKED",
    "TOKEN_KEYS", "PinnedAsset", "TunnelError", "TunnelService", "check_preconditions",
    "find_binary", "install_binary", "is_registered", "is_via_tunnel", "parse_ngrok_api",
    "parse_ngrok_url", "parse_quick_url", "redact", "require_terminal_unlock",
    "terminal_unlocked",
]
