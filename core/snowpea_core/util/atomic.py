"""One way to write and read the files snowpea keeps (security review 8, H8-2).

Every writer under ``$SNOWPEA_HOME`` and every writer of a project's
``.snowpea/`` files goes through :func:`write_atomic`:

* the content goes to a temp file with a random name in the same folder,
  created with ``O_CREAT | O_EXCL | O_NOFOLLOW`` (a planted file or link at a
  predictable ``name.tmp`` is never written through), owner-only by default,
  flushed to disk, then renamed over the destination;
* the destination itself must not be a symbolic link or anything but a
  regular file: snowpea refuses (:class:`UnsafePath`) instead of writing
  where the link points.

Loaders of files that carry authority (settings, credentials, the global MCP
file, hooks, trust records, the daemon's discovery file) read through
:func:`read_owned`, which refuses a symbolic link and a file that is not owned
by the user running snowpea.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from pathlib import Path
from typing import Any

#: Owner read/write only: the default for every file in the home.
PRIVATE = 0o600

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class UnsafePath(OSError):
    """The path is a symbolic link, not a regular file, or not the user's own."""


def _owned(info: os.stat_result) -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is None or info.st_uid == geteuid()


def refuse_link(path: str | os.PathLike[str]) -> None:
    """Raise :class:`UnsafePath` when ``path`` is a symbolic link or exists as
    anything but a regular file; nothing when it is missing."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePath(f"{os.fspath(path)} is a symbolic link; snowpea does not write "
                         "through it")
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePath(f"{os.fspath(path)} is not a regular file")


def write_atomic(path: str | os.PathLike[str], data: bytes | str, *, mode: int = PRIVATE,
                 replace_link: bool = False, encoding: str = "utf-8") -> os.stat_result:
    """Write ``data`` to ``path`` atomically; returns the written file's ``fstat``.

    ``replace_link``: a symbolic link at ``path`` is replaced by the file
    (never written through) — only the tamper guard, which puts its own copy
    back, asks for that.  Otherwise a link there raises :class:`UnsafePath`.
    """
    target = Path(os.fspath(path))
    if not replace_link:
        refuse_link(target)
    payload = data.encode(encoding) if isinstance(data, str) else bytes(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC | _BINARY
    fd = os.open(tmp, flags, mode)
    try:
        try:
            with contextlib.suppress(AttributeError, OSError):
                os.fchmod(fd, mode)  # the umask does not widen or narrow it
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            info = os.fstat(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return info


def open_append(path: str | os.PathLike[str], *, mode: int = PRIVATE,
                binary: bool = False, encoding: str = "utf-8") -> Any:
    """A file object appending to ``path`` (a log): created owner-only, never
    through a link, never anything but a regular file."""
    target = Path(os.fspath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NOFOLLOW | _CLOEXEC | _BINARY
    try:
        fd = os.open(target, flags, mode)
    except OSError as exc:
        if os.path.islink(target):
            raise UnsafePath(f"{target} is a symbolic link; snowpea does not write through "
                             "it") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafePath(f"{target} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    if binary:
        return os.fdopen(fd, "ab")
    return os.fdopen(fd, "a", encoding=encoding)


def append_text(path: str | os.PathLike[str], text: str, *, mode: int = PRIVATE) -> None:
    """Append ``text`` to ``path`` the way :func:`open_append` opens it."""
    with open_append(path, mode=mode) as handle:
        handle.write(text)


def read_owned(path: str | os.PathLike[str], *, limit: int | None = None) -> bytes:
    """The bytes of ``path``, read once without following a link.

    Raises ``FileNotFoundError`` when it is missing and :class:`UnsafePath`
    when it is a symbolic link, not a regular file, not owned by the user, or
    larger than ``limit``.
    """
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _BINARY)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if os.path.islink(os.fspath(path)):
            raise UnsafePath(f"{os.fspath(path)} is a symbolic link; snowpea does not "
                             "read it") from exc
        raise
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePath(f"{os.fspath(path)} is not a regular file")
        if not _owned(info):
            raise UnsafePath(f"{os.fspath(path)} is not owned by you; snowpea does not use it")
        if not _NOFOLLOW and os.path.islink(os.fspath(path)):
            raise UnsafePath(f"{os.fspath(path)} is a symbolic link; snowpea does not read it")
        data = handle.read() if limit is None else handle.read(limit + 1)
    if limit is not None and len(data) > limit:
        raise UnsafePath(f"{os.fspath(path)} is larger than {limit} bytes")
    return data


__all__ = ["PRIVATE", "UnsafePath", "append_text", "open_append", "read_owned", "refuse_link",
           "write_atomic"]
