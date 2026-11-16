"""The files the agent wrote, remembered for as long as it matters (review 6, M-8).

A script the agent itself wrote is read by the security check before it runs
(what cannot be read of it, and what it computes, asks the person).  "Its
own" used to mean "changed in the last ten minutes" plus an in-memory set that
a daemon restart forgot.  Now every path a session writes — through a file
tool, through a shell line whose effects the check saw, or found changed after
a shell call by the checkpoint scan — is recorded here: for the whole session
and across restarts.

Security review 8, M8-3: the record is bounded by SESSION, never by path.
When it grows past :data:`MAX_PATHS` the sessions that wrote least recently
go first, and a session that wrote in this daemon process is never dropped.
A session that writes more than :data:`MAX_PER_SESSION` paths stops listing
them one by one and is marked "everything recent": from then on any file
changed since that moment counts as written by the agent.

The record is one file, ``$SNOWPEA_HOME/security/written.log``
(``<session id>\\t<real path>`` per line; ``<session id>\\t@flooded <ns>`` for
the mark), in the control-plane table, so no tool edits it.  Without a home (a
pure check in a test) it is memory only.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("snowpea.security")

DIR_NAME = "security"
FILE_NAME = "written.log"
#: Most paths remembered over all sessions before whole old sessions are dropped.
MAX_PATHS = 50_000
#: Most paths listed for one session; past it the session is "everything recent".
MAX_PER_SESSION = 20_000
_FLOOD = "@flooded "

_LOCK = threading.RLock()
#: real path -> the session that wrote it last (``""`` when unknown).
_PATHS: dict[str, str] = {}
#: session -> None, least recently writing first.
_ORDER: dict[str, None] = {}
#: session -> paths it holds in :data:`_PATHS`.
_COUNT: dict[str, int] = {}
#: session -> the moment (``time_ns``) it became "everything recent".
_FLOODED: dict[str, int] = {}
#: sessions that wrote in this process: never dropped.
_RUNNING: set[str] = set()
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


def _remember(session: str, path: str) -> bool:
    """Add one entry in memory; ``True`` when it is new."""
    _ORDER.pop(session, None)
    _ORDER[session] = None
    if path.startswith(_FLOOD):
        with contextlib.suppress(ValueError):
            _FLOODED.setdefault(session, int(path[len(_FLOOD):]))
        return False
    old = _PATHS.get(path)
    if old == session:
        return False
    if old is not None:
        _COUNT[old] = max(0, _COUNT.get(old, 1) - 1)
    _PATHS[path] = session
    _COUNT[session] = _COUNT.get(session, 0) + 1
    return old is None


def _load() -> None:
    if not _HOME or _LOADED[:1] == _HOME[:1]:
        return
    _LOADED[:] = _HOME[:1]
    try:
        from snowpea_core.util.atomic import read_owned

        text = read_owned(_file(_HOME[0])).decode("utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        session, sep, path = line.partition("\t")
        if sep and path:
            _remember(session, path)
    _trim()


def _drop(session: str) -> None:
    for path in [p for p, s in _PATHS.items() if s == session]:
        del _PATHS[path]
    _COUNT.pop(session, None)
    _ORDER.pop(session, None)
    _FLOODED.pop(session, None)


def _trim() -> None:
    """Drop whole sessions, least recently writing first, never a running one."""
    if len(_PATHS) <= MAX_PATHS:
        return
    for session in list(_ORDER):
        if len(_PATHS) <= MAX_PATHS // 2:
            break
        if session in _RUNNING:
            continue
        _drop(session)
    if _HOME:
        _rewrite()


def _lines() -> str:
    rows = [f"{session}\t{path}\n" for path, session in _PATHS.items()]
    rows += [f"{session}\t{_FLOOD}{moment}\n" for session, moment in _FLOODED.items()]
    return "".join(rows)


def _rewrite() -> None:
    from snowpea_core.util.atomic import write_atomic

    try:
        write_atomic(_file(_HOME[0]), _lines())
    except OSError:
        log.debug("could not rewrite the written-paths record", exc_info=True)


def _append(lines: list[str]) -> None:
    if not lines or not _HOME:
        return
    from snowpea_core.util.atomic import append_text

    target = _file(_HOME[0])
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(target.parent, 0o700)
        append_text(target, "".join(lines))
    except OSError:
        log.debug("could not record written paths", exc_info=True)


def _real(path: str | os.PathLike[str]) -> str | None:
    try:
        return os.path.realpath(os.fspath(path))
    except (OSError, ValueError, TypeError):
        return None


def note(paths: list[str] | tuple[str, ...], session_id: str | None = None) -> None:
    """Remember that the agent wrote ``paths`` (absolute, or already resolved)."""
    session = "".join(ch for ch in str(session_id or "") if ch.isprintable() and ch != "\t")
    lines: list[str] = []
    with _LOCK:
        _RUNNING.add(session)
        for raw in paths:
            real = _real(raw)
            if real is None or "\n" in real or "\t" in real or real.startswith("@"):
                continue
            if session in _FLOODED:
                continue  # everything this session changed from now on counts already
            if _COUNT.get(session, 0) >= MAX_PER_SESSION and _PATHS.get(real) != session:
                # A little before now: file timestamps come from a coarser clock.
                moment = time.time_ns() - 2_000_000_000
                _FLOODED[session] = moment
                lines.append(f"{session}\t{_FLOOD}{moment}\n")
                log.info("session %s wrote more than %d files: from now on every file "
                         "changed since counts as written by the agent", session or "-",
                         MAX_PER_SESSION)
                continue
            if _remember(session, real):
                lines.append(f"{session}\t{real}\n")
        _append(lines)
        _trim()


def _changed_since(real: str, moment: int) -> bool:
    try:
        info = os.stat(real)
    except OSError:
        return False
    return max(info.st_mtime_ns, info.st_ctime_ns) >= moment


def contains(path: str | os.PathLike[str]) -> bool:
    """The agent wrote ``path`` (in any session this record still holds)."""
    real = _real(path)
    if real is None:
        return False
    with _LOCK:
        if real in _PATHS:
            return True
        floods = list(_FLOODED.values())
    return bool(floods) and _changed_since(real, min(floods))


def under(folder: str | os.PathLike[str]) -> bool:
    """The agent wrote something in ``folder`` or below it."""
    real = _real(folder)
    if real is None:
        return False
    prefix = real.rstrip(os.sep) + os.sep
    with _LOCK:
        if real in _PATHS or any(path.startswith(prefix) for path in _PATHS):
            return True
        floods = list(_FLOODED.values())
    if not floods:
        return False
    moment = min(floods)
    budget = 5000
    try:
        for base, _dirs, files in os.walk(real):
            for name in ("", *files):
                budget -= 1
                if budget < 0:
                    return True  # too much to look at: assume the agent's hand
                if _changed_since(os.path.join(base, name) if name else base, moment):
                    return True
    except OSError:
        return False
    return False


def reset() -> None:
    """Forget everything in memory and stop persisting (tests)."""
    with _LOCK:
        _PATHS.clear()
        _ORDER.clear()
        _COUNT.clear()
        _FLOODED.clear()
        _RUNNING.clear()
        _HOME.clear()
        _LOADED.clear()


__all__ = ["MAX_PATHS", "MAX_PER_SESSION", "contains", "note", "reset", "set_home", "under"]
