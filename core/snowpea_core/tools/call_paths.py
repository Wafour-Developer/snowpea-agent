"""Every file path one file-tool call touches.

``read_file`` takes ``paths`` and ``patch`` takes ``edits`` (each with an
optional ``path``) so one call can cover several files — a local model makes
one tool call per round, and a call per file was what made its turns long.
Every guard that used to look at ``args["path"]`` — config and secret tagging,
the plan-mode document exception, checkpoints — asks this instead, so a
batched call is judged by its most sensitive path.
"""

from __future__ import annotations

from typing import Any

#: Files one ``read_file`` call may read, and edits one ``patch`` call may make.
MAX_BATCH = 10


def call_paths(args: dict[str, Any] | None) -> list[str]:
    """``path``, then each of ``paths`` and each edit's ``path``, deduplicated, in order."""
    args = args or {}
    found: list[str] = []
    top = str(args.get("path") or "").strip()
    if top:
        found.append(top)
    # A malformed list must not raise: the permission hooks read this, and a
    # hook that fails would fall back to the tool's plainer tag.
    paths = args.get("paths")
    for raw in paths if isinstance(paths, list | tuple) else []:
        text = str(raw or "").strip()
        if text:
            found.append(text)
    edits = args.get("edits")
    for edit in edits if isinstance(edits, list | tuple) else []:
        if isinstance(edit, dict):
            text = str(edit.get("path") or "").strip() or top
            if text:
                found.append(text)
    seen: set[str] = set()
    return [path for path in found if not (path in seen or seen.add(path))]


__all__ = ["MAX_BATCH", "call_paths"]
