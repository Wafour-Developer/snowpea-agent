"""``glob``: match paths under the session workdir, through the backend.

The walk uses ``ctx.backend.list_dir`` rather than :mod:`pathlib`, so a docker
or ssh backend lists its own filesystem.  ``**`` spans directories; every other
wildcard follows :mod:`fnmatch`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools import ignore
from snowpea_core.tools.ignore import compile_pattern
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
        ".gradle",
    }
)

DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
MAX_DEPTH = 25


async def path_kind(ctx: ToolContext, path: str) -> str | None:
    """``"dir"``, ``"file"`` or ``None`` (missing) for ``path`` on the backend."""
    try:
        await ctx.backend.list_dir(path)
    except NotADirectoryError:
        return "file"
    except FileNotFoundError:
        return None
    except OSError:
        # Unreadable but present (permissions): let the search report it.
        return "dir" if await ctx.backend.exists(path) else None
    return "dir"


def display_root(ctx: ToolContext, root: str) -> str | None:
    """``root`` relative to the session workdir ("" for the workdir itself).

    ``None`` when it lies outside the workdir (or climbs with ``..``); the
    caller then shows paths under ``root`` as given.
    """
    text = root.strip()
    if text in ("", "."):
        return ""
    try:
        relative = ctx.backend.resolve(text).relative_to(ctx.backend.cwd)
    except (ValueError, TypeError):
        return None
    parts = [part for part in relative.parts if part not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join(parts)


def join_display(prefix: str, rel: str) -> str:
    """``prefix/rel`` without a leading ``./`` or a doubled slash."""
    prefix = prefix.rstrip("/")
    if not prefix or prefix == ".":
        return rel
    return f"{prefix}/{rel}" if rel else prefix


@dataclass
class Walk:
    """Files under a root (relative to it) and what the walk left out."""

    files: list[str] = field(default_factory=list)
    #: Files and directories a ``.gitignore`` rule ignored.
    ignored: int = 0
    #: Build-output directories pruned where no ``.gitignore`` governs.
    build_output: int = 0


async def _read_gitignore(ctx: ToolContext, path: str) -> str | None:
    try:
        return await ctx.backend.read_file(path)
    except (OSError, UnicodeDecodeError, ValueError):
        return None


async def walk_tree(
    ctx: ToolContext, root: str, *, max_files: int, respect_ignore: bool = True
) -> Walk:
    """Every file under ``root``, relative to it, pruned and capped.

    With ``respect_ignore`` the ``.gitignore`` files from the workdir down to
    ``root`` and every nested one apply; a directory no ``.gitignore``
    governs still loses manifest-made build output (``target/`` beside
    ``pom.xml``).  ``root`` itself is never ignored: naming it searches it.
    """
    result = Walk()
    rules: list[ignore.Rule] = []
    top = display_root(ctx, root) if respect_ignore else None
    governed = False
    if respect_ignore and top:
        parts = top.split("/")
        for depth in range(len(parts)):
            base = "/".join(parts[:depth])
            text = await _read_gitignore(ctx, join_display(base, ".gitignore"))
            if text is not None:
                rules.extend(ignore.parse(text, base))
                governed = True
    prefix = top or ""

    queue: list[tuple[str, int, bool]] = [("", 0, governed)]
    while queue and len(result.files) < max_files:
        rel, depth, dir_governed = queue.pop(0)
        target = f"{root.rstrip('/')}/{rel}" if rel else root
        try:
            entries = await ctx.backend.list_dir(target)
        except (FileNotFoundError, NotADirectoryError, OSError):
            continue
        if respect_ignore and ".gitignore" in entries:
            text = await _read_gitignore(ctx, f"{target.rstrip('/')}/.gitignore")
            if text is not None:
                rules.extend(ignore.parse(text, join_display(prefix, rel)))
                dir_governed = True
        names = {entry.rstrip("/") for entry in entries}
        for entry in entries:
            is_dir = entry.endswith("/")
            name = entry[:-1] if is_dir else entry
            child = f"{rel}/{name}" if rel else name
            if is_dir and (name in PRUNED_DIRS or depth >= MAX_DEPTH):
                continue
            if rules and ignore.ignored(rules, join_display(prefix, child), is_dir=is_dir):
                result.ignored += 1
                continue
            if is_dir:
                makers = ignore.BUILD_OUTPUT.get(name)
                if respect_ignore and not dir_governed and makers and makers & names:
                    result.build_output += 1
                    continue
                queue.append((child, depth + 1, dir_governed))
            else:
                result.files.append(child)
                if len(result.files) >= max_files:
                    break
    return result


async def walk(ctx: ToolContext, root: str, *, max_files: int) -> list[str]:
    """Every file path under ``root``, relative to it, pruned and capped."""
    return (await walk_tree(ctx, root, max_files=max_files)).files


def skipped_note(walked: Walk, lock_files: int = 0) -> str:
    """The line telling the model what the walk left out ("" when nothing)."""
    parts: list[str] = []
    if walked.ignored:
        parts.append(f"{walked.ignored} paths ignored by .gitignore")
    if walked.build_output:
        parts.append(f"{walked.build_output} build output dirs (target/ beside its manifest)")
    if lock_files:
        parts.append(f"{lock_files} lock files")
    if not parts:
        return ""
    return (
        f"[skipped {', '.join(parts)}; name one in path (or a lock file in glob) "
        "to search it]"
    )


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
    kind = await path_kind(ctx, root)
    if kind is None:
        return ToolResult(ok=False, error=f"no such file or directory: {root}")
    top = display_root(ctx, root)
    shown_root = root if top is None else top
    if kind == "file":
        name = shown_root.rsplit("/", 1)[-1]
        if regex.match(shown_root) or regex.match(name):
            return ToolResult(ok=True, output=shown_root, path=root)
        return ToolResult(ok=True, output=f"{root} is a file and does not match {pattern}")

    # Scan headroom so a narrow pattern still finds matches in a large tree.
    walked = await walk_tree(ctx, root, max_files=max(limit * 20, 2000))
    matches = sorted(
        join_display(shown_root, rel)
        for rel in walked.files
        if regex.match(rel)
        or regex.match(rel.rsplit("/", 1)[-1])
        or regex.match(join_display(shown_root, rel))
    )
    shown = matches[:limit]
    note = skipped_note(walked)
    if not shown:
        empty = f"no paths under {root} match {pattern}"
        return ToolResult(ok=True, output=f"{empty}\n{note}" if note else empty)
    suffix = f"\n… [{len(matches) - len(shown)} more matches]" if len(matches) > len(shown) else ""
    if note:
        suffix += f"\n{note}"
    return ToolResult(ok=True, output="\n".join(shown) + suffix, path=root)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="glob",
        category="file",
        description=descriptions.GLOB,
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '**/*.py' or 'src/**/test_*.ts'.",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default '.').",
                },
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
    "Walk",
    "compile_pattern",
    "display_root",
    "glob_tool",
    "join_display",
    "path_kind",
    "skipped_note",
    "walk",
    "walk_tree",
]
