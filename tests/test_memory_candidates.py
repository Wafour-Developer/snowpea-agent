"""Addendum 27, C7: memory provenance and suggested preferences.

Rows say who wrote them (``user-said``, ``model``, ``nudge``, ``auto-pref``)
and whether the person confirmed them; ``memory.update`` edits one in place.
With ``memory.autoExtract`` an extractor proposes at most one preference from
the person's own words after a completed turn: ``suggest`` sends
``memory.candidate`` and waits for ``memory.confirm``, ``on`` stores it.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import RpcClient, connect, fake_provider, make_daemon

import snowpea_core.memory as memory_mod
from snowpea_core.memory.scopes import GLOBAL_NAMESPACE
from snowpea_core.memory.store import MemoryStore
from snowpea_core.server.app_server import Daemon

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "memory_extract.json"
PREFERENCE = "Prefers rocket delivery"


async def _daemon(home: Path, mode: str | None) -> Daemon:
    settings = {"memory": {"autoExtract": mode}} if mode else None
    return await make_daemon(home, settings)


async def _turn(client: RpcClient, session_id: str, text: str) -> None:
    started = await client.ok("session.prompt", {"sessionId": session_id, "text": text})
    assert await client.wait_turn(str(started["turnId"])) == "complete"


async def _settle(daemon: Daemon) -> None:
    """Let the extractor's tracked task finish."""
    tasks = list(daemon.core.memory._tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _rows(client: RpcClient) -> list[dict[str, Any]]:
    return (await client.ok("memory.list", {"scope": "global"}))["entries"]


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every text the extractor is given."""
    texts: list[str] = []
    real = memory_mod.extract_preference

    async def recording(provider: Any, text: str) -> Any:
        texts.append(text)
        return await real(provider, text)

    monkeypatch.setattr(memory_mod, "extract_preference", recording)
    return texts


@pytest.fixture
async def session(
    tmp_path: Path, http: aiohttp.ClientSession, request: pytest.FixtureRequest
) -> AsyncIterator[tuple[Daemon, RpcClient, str]]:
    mode = getattr(request, "param", None)
    with fake_provider(FIXTURE):
        daemon = await _daemon(tmp_path / "home", mode)
        client = await connect(http, daemon)
        try:
            workdir = tmp_path / "w"
            workdir.mkdir()
            created = await client.ok(
                "session.create", {"workdir": str(workdir), "mode": "accept"}
            )
            yield daemon, client, str(created["sessionId"])
        finally:
            await client.stop()
            await daemon.stop()


async def test_off_by_default_nothing_is_extracted(
    session: tuple[Daemon, RpcClient, str], seen: list[str]
) -> None:
    daemon, client, session_id = session
    assert daemon.core.settings.memory.autoExtract == "off"
    await _turn(client, session_id, "I always want rocket delivery")
    await _settle(daemon)
    assert seen == []
    assert client.of_kind("memory.candidate") == []
    assert await _rows(client) == []


@pytest.mark.parametrize("session", ["suggest"], indirect=True)
async def test_suggest_sends_a_candidate_and_stores_only_on_confirm(
    session: tuple[Daemon, RpcClient, str], seen: list[str]
) -> None:
    daemon, client, session_id = session
    await _turn(client, session_id, "I always want rocket delivery")
    event = await client.wait(lambda e: e["kind"] == "memory.candidate")
    assert event["sessionId"] == session_id
    payload = event["payload"]
    assert payload["text"] == PREFERENCE and payload["category"] == "shopping"
    await _settle(daemon)
    assert await _rows(client) == []

    stored = await client.ok(
        "memory.confirm", {"candidateId": payload["candidateId"], "accept": True}
    )
    [row] = await _rows(client)
    assert row["id"] == stored["id"] and row["text"] == PREFERENCE
    assert row["source"] == "auto-pref" and row["confirmed"] is True
    assert row["sourceSession"] == session_id
    again = await client.call(
        "memory.confirm", {"candidateId": payload["candidateId"], "accept": True}
    )
    assert again["error"]["data"]["code"] == "not_found"

    # The same preference is not proposed twice once it is remembered.
    await _turn(client, session_id, "rocket delivery, as always")
    await _settle(daemon)
    assert len(client.of_kind("memory.candidate")) == 1


@pytest.mark.parametrize("session", ["suggest"], indirect=True)
async def test_a_dropped_candidate_stores_nothing_and_is_not_asked_again(
    session: tuple[Daemon, RpcClient, str], seen: list[str]
) -> None:
    daemon, client, session_id = session
    await _turn(client, session_id, "I always want rocket delivery")
    event = await client.wait(lambda e: e["kind"] == "memory.candidate")
    # The same text while it is pending is one card, not two.
    await _turn(client, session_id, "rocket delivery please, always")
    await _settle(daemon)
    assert len(client.of_kind("memory.candidate")) == 1
    dropped = await client.ok(
        "memory.confirm", {"candidateId": event["payload"]["candidateId"], "accept": False}
    )
    assert dropped == {"id": None}
    assert await _rows(client) == []
    await _turn(client, session_id, "did I say rocket delivery?")
    await _settle(daemon)
    assert len(client.of_kind("memory.candidate")) == 1


@pytest.mark.parametrize("session", ["suggest"], indirect=True)
async def test_confirming_a_remembered_text_stores_it_once(
    session: tuple[Daemon, RpcClient, str], seen: list[str]
) -> None:
    daemon, client, session_id = session
    await _turn(client, session_id, "I always want rocket delivery")
    event = await client.wait(lambda e: e["kind"] == "memory.candidate")
    held = await daemon.core.memory.store.write(PREFERENCE, namespace=GLOBAL_NAMESPACE)
    stored = await client.ok(
        "memory.confirm", {"candidateId": event["payload"]["candidateId"], "accept": True}
    )
    assert stored == {"id": held.id}
    assert [row["id"] for row in await _rows(client)] == [held.id]


@pytest.mark.parametrize("session", ["on"], indirect=True)
async def test_a_message_the_nudge_stored_is_not_extracted_again(
    session: tuple[Daemon, RpcClient, str], seen: list[str]
) -> None:
    daemon, client, session_id = session
    await _turn(client, session_id, "기억해: 로켓배송만 본다")
    await _settle(daemon)
    assert seen == []
    [row] = await _rows(client)
    assert row["source"] == "nudge"


@pytest.mark.parametrize("session", ["on"], indirect=True)
async def test_on_stores_an_auto_pref_row(
    session: tuple[Daemon, RpcClient, str], seen: list[str]
) -> None:
    daemon, client, session_id = session
    await _turn(client, session_id, "I always want rocket delivery")
    await _settle(daemon)
    [row] = await _rows(client)
    assert row["text"] == PREFERENCE
    assert row["source"] == "auto-pref" and row["confirmed"] is False
    assert client.of_kind("memory.candidate") == []


@pytest.mark.parametrize("session", ["suggest"], indirect=True)
async def test_only_the_persons_words_reach_the_extractor(
    session: tuple[Daemon, RpcClient, str], seen: list[str]
) -> None:
    daemon, client, session_id = session
    await _turn(client, session_id, "read the product page")
    await _settle(daemon)
    assert seen == ["read the product page"]
    assert client.of_kind("tool.result") or client.of_kind("tool.done")
    for event in client.of_kind("memory.candidate"):
        assert "PAGE-SAYS" not in event["payload"]["text"]
        assert "premium" not in event["payload"]["text"]


@pytest.mark.parametrize("session", ["on"], indirect=True)
async def test_a_turn_the_person_did_not_type_is_not_read(
    session: tuple[Daemon, RpcClient, str], seen: list[str]
) -> None:
    daemon, client, session_id = session
    session_obj = daemon.core.sessions.get(session_id)
    memory_mod.extract_after_turn(daemon.core, session_obj, "t-job", "always rocket")
    session_obj.turn_origins["t-job"] = "schedule"
    memory_mod.extract_after_turn(daemon.core, session_obj, "t-job", "always rocket")
    await _settle(daemon)
    assert seen == ["always rocket"]  # the first call, before the origin was set


async def test_writes_carry_their_origin_and_update_edits_in_place(
    session: tuple[Daemon, RpcClient, str],
) -> None:
    daemon, client, session_id = session
    written = await client.ok("memory.write", {"text": "답은 한국어로"})
    [row] = await _rows(client)
    assert row["source"] == "user-said" and row["confirmed"] is True

    store = daemon.core.memory.store
    model = await store.write("uses uv", namespace=GLOBAL_NAMESPACE, origin="model")
    assert model.origin == "model" and model.confirmed is False
    updated = await client.ok("memory.update", {"id": model.id, "text": "uses uv run"})
    assert updated["entry"]["text"] == "uses uv run"
    assert updated["entry"]["source"] == "model" and updated["entry"]["confirmed"] is True
    found = await client.ok("memory.list", {"scope": "global", "query": "uv run"})
    assert [e["id"] for e in found["entries"]] == [model.id]

    refused = await client.call(
        "memory.update", {"id": written["id"], "text": "내 번호는 010-1234-5678"}
    )
    assert refused["error"]["data"]["code"] == "invalid_params"
    assert "phone" in refused["error"]["message"]
    missing = await client.call("memory.update", {"id": "m-missing", "text": "x"})
    assert missing["error"]["data"]["code"] == "not_found"
    page = await store.write("[visited page] x — https://example.com", namespace="browser")
    refused = await client.call("memory.update", {"id": page.id, "text": "edited"})
    assert refused["error"]["data"]["code"] == "invalid_params"


def test_an_older_database_gains_the_columns(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, namespace TEXT NOT NULL DEFAULT 'default',
            text TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
            source_session TEXT, created_at TEXT NOT NULL
        );
        INSERT INTO memories VALUES ('m-old', 'global', 'old note', '[]', NULL, '2026-01-01');
        """
    )
    conn.commit()
    conn.close()
    store = MemoryStore(path, mirror_files=False)
    try:
        [old] = asyncio.run(store.list(namespace="global"))
        assert old.origin is None and old.confirmed is False
    finally:
        store.close()
