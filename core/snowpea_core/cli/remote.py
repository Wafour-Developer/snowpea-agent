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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from snowpea_core import __version__

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
    """The remote session could not be set up; the message says what to do."""


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
            f"{local_protocol}; update the older side (`snowpea update`) and try again"
        )
    if advert.protocol != local_protocol:
        return (
            f"remote snowpea {advert.version} (protocol {advert.protocol}) differs from this "
            f"machine's {local_protocol}; features newer than the older side are unavailable"
        )
    return None


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


class RemoteSession:
    """Bootstraps the remote daemon and keeps the tunnel to it open."""

    def __init__(
        self,
        target: RemoteTarget,
        *,
        local_protocol: str,
        confirm: Callable[[str], bool],
        ssh: str | None = None,
        run: Runner = _run,
        say: Callable[[str], None] | None = None,
    ) -> None:
        self.target = target
        self.local_protocol = local_protocol
        self._confirm = confirm
        self._ssh = ssh or shutil.which("ssh") or ""
        self._run = run
        self._say = say or (lambda text: print(f"snowpea: {text}", file=sys.stderr))
        self._control_dir: str | None = None
        self.advert: Advert | None = None
        self.local_port = 0
        self._tunnel: subprocess.Popen[bytes] | None = None
        self._watch: threading.Thread | None = None
        self._stop = threading.Event()

    # -- ssh -------------------------------------------------------------

    def ssh_argv(self, *extra: str) -> list[str]:
        if not self._ssh:
            raise RemoteError("`ssh` is not installed on this machine")
        argv = [self._ssh, "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
        if self.target.port:
            argv += ["-p", str(self.target.port)]
        if sys.platform != "win32":  # OpenSSH for Windows has no ControlMaster
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
        if done.returncode == START_FAILED:
            raise RemoteError(
                f"snowpea is installed on {self.target.label} but its daemon would not start; "
                "run `snowpea daemon start` there to see why"
            )
        if done.returncode != 0:
            raise RemoteError(
                f"could not reach {self.target.label} over ssh (exit {done.returncode})"
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
            raise RemoteError(f"snowpea is not installed on {self.target.label}")
        script = INSTALL_SCRIPT.format(
            source=INSTALL_SOURCE.format(version=__version__), url=INSTALLER_URL
        )
        done = self._run([*self.ssh_argv("-t"), script], None, False)
        if done.returncode != 0:
            raise RemoteError(
                f"the installer failed on {self.target.label}; install snowpea there by hand "
                "and try again"
            )

    def open_tunnel(self) -> int:
        """Start ``ssh -L`` to the remote daemon; the local port to give the terminal UI."""
        assert self.advert is not None
        self.local_port = free_port()
        self._start_tunnel()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if port_open(self.local_port):
                self._watch = threading.Thread(target=self._keep_open, daemon=True)
                self._watch.start()
                return self.local_port
            if self._tunnel is not None and self._tunnel.poll() is not None:
                break
            time.sleep(0.2)
        self.close()
        raise RemoteError(f"could not open the ssh tunnel to {self.target.label}")

    def _start_tunnel(self) -> None:
        assert self.advert is not None
        forward = f"127.0.0.1:{self.local_port}:127.0.0.1:{self.advert.port}"
        argv = self.ssh_argv("-N", "-o", "ExitOnForwardFailure=yes", "-L", forward)
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
                    [self._ssh, "-O", "exit", "-o", f"ControlPath={self._control_dir}/c-%C",
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


def tui_arguments(session: RemoteSession, cwd: str | None) -> list[str]:
    """The TUI flags that point it at the tunnel, with the remote working directory."""
    assert session.advert is not None
    return [
        "--port", str(session.local_port),
        "--token", session.advert.token,
        "--cwd", cwd or session.advert.cwd or "~",
        "--remote", session.target.label,
    ]  # fmt: skip


__all__ = [
    "Advert",
    "RemoteError",
    "RemoteSession",
    "RemoteTarget",
    "ask_yes_no",
    "check_compatible",
    "parse_probe_output",
    "parse_target",
    "tui_arguments",
]
