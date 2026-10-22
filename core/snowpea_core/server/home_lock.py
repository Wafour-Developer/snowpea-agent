"""One daemon per ``$SNOWPEA_HOME``: the ``daemon.lock`` file lock.

Kept free of the server's imports so the CLI can probe the lock cheaply.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any


class HomeLocked(RuntimeError):
    """Another daemon already holds this home's lock."""


#: Exit status of a daemon that found its home locked (EX_CANTCREAT).
HOME_LOCKED_EXIT = 73


#: Where the Windows lock sits in ``daemon.lock``: a byte far past the pid
#: written at the start, because a ``msvcrt`` lock is mandatory and would
#: keep the next daemon from reading who holds it.
_WINDOWS_LOCK_OFFSET = 1 << 20


def _lock_handle(handle: Any) -> None:
    """Take the exclusive, non-blocking lock on ``handle``; ``OSError`` if held."""
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        handle.seek(_WINDOWS_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def acquire_home_lock(home: Path) -> Any:
    """Hold ``<home>/daemon.lock`` for this process's lifetime, or raise :class:`HomeLocked`.

    ``flock`` on POSIX, ``msvcrt.locking`` on Windows: either way the OS
    drops it when the process dies, however it dies, so a crashed daemon
    never leaves a stale lock behind. On Windows an IDE and a browser that
    start the core at the same moment used to get two daemons on one home.
    """
    path = Path(home) / "daemon.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    handle = os.fdopen(fd, "r+")  # noqa: SIM115 - held for the process
    try:
        _lock_handle(handle)
    except OSError as exc:
        handle.seek(0)
        holder = handle.read(32).strip("\0 \r\n")
        handle.close()
        raise HomeLocked(
            f"another snowpea daemon{f' (pid {holder})' if holder else ''} is running on {home}"
        ) from exc
    handle.seek(0)
    handle.write(str(os.getpid()).ljust(16))
    handle.truncate()
    handle.flush()
    return handle


def home_is_locked(home: Path) -> bool:
    """True when a daemon holds ``home``'s lock (a probe; it is released at once)."""
    if not (Path(home) / "daemon.lock").exists():
        return False
    try:
        handle = acquire_home_lock(home)
    except HomeLocked:
        return True
    except OSError:
        return False
    release_home_lock(handle)
    return False


def release_home_lock(handle: Any) -> None:
    if handle is None:
        return
    with contextlib.suppress(Exception):
        try:
            import fcntl
        except ImportError:  # Windows
            import msvcrt

            handle.seek(_WINDOWS_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(Exception):
        handle.close()


__all__ = [
    "HOME_LOCKED_EXIT",
    "HomeLocked",
    "acquire_home_lock",
    "home_is_locked",
    "release_home_lock",
]
