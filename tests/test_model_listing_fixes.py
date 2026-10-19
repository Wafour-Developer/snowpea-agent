"""Model listings that put a usable model first (snowpea-browser, 2026-10-19).

The ChatGPT live list already worked; what users saw was its fallback, and the
same class of issue sat in other vendors' listings.  Pinned here: the Codex
catalog keeps listed models in priority order, revalidates with its ETag, tries
once more and reports why when it fails (never caching the fallback), and
drives reasoning, effort, windows and the default model; OpenAI, Gemini and
Anthropic listings are filtered, paged and ordered so ``models[0]`` is a
sensible default.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from snowpea_core.providers import codex_transport, context_windows
from snowpea_core.providers import models as model_discovery
from snowpea_core.providers.presets import PRESETS


def _jwt(account_id: str = "acct-1") -> str:
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


CREDENTIALS = {"auth_method": "chatgpt", "access_token": _jwt()}

CATALOG = {
    "models": [
        {"slug": "gpt-reserve", "visibility": "hide", "priority": 4},
        {
            "slug": "gpt-6-astra",
            "visibility": "list",
            "priority": 2,
            "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
            "default_reasoning_level": "high",
            "context_window": 400000,
        },
        {
            "slug": "gpt-6.1-sol",
            "visibility": "list",
            "priority": 1,
            "supported_reasoning_levels": [
                {"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")
            ],
            "default_reasoning_level": "low",
            "context_window": 1000000,
        },
        {
            "slug": "gpt-6-luna",
            "visibility": "list",
            "priority": 4,
            "supported_reasoning_levels": [],
        },
        {"slug": "codex-auto-review", "visibility": "hide", "priority": 43},
    ]
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Any:
    model_discovery.cache_clear()
    context_windows.cache_clear()
    monkeypatch.setattr(model_discovery, "CODEX_RETRY_DELAY_SEC", 0.0)
    yield
    model_discovery.cache_clear()
    context_windows.cache_clear()


# ---------------------------------------------------------------------------
# ChatGPT (Codex backend)
# ---------------------------------------------------------------------------


async def test_the_live_list_drops_hidden_models_and_keeps_priority_order(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CATALOG, headers={"etag": 'W/"v1"'})

    listing = await model_discovery.resolve_models(
        "openai", auth_method="chatgpt", credentials=CREDENTIALS, home=tmp_path,
        transport=httpx.MockTransport(handler),
    )
    assert listing.source == model_discovery.SOURCE_LIVE
    assert listing.models == ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"]
    request = seen[0]
    assert request.url.host == "chatgpt.com" and request.url.params["client_version"]
    assert request.headers["ChatGPT-Account-Id"] == "acct-1"
    assert model_discovery.codex_default_model() == "gpt-6.1-sol"


async def test_the_catalog_is_reused_then_revalidated_with_its_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("if-none-match") == 'W/"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json=CATALOG, headers={"etag": 'W/"v1"'})

    transport = httpx.MockTransport(handler)
    first = await model_discovery.codex_catalog(CREDENTIALS, transport=transport)
    again = await model_discovery.codex_catalog(CREDENTIALS, transport=transport)
    assert first == again and len(seen) == 1  # served from memory inside the TTL
    refreshed = await model_discovery.codex_catalog(CREDENTIALS, transport=transport, refresh=True)
    assert refreshed == first and seen[-1].headers["if-none-match"] == 'W/"v1"'


async def test_a_failure_is_retried_once_then_reported_and_never_cached(tmp_path: Path) -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"detail": "token not saved yet"})
        return httpx.Response(200, json=CATALOG)

    ids, error = await model_discovery.codex_catalog_result(
        CREDENTIALS, transport=httpx.MockTransport(flaky)
    )
    assert calls["n"] == 2 and ids[0] == "gpt-6.1-sol" and error is None

    model_discovery.cache_clear()

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    listing = await model_discovery.resolve_models(
        "openai", auth_method="chatgpt", credentials=CREDENTIALS, home=tmp_path,
        transport=httpx.MockTransport(down),
    )
    assert listing.source == model_discovery.SOURCE_CURATED
    assert listing.models == list(codex_transport.CODEX_MODELS)  # no models.dev API ids
    assert listing.error and "503" in listing.error
    assert not model_discovery.cache_path(tmp_path, "openai", "chatgpt").exists()
    assert model_discovery.codex_live_models() is None


async def test_an_empty_answer_is_a_failure_not_a_catalog() -> None:
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": []})

    ids, error = await model_discovery.codex_catalog_result(
        CREDENTIALS, transport=httpx.MockTransport(empty)
    )
    assert ids == [] and "no models" in (error or "")


async def test_reasoning_efforts_and_windows_come_from_the_catalog() -> None:
    await model_discovery.codex_catalog(
        CREDENTIALS, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=CATALOG))
    )
    assert codex_transport.supports_reasoning("gpt-6.1-sol") is True
    assert codex_transport.supports_reasoning("gpt-6-luna") is False  # catalog: no levels
    assert codex_transport.supports_reasoning("gpt-6-unknown") is True  # heuristic
    assert codex_transport.effort_for("gpt-6.1-sol", None) == "low"  # model default
    assert codex_transport.effort_for("gpt-6.1-sol", "xhigh") == "xhigh"
    assert codex_transport.effort_for("gpt-6-astra", "medium") in {"low", "high"}
    assert codex_transport.effort_for("gpt-6-astra", "xhigh") == "high"
    window_key = (PRESETS["openai"].base_url or "").rstrip("/")
    assert context_windows.last_known("openai", window_key, "gpt-6.1-sol") == 1000000

    provider = codex_transport.CodexProvider(dict(CREDENTIALS), model="gpt-6.1-sol")
    body = provider.build_request([], [])
    assert body["reasoning"] == {"effort": "low"}
    luna = codex_transport.CodexProvider(dict(CREDENTIALS), model="gpt-6-luna")
    assert "reasoning" not in luna.build_request([], [])


def test_gpt_6_models_take_reasoning_without_a_catalog() -> None:
    assert codex_transport.supports_reasoning("gpt-6.1-sol") is True
    default = codex_transport.DEFAULT_REASONING_EFFORT
    assert codex_transport.effort_for("gpt-6.1-sol", None) == default


async def test_a_model_saved_from_an_old_fallback_gives_way_to_the_live_default(
    tmp_path: Path,
) -> None:
    from snowpea_core.config.paths import Paths
    from snowpea_core.config.settings import Settings
    from snowpea_core.providers.registry import ProviderRegistry

    settings = Settings()
    settings.providers["openai"] = {**CREDENTIALS, "model": "gpt-5-codex"}
    registry = ProviderRegistry(settings, Paths.create(tmp_path / "home"))
    # Before any listing nothing contradicts the saved model.
    assert registry.model_for("openai") == "gpt-5-codex"
    await model_discovery.codex_catalog(
        CREDENTIALS, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=CATALOG))
    )
    assert registry.model_for("openai") == "gpt-6.1-sol"
    settings.providers["openai"]["model"] = "gpt-6-astra"
    assert registry.model_for("openai") == "gpt-6-astra"
    del settings.providers["openai"]["model"]
    assert registry.model_for("openai") == "gpt-6.1-sol"


# ---------------------------------------------------------------------------
# API-key listings
# ---------------------------------------------------------------------------


async def test_openai_lists_chat_models_newest_first() -> None:
    rows = [
        {"id": "gpt-5.6", "created": 100},
        {"id": "text-embedding-3-large", "created": 300},
        {"id": "gpt-6-astra", "created": 200},
        {"id": "whisper-1", "created": 50},
        {"id": "tts-1-hd", "created": 60},
        {"id": "gpt-6-astra-realtime-preview", "created": 210},
        {"id": "dall-e-3", "created": 70},
        {"id": "omni-moderation-latest", "created": 80},
        {"id": "gpt-6-astra-transcribe", "created": 220},
    ]
    ids = await model_discovery.list_models(
        PRESETS["openai"], api_key="k", refresh=True,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": rows})),
    )
    assert ids == ["gpt-6-astra", "gpt-5.6"]


GC = {"supportedGenerationMethods": ["generateContent"]}


async def test_gemini_follows_pages_keeps_generate_content_and_orders_newest_first() -> None:
    pages = {
        "": {
            "models": [
                {"name": "models/gemini-2.5-flash", **GC},
                {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embed"]},
            ],
            "nextPageToken": "p2",
        },
        "p2": {
            "models": [
                {"name": "models/gemini-3.8-flash", **GC},
                {"name": "models/imagen-4", "supportedGenerationMethods": ["predict"]},
                {"name": "models/gemini-3.1-pro-preview", **GC},
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("pageToken", "")])

    ids = await model_discovery.list_models(
        PRESETS["gemini"], api_key="k", refresh=True, transport=httpx.MockTransport(handler)
    )
    assert ids == ["gemini-3.8-flash", "gemini-3.1-pro-preview", "gemini-2.5-flash"]


async def test_anthropic_follows_pages_and_adds_no_preset_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after_id") == "claude-opus-5-5":
            last = {"data": [{"id": "claude-haiku-4-5-20251001"}], "has_more": False}
            return httpx.Response(200, json=last)
        assert request.url.params["limit"] == "1000"
        return httpx.Response(
            200,
            json={
                "data": [{"id": "claude-sonnet-5-5"}, {"id": "claude-opus-5-5"}],
                "has_more": True,
                "last_id": "claude-opus-5-5",
            },
        )

    ids = await model_discovery.list_models(
        PRESETS["anthropic"], api_key="k", refresh=True, strict=True,
        transport=httpx.MockTransport(handler),
    )
    assert ids == ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5-20251001"]
