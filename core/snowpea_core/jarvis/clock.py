"""Time for the Jarvis service, injectable so tests can move it by hand."""

from __future__ import annotations

import time
from datetime import UTC, datetime


def iso(moment: datetime) -> str:
    """The daemon's wire timestamp format (``config.paths.utc_now``) for ``moment``."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(text: str | None) -> datetime | None:
    """A wire timestamp back to an aware UTC datetime; ``None`` when unreadable."""
    if not text:
        return None
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Clock:
    """Monotonic seconds for timers and the wall clock for wire times."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)


__all__ = ["Clock", "iso", "parse_iso"]
