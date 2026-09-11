"""``/deepinit`` — hierarchical ``AGENTS.md`` for a repository (M7 contract §4).

Walk the project's top-level directories, hand each one to a subagent that
reads it and writes ``<dir>/AGENTS.md``, then write the root ``AGENTS.md`` that
points at them.  The walk, the skip list and the file production are code so
the command is idempotent: running it twice rewrites the same set of files.

Differences from the OMC original
---------------------------------
* OMC's deepinit uses its ``deepinit_manifest`` MCP tool to decide which
  directories deserve a file; snowpea uses a plain walk with a skip list and a
  minimum-file threshold, which needs no MCP server.
* OMC writes ``AGENTS.md`` at every level of the tree; snowpea stops at the
  top level plus the root, because deeper files went stale faster than they
  helped. Pass a directory argument to document a subtree instead.
* OMC's per-directory summariser is its ``explore`` agent; here it is an
  ordinary subagent restricted to read-only tools plus ``write_file``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from snowpea_core.agent.subagent import get_manager
from snowpea_core.commands.registry import Command, CommandContext

log = logging.getLogger("snowpea.commands.deepinit")

#: Directory names that never get their own ``AGENTS.md``.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
        ".snowpea",
        ".claude",
        ".omc",
        ".idea",
        ".vscode",
    }
)

#: A directory with fewer files than this is not worth its own document.
MIN_FILES = 2

#: Never fan out wider than this many directories in one run.
MAX_DIRS = 24

#: Tools a documentation subagent needs and nothing more.
DOC_TOOLS: list[str] = ["read_file", "write_file", "list_dir", "glob", "grep"]

AGENTS_FILE = "AGENTS.md"

DEEPINIT_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Directory to document; defaults to the session workdir.",
        }
    },
}


def interesting_dirs(root: Path) -> list[Path]:
    """Top-level directories worth documenting, in name order."""
    found: list[Path] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir() or entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue
        try:
            files = [child for child in entry.rglob("*") if child.is_file()]
        except OSError:  # pragma: no cover - unreadable tree
            continue
        if len(files) < MIN_FILES:
            continue
        found.append(entry)
        if len(found) >= MAX_DIRS:
            break
    return found


def dir_task(directory: Path, root: Path) -> str:
    """The brief one documentation subagent receives."""
    relative = directory.relative_to(root)
    return (
        f"Read the code under {relative} and write {relative}/{AGENTS_FILE}.\n"
        "The file is for an AI coding agent that has never seen this project. "
        "Cover, in this order: what this directory is for, the files an agent "
        "will touch most and what each one owns, the conventions the code "
        "follows here, and anything that is surprising or easy to get wrong.\n"
        "Keep it under 60 lines. Use write_file to create it, then answer with "
        "one sentence saying what you documented."
    )


def root_task(root: Path, documented: list[Path]) -> str:
    """The brief for the root document, which links the others."""
    listing = "\n".join(f"- {path.relative_to(root)}/{AGENTS_FILE}" for path in documented)
    return (
        f"Write {AGENTS_FILE} at the root of this project.\n"
        "Cover what the project is, how to run and test it, the layout of the "
        "top-level directories, and the conventions that hold everywhere.\n"
        f"End with a 'Per-directory guides' section linking to:\n{listing}\n"
        "Keep it under 80 lines. Use write_file to create it, then answer with "
        "one sentence saying what you wrote."
    )


async def cmd_deepinit(ctx: CommandContext, args: str) -> None:
    """``/deepinit [path]`` — write hierarchical ``AGENTS.md`` files."""
    target = args.strip().strip('"').strip("'")
    root = Path(ctx.session.workdir)
    if target:
        candidate = (root / target).resolve() if not Path(target).is_absolute() else Path(target)
        if not candidate.is_dir():
            await ctx.say(f"deepinit: {candidate} is not a directory.")
            return
        root = candidate

    directories = interesting_dirs(root)
    await ctx.say(
        f"deepinit: documenting {root} and {len(directories)} top-level "
        f"director{'y' if len(directories) == 1 else 'ies'}."
    )

    manager = get_manager(ctx.core)
    written: list[str] = []
    failed: list[str] = []

    for directory in directories:
        result = await manager.run(ctx.session, dir_task(directory, root), tools=list(DOC_TOOLS))
        name = str(directory.relative_to(root))
        if result.ok and (directory / AGENTS_FILE).is_file():
            written.append(f"{name}/{AGENTS_FILE}")
        else:
            failed.append(f"{name}: {result.error or 'no file was written'}")

    root_result = await manager.run(
        ctx.session, root_task(root, directories), tools=list(DOC_TOOLS)
    )
    if root_result.ok and (root / AGENTS_FILE).is_file():
        written.insert(0, AGENTS_FILE)
    else:
        failed.append(f"{AGENTS_FILE}: {root_result.error or 'no file was written'}")

    lines = [f"deepinit: wrote {len(written)} file(s)."]
    lines.extend(f"  {name}" for name in written)
    if failed:
        lines.append(f"{len(failed)} did not get written:")
        lines.extend(f"  {name}" for name in failed)
    await ctx.say("\n".join(lines))


COMMANDS: tuple[Command, ...] = (
    Command(
        name="deepinit",
        summary="Write hierarchical AGENTS.md documentation for the project: /deepinit [path].",
        run=cmd_deepinit,
        args_schema=DEEPINIT_ARGS_SCHEMA,
    ),
)


__all__ = [
    "AGENTS_FILE",
    "COMMANDS",
    "DOC_TOOLS",
    "MAX_DIRS",
    "MIN_FILES",
    "SKIP_DIRS",
    "cmd_deepinit",
    "dir_task",
    "interesting_dirs",
    "root_task",
]
