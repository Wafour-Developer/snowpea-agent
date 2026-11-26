"""``grep``: search file contents on the session backend.

ripgrep does the work when the backend has it, because it is an order of
magnitude faster and already knows about ignore files.  Otherwise the same
search runs in Python over ``backend.list_dir`` and ``backend.read_file``, so
the tool behaves the same on a container or an ssh host that has no ``rg``.

Both backends agree on: ``path`` may be a file; a missing path is an error;
each line starts with its path relative to the session workdir;
``.gitignore`` applies (git checkout or not); lock files are skipped unless
the glob or the path names them.  The Python path also says what it skipped.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools import glob as glob_tools
from snowpea_core.tools import ignore
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.vendor.hermes.tools.ansi_strip import strip_ansi
from snowpea_core.vendor.hermes.tools.binary_extensions import has_binary_extension

DEFAULT_LIMIT = 200
MAX_LIMIT = 2000
#: Files larger than this are skipped by the Python path; ripgrep has its own.
MAX_FILE_CHARS = 2_000_000
#: A matching line is shown stripped and cut to this many characters.
MAX_LINE_CHARS = 400

#: Cached per backend kind: does this backend have ripgrep on PATH?
_RIPGREP: dict[str, bool] = {}


async def has_ripgrep(ctx: ToolContext) -> bool:
    """True when ``rg`` is on the backend's PATH (probed once per backend kind)."""
    kind = str(getattr(ctx.backend, "kind", "local"))
    cached = _RIPGREP.get(kind)
    if cached is not None:
        return cached
    try:
        result = await ctx.backend.run("command -v rg", cwd=None, timeout=10.0)
        found = result.exit_code == 0 and bool(result.stdout.strip())
    except OSError:
        found = False
    _RIPGREP[kind] = found
    return found



def _glob_names_locks(glob: str) -> bool:
    """True when ``glob`` names lock files (``*.lock``, ``yarn.lock`` …)."""
    return bool(glob) and ignore.is_lock_file(glob)


async def _ripgrep(
    ctx: ToolContext,
    pattern: str,
    path: str,
    glob: str,
    limit: int,
    ignore_case: bool,
    *,
    is_file: bool,
) -> ToolResult:
    # --with-filename: rg leaves the name out for a single file, and the
    # Python path always prints it.  --no-require-git: the Python path reads
    # .gitignore whether or not the tree is a git checkout.
    argv = [
        "rg", "--line-number", "--no-heading", "--with-filename", "--no-require-git",
        "--color=never", f"--max-count={limit}",
    ]
    if ignore_case:
        argv.append("--ignore-case")
    if glob:
        argv.extend(["--glob", shlex.quote(glob)])
    if not is_file and not _glob_names_locks(glob):
        for name in (*sorted(ignore.LOCK_FILES), "*.lock"):
            argv.extend(["--glob", shlex.quote(f"!{name}")])
    argv.extend(["--regexp", shlex.quote(pattern), "--", shlex.quote(path or ".")])
    result = await ctx.backend.run(" ".join(argv), cwd=None, timeout=120.0)
    if result.timed_out:
        return ToolResult(ok=False, error="ripgrep timed out")
    # rg exits 1 when nothing matched, which is not an error for this tool.
    if result.exit_code not in (0, 1):
        return ToolResult(
            ok=False, error=strip_ansi(result.stderr).strip() or f"rg exited {result.exit_code}"
        )
    lines = [
        _same_as_python(line)
        for line in strip_ansi(result.stdout).splitlines()
        if line.strip()
    ]
    return _render(lines, pattern, limit)


_RG_LINE = re.compile(r"(.+?):(\d+):(.*)\Z", re.DOTALL)


def _same_as_python(line: str) -> str:
    """An rg line in the Python path's shape: no ``./``, text stripped and capped."""
    if line.startswith("./"):
        line = line[2:]
    match = _RG_LINE.match(line)
    if match is None:
        return line
    path, number, text = match.groups()
    return f"{path}:{number}:{text.strip()[:MAX_LINE_CHARS]}"


async def _grep_file(
    ctx: ToolContext, regex: re.Pattern[str], target: str, shown: str, limit: int
) -> tuple[list[str], str | None]:
    """Matching lines of one file as ``shown:line:text``, or an error."""
    try:
        content = await ctx.backend.read_file(target)
    except UnicodeDecodeError:
        return [], f"{shown} is not a UTF-8 text file"
    except (OSError, ValueError) as exc:
        return [], f"cannot read {shown}: {exc}"
    if len(content) > MAX_FILE_CHARS:
        return [], f"{shown} is larger than {MAX_FILE_CHARS} characters"
    lines: list[str] = []
    for number, line in enumerate(content.splitlines(), start=1):
        if regex.search(line):
            lines.append(f"{shown}:{number}:{line.strip()[:MAX_LINE_CHARS]}")
            if len(lines) >= limit + 1:
                break
    return lines, None


async def _python_grep(
    ctx: ToolContext,
    pattern: str,
    path: str,
    glob: str,
    limit: int,
    ignore_case: bool,
    *,
    is_file: bool,
    shown_root: str,
) -> ToolResult:
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return ToolResult(ok=False, error=f"invalid regular expression: {exc}")

    if is_file:
        # A file named in path is searched as asked: no glob, ignore or lock filter.
        lines, error = await _grep_file(ctx, regex, path, shown_root, limit)
        if error is not None:
            return ToolResult(ok=False, error=error)
        return _render(lines, pattern, limit)

    name_filter = glob_tools.compile_pattern(glob) if glob else None
    skip_locks = not _glob_names_locks(glob)
    walked = await glob_tools.walk_tree(ctx, path, max_files=20_000)
    lines: list[str] = []
    locks = 0
    for rel in walked.files:
        if has_binary_extension(rel):
            continue
        if name_filter is not None and not (
            name_filter.match(rel) or name_filter.match(rel.rsplit("/", 1)[-1])
        ):
            continue
        if skip_locks and ignore.is_lock_file(rel):
            locks += 1
            continue
        target = f"{path.rstrip('/')}/{rel}" if path not in ("", ".") else rel
        found, _ = await _grep_file(
            ctx, regex, target, glob_tools.join_display(shown_root, rel), limit - len(lines)
        )
        lines.extend(found)
        if len(lines) >= limit + 1:
            break
    return _render(
        lines,
        pattern,
        limit,
        glob_tools.skipped_note(walked, locks, ctx=ctx, search_root=path),
    )


def _render(lines: list[str], pattern: str, limit: int, note: str = "") -> ToolResult:
    if not lines:
        text = f"no matches for {pattern}"
        return ToolResult(ok=True, output=f"{text}\n{note}" if note else text)
    shown = lines[:limit]
    suffix = "\n… [more matches truncated]" if len(lines) > limit else ""
    if note:
        suffix += f"\n{note}"
    return ToolResult(ok=True, output="\n".join(shown) + suffix)


async def grep(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = str(args.get("pattern", ""))
    if not pattern:
        return ToolResult(ok=False, error="pattern is required")
    path = str(args.get("path", ".")).strip() or "."
    glob = str(args.get("glob", "")).strip()
    ignore_case = bool(args.get("ignoreCase", False))
    try:
        limit = int(args.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(MAX_LIMIT, limit))

    kind = await glob_tools.path_kind(ctx, path)
    if kind is None:
        return ToolResult(ok=False, error=f"no such file or directory: {path}")
    is_file = kind == "file"
    # Both backends print paths relative to the session workdir; a path
    # outside it is shown as given.
    top = glob_tools.display_root(ctx, path)
    shown_root = path if top is None else top

    if await has_ripgrep(ctx) and not (
        not is_file and glob_tools.search_root_inside_downloads(ctx, path)
    ):
        return await _ripgrep(
            ctx, pattern, shown_root, glob, limit, ignore_case, is_file=is_file
        )
    return await _python_grep(
        ctx, pattern, path, glob, limit, ignore_case, is_file=is_file, shown_root=shown_root
    )


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="grep",
        category="file",
        description=descriptions.GREP,
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for."},
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default '.').",
                },
                "glob": {
                    "type": "string",
                    "description": "Only search files matching this glob, e.g. '*.py'.",
                },
                "ignoreCase": {"type": "boolean", "description": "Case-insensitive search."},
                "limit": {
                    "type": "integer",
                    "description": f"Maximum matches to return (default {DEFAULT_LIMIT}).",
                },
            },
            "required": ["pattern"],
        },
        permission="read",
        run=grep,
    ),
)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_FILE_CHARS",
    "MAX_LIMIT",
    "TOOLS",
    "grep",
    "has_ripgrep",
]
