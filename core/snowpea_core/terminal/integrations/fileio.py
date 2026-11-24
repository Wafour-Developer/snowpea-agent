"""Safe writes of a person's CLI config files.

* Symlinks are followed (up to 40 hops, a dangling one included) and the
  target is written, so a dotfiles checkout keeps its link.
* A file with more than one hard link, or anything that is not a regular file,
  is refused: replacing it would silently split it from its other names.
* Writes are atomic: a temp file beside the target, ``fsync``, the target's
  mode copied, then ``os.replace``.
* Before the first change to an existing file, one backup is kept beside it
  (``<name>.snowpea-backup-<YYYYMMDD>``, mode 0600 since configs can hold keys).

Portions derived from herdr (https://github.com/herdrdev/herdr), Apache-2.0
(``src/integration/config_file.rs``).
"""

from __future__ import annotations

import itertools
import os
import shutil
import stat
from datetime import date
from pathlib import Path

_COUNTER = itertools.count()
MAX_LINK_HOPS = 40


class ConfigFileError(OSError):
    """A config file cannot be written safely."""


def resolve_target(path: Path) -> Path:
    """Follow ``path``'s symlinks to the file that would really be written."""
    current = Path(path)
    for _ in range(MAX_LINK_HOPS):
        if not current.is_symlink():
            return current
        link = Path(os.readlink(current))
        current = link if link.is_absolute() else current.parent / link
    raise ConfigFileError(f"{path}: too many symlink levels")


def check_writable(path: Path) -> Path:
    """The real target of ``path``; raise when it must not be replaced."""
    target = resolve_target(path)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return target
    if not stat.S_ISREG(info.st_mode):
        raise ConfigFileError(f"{target} is not a regular file; not editing it")
    if info.st_nlink > 1:
        raise ConfigFileError(
            f"{target} has {info.st_nlink} hard links; not editing it (it would split from the "
            "other names). Edit it by hand or make it a single file first.")
    return target


def read_text(path: Path) -> str | None:
    """The file's text (UTF-8, newlines untouched), or ``None`` when it is missing."""
    target = resolve_target(path)
    try:
        with open(target, encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def write_text(path: Path, text: str, *, mode: int | None = None,
               follow_symlinks: bool = True) -> Path:
    """Atomically replace ``path``'s target with ``text``; return the target.

    ``mode`` is set on the temp file before the rename (never chmod through a
    path afterwards); by default the old file's mode is kept.  With
    ``follow_symlinks=False`` a symlink at ``path`` is refused instead of followed.
    """
    if not follow_symlinks and os.path.islink(path):
        raise ConfigFileError(f"{path} is a symlink; not writing through it")
    target = check_writable(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = stat.S_IMODE(os.stat(target).st_mode)
        except FileNotFoundError:
            mode = None
    tmp = target.parent / f".snowpea-config-{os.getpid()}-{next(_COUNTER)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def backup_once(path: Path, existing: str | None) -> Path | None:
    """Copy ``path`` to ``<name>.snowpea-backup-<date>`` unless that exists; return it."""
    if existing is None:
        return None
    target = resolve_target(path)
    backup = target.with_name(f"{target.name}.snowpea-backup-{date.today():%Y%m%d}")
    if backup.exists():
        return backup
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, open(target, "rb") as src:
        shutil.copyfileobj(src, out)
    return backup


def remove_file(path: Path) -> bool:
    """Delete ``path`` (one file, never a directory); ``True`` when it was there."""
    try:
        if Path(path).is_dir() and not Path(path).is_symlink():
            raise ConfigFileError(f"{path} is a directory")
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
