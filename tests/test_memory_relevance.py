"""``search_memory`` drops hits that share only the query's common words."""

from __future__ import annotations

from pathlib import Path

from snowpea_core.memory.store import MemoryStore
from snowpea_core.memory.tools import query_words, relevant


async def test_hits_sharing_only_common_words_are_dropped(tmp_path: Path) -> None:
    # Browser trace: "danawa 다나와 site flow" returned 8 "Google Flow" notes.
    store = MemoryStore(tmp_path / "m.db", mirror_files=False)
    try:
        for n in range(30):
            await store.write(f"Google Flow note {n}: the flow editor on the labs site")
        for n in range(10):
            await store.write(f"unrelated note {n} about deployment")
        danawa = await store.write("다나와 (danawa.com): search box #AKCSearch, results .prod_item")
        query = "danawa 다나와 site flow"
        found = await store.search_many(query, namespaces=["default"], limit=50)
        assert any("Google Flow" in e.text for e in found)
        kept = await relevant(store, query, found, ["default"])
        assert [e.id for e in kept] == [danawa.id]
        # Nothing distinctive stored: the common-word hits are not "relevant" either.
        await store.delete(danawa.id)
        found = await store.search_many(query, namespaces=["default"], limit=50)
        assert await relevant(store, query, found, ["default"]) == []
        # A hit holding most of the words stays even when they are common.
        query = "google flow editor"
        found = await store.search_many(query, namespaces=["default"], limit=5)
        assert len(await relevant(store, query, found, ["default"])) == 5
    finally:
        store.close()


def test_query_words() -> None:
    assert query_words('Danawa, "site" a flow flow') == ["danawa", "site", "flow"]
