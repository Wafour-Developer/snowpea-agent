"""Path completion for the ``file.complete`` RPC."""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("snowpea.agent.file_complete")

DEFAULT_LIMIT = 30
MAX_LIMIT = 200

#: Always skipped, even outside git repos.
SKIP_NAMES = frozenset({".git", "node_modules", ".venv", "__pycache__"})

#: ``workdir -> (monotonic_ts, relative paths from git ls-files)``.
_GIT_CACHE: dict[Path, tuple[float, list[str]]] = {}
_CACHE_TTL = 5.0


@dataclass(frozen=True)
class CompleteEntry:
    path: str
    kind: str
    size: int | None = None


def _within_workdir(workdir: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(workdir.resolve())
        return True
    except ValueError:
        return False


def _git_paths(workdir: Path) -> list[str] | None:
    """Tracked + untracked paths from git, or ``None`` when not a repo."""
    now = time.monotonic()
    cached = _GIT_CACHE.get(workdir)
    if cached is not None and now - cached[0] < _CACHE_TTL:
        return cached[1]
    if not (workdir / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    _GIT_CACHE[workdir] = (now, paths)
    return paths


def _walk_tree(workdir: Path) -> list[str]:
    """Every file and directory under ``workdir``, relative, ``/`` separators."""
    found: list[str] = []
    root = workdir.resolve()
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_NAMES for part in path.parts):
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if path.is_symlink():
            target = path.resolve()
            if not _within_workdir(workdir, target):
                continue
        rel_text = rel.as_posix()
        if path.is_dir():
            found.append(rel_text + "/")
        else:
            found.append(rel_text)
    return found


def _all_paths(workdir: Path) -> list[str]:
    git_paths = _git_paths(workdir)
    if git_paths is not None:
        dirs: set[str] = set()
        files: list[str] = []
        for rel in git_paths:
            parts = rel.split("/")
            if any(part in SKIP_NAMES for part in parts):
                continue
            files.append(rel)
            for index in range(1, len(parts)):
                dirs.add("/".join(parts[:index]) + "/")
        top_dirs = {name + "/" for name in _list_top(workdir) if (workdir / name).is_dir()}
        dirs.update(top_dirs)
        return sorted(set(files) | dirs, key=lambda p: (p.endswith("/"), p.lower()))
    return _walk_tree(workdir)


def _list_top(workdir: Path) -> list[str]:
    names: list[str] = []
    try:
        for child in workdir.iterdir():
            if child.name in SKIP_NAMES:
                continue
            if child.is_symlink():
                if not _within_workdir(workdir, child):
                    continue
            names.append(child.name)
    except OSError:
        return []
    return sorted(names, key=str.lower)


def _show_hidden(query: str) -> bool:
    segment = query.rsplit("/", 1)[-1]
    return segment.startswith(".")


def _skip_hidden(rel: str) -> bool:
    parts = rel.rstrip("/").split("/")
    return any(part.startswith(".") for part in parts if part)


def _subsequence_score(query: str, path: str) -> tuple[int, int, int] | None:
    """Lower is better: kind, basename rank, path length."""
    q = query.lower()
    if not q:
        return (0, 0, len(path))
    basename = path.rstrip("/").rsplit("/", 1)[-1].lower()
    if basename.startswith(q):
        return (0, 0, len(path))
    if q in basename:
        return (0, 1, len(path))
    index = 0
    for ch in q:
        found = path.lower().find(ch, index)
        if found < 0:
            return None
        index = found + 1
    return (1, 2, len(path))


def _prefix_entries(workdir: Path, query: str, limit: int) -> tuple[list[CompleteEntry], bool]:
    """Complete inside ``query``'s directory prefix."""
    if not query.endswith("/") and "/" not in query:
        return [], False
    if query.endswith("/"):
        dir_rel = query
        prefix = ""
    else:
        dir_rel, prefix = query.rsplit("/", 1)
        dir_rel = dir_rel + "/"
    dir_path = (workdir / dir_rel).resolve()
    if not _within_workdir(workdir, dir_path) or not dir_path.is_dir():
        return [], False
    entries: list[CompleteEntry] = []
    try:
        children = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return [], False
    show_hidden = _show_hidden(prefix)
    for child in children:
        if child.name in SKIP_NAMES:
            continue
        if child.is_symlink() and not _within_workdir(workdir, child):
            continue
        if not show_hidden and child.name.startswith("."):
            continue
        if prefix and not child.name.lower().startswith(prefix.lower()):
            continue
        rel_parent = dir_rel if dir_rel != "./" else ""
        rel = f"{rel_parent}{child.name}"
        if child.is_dir():
            rel += "/"
            entries.append(CompleteEntry(path=rel, kind="dir"))
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = None
            entries.append(CompleteEntry(path=rel, kind="file", size=size))
    truncated = len(entries) > limit
    return entries[:limit], truncated


def complete_paths(
    workdir: Path, query: str, *, limit: int = DEFAULT_LIMIT
) -> tuple[list[CompleteEntry], bool]:
    """Return matching entries relative to ``workdir``."""
    workdir = workdir.resolve()
    limit = max(1, min(limit, MAX_LIMIT))
    query = query.replace("\\", "/")
    if ".." in query.split("/"):
        return [], False
    if not query:
        git_paths = _git_paths(workdir)
        if git_paths is not None:
            top_names: set[str] = set()
            for rel in git_paths:
                top_names.add(rel.split("/")[0])
            for rel in git_paths:
                parts = rel.split("/")
                for index in range(1, len(parts)):
                    top_names.add(parts[index - 1] + "/")
            names = sorted(top_names, key=lambda name: (name.endswith("/"), name.lower()))
        else:
            names = []
            for name in _list_top(workdir):
                child = workdir / name
                names.append(name + "/" if child.is_dir() else name)
        entries: list[CompleteEntry] = []
        show_hidden = _show_hidden("")
        for name in names:
            base = name.rstrip("/")
            if base in SKIP_NAMES:
                continue
            if not show_hidden and base.startswith("."):
                continue
            child = workdir / name.rstrip("/")
            if name.endswith("/") or child.is_dir():
                rel = name if name.endswith("/") else f"{name}/"
                entries.append(CompleteEntry(path=rel, kind="dir"))
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
                entries.append(CompleteEntry(path=name, kind="file", size=size))
        truncated = len(names) > limit
        return entries[:limit], truncated
    if "/" in query or query.endswith("/"):
        return _prefix_entries(workdir, query, limit)
    paths = _all_paths(workdir)
    show_hidden = _show_hidden(query)
    scored: list[tuple[tuple[int, int, int], str]] = []
    for rel in paths:
        if any(part in SKIP_NAMES for part in rel.split("/")):
            continue
        if not show_hidden and _skip_hidden(rel):
            continue
        score = _subsequence_score(query, rel)
        if score is not None:
            scored.append((score, rel))
    scored.sort(key=lambda item: (item[0], item[1].lower()))
    truncated = len(scored) > limit
    matched: list[CompleteEntry] = []
    for _, rel in scored[:limit]:
        full = workdir / rel.rstrip("/")
        if rel.endswith("/") or full.is_dir():
            matched.append(CompleteEntry(path=rel if rel.endswith("/") else rel + "/", kind="dir"))
            continue
        try:
            size = full.stat().st_size
        except OSError:
            size = None
        matched.append(CompleteEntry(path=rel, kind="file", size=size))
    return matched, truncated


def refuse_escape(workdir: Path, query: str) -> bool:
    """True when a relative query tries to leave ``workdir``."""
    if not query or query.startswith("/") or query.startswith("~"):
        return False
    parts = [part for part in query.replace("\\", "/").split("/") if part and part != "."]
    depth = 0
    for part in parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        else:
            depth += 1
    return False


__all__ = [
    "CompleteEntry",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "complete_paths",
    "refuse_escape",
]
