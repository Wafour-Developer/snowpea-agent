"""Open escalations that are not a waiting request (M19, 1.53.0).

An approval or question routed to the person stays visible through
``approval.list`` / ``question.list`` until it is answered.  The other
escalations — a delegated session finished, failed or stalled, Jarvis is out of
allowance, Jarvis chose to ask the person — are only a notification, so a
client that reloads would lose them.  This book keeps them in
``$SNOWPEA_HOME/jarvis/escalations.json`` until a person dismisses them
(``jarvis.escalations.list`` / ``jarvis.escalations.dismiss``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("snowpea.jarvis.escalations")

#: Open escalations kept; the oldest go first beyond this.
MAX_OPEN = 500


class EscalationBook:
    """Open escalations, newest first, persisted as one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if self.path.exists():
            # Owner-only on every open, not only when written (review L3).
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = raw.get("open") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict) and item.get("id")]

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            # Created owner-only: the text is never readable by others, even
            # for the moment before a chmod.
            # A fresh file every time, never through a symlink left in its place.
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(tmp, flags, 0o600)
            try:
                with contextlib.suppress(OSError, AttributeError):
                    os.fchmod(fd, 0o600)
                os.write(fd, json.dumps({"open": self._items}, ensure_ascii=False)
                         .encode("utf-8"))
            finally:
                os.close(fd)
            tmp.replace(self.path)
        except OSError:
            log.warning("could not write %s", self.path, exc_info=True)

    def add(self, escalation: dict[str, Any]) -> None:
        """Keep ``escalation`` open (a repeat of the same id moves to the top)."""
        with self._lock:
            self._items = [item for item in self._items if item.get("id") != escalation["id"]]
            self._items.insert(0, dict(escalation))
            del self._items[MAX_OPEN:]
            self._save()

    def page(
        self, *, before: str | None = None, limit: int = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page, newest first; ``before`` is the last id of the previous page."""
        with self._lock:
            items = list(self._items)
        start = 0
        if before:
            ids = [item.get("id") for item in items]
            if before not in ids:
                raise ValueError(f"not an open escalation: {before!r}")
            start = ids.index(before) + 1
        chunk = items[start : start + limit]
        more = start + limit < len(items)
        return chunk, (str(chunk[-1]["id"]) if more and chunk else None)

    def dismiss(self, ids: list[str] | None = None, *, everything: bool = False) -> list[str]:
        """Drop escalations; returns the ids that were open."""
        with self._lock:
            wanted = None if everything else set(ids or ())
            gone = [
                str(item["id"])
                for item in self._items
                if wanted is None or item.get("id") in wanted
            ]
            if gone:
                self._items = [item for item in self._items if str(item["id"]) not in gone]
                self._save()
        return gone

    def ids(self) -> list[str]:
        with self._lock:
            return [str(item["id"]) for item in self._items]


__all__ = ["MAX_OPEN", "EscalationBook"]
