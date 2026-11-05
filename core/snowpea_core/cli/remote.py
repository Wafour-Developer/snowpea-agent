"""``snowpea --remote [user@]host``: a session on another machine's daemon.

The agent, its tools, the project files, the memory and the approval queue all
live on the remote host.  This machine only holds the terminal: it reaches the
remote daemon through an ``ssh -L`` tunnel, and the daemon needs no open port
(it listens on the remote's loopback, as it always does).

Steps, in order:

1. one ``ssh`` call to the host (it asks for a password or passphrase once,
   and a ControlMaster socket lets every later call reuse that login);
2. the host has no ``snowpea``: offer to install the same version, with the
   project's own installer;
3. start (or reuse) the remote daemon and read its ``daemon.json``;
4. open the tunnel, keep it open while the terminal UI runs, and open it again
   if it drops.

Nothing here prints the token.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from snowpea_core import __version__
from snowpea_core.cli.render import EXIT_NO_DAEMON, EXIT_OK, EXIT_USAGE

#: ``[user@]host[:port]``.  Host names, IPv4 and bracketed IPv6; no whitespace,
#: no leading dash (that would be read as an ssh option).
_TARGET = re.compile(
    r"^(?:(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]*)@)?"
    r"(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*|\[[0-9A-Fa-f:.]+\])"
    r"(?::(?P<port>\d{1,5}))?$"
)

INSTALLER_URL = (
    "https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh"
)
INSTALL_SOURCE = "git+https://github.com/Wafour-Developer/snowpea-agent@v{version}"

#: Exit codes of :data:`PROBE_SCRIPT` the caller tells apart.
MISSING = 10
START_FAILED = 11

#: Run on the host through ``sh -s``.  ``daemon start`` reuses a live daemon.
PROBE_SCRIPT = f"""\
PATH="$HOME/.local/bin:$PATH"; export PATH
command -v snowpea >/dev/null 2>&1 || {{ echo SNOWPEA_MISSING; exit {MISSING}; }}
snowpea daemon start >/dev/null 2>&1 || {{ echo SNOWPEA_START_FAILED; exit {START_FAILED}; }}
home="${{SNOWPEA_HOME:-$HOME/.snowpea}}"
printf 'SNOWPEA_ADVERT '; tr -d '\\n' < "$home/daemon.json"; echo
printf 'SNOWPEA_CWD %s\\n' "$PWD"
"""

INSTALL_SCRIPT = (
    'SNOWPEA_SKIP_NODE=1 SNOWPEA_SKIP_PATH=1 SNOWPEA_INSTALL_SOURCE="{source}" '
    "sh -c 'curl -fsSL {url} | sh'"
)


class RemoteError(RuntimeError):
    """The remote session could not be set up; the message says what to do.

    ``code`` is what ``snowpea remote up`` reports in its ``error`` event
    (``auth``, ``hostKey``, ``unreachable``, ``notInstalled``, ``installDeclined``,
    ``versionMajor``, ``dockerMissing``, ``dockerBuild``, else ``internal``);
    ``reason`` is an optional finer label for the same failure.
    """

    def __init__(self, message: str, code: str = "internal", reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class RemoteTarget:
    host: str
    user: str | None = None
    port: int | None = None

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def label(self) -> str:
        return self.destination + (f":{self.port}" if self.port else "")


def parse_target(text: str) -> RemoteTarget:
    """``[user@]host[:port]`` → :class:`RemoteTarget`; :class:`RemoteError` otherwise."""
    match = _TARGET.match((text or "").strip())
    if match is None:
        raise RemoteError(
            f"'{text}' is not a host to connect to; use [user@]host or [user@]host:port"
        )
    port = match.group("port")
    if port is not None and not 0 < int(port) < 65536:
        raise RemoteError(f"'{port}' is not a port number")
    return RemoteTarget(
        host=match.group("host"), user=match.group("user"), port=int(port) if port else None
    )


@dataclass(frozen=True)
class Advert:
    """What the remote ``daemon.json`` says (the token stays inside this object)."""

    port: int
    token: str = field(repr=False)
    version: str
    protocol: str
    cwd: str


def parse_probe_output(output: str) -> Advert:
    """Read :data:`PROBE_SCRIPT`'s output; :class:`RemoteError` when it is not there."""
    import json

    advert_line = cwd = None
    for line in output.splitlines():
        if line.startswith("SNOWPEA_ADVERT "):
            advert_line = line[len("SNOWPEA_ADVERT ") :]
        elif line.startswith("SNOWPEA_CWD "):
            cwd = line[len("SNOWPEA_CWD ") :]
    if advert_line is None:
        raise RemoteError("the remote snowpea daemon did not publish daemon.json")
    try:
        data = json.loads(advert_line)
        port, token = int(data["port"]), str(data["token"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RemoteError("the remote daemon.json could not be read") from exc
    if not token or not 0 < port < 65536:
        raise RemoteError("the remote daemon.json has no usable port or token")
    return Advert(
        port=port,
        token=token,
        version=str(data.get("version") or ""),
        protocol=str(data.get("protocolVersion") or ""),
        cwd=cwd or "",
    )


def check_compatible(advert: Advert, local_protocol: str) -> str | None:
    """A warning for a protocol that differs in its minor part; an error for the major."""
    if not advert.protocol:
        return None
    remote_major, local_major = advert.protocol.split(".")[0], local_protocol.split(".")[0]
    if remote_major != local_major:
        raise RemoteError(
            f"the remote snowpea {advert.version} speaks protocol {advert.protocol}, this one "
            f"{local_protocol}; update the older side (`snowpea update`) and try again",
            code="versionMajor",
        )
    if advert.protocol != local_protocol:
        return (
            f"remote snowpea {advert.version} (protocol {advert.protocol}) differs from this "
            f"machine's {local_protocol}; features newer than the older side are unavailable"
        )
    return None


#: What ssh says on stderr, by failure; the first match wins.
_SSH_FAILURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "hostKey",
        (
            "host key verification failed",
            "remote host identification has changed",
            "offending key",
            "no matching host key type",
        ),
    ),
    (
        "auth",
        (
            "permission denied",
            "too many authentication failures",
            "no supported authentication methods",
            "authentication failed",
        ),
    ),
    (
        "unreachable",
        (
            "could not resolve hostname",
            "name or service not known",
            "connection refused",
            "connection timed out",
            "operation timed out",
            "no route to host",
            "network is unreachable",
            "connection closed by",
            "connection reset",
            "kex_exchange_identification",
            "connect to host",
            "broken pipe",
            "ssh_exchange_identification",
        ),
    ),
)


def classify_ssh_failure(returncode: int, stderr: str | None) -> str:
    """The ``error`` code for a failed ssh call (``hostKey``, ``auth``, ``unreachable``, else
    ``internal``).

    Exit 255 is ssh's own failure; any other status is the remote script's.
    """
    text = (stderr or "").lower()
    for code, needles in _SSH_FAILURES:
        if any(needle in text for needle in needles):
            return code
    return "unreachable" if returncode == 255 else "internal"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_open(port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.5):
        return True
    return False


Runner = Callable[[Sequence[str], str | None, bool], "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str], stdin: str | None, capture: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv is built from a validated target
        list(argv),
        input=stdin,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=None,
        check=False,
    )


def find_ssh_tool(name: str = "ssh") -> str:
    """``ssh``, ``ssh-keygen`` or ``ssh-keyscan``: on PATH, else where Windows keeps OpenSSH.

    An app started from the Start menu or the Dock may not have the tools on its PATH.
    """
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32":
        import os

        root = os.environ.get("SystemRoot") or r"C:\Windows"
        candidate = Path(root) / "System32" / "OpenSSH" / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)
    return ""


class RemoteSession:
    """Bootstraps the remote daemon and keeps the tunnel to it open.

    ``batch`` is for a caller with no terminal (``snowpea remote up``): ssh never
    prompts (``BatchMode``), gives up on a silent host (``ConnectTimeout``) and runs
    the installer without a pty.  ``options`` are extra ``-o`` values for every call
    (the host-key pin of ``remote up``).
    """

    def __init__(
        self,
        target: RemoteTarget,
        *,
        local_protocol: str,
        confirm: Callable[[str], bool],
        ssh: str | None = None,
        run: Runner = _run,
        say: Callable[[str], None] | None = None,
        batch: bool = False,
        options: Sequence[str] = (),
    ) -> None:
        self.target = target
        self.local_protocol = local_protocol
        self._confirm = confirm
        self._ssh = ssh if ssh is not None else find_ssh_tool("ssh")
        self._batch = batch
        self._options = list(options)
        self._run = run
        self._say = say or (lambda text: print(f"snowpea: {text}", file=sys.stderr))
        self._control_dir: str | None = None
        self.advert: Advert | None = None
        self.local_port = 0
        self._tunnel: subprocess.Popen[bytes] | None = None
        self._watch: threading.Thread | None = None
        self._stop = threading.Event()

    # -- ssh -------------------------------------------------------------

    def ssh_argv(self, *extra: str, mux: bool = True) -> list[str]:
        if not self._ssh:
            raise RemoteError(
                "`ssh` is not installed on this machine (install the OpenSSH client)",
                reason="sshMissing",
            )
        argv = [self._ssh, "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
        if self._batch:
            argv += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        for option in self._options:
            argv += ["-o", option]
        if self.target.port:
            argv += ["-p", str(self.target.port)]
        if sys.platform != "win32" and not mux:
            argv += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
        elif sys.platform != "win32":  # OpenSSH for Windows has no ControlMaster
            if self._control_dir is None:
                self._control_dir = tempfile.mkdtemp(prefix="snowpea-ssh-")
            argv += [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._control_dir}/c-%C",
                "-o", "ControlPersist=120",
            ]  # fmt: skip
        argv += [*extra, self.target.destination]
        return argv

    def _remote(self, script: str) -> subprocess.CompletedProcess[str]:
        return self._run([*self.ssh_argv(), "sh", "-s"], script, True)

    # -- steps -----------------------------------------------------------

    def prepare(self) -> Advert:
        """Make sure a daemon of this version runs on the host; remember where."""
        self._say(f"connecting to {self.target.label}")
        done = self._remote(PROBE_SCRIPT)
        if done.returncode == MISSING:
            self._install()
            done = self._remote(PROBE_SCRIPT)
            if done.returncode == MISSING:
                raise RemoteError(
                    f"snowpea was installed on {self.target.label} but is still not found there "
                    "(`snowpea` must be on the PATH of a non-interactive ssh login, or in "
                    "~/.local/bin)",
                    code="notInstalled",
                )
        if done.returncode == START_FAILED:
            raise RemoteError(
                f"snowpea is installed on {self.target.label} but its daemon would not start; "
                "run `snowpea daemon start` there to see why",
                reason="daemonFailed",
            )
        if done.returncode != 0:
            raise RemoteError(
                f"could not reach {self.target.label} over ssh (exit {done.returncode})",
                code=classify_ssh_failure(done.returncode, done.stderr),
            )
        advert = parse_probe_output(done.stdout or "")
        warning = check_compatible(advert, self.local_protocol)
        if warning:
            self._say(warning)
        self.advert = advert
        return advert

    def _install(self) -> None:
        question = (
            f"snowpea is not installed on {self.target.label}. Install snowpea {__version__} "
            f"there (runs {INSTALLER_URL} on that host)?"
        )
        if not self._confirm(question):
            raise RemoteError(
                f"snowpea is not installed on {self.target.label}", code="installDeclined"
            )
        script = INSTALL_SCRIPT.format(
            source=INSTALL_SOURCE.format(version=__version__), url=INSTALLER_URL
        )
        argv = self.ssh_argv() if self._batch else self.ssh_argv("-t")
        done = self._run([*argv, script], None, False)
        if done.returncode != 0:
            raise RemoteError(
                f"the installer failed on {self.target.label}; install snowpea there by hand "
                "and try again",
                code="notInstalled",
            )

    def open_tunnel(self, *, watch: bool = True, prefer_port: int | None = None) -> int:
        """Start ``ssh -L`` to the remote daemon; the local port to give the terminal UI.

        ``watch=False`` leaves reopening a dropped tunnel to the caller
        (:meth:`tunnel_alive`, then :meth:`reopen_tunnel`); ``prefer_port`` is the local port
        to try first (the previous one, so a client that kept it need not move).
        """
        assert self.advert is not None
        self.local_port = self._pick_port(prefer_port)
        self._stop.clear()
        self._start_tunnel()
        if self._wait_for_tunnel():
            if watch:
                self._watch = threading.Thread(target=self._keep_open, daemon=True)
                self._watch.start()
            return self.local_port
        if watch:
            self.close()
        else:
            self.stop_tunnel()
        raise RemoteError(
            f"could not open the ssh tunnel to {self.target.label}", code="unreachable"
        )

    @staticmethod
    def _pick_port(prefer: int | None) -> int:
        if prefer is not None and not port_open(prefer):
            with contextlib.suppress(OSError), socket.socket() as probe:
                probe.bind(("127.0.0.1", prefer))
                return prefer
        return free_port()

    def _wait_for_tunnel(self) -> bool:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if port_open(self.local_port):
                return True
            if self._tunnel is not None and self._tunnel.poll() is not None:
                return False
            time.sleep(0.2)
        return False

    def reopen_tunnel(self) -> bool:
        """Start ``ssh -L`` again on the same local port; True when it answers."""
        self.stop_tunnel()
        self._start_tunnel()
        return self._wait_for_tunnel()

    def tunnel_alive(self) -> bool:
        return self._tunnel is not None and self._tunnel.poll() is None

    def stop_tunnel(self) -> None:
        """End the ``ssh -L`` process only; the control master and the session stay."""
        tunnel, self._tunnel = self._tunnel, None
        if tunnel is not None and tunnel.poll() is None:
            tunnel.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                tunnel.wait(timeout=3)

    def _start_tunnel(self) -> None:
        assert self.advert is not None
        forward = f"127.0.0.1:{self.local_port}:127.0.0.1:{self.advert.port}"
        # A key-only (batch) tunnel is its own connection: as a client of the ControlMaster, ssh -N
        # would hand the forward to the master and exit, and its death would say nothing about
        # the tunnel.  An interactive session shares the master so a password is asked once.
        argv = self.ssh_argv(
            "-N", "-o", "ExitOnForwardFailure=yes", "-L", forward, mux=not self._batch
        )
        self._tunnel = subprocess.Popen(  # noqa: S603 - argv is built from a validated target
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL
        )

    def _keep_open(self) -> None:
        """Open the tunnel again when it drops (a network change, a sleep)."""
        delay = 1.0
        while not self._stop.wait(delay):
            if self._tunnel is not None and self._tunnel.poll() is None:
                delay = 1.0
                continue
            with contextlib.suppress(OSError):
                self._start_tunnel()
            delay = min(delay * 2, 15.0)

    def close(self) -> None:
        self._stop.set()
        tunnel, self._tunnel = self._tunnel, None
        if tunnel is not None and tunnel.poll() is None:
            tunnel.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                tunnel.wait(timeout=3)
        if self._control_dir:
            with contextlib.suppress(Exception):
                self._run(
                    [self._ssh, "-q", "-O", "exit", "-o", f"ControlPath={self._control_dir}/c-%C",
                     self.target.destination],
                    None,
                    True,
                )  # fmt: skip
            shutil.rmtree(self._control_dir, ignore_errors=True)
            self._control_dir = None

    def __enter__(self) -> RemoteSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def ask_yes_no(question: str) -> bool:
    """Ask on the terminal; no tty means no."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    print(f"snowpea: {question} [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        return input().strip().lower() in ("y", "yes")
    except EOFError:
        return False


def tui_flags(*, port: int, token: str, cwd: str, label: str) -> list[str]:
    """The TUI flags that point it at a daemon that is not the local one.

    ``label`` is what the screen shows instead of "local" (``me@box``,
    ``docker:snowpea-sandbox-ab12``).  Shared by ``--remote`` and ``--docker``.
    """
    return ["--port", str(port), "--token", token, "--cwd", cwd, "--remote", label]


def tui_arguments(session: RemoteSession, cwd: str | None) -> list[str]:
    """The TUI flags that point it at the tunnel, with the remote working directory."""
    assert session.advert is not None
    return tui_flags(
        port=session.local_port,
        token=session.advert.token,
        cwd=cwd or session.advert.cwd or "~",
        label=session.target.label,
    )


def daemon_healthy(port: int) -> bool:
    """``GET /health`` on the loopback port answers ``{"status": "ok"}``."""
    try:
        with urllib.request.urlopen(  # noqa: S310 - a fixed http://127.0.0.1 URL
            f"http://127.0.0.1:{port}/health", timeout=1.5
        ) as reply:
            return reply.status == 200 and b'"ok"' in reply.read(256)
    except (OSError, ValueError):
        return False


def wait_until_ready(
    port: int,
    *,
    alive: Callable[[], bool] = lambda: True,
    timeout: float = 60.0,
    interval: float = 0.3,
    probe: Callable[[int], bool] = daemon_healthy,
) -> bool:
    """Poll the daemon's ``/health`` until it answers.

    False when ``alive()`` says whatever hosts the daemon is gone, or ``timeout`` ran out.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe(port):
            return True
        if not alive():
            return False
        time.sleep(interval)
    return probe(port)


def run_tui_loop(
    command: Sequence[str],
    open_session: Callable[[], AbstractContextManager[Sequence[str]]],
    *,
    mode: str | None,
    inline: bool,
    restart_code: int,
    say: Callable[[str], None],
    restart_delay: float = 2.0,
) -> int:
    """Run the terminal UI against a daemon somewhere else, again after a restart exit.

    ``open_session()`` returns a context manager that yields the TUI flags for the
    daemon (and cleans up behind it); it raises :class:`RemoteError` when the daemon
    cannot be reached.  The TUI exits with ``restart_code`` after an in-place update:
    the loop then asks for the daemon again, as the first time.  Returns the exit code.
    """
    while True:
        try:
            with open_session() as flags:
                argv = [*command, *flags]
                if mode:
                    argv += ["--mode", mode]
                if inline:
                    argv.append("--inline")
                try:
                    code = subprocess.call(argv)  # noqa: S603 - argv built above
                except FileNotFoundError:
                    say(f"could not run the TUI: {argv[0]} is not on PATH")
                    return EXIT_USAGE
        except RemoteError as exc:
            say(str(exc))
            return EXIT_NO_DAEMON
        except KeyboardInterrupt:  # pragma: no cover - interactive
            return EXIT_OK
        if code != restart_code:
            return code
        time.sleep(restart_delay)


__all__ = [
    "Advert",
    "RemoteError",
    "RemoteSession",
    "RemoteTarget",
    "ask_yes_no",
    "check_compatible",
    "parse_probe_output",
    "daemon_healthy",
    "free_port",
    "parse_target",
    "run_tui_loop",
    "tui_arguments",
    "tui_flags",
    "wait_until_ready",
]
