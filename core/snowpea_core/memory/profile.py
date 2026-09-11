"""Key/value facts about the user (M5 contract §1).

A profile fact is an ordinary memory tagged ``profile:<key>``, so it is found
by the same full-text search as everything else and needs no second table.
Writing a key replaces whatever was stored under it before: the user has one
name and one deploy target, not a history of them.
"""

from __future__ import annotations

from snowpea_core.memory.store import MemoryEntry, MemoryStore

PROFILE_TAG = "profile"


def profile_tag(key: str) -> str:
    return f"{PROFILE_TAG}:{key.strip()}"


def key_of(entry: MemoryEntry) -> str | None:
    """The profile key an entry carries, or ``None`` when it is not a fact."""
    for tag in entry.tags:
        if tag.startswith(f"{PROFILE_TAG}:"):
            return tag.split(":", 1)[1]
    return None


class UserProfile:
    """Named facts per namespace, stored as tagged memories."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def set(
        self,
        key: str,
        value: str,
        *,
        namespace: str = "default",
        source_session: str | None = None,
    ) -> MemoryEntry:
        """Remember ``key`` as ``value``, dropping any earlier value."""
        for old in await self.store.by_tag(profile_tag(key), namespace=namespace):
            await self.store.delete(old.id)
        return await self.store.write(
            value,
            tags=[profile_tag(key)],
            namespace=namespace,
            source_session=source_session,
        )

    async def get(self, key: str, *, namespace: str = "default") -> str | None:
        entries = await self.store.by_tag(profile_tag(key), namespace=namespace)
        return entries[0].text if entries else None

    async def all(self, *, namespace: str = "default") -> dict[str, str]:
        """Every fact in the namespace, newest value per key."""
        facts: dict[str, str] = {}
        entries = await self.store.by_tag_prefix(f"{PROFILE_TAG}:", namespace=namespace)
        for entry in reversed(entries):
            key = key_of(entry)
            if key:
                facts[key] = entry.text
        return facts

    async def forget(self, key: str, *, namespace: str = "default") -> bool:
        removed = False
        for entry in await self.store.by_tag(profile_tag(key), namespace=namespace):
            removed = await self.store.delete(entry.id) or removed
        return removed


__all__ = ["PROFILE_TAG", "UserProfile", "key_of", "profile_tag"]
