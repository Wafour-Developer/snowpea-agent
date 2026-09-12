"""Filesystem tools: ``read_file``, ``write_file``, ``edit_file``, ``list_dir``.

All four go through the session's :class:`ExecutionBackend`, so a docker or ssh
backend (US-010) gets them for free.  ``write_file`` and ``edit_file`` return a
unified diff, which the agent loop turns into a ``diff`` session event.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from snowpea_core.tools.config_guard import permission_for_write
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.vendor.hermes.tools.binary_extensions import (
    has_binary_extension,
    has_opaque_document_extension,
)
from snowpea_core.vendor.hermes.tools.fuzzy_match import fuzzy_find_and_replace

log = logging.getLogger("snowpea.tools.fs")

#: Characters of file content returned by ``read_file`` before truncation.
MAX_READ_CHARS = 200_000


def unified_diff(path: str, before: str, after: str) -> str:
    """Unified diff between two versions of a file (empty when unchanged)."""
    if before == after:
        return ""
    patch = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    return "".join(patch)


async def _read_existing(ctx: ToolContext, path: str) -> str | None:
    try:
        return await ctx.backend.read_file(path)
    except FileNotFoundError:
        return None


async def read_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("path", "")).strip()
    if not path:
        return ToolResult(ok=False, error="path is required")
    if has_binary_extension(path) or has_opaque_document_extension(path):
        return ToolResult(
            ok=False,
            error=(
                f"{path} looks like a binary or opaque document; read it with a tool that "
                "understands the format instead of pulling the bytes into context"
            ),
        )
    try:
        content = await ctx.backend.read_file(path)
    except FileNotFoundError:
        return ToolResult(ok=False, error=f"no such file: {path}")
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    if len(content) > MAX_READ_CHARS:
        content = content[:MAX_READ_CHARS] + "\n… [truncated]"
    return ToolResult(ok=True, output=content, path=path)


async def write_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("path", "")).strip()
    if not path:
        return ToolResult(ok=False, error="path is required")
    content = str(args.get("content", ""))
    before = await _read_existing(ctx, path) or ""
    try:
        await ctx.backend.write_file(path, content)
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    return ToolResult(
        ok=True,
        output=f"wrote {len(content)} characters to {path}",
        diff=unified_diff(path, before, content) or None,
        path=path,
    )


async def edit_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Replace an exact ``old`` string with ``new``; the match must be unique."""
    path = str(args.get("path", "")).strip()
    if not path:
        return ToolResult(ok=False, error="path is required")
    old = str(args.get("old", ""))
    new = str(args.get("new", ""))
    if not old:
        return ToolResult(ok=False, error="old is required and must not be empty")
    before = await _read_existing(ctx, path)
    if before is None:
        return ToolResult(ok=False, error=f"no such file: {path}")
    occurrences = before.count(old)
    replace_all = bool(args.get("replaceAll", False))
    if occurrences == 0:
        # The model's whitespace or indentation often drifts from the file;
        # the vendored fuzzy matcher recovers the intended span or explains why
        # it could not (contract §7, hermes tools/fuzzy_match.py).
        fuzzy, error = _fuzzy_replace(before, old, new, replace_all)
        if fuzzy is None:
            return ToolResult(ok=False, error=error or f"old string not found in {path}")
        after = fuzzy
    else:
        if occurrences > 1 and not replace_all:
            return ToolResult(
                ok=False,
                error=f"old string appears {occurrences} times in {path}; pass replaceAll or "
                "extend the match",
            )
        after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    if after == before:
        return ToolResult(ok=False, error=f"the edit left {path} unchanged")
    try:
        await ctx.backend.write_file(path, after)
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    replaced = occurrences if replace_all and occurrences else 1
    return ToolResult(
        ok=True,
        output=f"replaced {replaced} occurrence(s) in {path}",
        diff=unified_diff(path, before, after) or None,
        path=path,
    )


def _fuzzy_replace(
    content: str, old: str, new: str, replace_all: bool
) -> tuple[str | None, str | None]:
    """Try the vendored fuzzy matcher; return ``(new content, error)``.

    Upstream returns ``(content, match_count, strategy, error)`` and never
    raises, reporting failure as a zero match count plus a message.
    """
    updated, matches, strategy, error = fuzzy_find_and_replace(
        content, old, new, replace_all
    )
    if error or not matches or updated == content:
        return None, error
    log.info("edit_file matched fuzzily via %s (%d match(es))", strategy, matches)
    return updated, None


async def list_dir(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("path", ".")).strip() or "."
    try:
        entries = await ctx.backend.list_dir(path)
    except FileNotFoundError:
        return ToolResult(ok=False, error=f"no such directory: {path}")
    except NotADirectoryError:
        return ToolResult(ok=False, error=f"not a directory: {path}")
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    return ToolResult(ok=True, output="\n".join(entries), path=path)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="read_file",
        category="file",
        description="Read a UTF-8 text file, relative to the session working directory.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File to read."}},
            "required": ["path"],
        },
        permission="read",
        run=read_file,
    ),
    Tool(
        name="write_file",
        category="file",
        description="Create or overwrite a text file with the given content.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to write."},
                "content": {"type": "string", "description": "Full new file content."},
            },
            "required": ["path", "content"],
        },
        permission="write",
        permission_for=permission_for_write,
        run=write_file,
    ),
    Tool(
        name="edit_file",
        category="file",
        description=(
            "Replace an exact string in a file. The old string must appear exactly once "
            "unless replaceAll is true."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to edit."},
                "old": {"type": "string", "description": "Exact text to replace."},
                "new": {"type": "string", "description": "Replacement text."},
                "replaceAll": {"type": "boolean", "description": "Replace every occurrence."},
            },
            "required": ["path", "old", "new"],
        },
        permission="write",
        permission_for=permission_for_write,
        run=edit_file,
    ),
    Tool(
        name="list_dir",
        category="file",
        description="List the entries of a directory; directories end with a slash.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory to list."}},
        },
        permission="read",
        run=list_dir,
    ),
)


__all__ = [
    "MAX_READ_CHARS",
    "TOOLS",
    "edit_file",
    "list_dir",
    "read_file",
    "unified_diff",
    "write_file",
]
