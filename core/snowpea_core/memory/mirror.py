"""The human-readable mirror of long-term memory (M5 contract §1b).

SQLite is the source of truth; this is the copy a person can open, read and
put under review:

* project memories → ``<project root>/.snowpea/memory.md``
* global memories  → ``$SNOWPEA_HOME/memory.md``
* agent memories   → no mirror (they belong to an agent, not to a human's
  working directory).
* any other namespace (``browser`` and the like) → no mirror.  Only the global
  namespace itself is mirrored: ``scope_of`` calls every unprefixed name
  "global", and the browser's visited pages ended up in a file that says it is
  safe to git.

One line per memory, newest appended at the end::

    - [2026-09-14] 배포 대상은 duho 서버다 #deploy #profile:deploy_target

A delete rewrites the whole file from the store rather than trying to find and
cut a line, because the file is a projection and the store is the truth.  Every
failure here is logged and swallowed: a read-only checkout must not cost the
user a memory.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from snowpea_core.memory.scopes import GLOBAL_NAMESPACE, project_of, scope_of

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.memory.store import MemoryEntry

log = logging.getLogger("snowpea.memory")

#: Name of the mirror file in both locations.
FILENAME = "memory.md"
#: Directory a project keeps its snowpea state in.
PROJECT_DIR = ".snowpea"

HEADER_GLOBAL = "# Snowpea memory — global"
HEADER_PROJECT = "# Snowpea memory — {name}"
NOTE = (
    "<!-- Written by snowpea. The SQLite store is the source of truth; "
    "edits here are not read back. Safe to git-ignore. -->"
)


def mirror_path(namespace: str, home: Path) -> Path | None:
    """Where ``namespace``'s mirror lives, or ``None`` when it has none."""
    # A whitelist: a namespace is mirrored because it is the global one or a
    # project's, never because it merely is not an agent's.
    if namespace == GLOBAL_NAMESPACE:
        return home / FILENAME
    if scope_of(namespace) == "project":
        root = project_of(namespace)
        return Path(root) / PROJECT_DIR / FILENAME if root else None
    return None


def header_for(namespace: str) -> str:
    """The first line of a fresh mirror file."""
    root = project_of(namespace)
    if root:
        return HEADER_PROJECT.format(name=Path(root).name or root)
    return HEADER_GLOBAL


def render_line(entry: MemoryEntry) -> str:
    """One ``- [date] text #tags`` line."""
    date = (entry.created_at or "")[:10]
    stamp = f"[{date}] " if date else ""
    tags = "".join(f" #{tag}" for tag in entry.tags)
    text = " ".join(entry.text.split())
    return f"- {stamp}{text}{tags}"


def render_document(namespace: str, entries: list[MemoryEntry]) -> str:
    """The whole mirror file for ``namespace``, oldest line first."""
    lines = [header_for(namespace), "", NOTE, ""]
    lines.extend(render_line(entry) for entry in entries)
    return "\n".join(lines).rstrip() + "\n"


def _write_private(path: Path, text: str, *, append: bool = False) -> None:
    """Write ``text`` to ``path``, creating it readable by its owner only.

    A mirror can hold personal facts, so a new file is mode 0600; an existing
    one is narrowed too.
    """
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.name != "nt":
        path.chmod(0o600)


#: What the browser tags every page line with.
BROWSER_TAG = "#source:browser"


def scrub_global(home: Path, entries: list[MemoryEntry]) -> bool:
    """Rebuild the global mirror from ``entries`` when it holds browser lines.

    Older versions mirrored every visited page into ``$SNOWPEA_HOME/memory.md``.
    Returns True when the file was rewritten.
    """
    path = home / FILENAME
    try:
        if not path.is_file() or BROWSER_TAG not in path.read_text(encoding="utf-8"):
            return False
        _write_private(path, render_document(GLOBAL_NAMESPACE, entries))
    except OSError as exc:
        log.warning("could not clean the global memory mirror %s: %s", path, exc)
        return False
    return True


def append(namespace: str, home: Path, entry: MemoryEntry) -> Path | None:
    """Append one line, creating the file (and its directory) if needed."""
    path = mirror_path(namespace, home)
    if path is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists() or not path.read_text(encoding="utf-8").strip()
        head = f"{header_for(namespace)}\n\n{NOTE}\n\n" if fresh else ""
        _write_private(path, f"{head}{render_line(entry)}\n", append=True)
    except OSError as exc:
        log.warning("could not mirror memory %s to %s: %s", entry.id, path, exc)
        return None
    return path


def rewrite(namespace: str, home: Path, entries: list[MemoryEntry]) -> Path | None:
    """Replace the mirror with exactly ``entries`` (used after a delete)."""
    path = mirror_path(namespace, home)
    if path is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(path, render_document(namespace, entries))
    except OSError as exc:
        log.warning("could not rewrite the memory mirror %s: %s", path, exc)
        return None
    return path


__all__ = [
    "FILENAME",
    "HEADER_GLOBAL",
    "HEADER_PROJECT",
    "NOTE",
    "BROWSER_TAG",
    "PROJECT_DIR",
    "append",
    "header_for",
    "mirror_path",
    "render_document",
    "render_line",
    "rewrite",
    "scrub_global",
]
