"""One way to compare path names on file systems that ignore case (review 6, M-7).

macOS and Windows compare names without case, and macOS without NFC/NFD
differences.  Every guard that asks "is this path that file?" — the security
lists, the control-plane table, the config guard, the project control paths —
folds both sides through :func:`fold_text` when :data:`FOLD` is set, so
``.Snowpea/Settings.json`` is the settings file there.
"""

from __future__ import annotations

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


__all__ = ["FOLD", "fold_path", "fold_text", "name_key", "path_key"]
