"""Queued ``session.notice`` lines: cap, age bound, and SQLite persistence."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from snowpea_core.config.paths import utc_now

log = logging.getLogger("snowpea.session.notices")

#: Longest ``session.notice`` text kept; notices beyond this many are dropped.
NOTICE_CHARS = 2000
MAX_NOTICES = 20
NOTICE_MAX_AGE = timedelta(hours=24)


def clip_notice_text(text: str) -> str:
    """Whitespace-normalise and cap one notice line."""
    return " ".join(text.split())[:NOTICE_CHARS]


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def bound_notice_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop notices older than :data:`NOTICE_MAX_AGE` and keep at most :data:`MAX_NOTICES`."""
    cutoff = datetime.now(UTC) - NOTICE_MAX_AGE
    kept: list[dict[str, str]] = []
    for item in items:
        text = str(item.get("text") or "")
        if not text:
            continue
        queued = _parse_iso(str(item.get("queuedAt") or ""))
        if queued is not None and queued < cutoff:
            continue
        kept.append({"text": text, "queuedAt": str(item.get("queuedAt") or utc_now())})
    return kept[-MAX_NOTICES:]


def _items_from_texts(texts: list[str], previous: list[dict[str, str]]) -> list[dict[str, str]]:
    """Rebuild stored rows, keeping ``queuedAt`` for texts still in the queue."""
    times = {str(item["text"]): str(item["queuedAt"]) for item in previous if item.get("text")}
    now = utc_now()
    items = [{"text": text, "queuedAt": times.get(text, now)} for text in texts if text]
    return bound_notice_items(items)


def _decode_items(raw: str | None) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    items: list[dict[str, str]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        items.append({"text": text, "queuedAt": str(entry.get("queuedAt") or utc_now())})
    return bound_notice_items(items)


def _encode_items(items: list[dict[str, str]]) -> str:
    return json.dumps(bound_notice_items(items), ensure_ascii=False)


async def load_into_session(session: Any, store: Any) -> None:
    """Restore a session's queued notices from the store."""
    items = _decode_items(await store.read_pending_notices(session.id))
    session.pending_notices = [item["text"] for item in items]
    session._pending_notice_items = items


async def persist_session_notices(store: Any, session: Any) -> None:
    """Write the in-memory queue to the session row."""
    previous = getattr(session, "_pending_notice_items", None)
    if not isinstance(previous, list):
        previous = _decode_items(await store.read_pending_notices(session.id))
    items = _items_from_texts(list(session.pending_notices), previous)
    session._pending_notice_items = items
    session.pending_notices = [item["text"] for item in items]
    await store.write_pending_notices(session.id, _encode_items(items))


async def clear_session_notices(store: Any, session: Any) -> None:
    """Drop every queued notice from memory and from the store."""
    session.pending_notices.clear()
    session._pending_notice_items = []
    await store.write_pending_notices(session.id, None)


def schedule_persist(core: Any, session: Any) -> None:
    """Persist after a synchronous queue change when a store exists."""
    store = getattr(core, "store", None) if core is not None else None
    if store is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def run() -> None:
        try:
            await persist_session_notices(store, session)
        except Exception:  # noqa: BLE001 - persistence must not break a turn
            log.debug("could not persist notices for %s", session.id, exc_info=True)

    loop.create_task(run())


__all__ = [
    "MAX_NOTICES",
    "NOTICE_CHARS",
    "NOTICE_MAX_AGE",
    "bound_notice_items",
    "clear_session_notices",
    "clip_notice_text",
    "load_into_session",
    "persist_session_notices",
    "schedule_persist",
]