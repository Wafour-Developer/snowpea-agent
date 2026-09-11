"""M5 US-014: long-term memory (AC-07).

The daemon runs in-process and a recording provider stands in for the model,
so the assertions are about what the *system prompt* actually carried: a fact
the user asked to be remembered in one session has to come back in the next
one, and survive a restart of the daemon over the same ``SNOWPEA_HOME``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest_asyncio
from _support import RpcClient, connect, make_daemon
from test_session_loop import prompt, start_session

from snowpea_core.config.paths import Paths
from snowpea_core.memory import MemoryServices
from snowpea_core.memory.retrieval import remember_candidate
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolSpec, Usage
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import PROTOCOL_VERSION

MEMORY_ID = re.compile(r'<memory id="(m-[0-9a-f]+)"')

FACT = "기억해: 내 배포 대상은 duho 서버다"
QUESTION = "내 배포 대상이 뭐였지?"


class RecordingProvider:
    """Echoes back what it was told to remember, and keeps every prompt."""

    vendor = "recording"

    def __init__(self) -> None:
        self.model = "recording-1"
        self.system_prompts: list[str] = []

    @property
    def last_system(self) -> str:
        return self.system_prompts[-1] if self.system_prompts else ""

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamEvent]:
        system = next((m.content for m in messages if m.role == "system"), "")
        system = system if isinstance(system, str) else json.dumps(system)
        self.system_prompts.append(system)
        # Answer the way the injected block asks the model to: quote the
        # remembered text and cite its id.
        reply = "알겠습니다."
        found = MEMORY_ID.findall(system)
        if found:
            memory_id = found[0]
            body = system.split(f'id="{memory_id}"', 1)[1].split(">", 1)[1].split("<", 1)[0]
            reply = f"{body} [mem:{memory_id}]"
        for index in range(0, len(reply), 8):
            yield StreamEvent(kind="text_delta", text=reply[index : index + 8])
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=1, output_tokens=1))
        yield StreamEvent(kind="done", stop_reason="end_turn")


def install(daemon: Daemon, provider: RecordingProvider) -> RecordingProvider:
    """Make every session of ``daemon`` talk to ``provider``."""
    assert daemon.core is not None
    daemon.core.providers.get = lambda *args, **kwargs: provider  # type: ignore[method-assign]
    return provider


@pytest_asyncio.fixture
async def home(tmp_path: Path) -> AsyncIterator[Path]:
    """An isolated ``SNOWPEA_HOME`` with no provider pinned by the environment."""
    previous = os.environ.pop("SNOWPEA_PROVIDER", None)
    try:
        yield tmp_path / "home"
    finally:
        if previous is not None:
            os.environ["SNOWPEA_PROVIDER"] = previous


async def run(client: RpcClient, session_id: str, text: str) -> str:
    """Prompt and wait for the turn to finish; returns its reason."""
    turn_id = await prompt(client, session_id, text)
    return await client.wait_turn(turn_id)


def assistant_text(client: RpcClient) -> str:
    done = client.of_kind("message.done")
    return str(done[-1]["payload"]["text"]) if done else ""


async def memories(home: Path, namespace: str = "default") -> list[Any]:
    """Read the stored rows directly, without going through the daemon."""
    services = MemoryServices.open(Paths.create(home))
    try:
        return await services.store.list(namespace=namespace)
    finally:
        services.close()


# ---------------------------------------------------------------------------
# (1) the nudge stores what the user asked to be remembered
# ---------------------------------------------------------------------------


async def test_remember_phrase_is_stored(
    home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(home)
    install(daemon, RecordingProvider())
    try:
        client = await connect(http, daemon)
        session_id = await start_session(client, tmp_path)
        assert await run(client, session_id, FACT) == "complete"
        await asyncio.sleep(0.05)
    finally:
        await daemon.stop()

    rows = await memories(home)
    assert [row.text for row in rows] == ["내 배포 대상은 duho 서버다"]
    assert rows[0].namespace == "default"
    assert rows[0].tags == ["auto"]


async def test_plain_message_is_not_stored(
    home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(home)
    install(daemon, RecordingProvider())
    try:
        client = await connect(http, daemon)
        session_id = await start_session(client, tmp_path)
        assert await run(client, session_id, "오늘 날씨 어때?") == "complete"
    finally:
        await daemon.stop()
    assert await memories(home) == []


def test_remember_candidate_strips_the_trigger() -> None:
    assert remember_candidate(FACT) == "내 배포 대상은 duho 서버다"
    assert remember_candidate("remember that the port is 8788") == "the port is 8788"
    assert remember_candidate("what is the port?") is None
    assert remember_candidate("기억해") is None


# ---------------------------------------------------------------------------
# (2) a later session recalls it — AC-07
# ---------------------------------------------------------------------------


async def test_second_session_recalls_the_fact(
    home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(home)
    provider = install(daemon, RecordingProvider())
    try:
        client = await connect(http, daemon)
        first = await start_session(client, tmp_path)
        assert await run(client, first, FACT) == "complete"
        await asyncio.sleep(0.05)

        second = await start_session(client, tmp_path)
        assert await run(client, second, QUESTION) == "complete"

        system = provider.last_system
        assert "duho" in system, system
        assert "[mem:<id>]" in system  # the citation instruction
        memory_id = MEMORY_ID.findall(system)[0]
        assert f"[mem:{memory_id}]" in assistant_text(client)
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (3) …and after a restart of the daemon over the same home
# ---------------------------------------------------------------------------


async def test_recall_survives_a_restart(
    home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    first_daemon = await make_daemon(home)
    install(first_daemon, RecordingProvider())
    try:
        client = await connect(http, first_daemon)
        session_id = await start_session(client, tmp_path)
        assert await run(client, session_id, FACT) == "complete"
        await asyncio.sleep(0.05)
    finally:
        await first_daemon.stop()

    second_daemon = await make_daemon(home)
    provider = install(second_daemon, RecordingProvider())
    try:
        client = await connect(http, second_daemon)
        session_id = await start_session(client, tmp_path)
        assert await run(client, session_id, QUESTION) == "complete"
        assert "duho" in provider.last_system
        assert "[mem:m-" in assistant_text(client)
    finally:
        await second_daemon.stop()


# ---------------------------------------------------------------------------
# (4) namespaces never cross
# ---------------------------------------------------------------------------


async def test_namespaces_are_isolated(home: Path) -> None:
    services = MemoryServices.open(Paths.create(home))
    try:
        await services.store.write("배포 대상은 duho 서버다", namespace="agent:a")
        assert len(await services.store.search("duho", namespace="agent:a")) == 1
        assert await services.store.search("duho", namespace="agent:b") == []
        assert await services.store.search("duho", namespace="default") == []
    finally:
        services.close()


async def test_session_namespace_scopes_the_nudge(
    home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(home)
    install(daemon, RecordingProvider())
    try:
        client = await connect(http, daemon)
        session_id = await start_session(client, tmp_path)
        assert daemon.core is not None
        session = daemon.core.sessions.get(session_id)
        assert session is not None
        session.memory_namespace = "agent:releaser"
        assert await run(client, session_id, FACT) == "complete"
        await asyncio.sleep(0.05)
    finally:
        await daemon.stop()

    assert await memories(home, "default") == []
    assert [row.text for row in await memories(home, "agent:releaser")] == [
        "내 배포 대상은 duho 서버다"
    ]


# ---------------------------------------------------------------------------
# (5) memory.write / memory.search round trip
# ---------------------------------------------------------------------------


async def test_memory_rpc_round_trip(
    home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(home)
    install(daemon, RecordingProvider())
    try:
        client = await connect(http, daemon)
        written = await client.ok(
            "memory.write",
            {"text": "배포 대상은 duho 서버다", "tags": ["profile:deploy_target"]},
        )
        assert written["id"].startswith("m-")

        found = await client.ok("memory.search", {"query": "duho", "limit": 5})
        assert [hit["id"] for hit in found["hits"]] == [written["id"]]
        assert found["hits"][0]["tags"] == ["profile:deploy_target"]

        scoped = await client.ok("memory.write", {"text": "agent only", "namespace": "agent:a"})
        assert scoped["id"] != written["id"]
        assert await client.ok("memory.search", {"query": "agent only"}) == {"hits": []}
        other = await client.ok("memory.search", {"query": "agent only", "namespace": "agent:a"})
        assert [hit["text"] for hit in other["hits"]] == ["agent only"]

        empty = await client.call("memory.write", {"text": "   "})
        assert empty["error"]["data"]["code"] == "invalid_params"
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (6) the trigram index matches Korean inside a word
# ---------------------------------------------------------------------------


async def test_korean_partial_word_search(home: Path) -> None:
    services = MemoryServices.open(Paths.create(home))
    try:
        assert services.store.tokenizer == "trigram", "sqlite lacks the trigram tokenizer"
        await services.store.write("내 배포 대상은 duho 서버다")
        await services.store.write("점심은 김치찌개를 먹었다")

        # "대상은" is glued to its particle, so a word tokenizer would miss it.
        assert len(await services.store.search("대상")) == 1
        assert len(await services.store.search("배포 대상")) == 1
        assert len(await services.store.search(QUESTION)) == 1
        assert (await services.store.search("김치"))[0].text.startswith("점심은")
    finally:
        services.close()


# ---------------------------------------------------------------------------
# tools and settings
# ---------------------------------------------------------------------------


async def test_memory_tools_are_active(home: Path, http: aiohttp.ClientSession) -> None:
    daemon = await make_daemon(home)
    try:
        client = await connect(http, daemon)
        tools = {tool["name"]: tool for tool in (await client.ok("tool.list"))["tools"]}
        assert tools["memory_write"]["state"] == "active"
        assert tools["memory_search"]["state"] == "active"
        assert tools["memory_write"]["permissionTag"] == "write"
        assert tools["memory_search"]["permissionTag"] == "read"
    finally:
        await daemon.stop()


async def test_memory_can_be_switched_off(
    home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(home, {"memory": {"enabled": False}})
    provider = install(daemon, RecordingProvider())
    try:
        client = await connect(http, daemon)
        session_id = await start_session(client, tmp_path)
        assert await run(client, session_id, FACT) == "complete"
        await asyncio.sleep(0.05)
        assert "<memory" not in provider.last_system
    finally:
        await daemon.stop()

    assert await memories(home) == []


async def test_hello_still_advertises_the_protocol(home: Path, http: aiohttp.ClientSession) -> None:
    """The namespace fields were added additively; the version did not move."""
    daemon = await make_daemon(home)
    try:
        client = await connect(http, daemon)
        info = await client.ok("system.info")
        assert info["protocolVersion"] == PROTOCOL_VERSION
    finally:
        await daemon.stop()
