"""``grep``: search file contents on the session backend.

ripgrep does the work when the backend has it, because it is an order of
magnitude faster and already knows about ignore files.  Otherwise the same
search runs in Python over ``backend.list_dir`` and ``backend.read_file``, so
the tool behaves the same on a container or an ssh host that has no ``rg``.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools import glob as glob_tools
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.vendor.hermes.tools.ansi_strip import strip_ansi
from snowpea_core.vendor.hermes.tools.binary_extensions import has_binary_extension

DEFAULT_LIMIT = 200
MAX_LIMIT = 2000
#: Files larger than this are skipped by the Python path; ripgrep has its own.
MAX_FILE_CHARS = 2_000_000

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



async def _ripgrep(
    ctx: ToolContext, pattern: str, path: str, glob: str, limit: int, ignore_case: bool
) -> ToolResult:
    argv = ["rg", "--line-number", "--no-heading", "--color=never", f"--max-count={limit}"]
    if ignore_case:
        argv.append("--ignore-case")
    if glob:
        argv.extend(["--glob", shlex.quote(glob)])
    argv.extend(["--regexp", shlex.quote(pattern), shlex.quote(path)])
    result = await ctx.backend.run(" ".join(argv), cwd=None, timeout=120.0)
    if result.timed_out:
        return ToolResult(ok=False, error="ripgrep timed out")
    # rg exits 1 when nothing matched, which is not an error for this tool.
    if result.exit_code not in (0, 1):
        return ToolResult(
            ok=False, error=strip_ansi(result.stderr).strip() or f"rg exited {result.exit_code}"
        )
    lines = [line for line in strip_ansi(result.stdout).splitlines() if line.strip()]
    return _render(lines, pattern, limit)


async def _python_grep(
    ctx: ToolContext, pattern: str, path: str, glob: str, limit: int, ignore_case: bool
) -> ToolResult:
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return ToolResult(ok=False, error=f"invalid regular expression: {exc}")
    name_filter = glob_tools.compile_pattern(glob) if glob else None

    candidates = await glob_tools.walk(ctx, path, max_files=20_000)
    lines: list[str] = []
    for rel in candidates:
        if has_binary_extension(rel):
            continue
        if name_filter is not None and not (
            name_filter.match(rel) or name_filter.match(rel.rsplit("/", 1)[-1])
        ):
            continue
        target = f"{path.rstrip('/')}/{rel}" if path not in ("", ".") else rel
        try:
            content = await ctx.backend.read_file(target)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if len(content) > MAX_FILE_CHARS:
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            if regex.search(line):
                lines.append(f"{rel}:{number}:{line.strip()[:400]}")
                if len(lines) >= limit + 1:
                    break
        if len(lines) >= limit + 1:
            break
    return _render(lines, pattern, limit)


def _render(lines: list[str], pattern: str, limit: int) -> ToolResult:
    if not lines:
        return ToolResult(ok=True, output=f"no matches for {pattern}")
    shown = lines[:limit]
    suffix = "\n… [more matches truncated]" if len(lines) > limit else ""
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

    if await has_ripgrep(ctx):
        return await _ripgrep(ctx, pattern, path, glob, limit, ignore_case)
    return await _python_grep(ctx, pattern, path, glob, limit, ignore_case)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="grep",
        category="file",
        description=descriptions.GREP,
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for."},
                "path": {"type": "string", "description": "Directory to search (default '.')."},
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
