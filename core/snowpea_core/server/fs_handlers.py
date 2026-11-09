"""``fs.*`` (protocol 1.40.0, capability ``fs``): files on the daemon's machine.

A browser that drives a remote daemon cannot see the project the agent works
on.  These methods give it the six things it needs -- stat, list, glob, ranged
read, atomic write, hash -- without a shell and without a second way in.

What a request may touch (its *roots*): the session's ``workdir``, its
``workspaceDir`` and the root conversation's attachments ``files/`` directory.
Every path is resolved with ``realpath`` (``..``, absolute paths and symbolic
links included) and must land inside a root, or the call fails with
``outside_root``.  Writes never go through a symbolic link, and an existing file
with other hard links is not overwritten.  On top of the roots, the files the
tools themselves treat as secrets (``tools/config_guard``, ``agent/prompt_refs``:
``$SNOWPEA_HOME`` state and credentials, ``.env*``, keys, ``.ssh``, browser
profiles, a project's ``.snowpea/settings.json``) are refused with ``protected``
for read, write and hash, whatever the root.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import fnmatch
import hashlib
import os
import re
import secrets
import stat as stat_mod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    FS_MAX_BYTES,
    FsEntry,
    FsGlobParams,
    FsHashResult,
    FsListParams,
    FsListResult,
    FsPathParams,
    FsReadParams,
    FsReadResult,
    FsStatResult,
    FsWriteParams,
    FsWriteResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

T = TypeVar("T")

#: Directories a ``**`` does not descend into (a name given literally still matches).
SKIP_DESCENT = frozenset({".git", "node_modules"})
_CHUNK = 1024 * 1024


def _real(path: str | Path) -> Path:
    return Path(os.path.realpath(path))


def _windows_clean(text: str) -> str:
    """``text`` as Windows would name the file: no ``:stream`` suffix, no trailing dots or spaces.

    ``secret.pem::$DATA``, ``secret.pem.`` and ``secret.pem `` all open
    ``secret.pem`` on NTFS, so each is judged as the plain name too.
    """
    segments = re.split(r"([\\/])", text)
    cleaned: list[str] = []
    for index, segment in enumerate(segments):
        if segment in ("/", "\\") or not segment:
            cleaned.append(segment)
            continue
        if index == 0 and re.fullmatch(r"[A-Za-z]:", segment):
            cleaned.append(segment)  # a drive letter keeps its colon
            continue
        cleaned.append(segment.split(":", 1)[0].rstrip(". "))
    return "".join(cleaned)


def _fold(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", text).casefold()


def _under(child: Path, parent: Path) -> bool:
    c, p = os.path.normcase(str(child)), os.path.normcase(str(parent))
    if c == p:
        return True
    return c.startswith(p.rstrip(os.sep) + os.sep)


@dataclass(frozen=True)
class Roots:
    """What one session's fs calls may touch."""

    workdir: Path
    roots: tuple[Path, ...]
    home: Path
    #: Roots that live under ``$SNOWPEA_HOME`` (the workspace, the attachments):
    #: the rest of the home is protected.
    home_roots: tuple[Path, ...]

    def inside(self, path: Path) -> bool:
        return any(_under(path, root) for root in self.roots)

    # -- resolving ---------------------------------------------------------

    def absolute(self, text: str) -> Path:
        if not text or "\0" in text:
            raise RpcError(errors.INVALID_PARAMS, "path is empty or has a NUL byte")
        path = Path(text)
        return path if path.is_absolute() else self.workdir / path

    def follow(self, text: str, *, secrets: bool = False) -> Path:
        """The real path (links followed) inside a root, or ``outside_root``.

        With ``secrets`` a protected file is refused first, so a request for
        ``<home>/settings.json`` is told ``protected``, not just "outside".
        """
        real = _real(self.absolute(text))
        if secrets:
            self.refuse_protected(real)
        if not self.inside(real):
            raise RpcError(errors.OUTSIDE_ROOT, "the path is outside this session's folders")
        return real

    def no_follow(self, text: str, *, secrets: bool = False) -> Path:
        """The path with its directories resolved and its last name left as it is."""
        path = self.absolute(text)
        if path.name in ("", ".", ".."):
            return self.follow(text, secrets=secrets)
        target = _real(path.parent) / path.name
        if secrets:
            self.refuse_protected(target)
        if not self.inside(target):
            raise RpcError(errors.OUTSIDE_ROOT, "the path is outside this session's folders")
        return target

    # -- the secrets the tools refuse ----------------------------------------

    def protected(self, real: Path) -> bool:
        from snowpea_core.agent.prompt_refs import is_secret_pattern
        from snowpea_core.tools.config_guard import PROJECT_DIR, PROJECT_FILES

        # A case-insensitive file system (macOS, Windows) or another Unicode form
        # reaches the same file under a different spelling: judge both spellings.
        folded = Path(_fold(str(real)))
        cleaned = Path(_windows_clean(str(real)))
        for candidate in (real, folded, cleaned, Path(_fold(str(cleaned)))):
            if is_secret_pattern(candidate):
                return True
            if candidate.parent.name == PROJECT_DIR and candidate.name in PROJECT_FILES:
                return True
        home = Path(_fold(str(self.home)))
        if _under(folded, home):
            return not any(_under(folded, Path(_fold(str(r)))) for r in self.home_roots)
        return False

    def is_home_file(self, info: os.stat_result) -> bool:
        """True when ``info`` is a file directly in the home (a hard link to a secret)."""
        try:
            with os.scandir(self.home) as scan:
                for entry in scan:
                    if entry.is_file(follow_symlinks=False):
                        other = entry.stat(follow_symlinks=False)
                        if (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
                            return True
        except OSError:
            return False
        return False

    def refuse_protected(self, real: Path) -> None:
        if self.protected(real):
            raise RpcError(errors.PROTECTED, f"{real.name} is a protected file")


def session_roots(core: Core, session: Any) -> Roots:
    """The roots of ``session``: workdir, workspace, root conversation's attachments."""
    from snowpea_core.attachments.store import AttachmentStore
    from snowpea_core.session.workspace import ensure_workspace
    from snowpea_core.tools.host_tools import root_session

    home = _real(core.paths.home)
    workdir = _real(Path(str(session.workdir)).expanduser())
    workspace = _real(ensure_workspace(core.paths.home, session))
    root = root_session(core, session) or session
    attachments = _real(AttachmentStore.from_paths(core.paths).files_dir(str(root.id)))
    roots = (workdir, workspace, attachments)
    return Roots(
        workdir=workdir,
        roots=roots,
        home=home,
        home_roots=tuple(r for r in (workspace, attachments) if _under(r, home)),
    )


# ---------------------------------------------------------------------------
# stat / list / glob
# ---------------------------------------------------------------------------


def _type_of(mode: int) -> str:
    if stat_mod.S_ISREG(mode):
        return "file"
    if stat_mod.S_ISDIR(mode):
        return "dir"
    return "other"


def _describe(roots: Roots, path: Path) -> dict[str, Any]:
    """lstat fields; a link reports its target's type when the target is inside a root."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise RpcError(errors.NOT_FOUND, "no such file or directory") from exc
    except OSError as exc:
        raise RpcError(errors.INTERNAL, f"could not stat: {exc.strerror or exc}") from exc
    is_link = stat_mod.S_ISLNK(info.st_mode)
    kind = _type_of(info.st_mode)
    if is_link:
        kind = "other"
        target = _real(path)
        if roots.inside(target):
            with contextlib.suppress(OSError):
                kind = _type_of(os.stat(target).st_mode)
    return {
        "type": kind,
        "size": int(info.st_size),
        "mtimeMs": info.st_mtime_ns / 1_000_000,
        "nlink": int(info.st_nlink),
        "isLink": is_link,
    }


def fs_stat(roots: Roots, params: FsPathParams) -> FsStatResult:
    target = roots.no_follow(params.path)
    return FsStatResult(path=str(target), **_describe(roots, target))


def _directory(roots: Roots, text: str) -> Path:
    base = roots.follow(text)
    try:
        isdir = base.is_dir()
    except OSError:
        isdir = False
    if not isdir:
        if not base.exists():
            raise RpcError(errors.NOT_FOUND, "no such directory")
        raise RpcError(errors.INVALID_PARAMS, "not a directory")
    return base


def fs_list(roots: Roots, params: FsListParams) -> FsListResult:
    base = _directory(roots, params.path)
    entries: list[FsEntry] = []
    truncated = False

    def walk(directory: Path, prefix: str, depth: int) -> bool:
        try:
            with os.scandir(directory) as scan:
                children = sorted(scan, key=lambda item: item.name)
        except OSError:
            return True
        for child in children:
            if len(entries) >= params.maxEntries:
                return False
            rel = prefix + child.name
            entries.append(FsEntry(path=rel, **_describe(roots, Path(child.path))))
            last = entries[-1]
            if (
                depth > 1
                and last.type == "dir"
                and not last.isLink
                and not walk(Path(child.path), rel + "/", depth - 1)
            ):
                return False
        return True

    truncated = not walk(base, "", params.depth)
    return FsListResult(path=str(base), entries=entries, truncated=truncated)


def _segments(pattern: str) -> list[str]:
    text = pattern.replace("\\", "/") if os.sep == "\\" else pattern
    if not text.strip() or "\0" in text or text.startswith("/") or Path(text).is_absolute():
        raise RpcError(errors.INVALID_PARAMS, "the pattern must be relative")
    segments = [part for part in text.split("/") if part not in ("", ".")]
    if not segments or ".." in segments:
        raise RpcError(errors.INVALID_PARAMS, "the pattern must be relative and stay below")
    collapsed: list[str] = []
    for part in segments:  # '**/**' is '**'
        if part == "**" and collapsed and collapsed[-1] == "**":
            continue
        collapsed.append(part)
    return collapsed


def fs_glob(roots: Roots, params: FsGlobParams) -> FsListResult:
    base = _directory(roots, params.path)
    segments = _segments(params.pattern)
    entries: list[FsEntry] = []
    seen: set[str] = set()
    full = False
    budget = params.maxEntries * 10 + 1000  # directory entries looked at, matched or not
    visited: set[tuple[str, int]] = set()

    def children(directory: Path) -> list[os.DirEntry[str]]:
        nonlocal budget, full
        try:
            with os.scandir(directory) as scan:
                found = sorted(scan, key=lambda item: item.name)
        except OSError:
            return []
        budget -= len(found) + 1
        if budget < 0:
            full = True  # looked at enough: report a short, truncated answer
            return []
        return found

    def is_real_dir(entry: os.DirEntry[str]) -> bool:
        try:
            return entry.is_dir(follow_symlinks=False)
        except OSError:
            return False

    def add(entry: os.DirEntry[str], rel: str) -> None:
        nonlocal full
        if rel in seen:
            return
        if len(entries) >= params.maxEntries:
            full = True
            return
        seen.add(rel)
        entries.append(FsEntry(path=rel, **_describe(roots, Path(entry.path))))

    def match(directory: Path, prefix: str, index: int) -> None:
        if full or (str(directory), index) in visited:
            return  # reached again by another '**' split: the same work
        visited.add((str(directory), index))
        segment = segments[index]
        last = index == len(segments) - 1
        if segment == "**":
            if not last:
                match(directory, prefix, index + 1)
            for entry in children(directory):
                if full:
                    return
                if not is_real_dir(entry):
                    if last:
                        add(entry, prefix + entry.name)
                    continue
                if last:
                    add(entry, prefix + entry.name)
                if entry.name in SKIP_DESCENT:
                    continue
                match(Path(entry.path), prefix + entry.name + "/", index)
            return
        for entry in children(directory):
            if full:
                return
            if not fnmatch.fnmatchcase(entry.name, segment):
                continue
            if last:
                add(entry, prefix + entry.name)
            elif is_real_dir(entry):
                match(Path(entry.path), prefix + entry.name + "/", index + 1)

    match(base, "", 0)
    entries.sort(key=lambda item: item.path)
    return FsListResult(path=str(base), entries=entries, truncated=full)


# ---------------------------------------------------------------------------
# read / hash
# ---------------------------------------------------------------------------


def _open_regular(roots: Roots, text: str) -> tuple[int, Path, os.stat_result]:
    """Open a file for reading after every check; the caller closes the descriptor."""
    real = roots.follow(text, secrets=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)  # a FIFO must not hang the daemon
    try:
        descriptor = os.open(real, flags)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise RpcError(errors.NOT_FOUND, "no such file") from exc
    except IsADirectoryError as exc:
        raise RpcError(errors.IS_DIR, "that is a directory") from exc
    except PermissionError as exc:
        raise RpcError(errors.UNAVAILABLE, "permission denied") from exc
    except OSError as exc:
        raise RpcError(errors.OUTSIDE_ROOT, f"could not open: {exc.strerror or exc}") from exc
    info = os.fstat(descriptor)
    if stat_mod.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise RpcError(errors.IS_DIR, "that is a directory")
    if not stat_mod.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise RpcError(errors.INVALID_PARAMS, "not a regular file")
    if info.st_nlink > 1 and roots.is_home_file(info):
        os.close(descriptor)  # a project name for one of snowpea's own files
        raise RpcError(errors.PROTECTED, f"{real.name} is a protected file")
    return descriptor, real, info


def fs_read(roots: Roots, params: FsReadParams) -> FsReadResult:
    cap = min(params.maxBytes, FS_MAX_BYTES)
    if params.length is not None and params.length > cap:
        raise RpcError(errors.TOO_LARGE, f"a read is at most {cap} bytes; ask for a range")
    descriptor, _real_path, info = _open_regular(roots, params.path)
    with os.fdopen(descriptor, "rb") as handle:
        size = int(info.st_size)
        offset = min(params.offset, size)
        wanted = size - offset if params.length is None else min(params.length, size - offset)
        if wanted > cap:
            raise RpcError(
                errors.TOO_LARGE,
                f"{wanted} bytes is over the {cap} byte limit; read it in ranges (offset, length)",
            )
        handle.seek(offset)
        data = handle.read(wanted)
    return FsReadResult(
        dataBase64=base64.b64encode(data).decode("ascii"),
        size=size,
        offset=offset,
        length=len(data),
        mtimeMs=info.st_mtime_ns / 1_000_000,
        eof=offset + len(data) >= size,
    )


def fs_hash(roots: Roots, params: FsPathParams) -> FsHashResult:
    descriptor, _real_path, info = _open_regular(roots, params.path)
    digest = hashlib.sha256()
    total = 0
    with os.fdopen(descriptor, "rb") as handle:
        while block := handle.read(_CHUNK):
            digest.update(block)
            total += len(block)
    return FsHashResult(sha256=digest.hexdigest(), size=total, mtimeMs=info.st_mtime_ns / 1_000_000)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _decode(text: str) -> bytes:
    # base64 is 4 characters per 3 bytes; refuse before decoding what cannot fit.
    if len(text) > (FS_MAX_BYTES // 3 + 1) * 4 + 4:
        raise RpcError(errors.TOO_LARGE, f"a write is at most {FS_MAX_BYTES} bytes")
    try:
        data = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise RpcError(errors.INVALID_PARAMS, "dataBase64 is not valid base64") from exc
    if len(data) > FS_MAX_BYTES:
        raise RpcError(errors.TOO_LARGE, f"a write is at most {FS_MAX_BYTES} bytes")
    return data


def fs_write(roots: Roots, params: FsWriteParams) -> FsWriteResult:
    data = _decode(params.dataBase64)
    target = roots.no_follow(params.path, secrets=True)
    if os.path.islink(target):
        raise RpcError(errors.OUTSIDE_ROOT, "refusing to write through a symbolic link")
    existing: os.stat_result | None = None
    try:
        existing = os.lstat(target)
    except (FileNotFoundError, NotADirectoryError):
        existing = None
    if existing is not None:
        if stat_mod.S_ISDIR(existing.st_mode):
            raise RpcError(errors.IS_DIR, "that is a directory")
        if params.mode == "create":
            raise RpcError(errors.EXISTS, "the file already exists")
        if not stat_mod.S_ISREG(existing.st_mode):
            raise RpcError(errors.INVALID_PARAMS, "not a regular file")
        if existing.st_nlink > 1:
            raise RpcError(
                errors.OUTSIDE_ROOT,
                "the file has other hard links, which may lie outside this session's folders",
            )
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)  # inside a root: no_follow checked the target
    except FileExistsError as exc:
        raise RpcError(errors.INVALID_PARAMS, "a parent of the path is a file") from exc
    except OSError as exc:
        raise RpcError(errors.UNAVAILABLE, f"could not create the folder: {exc.strerror}") from exc
    temp = parent / f".{target.name[:40]}.{secrets.token_hex(6)}.tmp"
    try:
        # The kernel applies the umask to 0o666; os.umask is process-wide and not touched.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        handle = os.open(temp, flags | getattr(os, "O_BINARY", 0), 0o666)
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            with contextlib.suppress(OSError):
                os.fsync(out.fileno())
        if existing is not None:
            with contextlib.suppress(OSError):
                os.chmod(temp, existing.st_mode & 0o7777)
        if params.mode == "create":
            _publish_new(temp, target)
        else:
            os.replace(temp, target)
    except PermissionError as exc:
        raise RpcError(errors.UNAVAILABLE, "permission denied") from exc
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()
    info = os.lstat(target)
    return FsWriteResult(
        path=str(target),
        size=int(info.st_size),
        mtimeMs=info.st_mtime_ns / 1_000_000,
        created=existing is None,
    )


def _publish_new(temp: Path, target: Path) -> None:
    """Put ``temp`` at ``target`` only if nothing is there, without a gap in between."""
    try:
        os.link(temp, target)  # atomic, fails when the name is taken
    except FileExistsError as exc:
        raise RpcError(errors.EXISTS, "the file already exists") from exc
    except OSError:  # no hard links on this file system: best effort
        if os.path.lexists(target):
            raise RpcError(errors.EXISTS, "the file already exists") from None
        os.replace(temp, target)


# ---------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------


def _roots_for(core: Core, session_id: str) -> Roots:
    session = core.sessions.get(session_id)
    if session is None:
        raise RpcError(errors.NOT_FOUND, f"no such session: {session_id}")
    return session_roots(core, session)


def _handler(
    work: Callable[[Roots, Any], T],
) -> Callable[[RpcConnection, Any, Core], Any]:
    async def handle(_conn: RpcConnection, params: Any, core: Core) -> T:
        roots = _roots_for(core, params.sessionId)
        try:
            return await asyncio.to_thread(work, roots, params)
        except RpcError:
            raise
        except ValueError as exc:  # a path Python cannot encode
            raise RpcError(errors.INVALID_PARAMS, "the path is not valid") from exc
        except OSError as exc:
            raise RpcError(errors.INTERNAL, f"{exc.strerror or exc}") from exc

    return handle


def register_fs_handlers(dispatcher: RpcDispatcher) -> None:
    dispatcher.register("fs.stat", _handler(fs_stat))
    dispatcher.register("fs.list", _handler(fs_list))
    dispatcher.register("fs.glob", _handler(fs_glob))
    dispatcher.register("fs.read", _handler(fs_read))
    dispatcher.register("fs.write", _handler(fs_write))
    dispatcher.register("fs.hash", _handler(fs_hash))


__all__ = [
    "Roots",
    "fs_glob",
    "fs_hash",
    "fs_list",
    "fs_read",
    "fs_stat",
    "fs_write",
    "register_fs_handlers",
    "session_roots",
]
