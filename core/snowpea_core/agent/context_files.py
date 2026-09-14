"""Nested project instruction files, attached to the tool result that needs them.

``/deepinit`` writes an ``AGENTS.md`` per package directory, but only the one at
the project root ever reached the prompt: the rest were written and never read
(CORE-context-files).  Quoting all of them up front would cost the whole
context window, so the root block only *lists* them (see
:func:`prompts.environment.find_nested_context_files`) and this module hands the
model the nearest one the moment a tool actually touches that directory — the
same placement as the LSP ``Diagnostics`` block after a write.

Each file is attached once per session, tracked in ``session.seen_context_files``
so a loop over twenty files in ``src/`` does not repeat ``src/AGENTS.md`` twenty
times.  Editing an instruction file clears both that mark and the cached
environment block, so the next turn sees what was just written.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.prompts import environment as prompt_env

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.session import Session
    from snowpea_core.tools.registry import ToolResult

log = logging.getLogger("snowpea.agent.context")

#: Tool name -> the argument naming the path the call is about.  ``shell`` is
#: handled separately: its path is the argument of a leading ``cd``.
PATH_ARGUMENTS: dict[str, str] = {
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "list_dir": "path",
    "glob": "path",
    "grep": "path",
}

#: File names a nested directory may use for its instructions.
NESTED_NAMES: tuple[str, ...] = prompt_env.NESTED_CONTEXT_FILE_NAMES

#: Every name that counts as a project instruction file when deciding whether a
#: write invalidates the cached prompt — including the root-only ones.
ALL_CONTEXT_NAMES: frozenset[str] = frozenset(
    {*prompt_env.CONTEXT_FILE_NAMES, *NESTED_NAMES}
)


def _shell_cd_target(command: str) -> str | None:
    """``"cd src && pytest"`` -> ``"src"``; anything else -> ``None``."""
    stripped = command.strip()
    if not stripped.startswith("cd "):
        return None
    rest = stripped[3:].lstrip()
    for separator in ("&&", ";", "||", "|"):
        head, found, _ = rest.partition(separator)
        if found:
            rest = head
    target = rest.strip().strip("\"'")
    return target or None


def path_for_call(name: str, args: dict[str, Any]) -> str | None:
    """The path a tool call is about, or ``None`` when it is about no path."""
    if name == "shell":
        cwd = args.get("cwd")
        if cwd:
            return str(cwd)
        return _shell_cd_target(str(args.get("command", "")))
    argument = PATH_ARGUMENTS.get(name)
    if argument is None:
        return None
    value = str(args.get(argument, "") or "").strip()
    return value or None


def is_context_file(path: str) -> bool:
    """True when ``path`` names a project instruction file, at any depth."""
    text = str(path or "").replace("\\", "/").strip()
    if not text:
        return False
    if text.endswith(".snowpea/instructions.md"):
        return True
    return text.rsplit("/", 1)[-1] in ALL_CONTEXT_NAMES


def nearest_context_file(workdir: Path | str, path: str) -> Path | None:
    """The closest ``AGENTS.md``/``CLAUDE.md`` at or above ``path``, below the root.

    The root file is excluded: it is already quoted in the system prompt, and
    repeating it in a tool result would spend the window twice.  A path outside
    the workdir has no nested file to find.
    """
    root = Path(workdir).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate = candidate.resolve()
    except OSError:  # pragma: no cover - a path that cannot be resolved
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    directory = candidate if candidate.is_dir() else candidate.parent
    while True:
        if directory == root:
            return None
        for name in NESTED_NAMES:
            found = directory / name
            try:
                if found.is_file():
                    return found
            except OSError:  # pragma: no cover
                continue
        if directory.parent == directory:
            return None
        directory = directory.parent


def attach_nested(
    session: Session,
    name: str,
    args: dict[str, Any],
    result: ToolResult,
    *,
    limit: int | None = None,
) -> ToolResult:
    """Append the nearest unseen nested instruction file to a tool result.

    Only fires for a file the system prompt does not already quote: the
    discovery chain and whatever nested files fitted the budget are in
    ``session.loaded_context_files``, and repeating one of those would spend
    the window twice.  Strictly additive and never raises — a call about no
    path, a directory with no instructions, or a file already shown leaves the
    result exactly as it was.
    """
    if limit is None:
        limit = prompt_env.context_file_max_chars(session.context_window)
    if not result.ok:
        return result
    try:
        path = path_for_call(name, args)
        if not path:
            return result
        found = nearest_context_file(session.workdir, path)
        if found is None:
            return result
        relative = found.relative_to(Path(session.workdir).resolve()).as_posix()
        if relative in session.seen_context_files or relative in session.loaded_context_files:
            return result
        text = found.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return result
    if not text.strip():
        return result
    session.seen_context_files.add(relative)
    clipped, _warning = prompt_env.truncate_context_content(
        text.strip(), relative, limit, read_path=relative
    )
    block = (
        f"<context file=\"{relative}\">\n{clipped}\n</context>\n"
        "These are the instructions for that directory and they outrank your defaults."
    )
    result.output = f"{result.output}\n\n{block}" if result.output else block
    return result


def note_write(session: Session, name: str, args: dict[str, Any]) -> bool:
    """Forget what a write to an instruction file made stale.

    Returns True when the call did write one.  Clearing the whole environment
    cache rather than this session's entry is deliberate: ``/init`` and
    ``/deepinit`` write from a *subagent* session, and it is the parent's
    prompt that has to pick the new file up.
    """
    if name not in ("write_file", "edit_file"):
        return False
    path = str(args.get("path", "") or "").strip()
    if not path or not is_context_file(path):
        return False
    session.loaded_context_files.clear()
    from snowpea_core.agent.agent import invalidate_environment

    invalidate_environment()
    try:
        root = Path(session.workdir).resolve()
        target = Path(path)
        if not target.is_absolute():
            target = root / target
        session.seen_context_files.discard(target.resolve().relative_to(root).as_posix())
    except (OSError, ValueError):  # pragma: no cover - a path outside the workdir
        session.seen_context_files.discard(path)
    log.debug("project instruction file %s changed; environment cache cleared", path)
    return True


__all__ = [
    "ALL_CONTEXT_NAMES",
    "NESTED_NAMES",
    "PATH_ARGUMENTS",
    "attach_nested",
    "is_context_file",
    "nearest_context_file",
    "note_write",
    "path_for_call",
]
