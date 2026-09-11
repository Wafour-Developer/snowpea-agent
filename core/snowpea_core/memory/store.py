"""Long-term memory storage (M5 contract §1).

Memories live in the same SQLite file as sessions (``$SNOWPEA_HOME/state.db``)
in two tables: ``memories`` holds the rows, ``memories_fts`` is an FTS5 index
over their text.  The index uses the ``trigram`` tokenizer so Korean — which
has no spaces between a word and its particle — is searchable at all; the
unicode61 tokenizer would only ever match whole space-delimited tokens.

``sqlite3`` is synchronous, so every public method is ``async`` and hands the
work to :func:`asyncio.to_thread`, exactly like
:class:`snowpea_core.session.store.Store`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.config.paths import Paths, utc_now

log = logging.getLogger("snowpea.memory")

#: Longest FTS5 ``MATCH`` expression we build, in trigrams.  A long question
#: otherwise turns into a hundred ORed terms for no extra recall.
MAX_QUERY_TRIGRAMS = 48

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id             TEXT PRIMARY KEY,
    namespace      TEXT NOT NULL DEFAULT 'default',
    text           TEXT NOT NULL,
    tags_json      TEXT NOT NULL DEFAULT '[]',
    source_session TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_namespace ON memories (namespace, created_at);
"""



class MemoryClosed(RuntimeError):
    """A :class:`MemoryStore` operation was attempted after :meth:`MemoryStore.close`.

    Raised instead of letting the underlying ``sqlite3.ProgrammingError`` leak
    out of a background task (the memory nudge, recall) that outlived the
    daemon that scheduled it (CORE-memory-race).
    """


def new_memory_id() -> str:
    return f"m-{uuid.uuid4().hex[:12]}"


@dataclass
class MemoryEntry:
    """One stored memory; ``score`` is only set by :meth:`MemoryStore.search`."""

    id: str
    text: str
    tags: list[str] = field(default_factory=list)
    namespace: str = "default"
    created_at: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "tags": list(self.tags),
            "namespace": self.namespace,
            "createdAt": self.created_at,
            "score": self.score,
        }


def trigrams(text: str, limit: int = MAX_QUERY_TRIGRAMS) -> list[str]:
    """Overlapping 3-grams of ``text``, deduplicated and order-preserving.

    The trigram tokenizer indexes a document's 3-grams, so ORing the query's
    own 3-grams is a substring search that works the same for ``duho`` and for
    ``배포 대상``.  bm25 then ranks the row that shares the most of them first.
    """
    normalised = " ".join(text.split()).lower()
    found: list[str] = []
    seen: set[str] = set()
    for index in range(len(normalised) - 2):
        gram = normalised[index : index + 3]
        if not gram.strip() or gram in seen:
            continue
        seen.add(gram)
        found.append(gram)
        if len(found) >= limit:
            break
    return found


def match_expression(query: str) -> str:
    """FTS5 ``MATCH`` expression for ``query``; ``""`` when it is too short."""
    grams = trigrams(query)
    if not grams:
        return ""
    return " OR ".join('"' + gram.replace('"', "") + '"' for gram in grams)


class MemoryStore:
    """Async facade over the ``memories`` tables."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        #: ``"trigram"`` normally; ``"unicode61"`` on an SQLite too old for it,
        #: in which case Korean only matches whole space-delimited tokens.
        self.tokenizer = "trigram"
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.executescript(SCHEMA)
            self.tokenizer = self._create_index()
            self._conn.commit()

    @classmethod
    def open(cls, paths: Paths) -> MemoryStore:
        """Open (creating if needed) the memory tables under ``$SNOWPEA_HOME``."""
        paths.ensure()
        return cls(paths.state_db)

    def _create_index(self) -> str:
        """Create ``memories_fts``, falling back when trigram is unavailable."""
        existing = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memories_fts'"
        ).fetchone()
        if existing is not None:
            return "trigram" if "trigram" in str(existing["sql"]) else "unicode61"
        for tokenizer in ("trigram", "unicode61"):
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE memories_fts USING fts5("
                    "text, content='memories', content_rowid='rowid',"
                    f" tokenize='{tokenizer}')"
                )
            except sqlite3.OperationalError:
                log.warning("fts5 %s tokenizer unavailable; trying the next one", tokenizer)
                continue
            return tokenizer
        raise sqlite3.OperationalError("sqlite was built without fts5; memory needs it")

    # -- plumbing ------------------------------------------------------
    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            if self._closed:
                raise MemoryClosed("memory store is closed")
            return list(self._conn.execute(sql, params))

    def _row_to_entry(self, row: sqlite3.Row, score: float = 0.0) -> MemoryEntry:
        tags = json.loads(row["tags_json"])
        return MemoryEntry(
            id=row["id"],
            text=row["text"],
            tags=list(tags) if isinstance(tags, list) else [],
            namespace=row["namespace"],
            created_at=row["created_at"],
            score=score,
        )

    # -- writes --------------------------------------------------------
    def _write(self, entry: MemoryEntry, source_session: str | None) -> None:
        with self._lock:
            if self._closed:
                raise MemoryClosed("memory store is closed")
            cursor = self._conn.execute(
                "INSERT INTO memories (id, namespace, text, tags_json, source_session, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entry.id,
                    entry.namespace,
                    entry.text,
                    json.dumps(entry.tags, ensure_ascii=False),
                    source_session,
                    entry.created_at,
                ),
            )
            self._conn.execute(
                "INSERT INTO memories_fts (rowid, text) VALUES (?, ?)",
                (cursor.lastrowid, entry.text),
            )
            self._conn.commit()

    async def write(
        self,
        text: str,
        *,
        tags: list[str] | None = None,
        namespace: str = "default",
        source_session: str | None = None,
    ) -> MemoryEntry:
        """Store one memory and return it."""
        entry = MemoryEntry(
            id=new_memory_id(),
            text=text.strip(),
            tags=list(tags or []),
            namespace=namespace or "default",
            created_at=utc_now(),
        )
        await asyncio.to_thread(self._write, entry, source_session)
        log.info("memory %s stored in %s (%d chars)", entry.id, entry.namespace, len(entry.text))
        return entry

    def _delete(self, memory_id: str) -> bool:
        with self._lock:
            if self._closed:
                raise MemoryClosed("memory store is closed")
            row = self._conn.execute(
                "SELECT rowid, text FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "INSERT INTO memories_fts (memories_fts, rowid, text) VALUES ('delete', ?, ?)",
                (row["rowid"], row["text"]),
            )
            self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.commit()
            return True

    async def delete(self, memory_id: str) -> bool:
        """Remove one memory; ``False`` when the id was unknown."""
        return await asyncio.to_thread(self._delete, memory_id)

    # -- reads ---------------------------------------------------------
    def _search(self, query: str, namespace: str, limit: int) -> list[sqlite3.Row]:
        expression = match_expression(query)
        if expression:
            try:
                return list(
                    self._query(
                        "SELECT m.*, bm25(memories_fts) AS score FROM memories_fts"
                        " JOIN memories m ON m.rowid = memories_fts.rowid"
                        " WHERE memories_fts MATCH ? AND m.namespace = ?"
                        " ORDER BY score LIMIT ?",
                        (expression, namespace, limit),
                    )
                )
            except sqlite3.OperationalError as exc:  # pragma: no cover - malformed query
                log.warning("fts match failed (%s); falling back to LIKE", exc)
        # Query shorter than one trigram, or an expression FTS5 refused: plain
        # substring search, which is what the user meant anyway.
        needle = " ".join(query.split())
        if not needle:
            return []
        return self._query(
            "SELECT *, 0.0 AS score FROM memories"
            " WHERE namespace = ? AND text LIKE ? ORDER BY created_at DESC LIMIT ?",
            (namespace, f"%{needle}%", limit),
        )

    async def search(
        self, query: str, *, namespace: str = "default", limit: int = 8
    ) -> list[MemoryEntry]:
        """Best ``limit`` matches for ``query`` inside ``namespace``, best first."""
        if not query.strip():
            return []
        rows = await asyncio.to_thread(self._search, query, namespace or "default", max(1, limit))
        return [self._row_to_entry(row, float(row["score"])) for row in rows]

    async def get(self, memory_id: str) -> MemoryEntry | None:
        rows = await asyncio.to_thread(
            self._query, "SELECT * FROM memories WHERE id = ?", (memory_id,)
        )
        return self._row_to_entry(rows[0]) if rows else None

    async def _tagged(self, pattern: str, namespace: str) -> list[MemoryEntry]:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM memories WHERE namespace = ? AND tags_json LIKE ?"
            " ORDER BY created_at DESC",
            (namespace or "default", pattern),
        )
        return [self._row_to_entry(row) for row in rows]

    async def by_tag(self, tag: str, *, namespace: str = "default") -> list[MemoryEntry]:
        """Memories carrying exactly ``tag``, newest first."""
        return await self._tagged(f'%"{tag}"%', namespace)

    async def by_tag_prefix(self, prefix: str, *, namespace: str = "default") -> list[MemoryEntry]:
        """Memories carrying a tag starting with ``prefix``, newest first."""
        return await self._tagged(f'%"{prefix}%', namespace)

    async def list(self, *, namespace: str = "default", limit: int = 100) -> list[MemoryEntry]:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM memories WHERE namespace = ? ORDER BY created_at DESC LIMIT ?",
            (namespace or "default", max(1, limit)),
        )
        return [self._row_to_entry(row) for row in rows]

    async def exists(self, text: str, *, namespace: str = "default") -> bool:
        """True when this exact text is already remembered in the namespace."""
        rows = await asyncio.to_thread(
            self._query,
            "SELECT 1 FROM memories WHERE namespace = ? AND text = ? LIMIT 1",
            (namespace or "default", text.strip()),
        )
        return bool(rows)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()


__all__ = [
    "MAX_QUERY_TRIGRAMS",
    "SCHEMA",
    "MemoryClosed",
    "MemoryEntry",
    "MemoryStore",
    "match_expression",
    "new_memory_id",
    "trigrams",
    "utc_now",
]
