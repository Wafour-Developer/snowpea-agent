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
from snowpea_core.memory import mirror
from snowpea_core.memory.scopes import project_of, scope_of

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



#: Aliases so annotations below still mean the builtins even though
#: :class:`MemoryStore` defines a method called ``list``.
Namespaces = list[str] | tuple[str, ...]
MemoryEntries = list["MemoryEntry"]


def _unique(namespaces: Namespaces) -> tuple[str, ...]:
    """The namespaces, deduplicated, empties dropped, order preserved."""
    seen: list[str] = []
    for namespace in namespaces:
        name = (namespace or "").strip()
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


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
    """One stored memory; ``score`` is only set by :meth:`MemoryStore.search`.

    :attr:`scope` and :attr:`project` are *derived*, never stored: the
    namespace already says which of the three scopes a row is in (M5 §1b), so
    a row written before scopes existed labels itself correctly on read.
    """

    id: str
    text: str
    tags: list[str] = field(default_factory=list)
    namespace: str = "default"
    created_at: str = ""
    score: float = 0.0
    #: ``"project"`` | ``"global"`` | ``"agent"``, from :attr:`namespace`.
    scope: str = "global"
    #: Project root this memory belongs to; ``""`` outside the project scope.
    project: str = ""

    def __post_init__(self) -> None:
        self.scope = scope_of(self.namespace)
        self.project = project_of(self.namespace)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "tags": list(self.tags),
            "namespace": self.namespace,
            "createdAt": self.created_at,
            "score": self.score,
            "scope": self.scope,
            "project": self.project,
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

    def __init__(self, path: Path, *, mirror_files: bool = True) -> None:
        self.path = path
        #: False turns the human-readable ``memory.md`` mirror off entirely.
        self.mirror = mirror_files
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        #: Bumped by every write and delete.  A cache keyed on it — the memory
        #: digest (M5 §1b) — is invalidated the moment the store changes,
        #: without the writer having to know who is caching what.
        self.revision = 0
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
    def open(cls, paths: Paths, *, mirror_files: bool = True) -> MemoryStore:
        """Open (creating if needed) the memory tables under ``$SNOWPEA_HOME``."""
        paths.ensure()
        return cls(paths.state_db, mirror_files=mirror_files)

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

    @property
    def home(self) -> Path:
        """``$SNOWPEA_HOME`` — where the global mirror file lives."""
        return self.path.parent

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
            self.revision += 1

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
        await self.mirror_append(entry)
        return entry

    async def mirror_append(self, entry: MemoryEntry) -> None:
        """Append ``entry`` to the human-readable mirror; never raises (§1b)."""
        if not self.mirror:
            return
        try:
            await asyncio.to_thread(mirror.append, entry.namespace, self.home, entry)
        except Exception:  # noqa: BLE001 - a mirror is a convenience, not the truth
            log.warning("memory mirror append failed for %s", entry.id, exc_info=True)

    async def mirror_rewrite(self, namespace: str) -> None:
        """Rebuild one namespace's mirror from the store; never raises (§1b)."""
        if not self.mirror or mirror.mirror_path(namespace, self.home) is None:
            return
        try:
            entries = await self.list_many(namespaces=[namespace], limit=100000, ascending=True)
            await asyncio.to_thread(mirror.rewrite, namespace, self.home, entries)
        except Exception:  # noqa: BLE001 - a mirror is a convenience, not the truth
            log.warning("memory mirror rewrite failed for %s", namespace, exc_info=True)

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
            self.revision += 1
            return True

    async def delete(self, memory_id: str) -> bool:
        """Remove one memory; ``False`` when the id was unknown.

        A delete rewrites the whole mirror file rather than cutting a line out
        of it: the file is a projection of the store (M5 §1b).
        """
        doomed = await self.get(memory_id)
        removed = await asyncio.to_thread(self._delete, memory_id)
        if removed and doomed is not None:
            await self.mirror_rewrite(doomed.namespace)
        return removed

    # -- reads ---------------------------------------------------------
    def _search(self, query: str, namespaces: tuple[str, ...], limit: int) -> list[sqlite3.Row]:
        slots = ", ".join("?" for _ in namespaces)
        expression = match_expression(query)
        if expression:
            try:
                return list(
                    self._query(
                        "SELECT m.*, bm25(memories_fts) AS score FROM memories_fts"
                        " JOIN memories m ON m.rowid = memories_fts.rowid"
                        f" WHERE memories_fts MATCH ? AND m.namespace IN ({slots})"
                        " ORDER BY score LIMIT ?",
                        (expression, *namespaces, limit),
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
            f" WHERE namespace IN ({slots}) AND text LIKE ?"
            " ORDER BY created_at DESC LIMIT ?",
            (*namespaces, f"%{needle}%", limit),
        )

    async def search(
        self, query: str, *, namespace: str = "default", limit: int = 8
    ) -> list[MemoryEntry]:
        """Best ``limit`` matches for ``query`` inside ``namespace``, best first."""
        return await self.search_many(query, namespaces=[namespace or "default"], limit=limit)

    async def search_many(
        self, query: str, *, namespaces: list[str] | tuple[str, ...], limit: int = 8
    ) -> list[MemoryEntry]:
        """Best ``limit`` matches across several namespaces at once, best first.

        One query, not one per namespace, so bm25 ranks a project memory and a
        global one against each other instead of interleaving two separate
        rankings (M5 §1b).  Order is relevance, never scope.
        """
        wanted = _unique(namespaces)
        if not query.strip() or not wanted:
            return []
        rows = await asyncio.to_thread(self._search, query, wanted, max(1, limit))
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
        return await self.list_many(namespaces=[namespace or "default"], limit=limit)

    async def list_many(
        self,
        *,
        namespaces: Namespaces,
        limit: int = 100,
        ascending: bool = False,
    ) -> MemoryEntries:
        """Memories in ``namespaces``, newest first (oldest first when asked)."""
        wanted = _unique(namespaces)
        if not wanted:
            return []
        slots = ", ".join("?" for _ in wanted)
        order = "ASC" if ascending else "DESC"
        rows = await asyncio.to_thread(
            self._query,
            f"SELECT * FROM memories WHERE namespace IN ({slots})"
            f" ORDER BY created_at {order} LIMIT ?",
            (*wanted, max(1, limit)),
        )
        return [self._row_to_entry(row) for row in rows]

    async def count(self, *, namespaces: Namespaces) -> int:
        """How many memories the namespaces hold in total."""
        wanted = _unique(namespaces)
        if not wanted:
            return 0
        slots = ", ".join("?" for _ in wanted)
        rows = await asyncio.to_thread(
            self._query,
            f"SELECT COUNT(*) AS n FROM memories WHERE namespace IN ({slots})",
            wanted,
        )
        return int(rows[0]["n"]) if rows else 0

    async def exists(self, text: str, *, namespace: str = "default") -> bool:
        """True when this exact text is already remembered in the namespace."""
        return await self.exists_any(text, namespaces=[namespace or "default"])

    async def exists_any(
        self, text: str, *, namespaces: Namespaces
    ) -> bool:
        """True when this exact text is already remembered in *any* namespace given.

        The auto-remember nudge asks across every scope the session can see, so
        a fact the scope-aware ``memory_write`` already filed under the project
        is not written a second time globally (M5 §1b).
        """
        wanted = _unique(namespaces)
        if not wanted:
            return False
        slots = ", ".join("?" for _ in wanted)
        rows = await asyncio.to_thread(
            self._query,
            f"SELECT 1 FROM memories WHERE namespace IN ({slots}) AND text = ? LIMIT 1",
            (*wanted, text.strip()),
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
    "MemoryEntries",
    "MemoryEntry",
    "MemoryStore",
    "Namespaces",
    "match_expression",
    "new_memory_id",
    "trigrams",
    "utc_now",
]
