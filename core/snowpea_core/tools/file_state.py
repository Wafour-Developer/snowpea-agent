"""Read-before-write guard (M15 §A3).

A per-session registry of what each session has read and who wrote what, so
``edit_file`` and ``write_file`` can refuse a write that would be blind:

* the file was never read in this session,
* it was read only partially (a windowed or truncated ``read_file``),
* a sibling subagent under the same parent wrote it after this session read it.

The rule already lives in the prompt; this makes it true.  Ported in shape from
Hermes ``tools/file_state.py`` (MIT) — see
``docs/design/deviations/CORE-policies.md`` — but keyed on snowpea's session ids
and on a content hash rather than an mtime, and grouped by the *parent* session
so a fan-out of subagents shares one view of the tree.

Turn it off with ``tools.readBeforeWrite: false``.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.tools.file_state")

#: Error code every refusal carries, so a surface can recognise it.
STALE_CODE = "stale_file"

#: Paths remembered per session before the oldest are dropped.
MAX_PATHS_PER_SESSION = 4096


def digest(text: str) -> str:
    """Content hash a read is recorded under."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


@dataclass
class ReadRecord:
    """One session's last read of one path."""

    hash: str
    read_complete: bool
    ts: float


@dataclass
class WriteRecord:
    """The last session to write one path, and when."""

    session_id: str
    hash: str
    ts: float


@dataclass
class _Group:
    """One parent's view of the tree: its own reads and its children's."""

    reads: dict[str, dict[str, ReadRecord]] = field(default_factory=dict)
    writers: dict[str, WriteRecord] = field(default_factory=dict)


class FileStateRegistry:
    """``path -> {hash, read_complete, last_writer_session}``, grouped by parent.

    Every method takes the *group* (the owning parent session id) and the
    session doing the work, because a subagent's reads must not satisfy its
    parent's read-before-write obligation, while a subagent's *writes* must
    still make its siblings stale.
    """

    def __init__(self) -> None:
        self._groups: dict[str, _Group] = {}
        self._lock = threading.Lock()

    # -- recording -----------------------------------------------------
    def record_read(
        self, group: str, session_id: str, path: str, content: str, *, complete: bool
    ) -> None:
        with self._lock:
            reads = self._groups.setdefault(group, _Group()).reads.setdefault(session_id, {})
            reads[path] = ReadRecord(hash=digest(content), read_complete=complete, ts=time.time())
            self._evict(reads)

    def record_write(self, group: str, session_id: str, path: str, content: str) -> None:
        """A successful write: the writer is now current, siblings are stale."""
        with self._lock:
            state = self._groups.setdefault(group, _Group())
            now = time.time()
            state.writers[path] = WriteRecord(
                session_id=session_id, hash=digest(content), ts=now
            )
            reads = state.reads.setdefault(session_id, {})
            reads[path] = ReadRecord(hash=digest(content), read_complete=True, ts=now)
            self._evict(reads)

    @staticmethod
    def _evict(reads: dict[str, ReadRecord]) -> None:
        for _ in range(len(reads) - MAX_PATHS_PER_SESSION):
            reads.pop(next(iter(reads)), None)

    # -- checking ------------------------------------------------------
    def check(self, group: str, session_id: str, path: str, *, exists: bool) -> str | None:
        """The one-line reason this write is refused, or ``None`` to allow it."""
        with self._lock:
            state = self._groups.get(group)
            record = state.reads.get(session_id, {}).get(path) if state else None
            writer = state.writers.get(path) if state else None

        if writer is not None and writer.session_id != session_id:
            if record is None:
                return (
                    f"{path} was written by another session ({writer.session_id}) and this "
                    "session has never read it; read_file it first so you do not overwrite "
                    "that change"
                )
            if writer.ts > record.ts:
                return (
                    f"{path} was written by another session ({writer.session_id}) after you "
                    "read it; read_file it again before writing"
                )
        if record is None:
            if not exists:
                return None  # creating a new file needs no prior read
            return f"{path} has not been read in this session; read_file it before writing"
        if not record.read_complete:
            return (
                f"{path} was only read in part (offset/limit or a truncated read); read_file "
                "the whole file before writing it"
            )
        return None

    # -- housekeeping --------------------------------------------------
    def forget_session(self, group: str, session_id: str) -> None:
        with self._lock:
            state = self._groups.get(group)
            if state is not None:
                state.reads.pop(session_id, None)

    def clear(self) -> None:
        """Drop everything (tests)."""
        with self._lock:
            self._groups.clear()


#: One registry per process; subagents share their parent's group inside it.
REGISTRY = FileStateRegistry()


def group_for(session: Any) -> str:
    """The registry group a session belongs to: its parent's id, or its own.

    ``Session.parent_session_id`` is set on every child ``delegate_task`` and
    ``agent.spawn`` open, so a fan-out of siblings resolves to one group and a
    human's session is a group of one.
    """
    parent = getattr(session, "parent_session_id", None)
    return str(parent or getattr(session, "id", "") or "-")


def enabled(core: Any) -> bool:
    """``tools.readBeforeWrite`` (default true)."""
    tools = getattr(getattr(core, "settings", None), "tools", None)
    return bool(getattr(tools, "readBeforeWrite", True))


def resolve(session: Any, path: str) -> str:
    """The key a path is remembered under: absolute, resolved against workdir."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        workdir = getattr(session, "workdir", None)
        if workdir is not None:
            candidate = Path(workdir) / candidate
    try:
        return str(candidate.resolve())
    except OSError:  # pragma: no cover - exotic filesystems
        return str(candidate)


def note_read(core: Any, session: Session, path: str, content: str, *, complete: bool) -> None:
    """Record a ``read_file`` result."""
    if not enabled(core):
        return
    REGISTRY.record_read(
        group_for(session), str(session.id), resolve(session, path), content, complete=complete
    )


def note_write(core: Any, session: Session, path: str, content: str) -> None:
    """Record a successful ``write_file`` / ``edit_file``."""
    if not enabled(core):
        return
    REGISTRY.record_write(
        group_for(session), str(session.id), resolve(session, path), content
    )


def check_stale(core: Any, session: Session, path: str, *, exists: bool) -> str | None:
    """``None`` when the write may proceed, else the ``stale_file`` reason."""
    if not enabled(core):
        return None
    return REGISTRY.check(
        group_for(session), str(session.id), resolve(session, path), exists=exists
    )


__all__ = [
    "MAX_PATHS_PER_SESSION",
    "REGISTRY",
    "STALE_CODE",
    "FileStateRegistry",
    "ReadRecord",
    "WriteRecord",
    "check_stale",
    "digest",
    "enabled",
    "group_for",
    "note_read",
    "note_write",
    "resolve",
]
