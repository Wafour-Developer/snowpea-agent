"""``web.search`` for a browser host (protocol 1.11.0, snowpea-browser addendum 21).

A real daemon over ``/ws``: the browser host gets the ``web_search`` tool's
provider chain; anyone else is refused, and a chain with no answer is
``unavailable`` with the reason.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from _support import connect
from test_host_tools import Daemon, daemon, open_client  # noqa: F401

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION, WebSearchParams
from snowpea_core.server.web_handlers import web_search_handler
from snowpea_core.tools import search_providers
from snowpea_core.tools.search_providers.base import SearchHit, SearchProviderUnavailable

HOST = "snowpea-browser-A"


def code_of(frame: dict[str, Any]) -> str:
    assert frame.get("error"), frame
    return str(frame["error"]["data"]["code"])


def answer(monkeypatch: pytest.MonkeyPatch, pid: str, count: int = 30) -> list[int]:
    """``pid`` answers ``count`` hits (capped by the limit asked); returns the limits seen."""
    seen: list[int] = []

    async def search(query: str, *, limit: int) -> list[SearchHit]:
        seen.append(limit)
        return [
            SearchHit(title=f"{query} {i}", url=f"https://example.com/{pid}/{i}", snippet="")
            for i in range(min(count, limit))
        ]

    monkeypatch.setattr(search_providers.get(pid), "search", search)
    return seen


def refuse(monkeypatch: pytest.MonkeyPatch, pid: str, why: str) -> None:
    async def search(query: str, *, limit: int) -> list[SearchHit]:
        raise SearchProviderUnavailable(why)

    monkeypatch.setattr(search_providers.get(pid), "search", search)


async def test_hello_advertises_web_search_at_1_11_0(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=HOST)
    try:
        hello = await client.ok(
            "system.hello",
            {"token": daemon.token, "clientVersion": "t", "protocolVersion": PROTOCOL_VERSION},
        )
        assert "webSearch" in hello["capabilities"] and "webSearch" in CAPABILITIES
        assert tuple(map(int, PROTOCOL_VERSION.split("."))) >= (1, 11, 0)
    finally:
        await client.stop()


async def test_the_configured_provider_answers_with_num_results(
    http: aiohttp.ClientSession, daemon: Daemon, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    daemon.core.settings.search.provider = "ddgs"
    seen = answer(monkeypatch, "ddgs")
    client = await open_client(http, daemon, client_id=HOST)
    try:
        result = await client.ok("web.search", {"query": "snowpea", "num": 3})
        assert result["provider"] == "ddgs"
        assert [hit["url"] for hit in result["results"]] == [
            f"https://example.com/ddgs/{i}" for i in range(3)
        ]
        assert result["results"][0] == {
            "title": "snowpea 0",
            "url": "https://example.com/ddgs/0",
            "snippet": "",
        }
        assert seen == [3]
        # Ten by default.
        assert len((await client.ok("web.search", {"query": "snowpea"}))["results"]) == 10
    finally:
        await client.stop()


async def test_it_falls_back_down_the_chain_like_the_tool(
    http: aiohttp.ClientSession, daemon: Daemon, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    daemon.core.settings.search.provider = "exa_free"
    refuse(monkeypatch, "exa_free", "anonymous MCP rate limit")
    answer(monkeypatch, "ddgs")
    client = await open_client(http, daemon, client_id=HOST)
    try:
        result = await client.ok("web.search", {"query": "snowpea", "num": 2})
        assert result["provider"] == "ddgs" and len(result["results"]) == 2
    finally:
        await client.stop()


async def test_no_provider_is_unavailable_with_the_reason(
    http: aiohttp.ClientSession, daemon: Daemon, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    daemon.core.settings.search.provider = "ddgs"
    for provider in search_providers.chain("ddgs"):
        refuse(monkeypatch, provider.meta.id, f"{provider.meta.id} is down")
    client = await open_client(http, daemon, client_id=HOST)
    try:
        frame = await client.call("web.search", {"query": "snowpea"})
        assert code_of(frame) == errors.UNAVAILABLE
        assert "ddgs: ddgs is down" in frame["error"]["data"]["details"]["reason"]
    finally:
        await client.stop()


async def test_only_a_browser_host_may_search(
    http: aiohttp.ClientSession, daemon: Daemon, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    seen = answer(monkeypatch, "ddgs")
    no_id = await open_client(http, daemon)
    ui = await connect(http, daemon)
    try:
        for client in (no_id, ui):
            frame = await client.call("web.search", {"query": "snowpea"})
            assert code_of(frame) == errors.UNAUTHORIZED
        assert seen == []
    finally:
        for client in (no_id, ui):
            await client.stop()

    gateway = SimpleNamespace(
        surface_id="gateway:telegram:1", client_kind="browser", client_id=HOST
    )
    with pytest.raises(RpcError) as caught:
        await web_search_handler(gateway, WebSearchParams(query="x"), daemon.core)  # type: ignore[arg-type]
    assert caught.value.code == errors.UNAUTHORIZED


@pytest.mark.parametrize(
    "params", [{"query": ""}, {"query": "x", "num": 0}, {"query": "x", "num": 21}]
)
async def test_bad_params_are_invalid(
    http: aiohttp.ClientSession, daemon: Daemon, params: dict[str, Any]  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=HOST)
    try:
        assert code_of(await client.call("web.search", params)) == errors.INVALID_PARAMS
    finally:
        await client.stop()
