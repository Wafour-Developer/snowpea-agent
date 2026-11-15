"""Projects the person trusted for external CLIs (security review M4).

An external CLI started in a folder loads that folder's own configuration —
Claude Code's project settings and hooks, Cursor's workspace (snowpea passes
``--trust``), Codex's and agy's project files — and so runs code the repository
ships.  Only the person decides that a project may do so: the first external
session there is opened by a person with ``trust: true`` (a one-time
confirmation in the client); after that Jarvis, a browser host or the
connector may open sessions there too.

Kept in ``$SNOWPEA_HOME/jarvis/external-trust.json`` (0600), never in the
project's own ``.snowpea/settings.json``, which the repository itself could
ship pre-set.  Keyed by the project root's real path.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from snowpea_core.config.paths import utc_now

log = logging.getLogger("snowpea.external.trust")

FILE_NAME = "external-trust.json"


def _key(root: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.path.expanduser(str(root)))


class ProjectTrust:
    """``trusted(root)`` / ``trust(root, by)``; one small JSON file."""

    def __init__(self, home: Path) -> None:
        self.path = Path(home) / "jarvis" / FILE_NAME
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return {}
        projects = raw.get("projects") if isinstance(raw, dict) else None
        return projects if isinstance(projects, dict) else {}

    def trusted(self, root: str | os.PathLike[str] | None) -> bool:
        if not root:
            return False
        entry = self._read().get(_key(root))
        return isinstance(entry, dict) and entry.get("trusted") is True

    def trust(self, root: str | os.PathLike[str], *, by: str) -> None:
        """Mark ``root`` trusted (only ever called for a person's request)."""
        with self._lock:
            projects = self._read()
            projects[_key(root)] = {"trusted": True, "by": by, "at": utc_now()}
            self._write({"projects": projects})

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{FILE_NAME}.{os.getpid()}.tmp")
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2))
        os.replace(tmp, self.path)


__all__ = ["ProjectTrust"]
