"""``glob``: match paths under the session workdir, through the backend.

The walk uses ``ctx.backend.list_dir`` rather than :mod:`pathlib`, so a docker
or ssh backend lists its own filesystem.  ``**`` spans directories; every other
wildcard follows :mod:`fnmatch`.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.session.paths import DOWNLOADS, GITIGNORE_TEXT
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
    #: Ignored paths under ``.snowpea-downloads/`` (sealed from project-wide walks).
    ignored_downloads: int = 0
    #: Display paths of downloads folders skipped on a project-wide walk.
    downloads_locations: set[str] = field(default_factory=set)
    #: Build-output directories pruned where no ``.gitignore`` governs.
    build_output: int = 0


def _resolved(ctx: ToolContext, path: str) -> Path:
    with contextlib.suppress(OSError, RuntimeError, ValueError, TypeError):
        return ctx.backend.resolve(path).resolve()
    return ctx.backend.resolve(path)


def containing_downloads_dir(ctx: ToolContext, root: str) -> Path | None:
    """The ``.snowpea-downloads`` folder that contains ``root``, if any."""
    resolved = _resolved(ctx, root)
    for parent in (resolved, *resolved.parents):
        if parent.name == DOWNLOADS:
            return parent
    return None


def downloads_display_path(ctx: ToolContext, folder: Path) -> str:
    """Path to show for ``folder``: relative inside the workdir, else absolute."""
    try:
        relative = folder.resolve().relative_to(ctx.backend.cwd.resolve())
    except (ValueError, TypeError):
        return str(folder.resolve())
    parts = [part for part in relative.parts if part not in ("", ".")]
    if ".." in parts:
        return str(folder.resolve())
    return "/".join(parts)


def downloads_rule_base(ctx: ToolContext, root: str) -> str | None:
    """Ignore rule base for the downloads folder that contains ``root``."""
    folder = containing_downloads_dir(ctx, root)
    if folder is None:
        return None
    return downloads_display_path(ctx, folder)


def search_root_inside_downloads(ctx: ToolContext, root: str) -> bool:
    """True when ``root`` is a downloads folder or inside one (any nesting)."""
    return containing_downloads_dir(ctx, root) is not None


def is_sealed_downloads_gitignore(text: str) -> bool:
    """The core/browser ``*`` seal written into a downloads folder."""
    if text == GITIGNORE_TEXT:
        return True
    return text.strip() == "*"


def skip_sealed_downloads_gitignore(
    ctx: ToolContext, root: str, rule_base: str, text: str
) -> bool:
    """Drop the downloads-folder seal when the walk is rooted there."""
    if not is_sealed_downloads_gitignore(text) or not search_root_inside_downloads(ctx, root):
        return False
    expected = downloads_rule_base(ctx, root)
    if expected is None:
        return False
    if rule_base == expected:
        return True
    folder = containing_downloads_dir(ctx, root)
    if folder is None:
        return False
    # Outside the workdir the walk prefix is ""; the seal lives on the root folder.
    if rule_base == "" and _resolved(ctx, root) == folder.resolve():
        return True
    return False


def downloads_segment_in_display(rel_path: str) -> str | None:
    """``results/.snowpea-downloads`` from a workdir-relative path, if present."""
    parts = [part for part in rel_path.split("/") if part and part != "."]
    for index, part in enumerate(parts):
        if part == DOWNLOADS:
            return "/".join(parts[: index + 1])
    return None


async def _read_gitignore(ctx: ToolContext, path: str) -> str | None:
    try:
        return await ctx.backend.read_file(path)
    except (OSError, UnicodeDecodeError, ValueError):
        return None


async def _load_downloads_to_root_gitignores(
    ctx: ToolContext, root: str, rules: list[ignore.Rule]
) -> bool:
    """Apply ``.gitignore`` files from the downloads folder down to ``root``.

    Returns whether any directory on that path was governed.
    """
    folder = containing_downloads_dir(ctx, root)
    if folder is None:
        return False
    root_path = _resolved(ctx, root)
    governed = False
    dl_base = downloads_display_path(ctx, folder)
    if root_path == folder:
        segments: tuple[str, ...] = ()
    else:
        try:
            segments = root_path.relative_to(folder).parts
        except ValueError:
            return False
    rule_base = dl_base
    for index in range(len(segments) + 1):
        if index:
            rule_base = join_display(rule_base, segments[index - 1])
        target = folder if index == 0 else folder.joinpath(*segments[:index])
        text = await _read_gitignore(ctx, f"{target}/.gitignore")
        if text is None:
            continue
        if skip_sealed_downloads_gitignore(ctx, root, rule_base, text):
            governed = True
            continue
        rules.extend(ignore.parse(text, rule_base))
        governed = True
    return governed


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
                if skip_sealed_downloads_gitignore(ctx, root, base, text):
                    governed = True
                    continue
                rules.extend(ignore.parse(text, base))
                governed = True
    elif respect_ignore and top is None and search_root_inside_downloads(ctx, root):
        governed = await _load_downloads_to_root_gitignores(ctx, root, rules)
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
                rule_base = join_display(prefix, rel)
                if skip_sealed_downloads_gitignore(ctx, root, rule_base, text):
                    dir_governed = True
                else:
                    rules.extend(ignore.parse(text, rule_base))
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
                rel_path = join_display(prefix, child)
                location = downloads_segment_in_display(rel_path)
                if location is not None:
                    result.ignored_downloads += 1
                    result.downloads_locations.add(location)
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


def skipped_note(
    walked: Walk,
    lock_files: int = 0,
    *,
    ctx: ToolContext,
    search_root: str,
) -> str:
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
    tail = "name one in path (or a lock file in glob) to search it"
    if walked.ignored_downloads and not search_root_inside_downloads(ctx, search_root):
        locations = sorted(walked.downloads_locations) or [DOWNLOADS]
        if len(locations) == 1:
            where = locations[0]
            tail = (
                f"downloaded files are in {where}/ — search there with path={where}; {tail}"
            )
        else:
            named = ", ".join(locations)
            tail = (
                f"downloaded files are in {named}/ — search one with path set to that "
                f"folder; {tail}"
            )
    return f"[skipped {', '.join(parts)}; {tail}]"


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
    note = skipped_note(walked, ctx=ctx, search_root=root)
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
    "is_sealed_downloads_gitignore",
    "join_display",
    "path_kind",
    "containing_downloads_dir",
    "downloads_display_path",
    "downloads_rule_base",
    "downloads_segment_in_display",
    "search_root_inside_downloads",
    "skip_sealed_downloads_gitignore",
    "skipped_note",
    "walk",
    "walk_tree",
]
