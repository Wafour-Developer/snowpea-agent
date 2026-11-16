"""One way to compare path names on file systems that ignore case (review 6, M-7).

macOS and Windows compare names without case, and macOS without NFC/NFD
differences.  Every guard that asks "is this path that file?" — the security
lists, the control-plane table, the config guard, the project control paths —
folds both sides through :func:`fold_text` when :data:`FOLD` is set, so
``.Snowpea/Settings.json`` is the settings file there.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path

#: Case-insensitive file systems by default: macOS and Windows.
FOLD = sys.platform in ("darwin", "win32")


def fold_text(text: str) -> str:
    """``text`` as compared on a case-insensitive file system (NFC, casefolded)."""
    return unicodedata.normalize("NFC", text).casefold()


def fold_path(path: Path) -> Path:
    return Path(fold_text(str(path)))


def name_key(text: str, fold: bool | None = None) -> str:
    """``text`` for a name comparison: folded where the file system folds."""
    return fold_text(text) if (FOLD if fold is None else fold) else text


def path_key(path: Path, fold: bool | None = None) -> Path:
    """``path`` for a path comparison: folded where the file system folds."""
    return fold_path(path) if (FOLD if fold is None else fold) else path


def same_file(a: Path | str, b: Path | str) -> bool:
    """``a`` and ``b`` are the same file or folder (security review 8, H8-3).

    By identity (``os.path.samefile``: device and inode) when both exist, so a
    case-insensitive or Unicode-normalising file system (macOS APFS, Windows)
    cannot pass one folder off as another by its spelling.  When either does
    not exist yet: the spellings, folded (NFC + casefold) where the file system
    folds (:data:`FOLD`).
    """
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return path_key(Path(a)) == path_key(Path(b))


def within(path: Path | str, base: Path | str) -> bool:
    """``path`` is ``base`` or lies inside it: :func:`same_file` on ``path`` and
    each of its parents against ``base``."""
    candidate = Path(path)
    return any(same_file(part, base) for part in (candidate, *candidate.parents))


__all__ = ["FOLD", "fold_path", "fold_text", "name_key", "path_key", "same_file", "within"]
