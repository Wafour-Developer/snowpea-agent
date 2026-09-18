"""Filesystem tools: ``read_file``, ``patch``, ``write_file``, ``list_dir``.

All four go through the session's :class:`ExecutionBackend`, so a docker or ssh
backend (US-010) gets them for free.  ``patch`` and ``write_file`` return a
unified diff, which the agent loop turns into a ``diff`` session event.
"""

from __future__ import annotations

import difflib
import logging
from pathlib import Path
from typing import Any

from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools import file_state
from snowpea_core.tools.config_guard import permission_for_write
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.tools.view_image import IMAGE_SUFFIXES, image_dimensions, resolve_image_path
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


async def _with_diagnostics(ctx: ToolContext, result: ToolResult) -> ToolResult:
    """Append the language server's ``Diagnostics`` block to a successful write.

    The agent gets what an editor shows the moment a file is saved, which is
    the whole point of M13.  It is strictly additive: a missing server, a
    disabled one or one that has not finished starting within the three-second
    budget leaves the result exactly as it was (contract §3, AC-43).
    """
    if not result.ok or not result.path:
        return result
    from snowpea_core import lsp

    block = await lsp.diagnostics_block(ctx, result.path)
    if not block:
        return result
    result.output = f"{result.output}\n\n{block}" if result.output else block
    return result


def _positive_int(args: dict[str, Any], key: str) -> int | None:
    """``args[key]`` as a positive int, or ``None`` when absent or unusable."""
    raw = args.get(key)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _window(content: str, offset: int | None, limit: int | None) -> tuple[str, bool]:
    """``(text, complete)`` for an optional 1-based line window."""
    if offset is None and limit is None:
        return content, True
    lines = content.splitlines(keepends=True)
    start = (offset or 1) - 1
    end = len(lines) if limit is None else start + limit
    window = lines[start:end]
    return "".join(window), start == 0 and end >= len(lines)


def _image_read_hint(path: str, resolved: Path) -> ToolResult:
    """Tell the model to use ``view_image`` instead of dumping image bytes."""
    try:
        size = resolved.stat().st_size
        data = resolved.read_bytes()
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    width, height = image_dimensions(data, "")
    if isinstance(width, int) and isinstance(height, int):
        size_label = f"{width}×{height}, "
    else:
        size_label = ""
    size_kb = max(1, size // 1024)
    return ToolResult(
        ok=True,
        output=(
            f"{path} is an image ({size_label}{size_kb} KB); use view_image to inspect it — "
            "read_file cannot show pictures to the model"
        ),
        path=path,
    )


async def read_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("path", "")).strip()
    if not path:
        return ToolResult(ok=False, error="path is required")
    if Path(path).suffix.lower() in IMAGE_SUFFIXES:
        resolved, error = resolve_image_path(ctx, path)
        if error or resolved is None:
            return ToolResult(ok=False, error=error or "path is required")
        return _image_read_hint(path, resolved)
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
    content, complete = _window(
        content, _positive_int(args, "offset"), _positive_int(args, "limit")
    )
    if len(content) > MAX_READ_CHARS:
        content = content[:MAX_READ_CHARS] + "\n… [truncated]"
        complete = False
    # The read-before-write guard is only as good as what it saw: a windowed or
    # truncated read is recorded as partial, so a later write is refused until
    # the whole file has been read (M15 §A3).
    file_state.note_read(ctx.core, ctx.session, path, content, complete=complete)
    return ToolResult(ok=True, output=content, path=path)


async def write_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("path", "")).strip()
    if not path:
        return ToolResult(ok=False, error="path is required")
    content = str(args.get("content", ""))
    existing = await _read_existing(ctx, path)
    before = existing or ""
    stale = file_state.check_stale(ctx.core, ctx.session, path, exists=existing is not None)
    if stale is not None:
        return ToolResult(ok=False, error=file_state.refusal(stale))
    try:
        await ctx.backend.write_file(path, content)
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    file_state.note_write(ctx.core, ctx.session, path, content)
    return await _with_diagnostics(
        ctx,
        ToolResult(
            ok=True,
            output=f"wrote {len(content)} characters to {path}",
            diff=unified_diff(path, before, content) or None,
            path=path,
        ),
    )


async def _replace_in_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
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
    stale = file_state.check_stale(ctx.core, ctx.session, path, exists=True)
    if stale is not None:
        return ToolResult(ok=False, error=file_state.refusal(stale))
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
    file_state.note_write(ctx.core, ctx.session, path, after)
    replaced = occurrences if replace_all and occurrences else 1
    return await _with_diagnostics(
        ctx,
        ToolResult(
            ok=True,
            output=f"replaced {replaced} occurrence(s) in {path}",
            diff=unified_diff(path, before, after) or None,
            path=path,
        ),
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
    log.info("patch matched fuzzily via %s (%d match(es))", strategy, matches)
    return updated, None


async def patch(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Targeted find-and-replace in one file (Hermes ``patch`` dialect)."""
    mode = str(args.get("mode", "replace") or "replace").strip().lower()
    if mode != "replace":
        return ToolResult(
            ok=False,
            error="only mode='replace' is supported; pass path, old_string, and new_string",
        )
    path = str(args.get("path", "")).strip()
    old = str(args.get("old_string", "") or "")
    new = str(args.get("new_string", "") or "")
    if not old:
        return ToolResult(ok=False, error="old_string is required and must not be empty")
    replace_all = bool(args.get("replace_all", False))
    return await _replace_in_file(
        ctx,
        {"path": path, "old": old, "new": new, "replaceAll": replace_all},
    )


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
        description=descriptions.READ_FILE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to read."},
                "offset": {
                    "type": "integer",
                    "description": "First line to return (1-based). Omit to start at the top.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many lines to return. Omit to read to the end.",
                },
            },
            "required": ["path"],
        },
        permission="read",
        run=read_file,
    ),
    Tool(
        name="patch",
        category="file",
        description=descriptions.PATCH,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to edit."},
                "old_string": {
                    "type": "string",
                    "description": (
                        "Exact text to find and replace. Must be unique in the file unless "
                        "replace_all=true. Include surrounding context lines to ensure uniqueness."
                    ),
                },
                "new_string": {
                    "type": "string",
                    "description": (
                        "Changed replacement text; it must differ from old_string. Pass empty "
                        "string '' to delete the matched text."
                    ),
                },
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "Replace all occurrences instead of requiring a unique match (default: false)"
                    ),
                    "default": False,
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
        permission="write",
        permission_for=permission_for_write,
        run=patch,
    ),
    Tool(
        name="write_file",
        category="file",
        description=descriptions.WRITE_FILE,
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
    "list_dir",
    "patch",
    "read_file",
    "unified_diff",
    "write_file",
]
