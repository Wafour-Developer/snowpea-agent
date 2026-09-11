"""``glob``: match paths under the session workdir, through the backend.

The walk uses ``ctx.backend.list_dir`` rather than :mod:`pathlib`, so a docker
or ssh backend lists its own filesystem.  ``**`` spans directories; every other
wildcard follows :mod:`fnmatch`.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

#: Directories never descended into; they dwarf everything a user asked for.
PRUNED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".next",
        ".tox",
    }
)

DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
MAX_DEPTH = 25


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """A regex for ``pattern`` where ``**`` crosses directory separators."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(char))
                i += 1
            else:
                out.append(fnmatch.translate(pattern[i : close + 1])[4:-3])
                i = close + 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z")


async def walk(ctx: ToolContext, root: str, *, max_files: int) -> list[str]:
    """Every file path under ``root``, relative to it, pruned and capped."""
    found: list[str] = []
    queue: list[tuple[str, int]] = [("", 0)]
    while queue and len(found) < max_files:
        rel, depth = queue.pop(0)
        target = f"{root.rstrip('/')}/{rel}" if rel else root
        try:
            entries = await ctx.backend.list_dir(target)
        except (FileNotFoundError, NotADirectoryError, OSError):
            continue
        for entry in entries:
            is_dir = entry.endswith("/")
            name = entry[:-1] if is_dir else entry
            child = f"{rel}/{name}" if rel else name
            if is_dir:
                if name in PRUNED_DIRS or depth >= MAX_DEPTH:
                    continue
                queue.append((child, depth + 1))
            else:
                found.append(child)
                if len(found) >= max_files:
                    break
    return found


async def glob_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        return ToolResult(ok=False, error="pattern is required")
    root = str(args.get("path", ".")).strip() or "."
    try:
        limit = int(args.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(MAX_LIMIT, limit))

    regex = compile_pattern(pattern)
    # Scan headroom so a narrow pattern still finds matches in a large tree.
    candidates = await walk(ctx, root, max_files=max(limit * 20, 2000))
    matches = [
        path
        for path in candidates
        if regex.match(path) or regex.match(path.rsplit("/", 1)[-1])
    ]
    matches.sort()
    shown = matches[:limit]
    if not shown:
        return ToolResult(ok=True, output=f"no paths under {root} match {pattern}")
    suffix = f"\n… [{len(matches) - len(shown)} more matches]" if len(matches) > len(shown) else ""
    return ToolResult(ok=True, output="\n".join(shown) + suffix, path=root)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="glob",
        category="file",
        description=(
            "Find files by path pattern under a directory. '**' spans directories; "
            "results are relative to the searched directory."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '**/*.py' or 'src/**/test_*.ts'.",
                },
                "path": {"type": "string", "description": "Directory to search (default '.')."},
                "limit": {
                    "type": "integer",
                    "description": f"Maximum paths to return (default {DEFAULT_LIMIT}).",
                },
            },
            "required": ["pattern"],
        },
        permission="read",
        run=glob_tool,
    ),
)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_DEPTH",
    "MAX_LIMIT",
    "PRUNED_DIRS",
    "TOOLS",
    "compile_pattern",
    "glob_tool",
    "walk",
]
