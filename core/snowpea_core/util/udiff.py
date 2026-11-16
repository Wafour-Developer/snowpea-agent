"""Unified diffs the way ``diff -u`` and ``git`` write them.

``str.splitlines`` also breaks lines at ``\\f``, ``\\v``, ``\\x1c``-``\\x1e``,
``\\x85``, U+2028/U+2029 and a bare ``\\r``: a diff built from it has hunks whose
counts do not match the file, and a last line without a newline runs into the
next diff line (``-c+b``).  :func:`lines` splits on ``\\n`` only, and
:func:`unified` writes ``\\ No newline at end of file`` after such a last line.
"""

from __future__ import annotations

import difflib

NO_NEWLINE = "\\ No newline at end of file\n"


def lines(text: str) -> list[str]:
    """``text`` cut after every ``\\n`` (kept); a last line without one stays as it is."""
    if not text:
        return []
    parts = text.split("\n")
    out = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def unified(before: str, after: str, fromfile: str, tofile: str, n: int = 3) -> str:
    """The unified diff from ``before`` to ``after`` ("" when they are equal)."""
    out: list[str] = []
    for line in difflib.unified_diff(lines(before), lines(after), fromfile=fromfile,
                                     tofile=tofile, n=n):
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n" + NO_NEWLINE)
    return "".join(out)


__all__ = ["NO_NEWLINE", "lines", "unified"]
