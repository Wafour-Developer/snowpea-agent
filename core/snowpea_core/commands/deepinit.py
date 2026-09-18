"""``/deepinit`` — hierarchical ``AGENTS.md`` for a repository (M7 contract §4).

Walk the project's top-level directories, map each one with a read-only
explorer, hand the notes to a writer/executor that creates
``<dir>/AGENTS.md``, then synthesize the root ``AGENTS.md`` the same way
(architect outlines, writer/executor writes).  The walk, the skip list and
the file production are code so the command is idempotent.

Success contract
----------------
``/deepinit`` must leave an ``AGENTS.md`` on every targeted path.  Subagents
get the first chance; anything still missing is filled by a deterministic
stub writer so a weak or budget-exhausted model cannot make the command
report total failure.

Differences from the OMC original
---------------------------------
* OMC's deepinit uses its ``deepinit_manifest`` MCP tool to decide which
  directories deserve a file; snowpea uses a plain walk with a skip list and a
  minimum-file threshold, which needs no MCP server.
* OMC writes ``AGENTS.md`` at every level of the tree; snowpea stops at the
  top level plus the root, because deeper files went stale faster than they
  helped. Pass a directory argument to document a subtree instead.
* OMC's per-directory summariser is ``explore`` and a ``writer`` produces the
  file.  snowpea does the same split: ``explorer``/``explore`` maps (read-only),
  then ``writer``/``executor`` writes; the root is outlined by ``architect``
  when available, then written by ``writer``/``executor``.
* Incomplete child turns are re-issued by ``SubagentManager``; missing files
  still get a deterministic stub.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from snowpea_core.agent.role_pick import pick_agent
from snowpea_core.agent.subagent import SubagentResult, get_manager
from snowpea_core.commands.registry import Command, CommandContext

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.agent.subagent import SubagentManager
    from snowpea_core.session.session import Session

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

#: Top-level entries listed in a fallback stub.
STUB_ENTRY_LIMIT = 24

#: Read-only mappers (phase 1).
MAP_AGENTS: tuple[str, ...] = ("explorer", "explore")

#: Writers of ``AGENTS.md`` (phase 2).
WRITE_AGENTS: tuple[str, ...] = ("writer", "executor")

#: Root outline synthesizers before the write (optional phase).
ROOT_MAP_AGENTS: tuple[str, ...] = ("architect", "explorer", "explore")

#: Caps how much of an explore report is pasted into the write brief.
MAP_SUMMARY_CHARS = 6000

#: Tools for the map phase — no writes.
MAP_TOOLS: list[str] = ["read_file", "list_dir", "glob", "grep"]

#: Tools for the write phase.
WRITE_TOOLS: list[str] = ["read_file", "write_file", "list_dir", "glob", "grep"]

#: Back-compat aliases used by older tests / callers.
DIR_DOC_AGENTS = WRITE_AGENTS
ROOT_DOC_AGENTS = WRITE_AGENTS
DOC_TOOLS = WRITE_TOOLS

AGENTS_FILE = "AGENTS.md"

FALLBACK_MARKER = "<!-- deepinit:fallback -->"

DEEPINIT_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Directory to document; defaults to the session workdir.",
        }
    },
}


def pick_doc_agent(
    session: Session,
    manager: SubagentManager,
    preference: tuple[str, ...],
    *,
    allow_general: bool = False,
) -> str | None:
    """First preferred role for this session, or ``None`` (anonymous)."""
    return pick_agent(
        session,
        manager,
        preference,
        core=getattr(manager, "core", None),
        allow_general=allow_general,
    )


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


def map_dir_task(directory: Path, root: Path) -> str:
    """Phase 1: explore a directory; do not write files."""
    relative = directory.relative_to(root)
    return (
        f"Map the code under {relative} for an AGENTS.md that another agent will write.\n"
        "Do NOT write or edit any files. Cover, in order: what this directory is for, "
        "the files an agent will touch most and what each owns, conventions, and "
        "anything surprising or easy to get wrong.\n"
        "Keep the report under 60 lines. End with one sentence naming the directory."
    )


def write_dir_task(directory: Path, root: Path, notes: str, *, retry: bool = False) -> str:
    """Phase 2: write ``AGENTS.md`` from explore notes."""
    relative = directory.relative_to(root)
    target = f"{relative}/{AGENTS_FILE}"
    clipped = (notes or "").strip()
    if len(clipped) > MAP_SUMMARY_CHARS:
        clipped = clipped[: MAP_SUMMARY_CHARS - 20].rstrip() + "\n…[truncated]"
    if retry:
        return (
            f"Previous attempt did not create {target}. "
            f"Your only job is to write {target} with write_file now.\n"
            "Use the explore notes below; keep the file under 60 lines.\n\n"
            f"Explore notes:\n{clipped or '(none — invent a short accurate stub from paths you list)'}\n\n"
            "After write_file, answer with one sentence saying what you documented."
        )
    return (
        f"Write {target} with write_file from the explore notes below.\n"
        "The file is for an AI coding agent that has never seen this project. "
        "Keep it under 60 lines. Call write_file before other exploration, then "
        "answer with one sentence saying what you documented.\n\n"
        f"Explore notes:\n{clipped or '(none — list_dir the path and write a short accurate stub)'}"
    )


def dir_task(directory: Path, root: Path, *, retry: bool = False) -> str:
    """Back-compat one-shot brief (write phase wording)."""
    return write_dir_task(directory, root, "", retry=retry)


def map_root_task(
    root: Path, documented: list[tuple[Path, str]] | list[Path]
) -> str:
    """Phase 1 for the root: synthesize an outline; do not write."""
    listing_lines: list[str] = []
    for item in documented:
        if isinstance(item, tuple):
            path, summary = item
            rel = path.relative_to(root)
            listing_lines.append(f"- {rel}: {summary}" if summary else f"- {rel}")
        else:
            listing_lines.append(f"- {item.relative_to(root)}")
    listing = "\n".join(listing_lines) or "- (no per-directory notes)"
    return (
        f"Outline a root {AGENTS_FILE} for this project. Do NOT write files.\n"
        "Cover what the project is, how to run and test it, top-level layout, "
        "and shared conventions. Keep the outline under 80 lines.\n\n"
        f"Per-directory notes:\n{listing}"
    )


def write_root_task(
    root: Path,
    documented: list[Path] | list[tuple[Path, str]],
    notes: str = "",
    *,
    retry: bool = False,
) -> str:
    """Phase 2 for the root: write ``AGENTS.md``."""
    listing_lines: list[str] = []
    links: list[str] = []
    for item in documented:
        if isinstance(item, tuple):
            path, summary = item
            rel = path.relative_to(root)
            if summary:
                listing_lines.append(f"- {rel}/{AGENTS_FILE}: {summary}")
            else:
                listing_lines.append(f"- {rel}/{AGENTS_FILE}")
            links.append(f"- {rel}/{AGENTS_FILE}")
        else:
            rel = item.relative_to(root)
            listing_lines.append(f"- {rel}/{AGENTS_FILE}")
            links.append(f"- {rel}/{AGENTS_FILE}")
    listing = "\n".join(listing_lines)
    link_list = "\n".join(links)
    clipped = (notes or "").strip()
    if len(clipped) > MAP_SUMMARY_CHARS:
        clipped = clipped[: MAP_SUMMARY_CHARS - 20].rstrip() + "\n…[truncated]"
    preface = (
        f"Previous attempt did not create {AGENTS_FILE}. "
        if retry
        else ""
    )
    return (
        f"{preface}Write {AGENTS_FILE} at the project root with write_file.\n"
        "Cover what the project is, how to run and test it, top-level layout, "
        "and shared conventions.\n"
        f"Top-level directory summaries:\n{listing}\n\n"
        f"End with a 'Per-directory guides' section linking to:\n{link_list}\n"
        "Keep it under 80 lines. Call write_file before other exploration.\n\n"
        f"Architect/explore outline:\n{clipped or '(none — use the summaries above)'}\n\n"
        "After write_file, answer with one sentence saying what you wrote."
    )


def root_task(
    root: Path,
    documented: list[Path] | list[tuple[Path, str]],
    *,
    retry: bool = False,
) -> str:
    """Back-compat one-shot root brief."""
    return write_root_task(root, documented, "", retry=retry)


def _skip_name(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def _readme_blurb(directory: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = directory / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:200]
    return ""


def _list_top_entries(directory: Path) -> tuple[list[str], list[str]]:
    """Return ``(files, subdirs)`` names at one level, skipping noise."""
    files: list[str] = []
    subdirs: list[str] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return files, subdirs
    for entry in entries:
        if _skip_name(entry.name):
            continue
        if entry.is_file():
            files.append(entry.name)
        elif entry.is_dir():
            subdirs.append(entry.name)
        if len(files) + len(subdirs) >= STUB_ENTRY_LIMIT:
            break
    return files, subdirs


def write_dir_fallback(directory: Path, root: Path) -> tuple[Path, str]:
    """Write a deterministic ``AGENTS.md`` for ``directory``. Always on disk."""
    relative = directory.relative_to(root)
    target = directory / AGENTS_FILE
    files, subdirs = _list_top_entries(directory)
    blurb = _readme_blurb(directory)
    purpose = blurb or f"Top-level package/directory `{relative}` in this project."
    lines = [
        FALLBACK_MARKER,
        f"# {relative}",
        "",
        "## Purpose",
        purpose,
        "",
        "## Key files",
    ]
    if files:
        lines.extend(f"- `{name}`" for name in files)
    else:
        lines.append("- (no non-ignored files at this level)")
    lines.extend(["", "## Subdirectories"])
    if subdirs:
        lines.extend(f"- `{name}/`" for name in subdirs)
    else:
        lines.append("- (none at this level)")
    lines.extend(
        [
            "",
            "## For AI agents",
            "This file was written by `/deepinit` as a fallback after the model "
            "did not produce one. Prefer reading the listed files and any nested "
            f"`{AGENTS_FILE}` before editing; refine this stub when you learn more.",
            "",
        ]
    )
    target.write_text("\n".join(lines), encoding="utf-8")
    summary = f"Fallback stub for {relative} ({len(files)} files, {len(subdirs)} dirs)."
    return target, summary


def write_root_fallback(
    root: Path, documented: list[tuple[Path, str]], directories: list[Path]
) -> Path:
    """Write a deterministic root ``AGENTS.md`` linking per-directory guides."""
    target = root / AGENTS_FILE
    blurb = _readme_blurb(root)
    purpose = blurb or f"Project root at `{root.name or root}`."
    entries = documented if documented else [(path, "") for path in directories]
    lines = [
        FALLBACK_MARKER,
        f"# {root.name or 'project'}",
        "",
        "## Purpose",
        purpose,
        "",
        "## Layout",
    ]
    if entries:
        for path, summary in entries:
            rel = path.relative_to(root)
            if summary:
                lines.append(f"- `{rel}/` — {summary}")
            else:
                lines.append(f"- `{rel}/`")
    else:
        lines.append("- (no top-level directories were documented)")
    files, _ = _list_top_entries(root)
    if files:
        lines.extend(["", "## Root files"])
        lines.extend(f"- `{name}`" for name in files)
    lines.extend(["", "## Per-directory guides"])
    if entries:
        for path, _ in entries:
            rel = path.relative_to(root)
            lines.append(f"- [{rel}/{AGENTS_FILE}]({rel}/{AGENTS_FILE})")
    else:
        lines.append("- (none)")
    lines.extend(
        [
            "",
            "## For AI agents",
            "This root guide was written by `/deepinit` as a fallback after the "
            "model did not produce one. Open the per-directory guides above and "
            "refine this stub when you know more about how to run and test the "
            "project.",
            "",
        ]
    )
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def _map_notes(result: object) -> str:
    if isinstance(result, SubagentResult) and result.ok:
        return (result.summary or "").strip()
    if isinstance(result, SubagentResult):
        return (result.summary or result.error or "").strip()
    if isinstance(result, Exception):
        return f"(explore failed: {result})"
    return ""


async def _map_directories(
    ctx: CommandContext,
    root: Path,
    directories: list[Path],
    *,
    agent: str | None,
) -> dict[Path, str]:
    """Phase 1: explore each directory in parallel."""
    if not directories:
        return {}
    manager = get_manager(ctx.core)
    results = await asyncio.gather(
        *[
            manager.run(
                ctx.session,
                map_dir_task(directory, root),
                agent=agent,
                tools=list(MAP_TOOLS),
                title=f"explore {directory.relative_to(root)}",
                prefer=MAP_AGENTS if agent is None else (),
            )
            for directory in directories
        ],
        return_exceptions=True,
    )
    return {
        directory: _map_notes(result)
        for directory, result in zip(directories, results)
    }


async def _write_directories(
    ctx: CommandContext,
    root: Path,
    notes_by_dir: dict[Path, str],
    *,
    agent: str | None,
    retry: bool = False,
) -> tuple[list[str], list[tuple[Path, str]], list[Path]]:
    """Phase 2: write each ``AGENTS.md`` from explore notes."""
    directories = list(notes_by_dir)
    if not directories:
        return [], [], []
    manager = get_manager(ctx.core)
    results = await asyncio.gather(
        *[
            manager.run(
                ctx.session,
                write_dir_task(
                    directory, root, notes_by_dir.get(directory, ""), retry=retry
                ),
                agent=agent,
                tools=list(WRITE_TOOLS),
                title=(
                    f"write {directory.relative_to(root)}/{AGENTS_FILE}"
                    + (" (retry)" if retry else "")
                ),
                force=retry,
                prefer=WRITE_AGENTS if agent is None else (),
            )
            for directory in directories
        ],
        return_exceptions=True,
    )
    written: list[str] = []
    documented: list[tuple[Path, str]] = []
    missing: list[Path] = []
    for directory, result in zip(directories, results):
        name = str(directory.relative_to(root))
        if (
            isinstance(result, SubagentResult)
            and result.ok
            and (directory / AGENTS_FILE).is_file()
        ):
            summary = (result.summary or "").strip().split("\n")[0]
            written.append(f"{name}/{AGENTS_FILE}")
            documented.append((directory, summary))
            continue
        if not (directory / AGENTS_FILE).is_file():
            missing.append(directory)
    return written, documented, missing


async def _document_root(
    ctx: CommandContext,
    root: Path,
    documented: list[tuple[Path, str]],
    directories: list[Path],
    *,
    map_agent: str | None,
    write_agent: str | None,
) -> tuple[str | None, str | None]:
    """Map (optional) then write the root ``AGENTS.md``."""
    manager = get_manager(ctx.core)
    listing = documented if documented else [(d, "") for d in directories]
    outline = ""
    if map_agent or pick_doc_agent(ctx.session, manager, ROOT_MAP_AGENTS):
        mapper = map_agent or pick_doc_agent(ctx.session, manager, ROOT_MAP_AGENTS)
        mapped = await manager.run(
            ctx.session,
            map_root_task(root, listing),
            agent=mapper,
            tools=list(MAP_TOOLS),
            title=f"outline {AGENTS_FILE}",
            prefer=ROOT_MAP_AGENTS if mapper is None else (),
        )
        outline = _map_notes(mapped)

    result = await manager.run(
        ctx.session,
        write_root_task(root, listing, outline),
        agent=write_agent,
        tools=list(WRITE_TOOLS),
        title=f"write {AGENTS_FILE}",
        prefer=WRITE_AGENTS if write_agent is None else (),
    )
    if result.ok and (root / AGENTS_FILE).is_file():
        return AGENTS_FILE, None
    return None, f"{AGENTS_FILE}: {result.error or 'no file was written'}"


def _fill_missing_with_fallback(
    root: Path,
    missing: list[Path],
    documented: list[tuple[Path, str]],
) -> tuple[list[str], list[str]]:
    """Write stubs for paths the model left empty. Returns written / hard fails."""
    written: list[str] = []
    hard_failed: list[str] = []
    for directory in missing:
        name = str(directory.relative_to(root))
        try:
            _, summary = write_dir_fallback(directory, root)
        except OSError as exc:
            hard_failed.append(f"{name}: {exc}")
            continue
        written.append(f"{name}/{AGENTS_FILE} (fallback)")
        documented.append((directory, summary))
    return written, hard_failed


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
    manager = get_manager(ctx.core)
    map_agent = pick_doc_agent(ctx.session, manager, MAP_AGENTS)
    write_agent = pick_doc_agent(ctx.session, manager, WRITE_AGENTS)
    root_map_agent = pick_doc_agent(ctx.session, manager, ROOT_MAP_AGENTS)
    root_write_agent = pick_doc_agent(ctx.session, manager, WRITE_AGENTS)
    roster = (
        f"team [{', '.join(ctx.session.team_agents)}]"
        if ctx.session.team_agents
        else "no team"
    )
    await ctx.say(
        f"deepinit: documenting {root} and {len(directories)} top-level "
        f"director{'y' if len(directories) == 1 else 'ies'} "
        f"({roster}; map→{map_agent or 'anonymous'}, "
        f"write→{write_agent or 'anonymous'}, "
        f"root map→{root_map_agent or 'anonymous'}, "
        f"root write→{root_write_agent or 'anonymous'})."
    )

    notes = await _map_directories(ctx, root, directories, agent=map_agent)
    await ctx.say(
        f"deepinit: explore done for {len(notes)} director"
        f"{'y' if len(notes) == 1 else 'ies'}; writing {AGENTS_FILE} files."
    )
    written, documented, missing = await _write_directories(
        ctx, root, notes, agent=write_agent
    )

    if missing:
        await ctx.say(
            f"deepinit: writing fallback {AGENTS_FILE} for {len(missing)} "
            f"director{'y' if len(missing) == 1 else 'ies'} the model skipped."
        )
        stub_written, hard_failed = _fill_missing_with_fallback(root, missing, documented)
        written.extend(stub_written)
    else:
        hard_failed = []

    root_label, _root_failure = await _document_root(
        ctx,
        root,
        documented,
        directories,
        map_agent=root_map_agent,
        write_agent=root_write_agent,
    )
    if root_label:
        written.insert(0, root_label)
    elif not (root / AGENTS_FILE).is_file():
        await ctx.say(f"deepinit: writing fallback root {AGENTS_FILE}.")
        try:
            write_root_fallback(root, documented, directories)
            written.insert(0, f"{AGENTS_FILE} (fallback)")
        except OSError as exc:
            hard_failed.append(f"{AGENTS_FILE}: {exc}")

    lines = [f"deepinit: wrote {len(written)} file(s)."]
    lines.extend(f"  {name}" for name in written)
    if hard_failed:
        lines.append(f"{len(hard_failed)} could not be written (filesystem error):")
        lines.extend(f"  {name}" for name in hard_failed)
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
    "DIR_DOC_AGENTS",
    "DOC_TOOLS",
    "FALLBACK_MARKER",
    "MAP_AGENTS",
    "MAP_TOOLS",
    "MAX_DIRS",
    "MIN_FILES",
    "ROOT_DOC_AGENTS",
    "ROOT_MAP_AGENTS",
    "SKIP_DIRS",
    "WRITE_AGENTS",
    "WRITE_TOOLS",
    "cmd_deepinit",
    "dir_task",
    "interesting_dirs",
    "map_dir_task",
    "pick_doc_agent",
    "root_task",
    "write_dir_fallback",
    "write_dir_task",
    "write_root_fallback",
    "write_root_task",
]
