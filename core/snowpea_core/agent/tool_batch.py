"""Plan parallel vs sequential tool-call segments within one model turn.

Adapted from Hermes ``agent/tool_dispatch_helpers.py`` (MIT) — see
``docs/design/deviations/CORE-policies.md``.  Snowpea maps its own tool
names and resolves paths against the session workdir.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from snowpea_core.providers.base import ToolCall

# Interactive or session-mutating tools — always a sequential barrier.
_NEVER_PARALLEL = frozenset(
    {
        "ask_user",
        "delegate_task",
        "set_mode",
        "settings_set",
        "queue_command",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "git_commit",
        "process_kill",
    }
)

# Read-only tools with no path overlap concerns.
_PARALLEL_SAFE = frozenset(
    {
        "web_search",
        "web_extract",
        "skill_search",
        "skill_list",
        "skill_view",
        "tool_search",
        "git_status",
        "git_diff",
        "git_log",
        "process_list",
        "image_generate",
        "video_generate",
        "music_generate",
        "transcribe_audio",
        "text_to_speech",
    }
)

_PATH_READERS = frozenset({"read_file", "view_image", "grep", "glob", "list_dir"})
_PATH_WRITERS = frozenset({"write_file", "patch"})
_PATH_SCOPED = _PATH_READERS | _PATH_WRITERS

_DESTRUCTIVE_SHELL = re.compile(
    r"""(?:^|\s|&&|\|\||;|`)(?:
        rm\s|rmdir\s|
        cp\s|install\s|
        mv\s|
        sed\s+-i|
        truncate\s|
        dd\s|
        shred\s|
        git\s+(?:reset|clean|checkout)\s
    )""",
    re.VERBOSE,
)
_REDIRECT_OVERWRITE = re.compile(r"[^>]>[^>]|^>[^>]")


def _canonical_path(raw_path: str, workdir: Path) -> Path:
    expanded = Path(raw_path).expanduser()
    candidate = expanded if expanded.is_absolute() else workdir / expanded
    resolved = os.path.normcase(os.path.realpath(os.path.abspath(str(candidate))))
    return Path(resolved)


def _paths_overlap(left: Path, right: Path) -> bool:
    left_parts = left.parts
    right_parts = right.parts
    if not left_parts or not right_parts:
        return bool(left_parts) == bool(right_parts) and bool(left_parts)
    common_len = min(len(left_parts), len(right_parts))
    return left_parts[:common_len] == right_parts[:common_len]


def _shell_is_destructive(command: str) -> bool:
    if not command:
        return False
    if _DESTRUCTIVE_SHELL.search(command):
        return True
    return bool(_REDIRECT_OVERWRITE.search(command))


def _extract_scope_paths(name: str, args: dict[str, object], workdir: Path) -> list[Path]:
    if name not in _PATH_SCOPED:
        return []
    raw_paths: list[str] = []
    path = args.get("path")
    if isinstance(path, str) and path.strip():
        raw_paths.append(path.strip())
    elif name in {"glob", "grep", "list_dir"}:
        raw_paths.append(".")
    scoped: list[Path] = []
    seen: set[str] = set()
    for raw in raw_paths:
        canonical = _canonical_path(raw, workdir)
        key = str(canonical)
        if key in seen:
            continue
        seen.add(key)
        scoped.append(canonical)
    return scoped


def plan_tool_batch_segments(
    calls: list[ToolCall],
    *,
    workdir: Path | str | None = None,
) -> list[tuple[str, list[ToolCall]]]:
    """Split ``calls`` into ordered ``(kind, batch)`` segments.

    ``kind`` is ``"parallel"`` or ``"sequential"``.  Path-scoped readers may
    overlap each other; any writer conflict closes the current parallel run.
    """
    base = Path(workdir or Path.cwd()).resolve()
    segments: list[list] = []
    current: list[ToolCall] = []
    reserved: list[tuple[Path, bool]] = []

    def _close_parallel() -> None:
        nonlocal current, reserved
        if current:
            segments.append(["parallel", current])
            current = []
            reserved = []

    def _add_sequential(call: ToolCall) -> None:
        _close_parallel()
        if segments and segments[-1][0] == "sequential":
            segments[-1][1].append(call)
        else:
            segments.append(["sequential", [call]])

    for call in calls:
        name = call.name
        if name in _NEVER_PARALLEL:
            _add_sequential(call)
            continue
        if name == "shell":
            _add_sequential(call)
            continue
        try:
            args = dict(call.arguments)
        except Exception:
            _add_sequential(call)
            continue
        if not isinstance(args, dict):
            _add_sequential(call)
            continue

        if name in _PATH_SCOPED:
            scoped_paths = _extract_scope_paths(name, args, base)
            if not scoped_paths:
                _add_sequential(call)
                continue
            is_writer = name in _PATH_WRITERS
            if any(
                (is_writer or existing_writer) and _paths_overlap(scoped, existing)
                for scoped in scoped_paths
                for existing, existing_writer in reserved
            ):
                _close_parallel()
            reserved.extend((path, is_writer) for path in scoped_paths)
            current.append(call)
            continue

        if name in _PARALLEL_SAFE:
            current.append(call)
            continue

        _add_sequential(call)

    _close_parallel()

    normalized: list[list] = []
    for kind, batch in segments:
        if kind == "parallel" and len(batch) < 2:
            kind = "sequential"
        if normalized and normalized[-1][0] == "sequential" and kind == "sequential":
            normalized[-1][1].extend(batch)
        else:
            normalized.append([kind, batch])
    return [(str(kind), list(batch)) for kind, batch in normalized]


__all__ = ["plan_tool_batch_segments"]
