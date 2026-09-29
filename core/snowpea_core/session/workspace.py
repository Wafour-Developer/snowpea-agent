"""Per-session workspace: ``$SNOWPEA_HOME/sessions/<YYYY-MM-DD>_<id>/`` (1.7.0).

``tmp/`` is scratch space; ``artifacts/`` holds what the user should get back
(a report, a downloaded file, a screenshot).  ``session.artifacts`` lists the
latter.  A browser host's REPL file sandbox uses the same directory, which
``tool.invoke`` passes as ``workspaceDir``.
"""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SUBDIRS = ("tmp", "artifacts")

#: Most artifacts ``session.artifacts`` lists.
MAX_ARTIFACTS = 500


def workspace_path(home: Path | str, session: Any) -> Path:
    """Where ``session``'s workspace lives, dated by its creation day."""
    created = str(getattr(session, "created_at", "") or "")
    day = created[:10] if len(created) >= 10 else datetime.now(UTC).strftime("%Y-%m-%d")
    return Path(home).expanduser() / "sessions" / f"{day}_{session.id}"


def ensure_workspace(home: Path | str, session: Any) -> Path:
    """Create the workspace if needed and record it on the session."""
    existing = getattr(session, "workspace_dir", None)
    root = Path(existing) if existing else workspace_path(home, session)
    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    session.workspace_dir = str(root)
    return root


def list_artifacts(root: Path | str) -> list[dict[str, Any]]:
    """Files under ``artifacts/``, newest first."""
    base = Path(root) / "artifacts"
    if not base.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        stat = path.stat()
        rows.append(
            {
                "path": str(path),
                "name": str(path.relative_to(base)),
                "size": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                "mimeType": mimetypes.guess_type(path.name)[0],
            }
        )
    rows.sort(key=lambda row: row["modifiedAt"], reverse=True)
    return rows[:MAX_ARTIFACTS]


__all__ = ["SUBDIRS", "ensure_workspace", "list_artifacts", "workspace_path"]
