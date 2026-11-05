"""Which machine a daemon runs on (protocol 1.40.0, ``system.hello`` ``machine``).

The id is a UUID made once and kept in ``<home>/machine-id`` (mode 0600), so it
is stable for one machine *and one state home* -- a second home on the same
machine, or a Docker container with no state volume, is a different machine to
a client that pins ids.  Nothing is derived from hardware, so the id reveals
nothing about the box.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Any, Literal

from snowpea_core.server.protocol import MachineInfo

MACHINE_ID_FILE = "machine-id"

#: Set by ``snowpea --docker`` / ``remote up --docker`` on the sandbox container.
SANDBOX_ENV = "SNOWPEA_SANDBOX"

_CGROUP_MARKERS = ("docker", "containerd", "kubepods", "libpod", "lxc")


def os_name() -> Literal["linux", "darwin", "windows"]:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    return "linux"


def in_container() -> bool:
    """True inside Docker (or another Linux container runtime)."""
    if os.environ.get(SANDBOX_ENV):
        return True
    if sys.platform != "linux":
        return False
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        text = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(marker in text for marker in _CGROUP_MARKERS)


def _valid(text: str) -> bool:
    try:
        uuid.UUID(text)
    except ValueError:
        return False
    return True


def machine_id(home: Path | str) -> str:
    """The persisted id of this home; created (0600) on first use."""
    path = Path(home) / MACHINE_ID_FILE
    with contextlib.suppress(OSError):
        existing = path.read_text(encoding="utf-8").strip()
        if _valid(existing):
            return existing
    fresh = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create so two daemons racing on one home agree on one id.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with contextlib.suppress(OSError):
            existing = path.read_text(encoding="utf-8").strip()
            if _valid(existing):
                return existing
        with contextlib.suppress(OSError):
            path.write_text(fresh + "\n", encoding="utf-8")
            path.chmod(0o600)
        return fresh
    except OSError:
        return fresh  # a read-only home: an id that lasts this run
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(fresh + "\n")
    return fresh


def machine_info(home: Path | str) -> MachineInfo:
    """``{id, hostname, os, container}`` for the daemon whose state is in ``home``."""
    return MachineInfo(
        id=machine_id(home),
        hostname=socket.gethostname(),
        os=os_name(),
        container=in_container(),
    )


def machine_of(core: Any) -> MachineInfo:
    """The daemon's :class:`MachineInfo`, made on first use if ``Daemon.start`` did not."""
    if core.machine is None:
        core.machine = machine_info(core.paths.home)
    return core.machine  # type: ignore[no-any-return]


__all__ = [
    "MACHINE_ID_FILE",
    "SANDBOX_ENV",
    "in_container",
    "machine_id",
    "machine_info",
    "machine_of",
    "os_name",
]
