"""Where a session's results, downloads and throwaway files go (1.71.0).

One rule, shared with the browser: every file the agent makes or downloads
that is not throwaway goes in the conversation's project folder, or in the
session's own ``artifacts/`` when it has no project.  Only true temporary
files go to scratch (``<workspace>/tmp``).

``session.create {noProject: true}`` marks a session that has no project
(the browser's default folder is not one).  A session without the flag, and
every session saved before it existed, has a project: its working directory.
A delegated child follows the conversation above it.

Downloads go in ``<results>/.snowpea-downloads``: a dot-folder, so a page
that plants ``conftest.py``, ``*.test.js``, a ``Makefile`` or a
``package.json`` there is skipped by pytest, ``go ./...`` and most globs, and
its ``.gitignore`` (``*``) keeps ``git add -A`` from committing it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snowpea_core.session.workspace import workspace_path

DOWNLOADS = ".snowpea-downloads"
#: What core writes into a downloads folder it finds without one.
GITIGNORE = ".gitignore"
GITIGNORE_TEXT = "*\n"


@dataclass(frozen=True)
class SessionPaths:
    """The resolved folders of one session, all absolute."""

    workdir: str
    workspace_dir: str | None
    results_dir: str
    downloads_dir: str
    scratch_dir: str | None
    has_project: bool

    def wire(self) -> dict[str, Any]:
        """The protocol fields (``session.paths``, ``session.create``, ``tool.invoke``)."""
        return {
            "workdir": self.workdir,
            "workspaceDir": self.workspace_dir,
            "resultsDir": self.results_dir,
            "downloadsDir": self.downloads_dir,
            "scratchDir": self.scratch_dir,
            "hasProject": self.has_project,
        }


def no_project(session: Any, core: Any = None) -> bool:
    """Whether ``session`` (or the conversation above it) has no project.

    The nearest session up the ``parent_session_id`` chain that says so wins;
    a scheduled run is a root of its own, and a lost parent or a cycle ends
    the walk with ``False`` (a project, as before the flag existed).
    """
    sessions = getattr(core, "sessions", None) if core is not None else None
    seen: set[str] = set()
    current = session
    while current is not None:
        if getattr(current, "no_project", False):
            return True
        parent_id = getattr(current, "parent_session_id", None)
        if (
            not parent_id
            or getattr(current, "kind", None) == "scheduled"
            or sessions is None
            or str(parent_id) in seen
        ):
            return False
        seen.add(str(parent_id))
        try:
            current = sessions.get(str(parent_id))
        except Exception:  # noqa: BLE001 - an unreadable registry ends the walk
            return False
    return False


def resolve(session: Any, core: Any = None, home: Path | str | None = None) -> SessionPaths:
    """The folders of ``session``; nothing is created (whoever saves a download
    makes ``.snowpea-downloads/``, so a project gets no folder it never used).

    ``home`` (default ``core.paths.home``) locates a workspace the session has
    not recorded yet.  Without any workspace a no-project session's results
    fall back to the working directory and ``scratch_dir`` is ``None``.
    """
    workdir = str(Path(getattr(session, "workdir", "") or ".").expanduser().resolve())
    workspace = getattr(session, "workspace_dir", None)
    if not workspace:
        if home is None and core is not None:
            home = getattr(getattr(core, "paths", None), "home", None)
        if home is not None and getattr(session, "id", None):
            workspace = str(workspace_path(home, session))
    has_project = not no_project(session, core)
    if has_project or not workspace:
        results = workdir
    else:
        results = str(Path(workspace) / "artifacts")
    return SessionPaths(
        workdir=workdir,
        workspace_dir=str(workspace) if workspace else None,
        results_dir=results,
        downloads_dir=str(Path(results) / DOWNLOADS),
        scratch_dir=str(Path(workspace) / "tmp") if workspace else None,
        has_project=has_project,
    )


def seal_downloads(folder: Path | str) -> None:
    """Give an existing downloads folder its ``.gitignore`` (``*``) if it has none.

    An existing file (or link) of that name is never touched.
    """
    folder = Path(folder)
    marker = folder / GITIGNORE
    if not folder.is_dir() or folder.is_symlink() or os.path.lexists(marker):
        return
    try:
        with open(marker, "x", encoding="utf-8") as handle:
            handle.write(GITIGNORE_TEXT)
    except OSError:  # raced with another writer, or read-only: nothing to add
        pass


def ensure_downloads(paths: SessionPaths) -> Path:
    """Create the session's downloads folder, sealed, when something goes there."""
    folder = Path(paths.downloads_dir)
    folder.mkdir(parents=True, exist_ok=True)
    seal_downloads(folder)
    return folder


def seal_parent_downloads(target: Path | str) -> None:
    """Seal the downloads folder ``target`` was just written into, if any."""
    for parent in Path(target).parents:
        if parent.name == DOWNLOADS:
            seal_downloads(parent)
            return


__all__ = [
    "DOWNLOADS",
    "GITIGNORE",
    "SessionPaths",
    "ensure_downloads",
    "no_project",
    "resolve",
    "seal_downloads",
    "seal_parent_downloads",
]
