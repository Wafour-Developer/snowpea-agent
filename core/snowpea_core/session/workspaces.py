"""Workspaces: the project folders the left sidebar groups conversations by (M17 2d).

A workspace is a registry row keyed by the folder's canonical path (``realpath``),
with a title and a position.  The rows live in the session store but outside the
event log, like dsh's workspace registry: membership is a session's workdir
resolving to the same path.  Once per store (a flag in ``workspace_meta``
remembers it) the registry is filled from the distinct workdirs of the stored
sessions; after that only a new top-level session adds the row for its folder
when there is none (1.50.0).  Listing never adds rows, so a removed workspace
stays removed until a conversation is started in that folder again.

Sessions also carry ``pinned`` and ``archived`` flags (columns on the session row).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import uuid
import weakref
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id          TEXT PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

#: ``workspace_meta`` key set once the registry was filled from the stored sessions.
ADOPTED_KEY = "adopted"

#: Columns added to ``sessions`` for the sidebar.
SESSION_FLAGS: tuple[tuple[str, str], ...] = (("pinned", "INTEGER"), ("archived", "INTEGER"))

#: ``id(store)`` -> a weak reference to that store.  ``id`` alone is reused once a
#: store is gone, so a new store at the same address was taken as ready and its
#: tables were never made ("no such table: workspaces").
_READY: dict[int, weakref.ref[Any]] = {}
#: Two first calls at once (session.list and workspace.add right after connect)
#: ran the migration on two threads: both saw no column and the second ALTER
#: failed with "duplicate column name".  One migration at a time.
_ENSURE_LOCK = threading.Lock()


def canonical(path: str | os.PathLike[str]) -> str:
    """The folder's canonical path; the key a workspace is found by."""
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path).expanduser().absolute())


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _title_for(path: str) -> str:
    return Path(path).name or path


def _ensure_sync(store: Store) -> None:
    with _ENSURE_LOCK:
        _ensure_locked(store)


def _add_column(store: Store, name: str, decl: str) -> None:
    try:
        store._execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")
    except sqlite3.OperationalError as exc:
        # Another connection to the same file (another process) added it first.
        if "duplicate column" not in str(exc).lower():
            raise


def _ensure_locked(store: Store) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            store._execute(statement)
    existing = {str(row["name"]) for row in store._query("PRAGMA table_info(sessions)")}
    for name, decl in SESSION_FLAGS:
        if name not in existing:
            _add_column(store, name, decl)
    if store._query("SELECT 1 FROM workspace_meta WHERE key = ?", (ADOPTED_KEY,)):
        return
    if store._query("SELECT COUNT(*) AS n FROM workspaces")[0]["n"]:
        # A registry from 1.47-1.49 already adopted every folder as it was listed.
        _mark_adopted(store)
        return
    # First open: one workspace per distinct session folder, most recent first.
    rows = store._query(
        "SELECT workdir, MAX(created_at) AS last FROM sessions WHERE workdir IS NOT NULL "
        "AND workdir != '' AND (parent_session_id IS NULL OR parent_session_id = '') "
        "GROUP BY workdir ORDER BY last DESC"
    )
    seen: set[str] = set()
    position = 0
    for row in rows:
        path = canonical(row["workdir"])
        if path in seen:
            continue
        seen.add(path)
        store._execute(
            "INSERT OR IGNORE INTO workspaces (id, path, title, position, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"w-{uuid.uuid4().hex[:10]}", path, _title_for(path), position, _now()),
        )
        position += 1
    _mark_adopted(store)


def _mark_adopted(store: Store) -> None:
    store._execute(
        "INSERT OR REPLACE INTO workspace_meta (key, value) VALUES (?, ?)", (ADOPTED_KEY, _now())
    )


async def ensure(store: Store) -> None:
    """Create the tables and the session flags once per store; fill them on first open."""
    key = id(store)
    ref = _READY.get(key)
    if ref is not None and ref() is store:
        return
    await asyncio.to_thread(_ensure_sync, store)
    _READY[key] = weakref.ref(store)


def _row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "path": row["path"],
        "title": row["title"],
        "position": row["position"],
        "createdAt": row["created_at"],
    }


async def list_all(store: Store) -> list[dict[str, Any]]:
    """Workspaces in order, each with the count of its non-archived sessions."""
    await ensure(store)
    rows = await asyncio.to_thread(
        store._query, "SELECT * FROM workspaces ORDER BY position, created_at"
    )
    counts = await session_counts(store)
    out = []
    for row in rows:
        item = _row(row)
        item["sessions"] = counts.get(item["path"], 0)
        out.append(item)
    return out


async def session_counts(store: Store) -> dict[str, int]:
    rows = await asyncio.to_thread(
        store._query,
        "SELECT workdir, COUNT(*) AS n FROM sessions WHERE (archived IS NULL OR archived = 0) "
        "AND (parent_session_id IS NULL OR parent_session_id = '') GROUP BY workdir",
    )
    counts: dict[str, int] = {}
    for row in rows:
        if row["workdir"]:
            path = canonical(row["workdir"])
            counts[path] = counts.get(path, 0) + int(row["n"])
    return counts


async def find_by_path(store: Store, path: str) -> dict[str, Any] | None:
    await ensure(store)
    rows = await asyncio.to_thread(
        store._query, "SELECT * FROM workspaces WHERE path = ?", (canonical(path),)
    )
    return _row(rows[0]) if rows else None


async def add(store: Store, path: str, title: str | None = None) -> dict[str, Any]:
    """The workspace for ``path``, created (at the top) when there is none."""
    await ensure(store)
    found = await find_by_path(store, path)
    if found is not None:
        return found
    key = canonical(path)
    await asyncio.to_thread(store._execute, "UPDATE workspaces SET position = position + 1")
    await asyncio.to_thread(
        store._execute,
        "INSERT OR IGNORE INTO workspaces (id, path, title, position, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (f"w-{uuid.uuid4().hex[:10]}", key, (title or "").strip() or _title_for(key), _now()),
    )
    created = await find_by_path(store, key)
    assert created is not None
    return created


async def rename(store: Store, workspace_id: str, title: str) -> dict[str, Any] | None:
    await ensure(store)
    clean = " ".join(str(title or "").split())
    if not clean:
        raise ValueError("a workspace needs a title")
    await asyncio.to_thread(
        store._execute, "UPDATE workspaces SET title = ? WHERE id = ?", (clean, workspace_id)
    )
    return await get(store, workspace_id)


async def get(store: Store, workspace_id: str) -> dict[str, Any] | None:
    await ensure(store)
    rows = await asyncio.to_thread(
        store._query, "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
    )
    return _row(rows[0]) if rows else None


async def remove(store: Store, workspace_id: str) -> bool:
    """Drop the registry row only; the sessions in the folder stay."""
    await ensure(store)
    found = await get(store, workspace_id)
    if found is None:
        return False
    await asyncio.to_thread(store._execute, "DELETE FROM workspaces WHERE id = ?", (workspace_id,))
    return True


async def reorder(store: Store, ids: list[str]) -> None:
    """Put ``ids`` first, in that order; the rest keep their relative order after them."""
    await ensure(store)
    current = [item["id"] for item in await list_all(store)]
    wanted = [i for i in ids if i in current]
    order = wanted + [i for i in current if i not in wanted]
    for position, workspace_id in enumerate(order):
        await asyncio.to_thread(
            store._execute,
            "UPDATE workspaces SET position = ? WHERE id = ?",
            (position, workspace_id),
        )


async def set_flag(store: Store, session_id: str, flag: str, value: bool) -> None:
    if flag not in ("pinned", "archived"):
        raise ValueError(flag)
    await ensure(store)
    await asyncio.to_thread(
        store._execute, f"UPDATE sessions SET {flag} = ? WHERE id = ?", (int(value), session_id)
    )


async def session_flags(store: Store) -> dict[str, dict[str, bool]]:
    """``{sessionId: {pinned, archived}}`` for every session with a flag set."""
    await ensure(store)
    rows = await asyncio.to_thread(
        store._query,
        "SELECT id, pinned, archived FROM sessions WHERE pinned = 1 OR archived = 1",
    )
    return {
        row["id"]: {"pinned": bool(row["pinned"]), "archived": bool(row["archived"])}
        for row in rows
    }


__all__ = [
    "SCHEMA",
    "add",
    "canonical",
    "ensure",
    "find_by_path",
    "get",
    "list_all",
    "remove",
    "rename",
    "reorder",
    "session_counts",
    "session_flags",
    "set_flag",
]
