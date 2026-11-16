"""The tamper guard: stores only the running daemon may write (security review 7, P2).

The security check reads command lines and code; a program that computes its
target while it runs is not stopped by it.  This is the layer that parses
nothing.  While the daemon runs it is the only legitimate writer of

* the trusted-projects record, its migration marker and the legacy
  external-CLI trust file (:mod:`snowpea_core.trust.projects`), and
* the Jarvis delegation store (:mod:`snowpea_core.jarvis.delegation`).

The daemon keeps the authoritative copy of each — what it loaded when it
started, then what it last wrote.  Before every read that feeds a decision the
guard looks at the file (one ``stat``; the content is read and compared only
when the ``stat`` changed).  When the file is not what the daemon last wrote,
the change is NOT honoured: the daemon's copy stays in force, the file is put
back from it (atomically, owner-only), a line goes to the log and to
``logs/security.jsonl``, and the owner of the store is told so it can tell the
person.

No thread, no watcher: the check runs where the store is read, so a change
made behind the daemon's back can never be the one a decision uses.  Its cost
is one ``stat`` per guarded file per read (the SQLite store: one ``stat`` and
one ``PRAGMA data_version``).  For the two seconds after a file was last
written the content is read and compared too: two writes of the same size in
one clock tick show the same ``stat``.

The limit, stated honestly: the guard holds while the daemon runs.  At start
(the daemon was down) the disk is taken as it is.  A program of the same user
could also stop the daemon; OS-level isolation of tool processes is the real
boundary and is separate work.
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

log = logging.getLogger("snowpea.security")

#: Largest file a guard keeps a copy of (the stores are a few kilobytes).
MAX_BYTES = 16 * 1024 * 1024
#: How long after a file's last change its ``stat`` alone is not believed (the
#: coarsest timestamps in use are two seconds).
RACY_NS = 2_000_000_000
#: ``(path, what happened)``: ``changed`` | ``removed`` | ``created``.
Listener = Callable[[str, str], None]


def _signature(path: Path) -> tuple[int, int, int, int, int] | None:
    """What a cheap look at ``path`` shows; ``None`` when it is not there."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def _read(path: Path) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        return None
    return data if len(data) <= MAX_BYTES else None


def audit(home: str | os.PathLike[str] | None, record: dict[str, Any]) -> None:
    """One line in ``$SNOWPEA_HOME/logs/security.jsonl``; never raises."""
    if not home:
        return
    try:
        logs = Path(os.fspath(home)) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
        with (logs / "security.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
    except OSError:  # pragma: no cover - the audit line never breaks a decision
        log.debug("could not write the tamper audit line", exc_info=True)


class FileGuard:
    """One file only the daemon writes while it runs.

    ``arm()`` takes the disk as it is (daemon start).  ``wrote()`` is called by
    the daemon right after its own write.  ``check()`` undoes anything else.
    Thread-safe; an unarmed guard does nothing (a CLI or a test that uses the
    store without a daemon).
    """

    def __init__(self, path: str | os.PathLike[str], *, home: str | os.PathLike[str] | None = None,
                 what: str = "") -> None:
        self.path = Path(os.fspath(path))
        self.home = home
        self.what = what or self.path.name
        self.armed = False
        self._content: bytes | None = None
        self._signature: tuple[int, int, int, int, int] | None = None
        #: The ``stat`` alone is believed (see :meth:`_fresh`).
        self._settled = False
        self._lock = threading.RLock()
        self.listeners: list[Listener] = []

    def _capture(self) -> None:
        self._content = _read(self.path)
        self._signature = _signature(self.path)
        self._settled = False

    def arm(self) -> None:
        with self._lock:
            self._capture()
            self.armed = True

    def wrote(self) -> None:
        """The daemon itself just wrote (or removed) the file: that is the copy now."""
        with self._lock:
            if self.armed:
                self._capture()

    def _fresh(self, signature: tuple[int, int, int, int, int] | None) -> bool:
        """The file's timestamps are too close to now to tell two writes apart: a
        second write of the same size in the same clock tick shows the same
        ``stat``.  Until they are old enough, the content itself is compared."""
        if signature is None:
            return False
        return time.time_ns() - max(signature[2], signature[3]) < RACY_NS

    def check(self) -> bool:
        """True when the file had been changed behind the daemon's back (and was put back)."""
        with self._lock:
            if not self.armed:
                return False
            now = _signature(self.path)
            if now == self._signature and self._settled:
                return False
            data = _read(self.path)
            if data == self._content and (data is not None or now is None):
                # Untouched, or touched, re-moded or rewritten with the same bytes.
                self._signature = now
                self._settled = not self._fresh(now)
                return False
            happened = ("removed" if now is None else
                        "created" if self._content is None else "changed")
            self._restore()
            self._signature = _signature(self.path)
            self._settled = False
        log.warning("%s was %s by something outside snowpea while the daemon runs; "
                    "the change was not applied and the file was put back", self.path, happened)
        audit(self.home, {"kind": "tamper", "path": str(self.path), "what": self.what,
                          "happened": happened})
        for listener in list(self.listeners):
            try:
                listener(str(self.path), happened)
            except Exception:  # noqa: BLE001 - a listener never undoes the restore
                log.warning("tamper listener failed", exc_info=True)
        return True

    def _restore(self) -> None:
        """Put the daemon's copy back: atomically, owner-only, never through a link."""
        try:
            if self._content is None:
                with contextlib.suppress(FileNotFoundError):
                    if self.path.is_dir() and not self.path.is_symlink():
                        return  # a folder in its place: nothing a tool reads as the store
                    self.path.unlink()
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.guard")
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(tmp, flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(self._content)
            os.replace(tmp, self.path)
        except OSError:
            log.warning("could not put %s back", self.path, exc_info=True)


class SqliteGuard:
    """The same guard for a SQLite store the daemon keeps open.

    The daemon's connection is the only writer.  ``PRAGMA data_version``
    changes on that connection exactly when ANOTHER connection commits, and a
    replaced or removed file shows as another inode: either is a change behind
    the daemon's back.  The authoritative copy is a snapshot in memory, taken
    when the guard is armed and after every write of the daemon's own
    (:meth:`wrote`); :meth:`check` copies it back over the file.
    """

    def __init__(self, path: str | os.PathLike[str], *, home: str | os.PathLike[str] | None = None,
                 what: str = "") -> None:
        self.path = Path(os.fspath(path))
        self.home = home
        self.what = what or self.path.name
        self.armed = False
        self._snapshot: sqlite3.Connection | None = None
        self._version: int | None = None
        self._changes = 0
        self._inode: int | None = None
        self.listeners: list[Listener] = []

    @staticmethod
    def _data_version(db: sqlite3.Connection) -> int:
        return int(db.execute("PRAGMA data_version").fetchone()[0])

    def _stat(self) -> int | None:
        try:
            return os.stat(self.path).st_ino
        except OSError:
            return None

    def _take(self, db: sqlite3.Connection) -> None:
        snapshot = sqlite3.connect(":memory:", check_same_thread=False)
        db.backup(snapshot)
        if self._snapshot is not None:
            self._snapshot.close()
        self._snapshot = snapshot
        self._version = self._data_version(db)
        self._changes = db.total_changes
        self._inode = self._stat()

    def arm(self, db: sqlite3.Connection) -> None:
        """Take the store as it is now (daemon start)."""
        self._take(db)
        self.armed = True

    def wrote(self, db: sqlite3.Connection) -> None:
        """Called after the daemon's own operations: keep what it wrote."""
        if self.armed and not db.in_transaction and db.total_changes != self._changes:
            self._take(db)

    def check(self, db: sqlite3.Connection,
              reopen: Callable[[], sqlite3.Connection]) -> sqlite3.Connection:
        """The connection to use: ``db`` itself, or a fresh one over the restored file."""
        if not self.armed or self._snapshot is None or db.in_transaction:
            return db
        inode = self._stat()
        replaced = inode != self._inode
        if not replaced and self._data_version(db) == self._version:
            return db
        happened = "removed" if inode is None else "changed"
        try:
            if replaced:
                # Another file (or none) sits at the path: the daemon's connection
                # still holds the old one.  Start over on the path, from the copy.
                with contextlib.suppress(sqlite3.Error):
                    db.close()
                for suffix in ("", "-wal", "-shm", "-journal"):
                    with contextlib.suppress(OSError):
                        os.unlink(self.path.with_name(self.path.name + suffix))
                db = reopen()
            self._snapshot.backup(db)
        except (sqlite3.Error, OSError):
            log.warning("could not put %s back", self.path, exc_info=True)
        self._version = self._data_version(db)
        self._changes = db.total_changes
        self._inode = self._stat()
        log.warning("%s was %s by something outside snowpea while the daemon runs; "
                    "the change was not applied and the store was put back", self.path, happened)
        audit(self.home, {"kind": "tamper", "path": str(self.path), "what": self.what,
                          "happened": happened})
        for listener in list(self.listeners):
            try:
                listener(str(self.path), happened)
            except Exception:  # noqa: BLE001 - a listener never undoes the restore
                log.warning("tamper listener failed", exc_info=True)
        return db


__all__ = ["FileGuard", "Listener", "SqliteGuard", "audit"]
