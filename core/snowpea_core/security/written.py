"""The files the agent wrote, remembered for as long as it matters (review 6, M-8).

A script the agent itself wrote is judged strictly: what cannot be read of it,
and what it computes, asks the person.  "Its own" used to mean "changed in the
last ten minutes" plus an in-memory set that a daemon restart forgot.  Now
every path a session writes — through a file tool, through a shell line whose
effects the check saw, or found changed after a shell call by the checkpoint
scan — is recorded here and stays recorded: for the whole session and across
restarts.

The record is one append-only file, ``$SNOWPEA_HOME/security/written.log``
(``<session id>\\t<real path>`` per line, in the control-plane table, so no
tool edits it).  Without a home (a pure check in a test) it is memory only.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("snowpea.security")

DIR_NAME = "security"
FILE_NAME = "written.log"
#: Most paths remembered; the oldest half goes when it is reached.
MAX_PATHS = 50_000

_LOCK = threading.RLock()
#: real path -> the session that wrote it last (``""`` when unknown).
_PATHS: dict[str, str] = {}
_HOME: list[str] = []
_LOADED: list[str] = []


def _file(home: str) -> Path:
    return Path(home) / DIR_NAME / FILE_NAME


def set_home(home: str | os.PathLike[str] | None) -> None:
    """Persist under ``home`` from now on (and read what an earlier daemon recorded)."""
    with _LOCK:
        value = os.fspath(home) if home else ""
        if _HOME[:1] == [value]:
            return
        _HOME[:] = [value] if value else []
        _load()


def _load() -> None:
    if not _HOME or _LOADED[:1] == _HOME[:1]:
        return
    _LOADED[:] = _HOME[:1]
    try:
        with _file(_HOME[0]).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                session, sep, path = line.rstrip("\n").partition("\t")
                if sep and path:
                    _PATHS[path] = session
    except OSError:
        return
    _trim()


def _trim() -> None:
    if len(_PATHS) <= MAX_PATHS:
        return
    for old in list(_PATHS)[: MAX_PATHS // 2]:
        _PATHS.pop(old, None)
    if _HOME:
        _rewrite()


def _rewrite() -> None:
    target = _file(_HOME[0])
    tmp = target.with_name(f".{FILE_NAME}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.writelines(f"{session}\t{path}\n" for path, session in _PATHS.items())
        os.replace(tmp, target)
    except OSError:
        log.debug("could not rewrite the written-paths record", exc_info=True)
        with contextlib.suppress(OSError):
            tmp.unlink()


def _real(path: str | os.PathLike[str]) -> str | None:
    try:
        return os.path.realpath(os.fspath(path))
    except (OSError, ValueError, TypeError):
        return None


def note(paths: list[str] | tuple[str, ...], session_id: str | None = None) -> None:
    """Remember that the agent wrote ``paths`` (absolute, or already resolved)."""
    session = "".join(ch for ch in str(session_id or "") if ch.isprintable() and ch != "\t")
    fresh: list[str] = []
    with _LOCK:
        for raw in paths:
            real = _real(raw)
            if real is None or "\n" in real or "\t" in real:
                continue
            if _PATHS.get(real) is None:
                fresh.append(real)
            _PATHS[real] = session
        if fresh and _HOME:
            target = _file(_HOME[0])
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    os.chmod(target.parent, 0o700)
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(target, flags, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.writelines(f"{session}\t{path}\n" for path in fresh)
            except OSError:
                log.debug("could not record written paths", exc_info=True)
        _trim()


def contains(path: str | os.PathLike[str]) -> bool:
    """The agent wrote ``path`` (in any session this record still holds)."""
    real = _real(path)
    with _LOCK:
        return real is not None and real in _PATHS


def under(folder: str | os.PathLike[str]) -> bool:
    """The agent wrote something in ``folder`` or below it."""
    real = _real(folder)
    if real is None:
        return False
    prefix = real.rstrip(os.sep) + os.sep
    with _LOCK:
        return real in _PATHS or any(path.startswith(prefix) for path in _PATHS)


def reset() -> None:
    """Forget everything in memory and stop persisting (tests)."""
    with _LOCK:
        _PATHS.clear()
        _HOME.clear()
        _LOADED.clear()


__all__ = ["contains", "note", "reset", "set_home", "under"]
