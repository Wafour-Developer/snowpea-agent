"""``snowpea remote up``: connect to a remote or container daemon, no terminal UI.

For a client that owns the UI itself (the snowpea browser): it runs this command
as a child process, reads JSON lines on stdout and answers questions as JSON
lines on stdin.  The setup is the one ``snowpea --remote`` and ``snowpea
--docker`` use (``cli/remote.py``, ``cli/sandbox.py``); this module only replaces
the terminal: no prompt, no password (ssh runs with ``BatchMode``), no TUI.

    snowpea remote up [user@]host[:port] [--cwd DIR] --json
    snowpea remote up --docker [IMAGE] [--docker-volume NAME] [--docker-keep] --cwd DIR --json

The same entry exists in the bundled ``snowpea-core`` binary, which has no node
and no TUI.

stdout carries one JSON object per line and nothing else::

    {"event":"ready","port":N,"token":"...","version":"...","protocol":"1.40.0",
     "machine":{"id":...,"hostname":...,"os":...,"container":...},"cwd":"..."}
    {"event":"reconnecting"}                 the tunnel or the daemon went away
    {"event":"ready", ...}                   again, after the tunnel is back (port/token may differ)
    {"event":"ask","id":"..","kind":"install","version":".."}
    {"event":"ask","id":"..","kind":"hostKey","fingerprint":"SHA256:..","keyType":"ssh-ed25519"}
    {"event":"error","code":"auth","message":"..."}     fatal; "closed" follows
    {"event":"closed"}

The token appears in ``ready`` on stdout and nowhere else: not on stderr, not in
a log, not in any argv.  An ask is answered with ``{"id":"..","answer":true}``
on stdin.  End of stdin, SIGTERM or SIGINT tears everything down (closes the ssh
master, stops the container unless ``--docker-keep``), emits ``closed`` and
exits 0; an ``error`` event ends in a nonzero exit.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, NoReturn

from snowpea_core import __version__
from snowpea_core.cli import remote, sandbox
from snowpea_core.cli.remote import RemoteError, RemoteSession, RemoteTarget
from snowpea_core.server.protocol import PROTOCOL_VERSION

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

#: How long a connector-held daemon outlives its client (``system.hello`` ``holdSec``).
DEFAULT_HOLD_SEC = 300
#: Errors that a retry cannot fix: the run ends instead of reconnecting.
FATAL_CODES = frozenset(
    {
        "auth",
        "hostKey",
        "notInstalled",
        "installDeclined",
        "versionMajor",
        "dockerMissing",
        "dockerBuild",
    }
)
#: A connection that stays down this long is given up (the held daemon is gone by then).
GIVE_UP_SEC = 600.0
#: The health check of an open tunnel.
HEALTH_EVERY_SEC = 5.0
HEALTH_FAILURES = 3

#: Key types, most preferred first, as ``ssh-keyscan -t`` and known_hosts name them.
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
             "ssh-rsa")  # fmt: skip
_SCAN_NAMES = {
    "ssh-ed25519": "ed25519",
    "ecdsa-sha2-nistp256": "ecdsa",
    "ecdsa-sha2-nistp384": "ecdsa",
    "ecdsa-sha2-nistp521": "ecdsa",
    "ssh-rsa": "rsa",
}
_ALGORITHMS = {"ssh-rsa": "rsa-sha2-512,rsa-sha2-256,ssh-rsa"}


class Stopped(Exception):
    """The client asked to stop (end of stdin, SIGTERM) while the run was working."""


# ---------------------------------------------------------------------------
# stdout: one JSON object per line
# ---------------------------------------------------------------------------


class Emitter:
    """Writes events to the real stdout, one line each, flushed; scrubs secrets elsewhere."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.secrets: set[str] = set()
        self.broken = False

    def scrub(self, text: str) -> str:
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "***")
        return text

    def emit(self, event: str, **fields: Any) -> None:
        payload = {"event": event, **fields}
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if event != "ready":  # the token is for the ready line only
            line = self.scrub(line)
        data = (line + "\n").encode("utf-8")
        with self._lock:
            if self.broken:
                return
            try:
                self._stream.write(data)
                self._stream.flush()
            except (OSError, ValueError):
                self.broken = True  # the client is gone: stdin EOF follows


def log(emitter: Emitter | None, text: str) -> None:
    """A human line on stderr, without the token."""
    shown = emitter.scrub(text) if emitter is not None else text
    print(f"snowpea: {shown}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# stdin: answers to asks, and the end of the run
# ---------------------------------------------------------------------------


class Asks:
    """Questions as events, answered by JSON lines on stdin."""

    def __init__(self, emitter: Emitter, stop: threading.Event) -> None:
        self._emitter = emitter
        self._stop = stop
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, list[bool]]] = {}

    def ask(self, kind: str, **fields: Any) -> bool:
        """Block for the answer; :class:`Stopped` when the client goes away first."""
        ident = uuid.uuid4().hex
        waiter: tuple[threading.Event, list[bool]] = (threading.Event(), [])
        with self._lock:
            self._pending[ident] = waiter
        try:
            self._emitter.emit("ask", id=ident, kind=kind, **fields)
            while not waiter[0].wait(0.2):
                if self._stop.is_set():
                    raise Stopped
            return waiter[1][0]
        finally:
            with self._lock:
                self._pending.pop(ident, None)

    def feed(self, line: str) -> None:
        """One line of stdin: ``{"id":..,"answer":bool}`` (``accept`` is read as well)."""
        try:
            data = json.loads(line)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        answer = data.get("answer", data.get("accept"))
        with self._lock:
            waiter = self._pending.get(str(data.get("id")))
        if waiter is not None and isinstance(answer, bool) and not waiter[0].is_set():
            waiter[1].append(answer)
            waiter[0].set()


def pump_stdin(stream: IO[bytes] | None, asks: Asks, stop: threading.Event) -> threading.Thread:
    """Feed stdin lines to ``asks`` on a thread (a blocking read on a pipe, Windows included).

    ``None`` is the process's own stdin, read with ``os.read`` on descriptor 0: a thread
    blocked in a buffered ``sys.stdin`` read aborts the interpreter when it exits.
    """

    def read() -> bytes:
        if stream is None:
            return os.read(0, 65536)
        return stream.read(65536)

    def run() -> None:
        pending = b""
        try:
            while chunk := read():
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    asks.feed(raw.decode("utf-8", "replace").strip())
        except (OSError, ValueError):
            pass
        finally:
            stop.set()  # end of input: the client is gone

    thread = threading.Thread(target=run, name="remote-up-stdin", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# running local tools without a terminal
# ---------------------------------------------------------------------------


def ssh_runner(emitter: Emitter) -> remote.Runner:
    """``RemoteSession``'s runner: no stdin, stderr captured, stdout never the client's pipe."""

    def run(
        argv: Sequence[str], stdin: str | None, capture: bool
    ) -> subprocess.CompletedProcess[str]:
        try:
            done = subprocess.run(  # noqa: S603 - argv built from a validated target
                list(argv),
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE if capture else 2,  # fd 2: installer output → stderr
                stderr=subprocess.PIPE,
                check=False,
                timeout=240 if capture else 1800,
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteError("ssh did not answer in time", code="unreachable") from exc
        except OSError as exc:
            raise RemoteError(f"could not run ssh: {exc}", reason="sshMissing") from exc
        if done.stderr:
            for line in done.stderr.strip().splitlines():
                log(emitter, line)
        return done

    return run


def docker_runner() -> sandbox.DockerRun:
    """The sandbox's docker runner with the build output on stderr, not on the client's stdout."""

    def run(argv: Sequence[str], capture: bool) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - argv is built by the sandbox
            list(argv),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else 2,
            stderr=subprocess.STDOUT if capture else None,
            check=False,
            timeout=120 if capture else None,
        )

    return run


Exec = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _exec(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed ssh tool names with validated values
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# the host key, before the first connect
# ---------------------------------------------------------------------------


def fingerprint(key_b64: str) -> str:
    """OpenSSH's ``SHA256:`` fingerprint of a base64 public key blob."""
    blob = base64.b64decode(key_b64)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


@dataclass(frozen=True)
class HostKey:
    key_type: str
    key_b64: str

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.key_b64)


def parse_scan(output: str) -> HostKey | None:
    """The preferred key in ``ssh-keyscan`` output (``host keytype base64`` lines)."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#") and parts[1] in KEY_TYPES:
            found.setdefault(parts[1], parts[2])
    for key_type in KEY_TYPES:
        if key_type in found:
            return HostKey(key_type, found[key_type])
    return None


def parse_ssh_config(output: str) -> dict[str, str]:
    """``ssh -G`` output → lower-case option → value (the first line of a repeated one)."""
    config: dict[str, str] = {}
    for line in output.splitlines():
        key, _, value = line.partition(" ")
        if key and key.lower() not in config:
            config[key.lower()] = value.strip()
    return config


def _expand_home(path: str) -> str:
    """``~`` as ssh reads it: the account's home directory, not whatever ``$HOME`` says."""
    if not path.startswith("~"):
        return path
    if sys.platform != "win32":
        import pwd

        with contextlib.suppress(KeyError, OSError):
            home = pwd.getpwuid(os.getuid()).pw_dir
            return home + path[1:] if path[1:2] in ("", "/") else path
    return os.path.expanduser(path)


class HostKeys:
    """Learn whether the host is known to ssh, and ask before the first connect if not.

    ssh itself then runs with ``StrictHostKeyChecking=yes``: an unknown key is never
    accepted behind the client's back, and a changed one is an ``hostKey`` error
    with no question.
    """

    def __init__(
        self,
        ssh: str,
        asks: Asks,
        *,
        execute: Exec = _exec,
        say: Callable[[str], None] = lambda _t: None,
    ) -> None:
        self._ssh = ssh
        self._asks = asks
        self._exec = execute
        self._say = say
        self._keygen = remote.find_ssh_tool("ssh-keygen")
        self._keyscan = remote.find_ssh_tool("ssh-keyscan")

    def ensure(self, target: RemoteTarget) -> list[str]:
        """The ssh ``-o`` options that make the host key check strict (and pin a new key)."""
        strict = ["StrictHostKeyChecking=yes"]
        if not self._ssh:
            return strict  # RemoteSession reports the missing ssh
        argv = [self._ssh, "-G"]
        if target.port:
            argv += ["-p", str(target.port)]
        argv.append(target.destination)
        try:
            done = self._exec(argv)
        except (OSError, subprocess.TimeoutExpired):
            return strict
        if done.returncode != 0:
            return strict
        config = parse_ssh_config(done.stdout or "")
        if (
            config.get("proxycommand", "none") != "none"
            or config.get("proxyjump", "none") != "none"
        ):
            return strict  # cannot scan through a proxy; ssh refuses an unknown key itself
        host = (config.get("hostname") or target.host).strip("[]")
        port = int(config.get("port") or target.port or 22)
        alias = config.get("hostkeyalias")
        lookup = alias or (host if port == 22 else f"[{host}]:{port}")
        user_files = self._files(config.get("userknownhostsfile", "~/.ssh/known_hosts"))
        global_files = self._files(config.get("globalknownhostsfile", ""))
        if any(self._known(lookup, path) for path in [*user_files, *global_files]):
            return strict
        key = self._scan(host, port)
        if key is None:
            raise RemoteError(
                f"could not read the host key of {target.label} (is it reachable?)",
                code="unreachable",
            )
        if not self._asks.ask("hostKey", fingerprint=key.fingerprint, keyType=key.key_type,
                              host=target.label):  # fmt: skip
            raise RemoteError(
                f"the host key of {target.label} was not accepted", code="hostKey",
                reason="rejected",
            )  # fmt: skip
        pinned = [] if user_files else [_expand_home("~/.ssh/known_hosts")]
        self._add(lookup, key, (user_files or pinned)[0])
        extra = [f"UserKnownHostsFile={pinned[0]}"] if pinned else []  # ssh was told "none"
        return [
            *strict,
            f"HostKeyAlgorithms={_ALGORITHMS.get(key.key_type, key.key_type)}",
            *extra,
        ]

    @staticmethod
    def _files(value: str) -> list[str]:
        return [_expand_home(item) for item in value.split() if item.lower() != "none"]

    def _known(self, lookup: str, path: str) -> bool:
        if not self._keygen or not Path(path).is_file():
            return False
        try:
            done = self._exec([self._keygen, "-F", lookup, "-f", path])
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0 and bool((done.stdout or "").strip())

    def _scan(self, host: str, port: int) -> HostKey | None:
        if not self._keyscan:
            return None
        types = ",".join(dict.fromkeys(_SCAN_NAMES.values()))
        try:
            done = self._exec([self._keyscan, "-T", "10", "-p", str(port), "-t", types, host])
        except (OSError, subprocess.TimeoutExpired):
            return None
        return parse_scan(done.stdout or "")

    def _add(self, lookup: str, key: HostKey, path: str) -> None:
        known = Path(path)
        try:
            known.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            prefix = ""
            if known.is_file() and known.stat().st_size:
                with known.open("rb") as handle:
                    handle.seek(-1, os.SEEK_END)
                    prefix = "" if handle.read(1) == b"\n" else "\n"
            fresh = not known.exists()
            with known.open("a", encoding="utf-8") as handle:
                handle.write(f"{prefix}{lookup} {key.key_type} {key.key_b64}\n")
            if fresh and sys.platform != "win32":
                known.chmod(0o600)
        except OSError as exc:
            raise RemoteError(
                f"could not save the host key to {path}: {exc.strerror or exc}", code="internal"
            ) from exc
        self._say(f"added the host key {key.fingerprint} to {path}")


# ---------------------------------------------------------------------------
# what the daemon says about itself
# ---------------------------------------------------------------------------


@dataclass
class Hello:
    version: str = ""
    protocol: str = ""
    machine: dict[str, Any] | None = None
    capabilities: list[str] = field(default_factory=list)


def daemon_hello(
    port: int, token: str, *, hold_sec: int = DEFAULT_HOLD_SEC, timeout: float = 10.0
) -> Hello:
    """``system.hello`` over the tunnel: the daemon's machine, version and protocol.

    ``holdSec`` tells a 1.40 daemon to exit that long after the last connection goes
    instead of after its ordinary idle timeout.  An older daemon rejects the unknown
    field; the hello is then repeated without it and has no ``machine``.
    """
    import aiohttp

    async def once(with_hold: bool) -> dict[str, Any]:
        params: dict[str, Any] = {
            "token": token,
            "clientVersion": __version__,
            "protocolVersion": PROTOCOL_VERSION,
            "clientKind": "connector",
        }
        if with_hold and hold_sec > 0:
            params["holdSec"] = hold_sec
        async with (
            aiohttp.ClientSession() as http,
            http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws,
        ):
            await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "system.hello",
                                "params": params})  # fmt: skip
            frame: dict[str, Any] = await ws.receive_json()
            return frame

    try:
        frame = asyncio.run(asyncio.wait_for(once(True), timeout))
        if "error" in frame and (frame["error"] or {}).get("code") == -32602:
            frame = asyncio.run(asyncio.wait_for(once(False), timeout))  # before 1.40.0
    except (TimeoutError, OSError, ValueError, aiohttp.ClientError) as exc:
        raise RemoteError(f"the daemon did not answer system.hello ({exc})") from exc
    error = frame.get("error")
    if error:
        code = ((error.get("data") or {}).get("code")) or ""
        if code == "protocol_incompatible":
            raise RemoteError(str(error.get("message") or "protocol mismatch"), code="versionMajor")
        raise RemoteError(f"the daemon refused system.hello: {error.get('message')}")
    result = frame.get("result") or {}
    machine = result.get("machine")
    return Hello(
        version=str(result.get("serverVersion") or ""),
        protocol=str(result.get("protocolVersion") or ""),
        machine=machine if isinstance(machine, dict) else None,
        capabilities=[str(c) for c in result.get("capabilities") or []],
    )


# ---------------------------------------------------------------------------
# the two kinds of connection
# ---------------------------------------------------------------------------


@dataclass
class Ready:
    port: int
    token: str = field(repr=False)
    version: str
    protocol: str
    machine: dict[str, Any] | None
    cwd: str


class SshConnection:
    """``remote up host``: the host key, the remote daemon, the tunnel."""

    def __init__(
        self,
        target: RemoteTarget,
        *,
        cwd: str | None,
        hold_sec: int,
        emitter: Emitter,
        asks: Asks,
        ssh: str | None = None,
        run: remote.Runner | None = None,
        execute: Exec = _exec,
        hello: Callable[..., Hello] = daemon_hello,
    ) -> None:
        self.target = target
        self._cwd = cwd
        self._hold = hold_sec
        self._emitter = emitter
        self._asks = asks
        self._ssh = ssh if ssh is not None else remote.find_ssh_tool("ssh")
        self._run = run or ssh_runner(emitter)
        self._exec = execute
        self._hello = hello
        self.session: RemoteSession | None = None
        self._last_port: int | None = None

    def _say(self, text: str) -> None:
        log(self._emitter, text)

    def _confirm_install(self, _question: str) -> bool:
        return self._asks.ask("install", version=__version__, target=self.target.label)

    def start(self) -> Ready:
        options = HostKeys(self._ssh, self._asks, execute=self._exec, say=self._say).ensure(
            self.target
        )
        self.session = RemoteSession(
            self.target,
            local_protocol=PROTOCOL_VERSION,
            confirm=self._confirm_install,
            ssh=self._ssh,
            run=self._run,
            say=self._say,
            batch=True,
            options=options,
        )
        return self._connect()

    def _connect(self) -> Ready:
        session = self.session
        assert session is not None
        advert = session.prepare()
        self._emitter.secrets.add(advert.token)
        port = session.open_tunnel(watch=False, prefer_port=self._last_port)
        self._last_port = port
        return self._ready(port, advert)

    def _ready(self, port: int, advert: remote.Advert) -> Ready:
        try:
            hello = self._hello(port, advert.token, hold_sec=self._hold)
        except RemoteError as exc:
            if exc.code == "versionMajor":
                raise
            self._say(f"{exc}; going on without the daemon's machine identity")
            hello = Hello()
        return Ready(
            port=port,
            token=advert.token,
            version=hello.version or advert.version,
            protocol=hello.protocol or advert.protocol,
            machine=hello.machine,
            cwd=self._cwd or advert.cwd,
        )

    def healthy(self) -> bool:
        session = self.session
        return bool(
            session and session.tunnel_alive() and remote.daemon_healthy(session.local_port)
        )

    def tunnel_down(self) -> bool:
        return not (self.session and self.session.tunnel_alive())

    def recover(self) -> Ready:
        """Probe the host again (its daemon may have restarted with a new token), re-tunnel."""
        assert self.session is not None
        self.session.stop_tunnel()
        return self._connect()

    def close(self) -> None:
        if self.session is not None:
            self.session.close()


class DockerConnection:
    """``remote up --docker``: a throwaway container with the project mounted in."""

    def __init__(
        self,
        options: sandbox.SandboxOptions,
        *,
        hold_sec: int,
        emitter: Emitter,
        run: sandbox.DockerRun | None = None,
        popen: sandbox.DockerPopen | None = None,
        docker: str | None = None,
        hello: Callable[..., Hello] = daemon_hello,
    ) -> None:
        self._emitter = emitter
        self._hold = hold_sec
        self._hello = hello
        self.box = sandbox.Sandbox(
            options,
            local_protocol=PROTOCOL_VERSION,
            docker=docker,
            run=run or docker_runner(),
            popen=popen,
            say=lambda text: log(emitter, text),
        )

    def start(self) -> Ready:
        self.box.prepare_image()
        self.box.start()
        self._emitter.secrets.add(self.box.token)
        try:
            hello = self._hello(self.box.host_port, self.box.token, hold_sec=self._hold)
        except RemoteError as exc:
            if exc.code == "versionMajor":
                self.box.stop()
                raise
            log(self._emitter, f"{exc}; going on without the daemon's machine identity")
            hello = Hello()
        return Ready(
            port=self.box.host_port,
            token=self.box.token,
            version=hello.version,
            protocol=hello.protocol,
            machine=hello.machine,
            cwd=str(self.box.options.cwd),
        )

    def healthy(self) -> bool:
        return self.box.alive and remote.daemon_healthy(self.box.host_port)

    def tunnel_down(self) -> bool:
        return not self.box.alive

    def recover(self) -> Ready:
        # A container that stopped took its sessions with it (unless --docker-volume): no
        # silent second container; the client decides.
        raise RemoteError("the sandbox container stopped", reason="containerExited")

    def close(self) -> None:
        self.box.close()


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def ready_event(ready: Ready) -> dict[str, Any]:
    return {
        "port": ready.port,
        "token": ready.token,
        "version": ready.version,
        "protocol": ready.protocol,
        "machine": ready.machine,
        "cwd": ready.cwd,
    }


def supervise(
    conn: SshConnection | DockerConnection,
    emitter: Emitter,
    stop: threading.Event,
    *,
    health_every: float = HEALTH_EVERY_SEC,
    poll: float = 0.5,
    give_up_sec: float = GIVE_UP_SEC,
    backoff_cap: float = 15.0,
) -> None:
    """Keep the connection open until ``stop``; emit ``reconnecting`` / ``ready`` on a drop.

    Raises :class:`RemoteError` for a failure a retry cannot fix, and when the connection
    stays down for ``give_up_sec``.
    """
    failures = 0
    next_check = time.monotonic() + health_every
    while not stop.wait(poll):
        if conn.tunnel_down():
            failures = HEALTH_FAILURES  # the tunnel process is gone: no need to wait
        elif time.monotonic() >= next_check:
            next_check = time.monotonic() + health_every
            failures = 0 if conn.healthy() else failures + 1
        if failures < HEALTH_FAILURES:
            continue
        emitter.emit("reconnecting")
        began = time.monotonic()
        delay = min(1.0, backoff_cap)
        while not stop.is_set():
            try:
                ready = conn.recover()
            except RemoteError as exc:
                if exc.code in FATAL_CODES or exc.reason == "containerExited":
                    raise
                if time.monotonic() - began > give_up_sec:
                    raise RemoteError(
                        f"could not reconnect for {int(give_up_sec)} s: {exc}",
                        code="unreachable" if exc.code == "internal" else exc.code,
                    ) from exc
                log(emitter, f"reconnecting: {exc}")
                if stop.wait(delay):
                    return
                delay = min(delay * 2, backoff_cap)
                continue
            if stop.is_set():
                return
            emitter.emit("ready", **ready_event(ready))
            failures = 0
            next_check = time.monotonic() + health_every
            break


def run_up(
    conn: SshConnection | DockerConnection,
    emitter: Emitter,
    stop: threading.Event,
    **supervise_options: Any,
) -> int:
    """Connect, announce ``ready``, supervise, tear down.  The process exit code."""
    code = EXIT_OK
    try:
        try:
            ready = conn.start()
            if stop.is_set():
                raise Stopped  # the client left while this was blocking: no ready for it
            emitter.emit("ready", **ready_event(ready))
            supervise(conn, emitter, stop, **supervise_options)
        except Stopped:
            pass
        except RemoteError as exc:
            code = EXIT_ERROR
            fields: dict[str, Any] = {"code": exc.code, "message": str(exc)}
            if exc.reason:
                fields["reason"] = exc.reason
            emitter.emit("error", **fields)
        except Exception as exc:  # noqa: BLE001 - the client is told, never left with a traceback
            code = EXIT_ERROR
            emitter.emit("error", code="internal", message=f"{type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            conn.close()
        emitter.emit("closed")
    return code


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of printing usage and exiting 2."""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The arguments of ``remote up`` (also attached to ``snowpea remote up --help``)."""
    parser.add_argument(
        "target", nargs="?", default=None, metavar="[USER@]HOST[:PORT]",
        help="the machine to reach over ssh (omit with --docker)",
    )  # fmt: skip
    parser.add_argument(
        "--docker", nargs="?", const="", default=None, metavar="IMAGE",
        help="run the daemon in a throwaway Docker container instead (IMAGE must already "
        "have snowpea; the image is built on first use when omitted)",
    )  # fmt: skip
    parser.add_argument(
        "--docker-volume",
        default=None,
        metavar="NAME",
        help="with --docker: keep the sandbox's ~/.snowpea in this volume",
    )
    parser.add_argument(
        "--docker-keep",
        action="store_true",
        help="with --docker: stop the container at the end but do not remove it",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        metavar="DIR",
        help="the project directory (with --docker: mounted at the same path)",
    )
    parser.add_argument("--json", action="store_true", help="required: JSON lines on stdout")
    parser.add_argument(
        "--hold-sec", type=int, default=DEFAULT_HOLD_SEC, metavar="N",
        help="seconds the remote daemon stays up after the last connection goes "
        f"(default {DEFAULT_HOLD_SEC}; 0 keeps the daemon's ordinary idle timeout)",
    )  # fmt: skip
    parser.add_argument("--home", default=None, help="this machine's snowpea home "
                        "(--docker copies its settings into the container)")  # fmt: skip


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="snowpea remote up",
        description="Connect to a remote or container daemon and report it as JSON lines "
        "(no terminal UI).",
    )
    add_arguments(parser)
    return parser


def _usage_error(emitter: Emitter | None, message: str) -> int:
    """A usage error is a JSON ``error`` + ``closed`` when asked for ``--json``, else text."""
    if emitter is not None:
        emitter.emit("error", code="internal", reason="usage", message=message)
        emitter.emit("closed")
    else:
        print(f"snowpea remote up: {message}", file=sys.stderr)
    return EXIT_USAGE


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | None = None,
    install_signals: bool = True,
) -> int:
    """``snowpea remote up …`` / ``snowpea-core remote up …`` (everything after ``remote up``)."""
    raw = list(sys.argv[1:] if argv is None else argv)
    wants_json = "--json" in raw
    # The client's pipe is for events only: anything printed by accident goes to stderr.
    real_out = stdout if stdout is not None else sys.stdout.buffer
    if stdout is None:
        sys.stdout = sys.stderr
    emitter = Emitter(real_out)
    try:
        args = build_parser().parse_args(raw)
    except _UsageError as exc:
        return _usage_error(emitter if wants_json else None, str(exc))
    except SystemExit as exc:  # --help
        return int(exc.code or 0) if isinstance(exc.code, int) else EXIT_OK
    if not args.json:
        return _usage_error(None, "--json is required (this command speaks JSON lines)")

    stop = threading.Event()
    asks = Asks(emitter, stop)
    conn: SshConnection | DockerConnection
    try:
        if args.docker is not None:
            if args.target:
                raise RemoteError("a host and --docker are different places; pick one")
            options = sandbox.options_from_args(
                cwd=args.cwd, home=args.home, image=args.docker or None,
                volume=args.docker_volume, keep=args.docker_keep,
            )  # fmt: skip
            conn = DockerConnection(options, hold_sec=args.hold_sec, emitter=emitter)
        else:
            if not args.target:
                raise RemoteError("give a host to connect to, or --docker")
            if args.docker_volume or args.docker_keep:
                raise RemoteError("--docker-volume and --docker-keep go with --docker")
            conn = SshConnection(
                remote.parse_target(args.target), cwd=args.cwd, hold_sec=args.hold_sec,
                emitter=emitter, asks=asks,
            )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - bad setup is an event, never a traceback
        if not isinstance(exc, RemoteError):
            exc = RemoteError(f"{type(exc).__name__}: {exc}")
        emitter.emit("error", code=exc.code, message=str(exc),
                     **({"reason": exc.reason} if exc.reason else {}))  # fmt: skip
        emitter.emit("closed")
        return EXIT_USAGE if exc.code == "internal" else EXIT_ERROR

    if install_signals:
        _handle_signals(stop)
    pump_stdin(stdin, asks, stop)
    return run_up(conn, emitter, stop)


def _handle_signals(stop: threading.Event) -> None:
    def handler(_signum: int, _frame: object) -> None:
        stop.set()

    names = ["SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"]
    for name in names:
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(number, handler)


__all__ = [
    "Asks",
    "add_arguments",
    "DockerConnection",
    "Emitter",
    "HostKeys",
    "SshConnection",
    "build_parser",
    "daemon_hello",
    "fingerprint",
    "main",
    "parse_scan",
    "run_up",
    "supervise",
]
