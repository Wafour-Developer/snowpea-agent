"""The tamper guard: stores only the running daemon may write (security reviews 7 and 8).

The security check reads command lines and code; a program that computes its
target while it runs is not stopped by it.  This is the layer that parses
nothing.  While the daemon runs it is meant to be the only writer of

* the trusted-projects record, its migration marker and the legacy
  external-CLI trust file (:mod:`snowpea_core.trust.projects`), and
* the Jarvis delegation store (:mod:`snowpea_core.jarvis.delegation`).

What it does, exactly (security review 8, H8-1):

* The daemon keeps the authoritative copy of each in memory — what it loaded
  when it started, then the bytes it last wrote itself.  A decision is made
  from that copy, never from a second read of the disk: :meth:`FileGuard.verified`
  returns the copy after looking at the file, and the delegation store is an
  in-memory SQLite database that the disk file only mirrors
  (:class:`SqliteGuard`).  So bytes written by another program between the
  look and the use are never the ones a decision reads, and the daemon never
  takes a foreign file as its own: its copy changes only by its own writes.
* Before such a read the guard looks at the file (one ``lstat``; the content
  is read and compared when the ``lstat`` moved, for two seconds after a
  write, and at least once a minute for the SQLite file).  A file that is not
  the daemon's copy is put back from it (atomically, owner-only, never through
  a link), a line goes to the log and to ``logs/security.jsonl``, and the
  owner of the store is told so it can tell the person.  Repeated changes of
  one file are told once at once and then at most once a minute, with a count
  (review 8, L8-1).  When putting the file back fails, the notice says so.

The limits, stated honestly: the guard holds while the daemon runs.  At start
(the daemon was down) the disk is taken as it is.  A program running as the
same user can stop or replace the daemon, or edit snowpea's other files; only
OS-level isolation of the processes snowpea starts would stop that, and that
is separate work (``docs/design/m19-jarvis-mode.md``, review 8).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from snowpea_core.util.atomic import read_owned, write_atomic

log = logging.getLogger("snowpea.security")

#: Largest file a guard keeps a copy of (the stores are a few kilobytes).
MAX_BYTES = 16 * 1024 * 1024
#: How long after a file's last change its ``stat`` alone is not believed (the
#: coarsest timestamps in use are two seconds).
RACY_NS = 2_000_000_000
#: The SQLite file's content is compared at least this often (seconds).
CADENCE_S = 60.0
#: After a notice, further changes of the same file are told at most this often.
NOTICE_WINDOW_S = 60.0
#: SQLite files next to a store that a hot journal or a WAL would be read from.
SIDECARS: tuple[str, ...] = ("-journal", "-wal", "-shm")

#: ``(path, what happened, note)``: ``changed`` | ``removed`` | ``created``;
#: ``note`` is ``{"count": changes told by this call, "restored": the file was
#: put back}``.
Listener = Callable[[str, str, dict[str, Any]], None]
Signature = tuple[int, int, int, int, int]


def _signature(path: Path) -> Signature | None:
    """What a cheap look at ``path`` shows; ``None`` when it is not there."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def _read(path: Path) -> bytes | None:
    """The file's bytes; ``None`` when missing, a link, not the user's or too large."""
    try:
        return read_owned(path, limit=MAX_BYTES)
    except OSError:
        return None


def _fresh(signature: Signature | None) -> bool:
    """The file's timestamps are too close to now to tell two writes apart: a
    second write of the same size in the same clock tick shows the same
    ``stat``.  Until they are old enough, the content itself is compared."""
    if signature is None:
        return False
    return time.time_ns() - max(signature[2], signature[3]) < RACY_NS


def audit(home: str | os.PathLike[str] | None, record: dict[str, Any]) -> None:
    """One line in ``$SNOWPEA_HOME/logs/security.jsonl``; never raises."""
    if not home:
        return
    try:
        logs = Path(os.fspath(home)) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(logs / "security.jsonl", flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
    except OSError:  # pragma: no cover - the audit line never breaks a decision
        log.debug("could not write the tamper audit line", exc_info=True)


class _Notices:
    """L8-1: the first change of a file is told at once, later ones at most once
    per :data:`NOTICE_WINDOW_S`, as one notice with a count."""

    def __init__(self) -> None:
        self.last: float | None = None
        self.pending = 0
        self.happened = ""
        self.restored = True

    def add(self, happened: str, restored: bool) -> tuple[str, int, bool] | None:
        now = time.monotonic()
        if self.last is None or now - self.last >= NOTICE_WINDOW_S:
            told = (happened, 1 + self.pending, restored and self.restored)
            self.last, self.pending, self.restored = now, 0, True
            return told
        self.pending += 1
        self.happened = happened
        self.restored = self.restored and restored
        return None

    def due(self) -> tuple[str, int, bool] | None:
        if not self.pending or self.last is None:
            return None
        now = time.monotonic()
        if now - self.last < NOTICE_WINDOW_S:
            return None
        told = (self.happened, self.pending, self.restored)
        self.last, self.pending, self.restored = now, 0, True
        return told


def _tell(guard: Any, event: tuple[str, int, bool] | None) -> None:
    """Log, audit and notify one (possibly summarised) change; outside any lock."""
    if event is None:
        return
    happened, count, restored = event
    again = f" ({count} times since the last notice)" if count > 1 else ""
    if restored:
        log.warning("%s was %s by something outside snowpea while the daemon runs%s; "
                    "snowpea kept its own copy and put the file back", guard.path, happened,
                    again)
    else:
        log.error("%s was %s by something outside snowpea while the daemon runs%s; "
                  "snowpea kept its own copy but could not put the file back",
                  guard.path, happened, again)
    audit(guard.home, {"kind": "tamper", "path": str(guard.path), "what": guard.what,
                       "happened": happened, "count": count, "restored": restored})
    note = {"count": count, "restored": restored}
    for listener in list(guard.listeners):
        try:
            listener(str(guard.path), happened, note)
        except Exception:  # noqa: BLE001 - a listener never undoes the restore
            log.warning("tamper listener failed", exc_info=True)


class FileGuard:
    """One file only the daemon writes while it runs.

    ``arm()`` takes the disk as it is (daemon start).  ``wrote(data)`` is
    called by the daemon with the bytes it just wrote (``None``: removed).
    :meth:`verified` is what a decision reads: the daemon's copy, after
    undoing any other change.  Thread-safe; an unarmed guard only reads the
    file (a CLI or a test that uses the store without a daemon).
    """

    def __init__(self, path: str | os.PathLike[str], *, home: str | os.PathLike[str] | None = None,
                 what: str = "") -> None:
        self.path = Path(os.fspath(path))
        self.home = home
        self.what = what or self.path.name
        self.armed = False
        self._content: bytes | None = None
        self._signature: Signature | None = None
        #: The ``stat`` alone is believed (see :func:`_fresh`).
        self._settled = False
        self._lock = threading.RLock()
        self._notices = _Notices()
        self.listeners: list[Listener] = []

    def arm(self) -> None:
        with self._lock:
            self._content = _read(self.path)
            self._signature = _signature(self.path)
            self._settled = False
            self.armed = True

    def wrote(self, data: bytes | None) -> None:
        """The daemon itself just wrote ``data`` (``None``: removed the file).

        The copy is the bytes the daemon wrote, never what is read back: a
        program that writes between the daemon's write and this call is
        caught by the next look, not adopted."""
        with self._lock:
            if self.armed:
                self._content = None if data is None else bytes(data)
                self._signature = None
                self._settled = False

    def _look(self) -> tuple[bool, tuple[str, int, bool] | None]:
        """Under the lock: undo a foreign change; ``(changed, notice to give)``."""
        now = _signature(self.path)
        if now == self._signature and self._settled:
            return False, self._notices.due()
        data = _read(self.path)
        if data == self._content and (data is not None or now is None):
            # Untouched, or touched, re-moded or rewritten with the same bytes.
            self._signature = now
            self._settled = not _fresh(now)
            return False, self._notices.due()
        happened = ("removed" if now is None else
                    "created" if self._content is None else "changed")
        restored = self._restore()
        self._signature = _signature(self.path)
        self._settled = False
        return True, self._notices.add(happened, restored)

    def check(self) -> bool:
        """True when the file had been changed behind the daemon's back."""
        with self._lock:
            if not self.armed:
                return False
            changed, event = self._look()
        _tell(self, event)
        return changed

    def verified(self) -> bytes | None:
        """The bytes a decision uses: the daemon's copy (after a look that puts
        back any foreign change) while armed; the file read once otherwise."""
        with self._lock:
            if not self.armed:
                return _read(self.path)
            _changed, event = self._look()
            content = self._content
        _tell(self, event)
        return content

    def _restore(self) -> bool:
        """Put the daemon's copy back: atomically, owner-only, never through a link."""
        try:
            if self._content is None:
                with contextlib.suppress(FileNotFoundError):
                    if self.path.is_dir() and not self.path.is_symlink():
                        return False  # a folder in its place: nothing a tool reads as the store
                    self.path.unlink()
                return True
            write_atomic(self.path, self._content, replace_link=True)
            return True
        except OSError:
            log.warning("could not put %s back", self.path, exc_info=True)
            return False


def drop_sidecars(path: Path) -> list[str]:
    """Remove a journal or WAL next to ``path`` the daemon did not create (M8-4):
    the daemon's store never has one, so one there would only be replayed into
    it by the next open."""
    found: list[str] = []
    for suffix in SIDECARS:
        side = path.with_name(path.name + suffix)
        try:
            os.lstat(side)
        except OSError:
            continue
        try:
            os.unlink(side)
            found.append(side.name)
        except OSError:
            log.warning("could not remove %s", side, exc_info=True)
            found.append(side.name)
    return found


def _rollback_header(data: bytes) -> bytes:
    """A database file whose header says WAL is read as a rollback-journal one:
    an in-memory copy has no WAL (bytes 18 and 19 of the header)."""
    if len(data) >= 20 and data[:16] == b"SQLite format 3\x00" and (data[18], data[19]) == (2, 2):
        return data[:18] + b"\x01\x01" + data[20:]
    return data


class SqliteGuard:
    """The delegation store: the daemon's copy is an in-memory SQLite database.

    :meth:`load` (daemon start) reads the file once — without replaying a
    journal or WAL, which are removed first — into memory; from then on every
    query and every write of the daemon goes to memory, and after each write
    of its own the whole database is written to the file atomically
    (:meth:`wrote`).  The file is only ever an output while the daemon runs.
    :meth:`check` compares the file with the bytes last written and puts them
    back when anything else is there (another commit, a replaced or removed
    file, a journal or WAL next to it).
    """

    def __init__(self, path: str | os.PathLike[str], *, home: str | os.PathLike[str] | None = None,
                 what: str = "") -> None:
        self.path = Path(os.fspath(path))
        self.home = home
        self.what = what or self.path.name
        self.armed = False
        self._written: bytes | None = None
        self._signature: Signature | None = None
        self._settled = False
        self._compared = 0.0
        self._changes = 0
        self._unwritten = False
        self._lock = threading.RLock()
        self._notices = _Notices()
        self.listeners: list[Listener] = []

    def load(self, prepare: Callable[[sqlite3.Connection], None]) -> sqlite3.Connection:
        """Take the store as it is on disk now (the daemon's start) into memory.

        ``prepare`` sets up a connection (row factory, schema).  A file that
        is not a readable database starts an empty store (no grant: the least
        authority) and is said in the log."""
        with self._lock:
            dropped = drop_sidecars(self.path)
            if dropped:
                log.warning("removed %s next to %s before loading it: the daemon never "
                            "leaves one, so it was not replayed", ", ".join(dropped), self.path)
            db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
            data = _read(self.path)
            if data:
                try:
                    db.deserialize(_rollback_header(data))
                    db.execute("SELECT count(*) FROM sqlite_master").fetchone()
                except sqlite3.Error:
                    log.warning("%s is not a readable database; starting an empty store",
                                self.path)
                    db.close()
                    db = sqlite3.connect(":memory:", check_same_thread=False,
                                         isolation_level=None)
            prepare(db)
            self.armed = True
            self._changes = -1  # write the copy out once: the file is the daemon's from now on
            self.wrote(db)
            return db

    def wrote(self, db: sqlite3.Connection) -> None:
        """Called after the daemon's own operations: write what it holds to disk."""
        with self._lock:
            if not self.armed or db.in_transaction or db.total_changes == self._changes:
                return
            data = db.serialize()
            self._written = data
            self._changes = db.total_changes
            self._unwritten = not self._persist()

    def _persist(self) -> bool:
        self._signature = None
        self._settled = False
        try:
            write_atomic(self.path, self._written or b"", replace_link=True)
            return True
        except OSError:
            log.warning("could not write %s; snowpea keeps using its own copy", self.path,
                        exc_info=True)
            return False

    def check(self) -> bool:
        """True when the file had been changed behind the daemon's back (it is put
        back).  Nothing a decision reads comes from the file."""
        with self._lock:
            if not self.armed or self._written is None:
                return False
            changed, event = self._look()
        _tell(self, event)
        return changed

    def _look(self) -> tuple[bool, tuple[str, int, bool] | None]:
        if self._unwritten:
            # The daemon's own last write did not reach the disk: try again, quietly.
            self._unwritten = not self._persist()
            return False, self._notices.due()
        now = _signature(self.path)
        sidecar = any(os.path.lexists(self.path.with_name(self.path.name + suffix))
                      for suffix in SIDECARS)
        moment = time.monotonic()
        if (now == self._signature and self._settled and not sidecar
                and moment - self._compared < CADENCE_S):
            return False, self._notices.due()
        data = _read(self.path)
        self._compared = moment
        if data == self._written and not sidecar:
            self._signature = now
            self._settled = not _fresh(now)
            return False, self._notices.due()
        happened = "removed" if now is None else "changed"
        drop_sidecars(self.path)
        restored = self._persist()
        return True, self._notices.add(happened, restored)

    def disarm(self) -> None:
        with self._lock:
            self.armed = False


__all__ = ["FileGuard", "Listener", "SqliteGuard", "audit", "drop_sidecars"]
