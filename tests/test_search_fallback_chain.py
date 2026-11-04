"""DDGS/Exa Free fallback contracts, including the browser host RPC.

Deterministic tests never access the network. Live provider checks require
SNOWPEA_TEST_LIVE_SEARCH=1 and fail if a provider cannot return real hits.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.server.protocol import WebSearchParams
from snowpea_core.server.web_handlers import web_search_handler
from snowpea_core.tools import search_providers, web
from snowpea_core.tools.search_providers.base import SearchHit, SearchProviderUnavailable


@pytest.mark.parametrize("preferred,backup", [("ddgs", "exa_free"), ("exa_free", "ddgs")])
@pytest.mark.parametrize("failure", ["empty", "unavailable", "exception", "timeout"])
async def test_keyless_fallback_preserves_query_limit_order_and_reason(
    monkeypatch: pytest.MonkeyPatch, preferred: str, backup: str, failure: str
) -> None:
    calls = []
    query = "DeepSWE benchmark datacurve 한글"
    hits = [SearchHit("Real-shaped result", "https://example.org/result", "snippet")]

    for provider in search_providers.chain(preferred):
        pid = provider.meta.id
        monkeypatch.setattr(provider, "available", lambda settings: True)

        async def search(q: str, *, limit: int, pid: str = pid) -> list[SearchHit]:
            calls.append((pid, q, limit))
            if pid == backup:
                return hits
            assert pid == preferred, "a later provider must not run after success"
            if failure == "empty":
                return []
            if failure == "unavailable":
                raise SearchProviderUnavailable("HTTP 429 rate limit")
            if failure == "timeout":
                raise TimeoutError("provider timed out")
            raise RuntimeError("malformed provider response")

        monkeypatch.setattr(provider, "search", search)

    settings = Settings()
    settings.search.provider = preferred
    outcome = await web.search_chain(settings, query, 3)
    assert calls == [(preferred, query, 3), (backup, query, 3)]
    assert outcome.provider == backup
    assert outcome.hits == hits
    assert outcome.fallback_from == preferred
    assert outcome.reason
    assert outcome.skipped[0].startswith(f"{preferred}:")

    calls.clear()
    result = await web_search_handler(
        SimpleNamespace(client_kind="browser", client_id="browser-test"),
        WebSearchParams(query=query, num=3),
        SimpleNamespace(settings=settings),
    )
    assert result.provider == backup
    assert result.results[0].url == hits[0].url
    assert calls == [(preferred, query, 3), (backup, query, 3)]


async def test_all_empty_providers_are_unavailable_not_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    for provider in search_providers.chain("ddgs"):
        pid = provider.meta.id
        monkeypatch.setattr(provider, "available", lambda settings: True)

        async def search(query: str, *, limit: int, pid: str = pid) -> list[SearchHit]:
            calls.append(pid)
            return []

        monkeypatch.setattr(provider, "search", search)
    outcome = await web.search_chain(Settings(), "query", 2)
    assert not outcome.provider and not outcome.hits
    assert calls == [p.meta.id for p in search_providers.chain("ddgs")]
    assert len(calls) == len(set(calls))
    assert all(f"{pid}: no results" in outcome.detail for pid in calls)


@pytest.mark.skipif(
    os.environ.get("SNOWPEA_TEST_LIVE_SEARCH") != "1", reason="opt-in live network search"
)
@pytest.mark.parametrize("pid", ["ddgs", "exa_free"])
async def test_live_keyless_provider_returns_real_results(pid: str) -> None:
    provider = search_providers.get(pid)
    assert provider is not None
    provider.bind(Settings())
    hits = await asyncio.wait_for(
        provider.search("DeepSWE benchmark datacurve", limit=3), timeout=45
    )
    assert 0 < len(hits) <= 3
    assert all(hit.title and hit.url.startswith(("https://", "http://")) for hit in hits)
