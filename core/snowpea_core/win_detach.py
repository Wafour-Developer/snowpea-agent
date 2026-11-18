"""Windows: a browser-spawned, uv-installed core moves its daemon out of uv's job.

uv's ``snowpea-core.exe`` is a trampoline: it starts the venv's ``python.exe``
inside a job object that kills everything in it when the trampoline goes.  The
snowpea browser (every shipped build, whose launcher cannot change) spawns the
core and keeps it only while ``daemon.json``'s pid is its child's pid; that is
the trampoline here, never the Python that writes the advert, so after 5 s it
ends the child — and the job takes the daemon with it, in a loop.

So when the browser starts a non-frozen core on Windows, this process (the
launcher) starts the real daemon again outside the job (detached, no window),
waits until its advert is ready and ``/health`` answers, lingers so the
browser's poll sees that healthy advert first, then exits with 73.  The old
browser then ends (or reaps) a child that is already gone and reuses the
daemon from the advert; 73 is also what it reads, should it see the exit
first, as "a live daemon holds the home: wait for it".  When the job does not
allow breakaway the daemon runs in-process as before.

Kept free of daemon imports: it runs before ``server.app_server`` loads.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

#: Set in the detached daemon's environment: it never detaches again.
GUARD_ENV = "SNOWPEA_DETACHED_DAEMON"

CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACH_FLAGS = (
    CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
)

#: The browser's exit code for "the home is held by a live daemon: wait for it".
HOME_LOCKED_EXIT = 73
#: How long the launcher waits for the detached daemon's healthy advert.  The
#: browser takes a booting advert after 15 s and gives up at 45 s.
READY_WAIT_S = 40.0
#: After the advert is healthy: long enough for the browser's 100 ms poll (and
#: its 2 s /health probe) to see it before the launcher's exit, well under the
#: 5 s it then waits for the launcher to exit by itself.
LINGER_S = 1.5


def should_detach(
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    frozen: bool | None = None,
) -> bool:
    """True on Windows, not frozen, spawned by the browser, and not already detached."""
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    return (
        plat == "win32"
        and not is_frozen
        and env.get("SNOWPEA_SPAWNED_BY", "").strip() == "browser"
        and not env.get(GUARD_ENV)
    )


def child_command(argv: Sequence[str]) -> list[str]:
    """The detached daemon's argv: this interpreter running the same arguments."""
    return [sys.executable, "-m", "snowpea_core", *argv]


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    env[GUARD_ENV] = "1"
    return env


def spawn_detached(argv: Sequence[str]) -> subprocess.Popen[bytes] | None:
    """Start the daemon outside the job; None (logged) when that is refused."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    try:
        # stdin from NUL; stdout/stderr stay the handles the browser gave us
        # (its launch log): Popen duplicates them into the child.
        return subprocess.Popen(  # noqa: S603 - our own interpreter and arguments
            child_command(argv),
            stdin=subprocess.DEVNULL,
            env=child_env(),
            creationflags=DETACH_FLAGS,
        )
    except OSError as exc:
        print(
            f"snowpea-core: cannot start the daemon outside the launcher's job ({exc}); "
            "running it in this process",
            file=sys.stderr,
            flush=True,
        )
        return None


def _home(home: str | None) -> Path:
    from snowpea_core.config.paths import resolve_home

    return resolve_home(home)


def healthy(port: int, timeout: float = 2.0) -> bool:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/health")
        response = conn.getresponse()
        if response.status != 200:
            return False
        return json.loads(response.read() or b"{}").get("status") == "ok"
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def ready_advert(daemon_json: Path, since: float) -> dict | None:
    """``daemon.json`` when written since ``since`` by another pid, ready, with a port."""
    try:
        if daemon_json.stat().st_mtime < since - 1.0:
            return None
        advert = json.loads(daemon_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(advert, dict) or advert.get("restarting"):
        return None
    port = advert.get("port")
    if (
        advert.get("pid") in (None, os.getpid())
        or advert.get("ready", True) is not True
        or not isinstance(port, int)
        or port <= 0
    ):
        return None
    return advert


def wait_for_daemon(
    child: subprocess.Popen[bytes],
    daemon_json: Path,
    since: float,
    *,
    timeout: float = READY_WAIT_S,
    linger: float = LINGER_S,
    is_healthy: Callable[[int], bool] = healthy,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """The launcher's exit code once the detached daemon is up (or gone)."""
    deadline = clock() + timeout
    while True:
        code = child.poll()
        if code is not None:
            # It died (73: another daemon holds the home): the browser reads
            # the same code it would have had from us.
            return code
        advert = ready_advert(daemon_json, since)
        if advert is not None and is_healthy(advert["port"]):
            print(
                f"snowpea-core: daemon pid {advert['pid']} runs detached on port "
                f"{advert['port']}; launcher exits",
                file=sys.stderr,
                flush=True,
            )
            sleep(linger)
            return HOME_LOCKED_EXIT
        if clock() >= deadline:
            # Still booting: the browser waits for the live advert on 73.
            return HOME_LOCKED_EXIT
        sleep(0.2)


def run_detached(argv: Sequence[str], home: str | None) -> int | None:
    """Detach the daemon and wait for it: the launcher's exit code, or None to run in-process."""
    since = time.time()
    child = spawn_detached(argv)
    if child is None:
        return None
    return wait_for_daemon(child, _home(home) / "daemon.json", since)
