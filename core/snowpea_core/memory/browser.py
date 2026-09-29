"""Browsing memory: pages a browser host reports, kept in their own namespace (1.7.0).

``memory.ingest`` stores one memory per visited page in the ``browser``
namespace, tagged ``source:browser``, ``url:<url>``, ``hash:<digest>`` and
``visited:<iso>``; the same URL with the same text is stored once.
``memory.delete {source: "browser", url?, before?}`` forgets a site or clears
history.  A session recalls from the namespace only when it opted in
(``session.create {browserMemory}``; on by default for a browser client).
"""

from __future__ import annotations

import hashlib
from typing import Any

BROWSER_NAMESPACE = "browser"
SOURCE_TAG = "source:browser"

#: Page text kept per memory; retrieval needs the gist, not the whole page.
TEXT_CHARS = 4000


def page_digest(url: str, text: str) -> str:
    return hashlib.sha256(f"{url}\n{text}".encode()).hexdigest()[:16]


def page_memory(url: str, title: str, text: str) -> str:
    body = " ".join(text.split())[:TEXT_CHARS]
    head = f"{title.strip()} — {url}" if title.strip() else url
    return f"[visited page] {head}\n{body}".strip()


async def ingest(store: Any, items: list[dict[str, Any]], session_id: str | None) -> dict[str, int]:
    """Store each page once (url + text hash); returns ``{added, skipped}``."""
    added = skipped = 0
    for item in items:
        url = str(item.get("url") or "").strip()
        if not url:
            skipped += 1
            continue
        text = str(item.get("text") or "")
        digest = page_digest(url, text)
        if await store.by_tag(f"hash:{digest}", namespace=BROWSER_NAMESPACE):
            skipped += 1
            continue
        tags = [SOURCE_TAG, f"url:{url}", f"hash:{digest}"]
        visited = str(item.get("visitedAt") or "").strip()
        if visited:
            tags.append(f"visited:{visited}")
        await store.write(
            page_memory(url, str(item.get("title") or ""), text),
            tags=tags,
            namespace=BROWSER_NAMESPACE,
            source_session=session_id,
        )
        added += 1
    return {"added": added, "skipped": skipped}


def _tag_value(tags: list[str], prefix: str) -> str | None:
    return next((tag[len(prefix) :] for tag in tags if tag.startswith(prefix)), None)


async def forget(store: Any, *, url: str | None = None, before: str | None = None) -> int:
    """Delete browsing memories for ``url`` (a page, or a site when it has no path),
    or visited before ``before`` (ISO time), or all of them; returns how many."""
    entries = await store.by_tag(SOURCE_TAG, namespace=BROWSER_NAMESPACE)
    removed = 0
    for entry in entries:
        page = _tag_value(entry.tags, "url:") or ""
        visited = _tag_value(entry.tags, "visited:") or entry.created_at
        if url and not (page == url or page.startswith(url.rstrip("/") + "/")):
            continue
        if before and not (str(visited) < before):
            continue
        if await store.delete(entry.id):
            removed += 1
    return removed


__all__ = ["BROWSER_NAMESPACE", "SOURCE_TAG", "forget", "ingest", "page_digest"]
