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
from snowpea_core.tools.config_guard import permission_for_read, permission_for_write
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
#: Characters an ``offset`` read without a ``limit`` returns: it is a window
#: already, and the rest of a long file would ride along in every later round.
#: A whole-file read keeps :data:`MAX_READ_CHARS`, since writes need one.
WINDOW_READ_CHARS = 20_000


#: What ``diff`` prints after a last line that has no newline.
NO_NEWLINE = "\\ No newline at end of file\n"


def unified_diff(path: str, before: str, after: str, *, created: bool = False) -> str:
    """Unified diff between two versions of a file (empty when unchanged).

    A last line without a newline is followed by ``\\ No newline at end of
    file``, as ``diff`` and ``git`` write it, instead of running into the next
    line.  ``created`` diffs a new file from ``/dev/null``.
    """
    if before == after and not created:
        return ""
    lines = []
    for line in difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="/dev/null" if created else f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    ):
        if line.endswith("\n"):
            lines.append(line)
        else:
            lines.append(line + "\n" + NO_NEWLINE)
    return "".join(lines)


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


def format_numbered_lines(content: str, *, start_line: int = 1) -> str:
    """Numbered lines shared by ``read_file`` and ``@`` prompt references."""
    lines = content.splitlines()
    if not lines:
        return ""
    width = len(str(start_line + len(lines) - 1))
    return "\n".join(
        f"{index:>{width}}| {line}" for index, line in enumerate(lines, start=start_line)
    )


def _capped_window(content: str, offset: int, budget: int) -> tuple[str, str]:
    """An open-ended window cut at a line end within ``budget``, and its note."""
    lines = content.splitlines(keepends=True)
    start = offset - 1
    taken: list[str] = []
    size = 0
    for line in lines[start:]:
        if taken and size + len(line) > budget:
            break
        taken.append(line)
        size += len(line)
    end = start + len(taken)
    if end >= len(lines) and size <= budget:
        return "".join(taken), ""
    note = (
        f"\n… [truncated at line {end} of {len(lines)}; grep for what you need, "
        f"or read on with offset={end + 1} and a limit]"
    )
    return "".join(taken)[:budget], note


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
    """One file, or several with ``paths`` — one round instead of one per file."""
    many = [str(item or "").strip() for item in args.get("paths") or [] if str(item or "").strip()]
    if many:
        return await _read_many(ctx, args, many)
    return await _read_one(ctx, args)


async def _read_many(ctx: ToolContext, args: dict[str, Any], paths: list[str]) -> ToolResult:
    """``paths`` (plus ``path``, if given): each whole file under a ``==> path <==`` header."""
    from snowpea_core.tools.call_paths import MAX_BATCH, call_paths

    wanted = call_paths({"path": args.get("path"), "paths": paths})
    if len(wanted) > MAX_BATCH:
        return ToolResult(
            ok=False, error=f"read at most {MAX_BATCH} files per call; got {len(wanted)}"
        )
    parts: list[str] = []
    budget = MAX_READ_CHARS
    failed = 0
    for path in wanted:
        result = await _read_one(ctx, {"path": path}, budget=budget)
        if result.ok:
            budget = max(0, budget - len(result.output))
            parts.append(f"==> {path} <==\n{result.output}")
        else:
            failed += 1
            parts.append(f"==> {path} <==\n[error] {result.error}")
    if failed == len(wanted):
        return ToolResult(ok=False, error="\n\n".join(parts))
    return ToolResult(ok=True, output="\n\n".join(parts))


async def _read_one(
    ctx: ToolContext, args: dict[str, Any], *, budget: int = MAX_READ_CHARS
) -> ToolResult:
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
    offset, limit = _positive_int(args, "offset"), _positive_int(args, "limit")
    note = ""
    if offset is not None and limit is None:
        content, note = _capped_window(content, offset, min(budget, WINDOW_READ_CHARS))
        complete = offset == 1 and not note
    else:
        content, complete = _window(content, offset, limit)
    if len(content) > budget:
        # A batched read shares one budget: the files after it are cut, not lost.
        content = content[:budget]
        note = "\n… [truncated; grep for what you need, or read it with offset and limit]"
        complete = False
    content += note
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
    if stale_is_owned(stale):
        return ToolResult(ok=False, error=file_state.refusal(stale or ""))
    try:
        await ctx.backend.write_file(path, content)
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    file_state.note_write(ctx.core, ctx.session, path, content)
    return await _with_diagnostics(
        ctx,
        ToolResult(
            ok=True,
            output=stale_note(f"wrote {len(content)} characters to {path}", stale),
            diff=unified_diff(path, before, content, created=existing is None) or None,
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
    replace_all = bool(args.get("replaceAll", False))
    stale = file_state.check_stale(ctx.core, ctx.session, path, exists=True)
    if stale_is_owned(stale):
        return ToolResult(ok=False, error=file_state.refusal(stale or ""))
    after, occurrences, strategy, error = _replace_text(path, before, old, new, replace_all)
    if after is None:
        return ToolResult(ok=False, error=error)
    try:
        await ctx.backend.write_file(path, after)
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    file_state.note_write(ctx.core, ctx.session, path, after)
    replaced = occurrences if replace_all and occurrences else 1
    if occurrences == 0:
        output = (
            f"matched approximately (strategy: {strategy}); replaced {replaced} occurrence"
            f"{'s' if replaced != 1 else ''} — re-read the region if the change looks wrong"
        )
    else:
        output = f"replaced {replaced} occurrence(s) in {path}"
    output = stale_note(output, stale)
    return await _with_diagnostics(
        ctx,
        ToolResult(
            ok=True,
            output=output,
            diff=unified_diff(path, before, after) or None,
            path=path,
        ),
    )


def _replace_text(
    path: str, before: str, old: str, new: str, replace_all: bool
) -> tuple[str | None, int, str | None, str | None]:
    """``(after, exact occurrences, fuzzy strategy, error)`` of one replacement."""
    occurrences = before.count(old)
    strategy: str | None = None
    if occurrences == 0:
        # The model's whitespace or indentation often drifts from the file;
        # the vendored fuzzy matcher recovers the intended span or explains why
        # it could not (contract §7, hermes tools/fuzzy_match.py).
        fuzzy, strategy, error = _fuzzy_replace(before, old, new, replace_all)
        if fuzzy is None:
            return None, 0, strategy, error or f"old string not found in {path}"
        after = fuzzy
    else:
        if occurrences > 1 and not replace_all:
            return (
                None,
                occurrences,
                None,
                f"old string appears {occurrences} times in {path}; pass replaceAll or "
                "extend the match",
            )
        after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    if after == before:
        return None, occurrences, strategy, f"the edit left {path} unchanged"
    return after, occurrences, strategy, None


#: Largest dry-run diff put on an approval request (addendum 28, C11).
PREVIEW_DIFF_MAX = 200 * 1024


async def preview_diff(backend: Any, tool: str, args: dict[str, Any]) -> str | None:
    """The unified diff ``patch`` or ``write_file`` would write, without writing it.

    Shown with the approval prompt.  ``None`` when the call would fail, is
    another tool, or the diff is larger than :data:`PREVIEW_DIFF_MAX`.
    """
    if backend is None or tool not in ("patch", "write_file"):
        return None
    contents: dict[str, str] = {}
    originals: dict[str, str] = {}

    async def current(path: str) -> str | None:
        if path not in contents:
            try:
                text = await backend.read_file(path)
            except FileNotFoundError:
                text = None
            if text is None:
                return None
            contents[path] = originals[path] = text
        return contents[path]

    try:
        if tool == "write_file":
            path = str(args.get("path", "")).strip()
            if not path:
                return None
            existing = await current(path)
            if existing is not None:
                originals[path] = existing
            contents[path] = str(args.get("content", ""))
        else:
            if str(args.get("mode", "replace") or "replace").strip().lower() != "replace":
                return None
            top = str(args.get("path", "") or "").strip()
            raw = args.get("edits")
            edits = (
                [edit for edit in raw if isinstance(edit, dict)]
                if isinstance(raw, list) and raw
                else [args]
            )
            for edit in edits:
                path = str(edit.get("path") or top).strip()
                old = str(edit.get("old_string", "") or "")
                if not path or not old:
                    return None
                before = await current(path)
                if before is None:
                    return None
                after, _count, _strategy, _error = _replace_text(
                    path,
                    before,
                    old,
                    str(edit.get("new_string", "") or ""),
                    bool(edit.get("replace_all", False)),
                )
                if after is None:
                    return None
                contents[path] = after
    except Exception:  # noqa: BLE001 - a preview never blocks the prompt
        log.debug("could not preview %s", tool, exc_info=True)
        return None
    parts = (
        unified_diff(path, originals.get(path) or "", text, created=path not in originals)
        for path, text in contents.items()
    )
    diff = "\n".join(part for part in parts if part)
    if not diff or len(diff.encode("utf-8")) > PREVIEW_DIFF_MAX:
        return None
    return diff


def _fuzzy_replace(
    content: str, old: str, new: str, replace_all: bool
) -> tuple[str | None, str | None, str | None]:
    """Try the vendored fuzzy matcher; return ``(content, strategy, error)``.

    Upstream returns ``(content, match_count, strategy, error)`` and never
    raises, reporting failure as a zero match count plus a message.
    """
    updated, matches, strategy, error = fuzzy_find_and_replace(
        content, old, new, replace_all
    )
    if error or not matches or updated == content:
        return None, strategy, error
    old_lines = old.count("\n") + 1
    new_lines = new.count("\n") + 1
    removed_lines = content.count("\n") - updated.count("\n") + matches * new_lines
    matched_lines = removed_lines // matches
    allowed_extra = max(3, old_lines * 0.4)
    if matched_lines - old_lines > allowed_extra:
        return (
            None,
            strategy,
            (
                "old_string matched approximately but the match spans "
                f"{matched_lines} lines where old_string has {old_lines}; re-read the file "
                "and pass the exact current text"
            ),
        )
    log.info("patch matched fuzzily via %s (%d match(es))", strategy, matches)
    return updated, strategy, None


def stale_is_owned(stale: str | None) -> bool:
    """True when the guard names a sibling subagent still writing the file."""
    return bool(stale) and str(stale).startswith(file_state.OWNED_CODE)


def stale_note(output: str, stale: str | None) -> str:
    """Append the read-before-write warning to a successful write, Hermes-style.

    Hermes' ``check_stale`` warns and lets the write through; snowpea used to
    refuse instead, and a model that had looked at the file with grep or a
    shell command — not ``read_file`` — paid a re-read round for every edit.
    Only a file a sibling subagent is still writing is refused (M15 §C3).
    """
    if not stale:
        return output
    return f"{output}\nnote: {stale}"


async def patch(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Targeted find-and-replace (Hermes ``patch`` dialect); ``edits`` for several at once."""
    mode = str(args.get("mode", "replace") or "replace").strip().lower()
    if mode != "replace":
        return ToolResult(
            ok=False,
            error="only mode='replace' is supported; pass path, old_string, and new_string",
        )
    if args.get("edits"):
        return await _patch_many(ctx, args)
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



async def _patch_many(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Apply ``edits`` in order; stop at the first that fails and say what was applied.

    Each edit is ``{path?, old_string, new_string, replace_all?}``; a missing
    ``path`` means the call's own.  Edits to the same file see the earlier
    ones, exactly as separate calls in order would.
    """
    from snowpea_core.tools.call_paths import MAX_BATCH

    raw = args.get("edits")
    edits = [edit for edit in raw if isinstance(edit, dict)] if isinstance(raw, list) else []
    if not edits:
        return ToolResult(ok=False, error="edits must be a list of {path, old_string, new_string}")
    if len(edits) > MAX_BATCH:
        return ToolResult(ok=False, error=f"at most {MAX_BATCH} edits per call; got {len(edits)}")
    top = str(args.get("path", "") or "").strip()
    applied: list[str] = []
    diffs: list[str] = []
    for index, edit in enumerate(edits, 1):
        path = str(edit.get("path") or top).strip()
        old = str(edit.get("old_string", "") or "")
        if not path or not old:
            error = f"edit {index}: path and a non-empty old_string are required"
            return _partial(applied, diffs, error)
        result = await _replace_in_file(
            ctx,
            {
                "path": path,
                "old": old,
                "new": str(edit.get("new_string", "") or ""),
                "replaceAll": bool(edit.get("replace_all", False)),
            },
        )
        if not result.ok:
            return _partial(applied, diffs, f"edit {index} ({path}): {result.error}")
        applied.append(f"{index}. {path}: {result.output}")
        if result.diff:
            diffs.append(result.diff)
    return ToolResult(
        ok=True,
        output=f"applied {len(applied)} edit(s)\n" + "\n".join(applied),
        diff="\n".join(diffs) or None,
        path=top or None,
    )


def _partial(applied: list[str], diffs: list[str], error: str) -> ToolResult:
    """A batch that stopped: what failed, and the edits that are already on disk."""
    done = (
        "earlier edits were applied and stay:\n" + "\n".join(applied)
        if applied
        else "no edit was applied"
    )
    return ToolResult(ok=False, error=f"{error}\n{done}", diff="\n".join(diffs) or None)

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="read_file",
        category="file",
        description=descriptions.READ_FILE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to read."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 10,
                    "description": "Several files to read in this one call (whole files).",
                },
                "offset": {
                    "type": "integer",
                    "description": "First line to return (1-based). Omit to start at the top.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "How many lines to return. Omit to read to the end (with an "
                        "offset, at most about 20,000 characters)."
                    ),
                },
            },
        },
        permission="read",
        permission_for=permission_for_read,
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
                        "Replace all occurrences instead of requiring a unique match "
                        "(default: false)"
                    ),
                    "default": False,
                },
                "edits": {
                    "type": "array",
                    "maxItems": 10,
                    "description": (
                        "Several changes in one call, applied in order, instead of "
                        "path/old_string/new_string."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean"},
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
            },
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
    "preview_diff",
    "unified_diff",
    "write_file",
]
