"""One model-resolution chain for every vendor and every auth method.

``providers/models.py:resolve_models`` answers four rungs deep — live listing,
``providers.<vendor>`` settings override, the last good cached list, then this
build's curated list merged with the public models.dev catalog — and reports
which rung answered.  These tests pin each rung, and the per-vendor endpoints
they are supposed to hit, with :class:`httpx.MockTransport`: no test in this
file touches the network.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import models as model_discovery
from snowpea_core.providers.codex_transport import CODEX_MODELS
from snowpea_core.providers.gemini_codeassist_transport import CODE_ASSIST_MODELS
from snowpea_core.providers.presets import PRESETS
from snowpea_core.providers.registry import ProviderRegistry


@pytest.fixture(autouse=True)
def _clear_model_cache() -> Iterator[None]:
    model_discovery.cache_clear()
    yield
    model_discovery.cache_clear()


def _jwt(account_id: str = "acct-1") -> str:
    """An access token carrying OpenAI's ``chatgpt_account_id`` claim."""
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _json(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


CODEX_PAYLOAD = {
    "models": [
        {"slug": "gpt-5.1-codex", "priority": 2},
        {"slug": "gpt-6-codex", "priority": 1},
        {"slug": "internal-only", "priority": 0, "visibility": "hidden"},
        {"slug": "gpt-5-preview", "priority": 3, "supported_in_api": False},
    ]
}


# ---------------------------------------------------------------------------
# rung 1 — live
# ---------------------------------------------------------------------------


async def test_chatgpt_account_lists_live_from_the_codex_backend(tmp_path: Path) -> None:
    """A ChatGPT subscription has a catalog after all — the Codex backend's.

    It is per-account, which is why the ``ChatGPT-Account-Id`` header matters:
    without it the backend answers ``200 {"models": []}`` and the picker
    silently degrades to the curated list.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(CODEX_PAYLOAD)

    listing = await model_discovery.resolve_models(
        "openai",
        auth_method="chatgpt",
        credentials={"auth_method": "chatgpt", "access_token": _jwt()},
        home=tmp_path,
        transport=_transport(handler),
    )

    assert listing.source == model_discovery.SOURCE_LIVE
    # Sorted by priority, hidden dropped, ``supported_in_api: false`` kept —
    # that flag describes the public API, not the Codex backend.
    assert listing.models == ["gpt-6-codex", "gpt-5.1-codex", "gpt-5-preview"]
    assert "chatgpt.com/backend-api/codex/models" in str(seen[0].url)
    assert seen[0].headers["ChatGPT-Account-Id"] == "acct-1"
    assert seen[0].headers["authorization"].startswith("Bearer ")
    assert listing.detail == "from your ChatGPT account"


async def test_an_expired_chatgpt_session_is_refreshed_before_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing must not be what makes a user log in again."""
    from snowpea_core.providers import openai_oauth

    saved: list[dict[str, Any]] = []

    async def refresh(credentials: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {**credentials, "access_token": _jwt("acct-2"), "expires_at": 1e12}

    monkeypatch.setattr(openai_oauth, "refresh_credentials", refresh)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(CODEX_PAYLOAD)

    listing = await model_discovery.resolve_models(
        "openai",
        auth_method="chatgpt",
        credentials={
            "auth_method": "chatgpt",
            "access_token": _jwt(),
            "refresh_token": "r",
            "expires_at": 0,
        },
        home=tmp_path,
        transport=_transport(handler),
        on_credentials=saved.append,
    )

    assert listing.source == model_discovery.SOURCE_LIVE
    assert seen[0].headers["ChatGPT-Account-Id"] == "acct-2"
    assert saved and saved[0]["access_token"] == _jwt("acct-2")


async def test_gemini_api_key_lists_live_and_strips_the_models_prefix(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1beta/models")
        return _json({"models": [{"name": "models/gemini-9-pro"}, {"name": "gemini-9-flash"}]})

    listing = await model_discovery.resolve_models(
        "gemini", api_key="k", home=tmp_path, transport=_transport(handler)
    )

    assert listing.source == model_discovery.SOURCE_LIVE
    assert listing.models == ["gemini-9-pro", "gemini-9-flash"]


async def test_anthropic_lists_live_from_v1_models(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["anthropic-version"]
        return _json({"data": [{"id": "claude-9-sonnet"}]})

    listing = await model_discovery.resolve_models(
        "anthropic", api_key="k", home=tmp_path, transport=_transport(handler)
    )

    assert listing.source == model_discovery.SOURCE_LIVE
    # The preset's own models stay behind the live ones rather than vanishing.
    assert listing.models[0] == "claude-9-sonnet"
    assert "claude-sonnet-5" in listing.models


async def test_a_local_server_without_v1_models_falls_back_to_ollama_tags(
    tmp_path: Path,
) -> None:
    """Ollama serves ``/api/tags``; only vLLM and LM Studio do ``/v1/models``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return _json({"models": [{"name": "qwen3:8b"}, {"name": "llama3.3:70b"}]})
        return httpx.Response(404, text="not found")

    listing = await model_discovery.resolve_models(
        "local",
        base_url="http://localhost:11434/v1",
        home=tmp_path,
        transport=_transport(handler),
    )

    assert listing.source == model_discovery.SOURCE_LIVE
    assert listing.models == ["qwen3:8b", "llama3.3:70b"]


@pytest.mark.parametrize(
    "vendor", ["openai", "openrouter", "xai", "glm", "minimax", "kimi", "deepseek", "qwen"]
)
async def test_every_openai_compatible_vendor_lists_from_its_own_base_url(
    vendor: str, tmp_path: Path
) -> None:
    """The chain must reach each preset's real endpoint, not a shared guess."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _json({"data": [{"id": f"{vendor}-live-1"}]})

    listing = await model_discovery.resolve_models(
        vendor, api_key="k", home=tmp_path, transport=_transport(handler)
    )

    assert listing.models == [f"{vendor}-live-1"]
    assert listing.source == model_discovery.SOURCE_LIVE
    assert seen == [f"{PRESETS[vendor].base_url.rstrip('/')}/models"]


# ---------------------------------------------------------------------------
# rung 2 — the settings override
# ---------------------------------------------------------------------------


async def test_oauth_models_override_beats_the_curated_codex_list(tmp_path: Path) -> None:
    """``providers.openai.oauth_models`` is how an account with early access to
    a model nothing advertises yet gets it into the picker."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="backend down")

    listing = await model_discovery.resolve_models(
        "openai",
        auth_method="chatgpt",
        credentials={
            "auth_method": "chatgpt",
            "access_token": _jwt(),
            "oauth_models": ["gpt-private-1", "gpt-private-2"],
        },
        home=tmp_path,
        transport=_transport(handler),
    )

    assert listing.source == model_discovery.SOURCE_SETTINGS
    assert listing.models == ["gpt-private-1", "gpt-private-2"]


async def test_models_override_applies_to_a_key_account(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    listing = await model_discovery.resolve_models(
        "deepseek",
        api_key="k",
        credentials={"api_key": "k", "models": ["deepseek-private"]},
        home=tmp_path,
        transport=_transport(handler),
    )

    assert listing.source == model_discovery.SOURCE_SETTINGS
    assert listing.models == ["deepseek-private"]


async def test_oauth_models_is_ignored_for_a_key_account(tmp_path: Path) -> None:
    """``oauth_models`` describes the OAuth backend's catalog only; an API key
    reaching the public API must not inherit it."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    listing = await model_discovery.resolve_models(
        "openai",
        api_key="sk-test",
        credentials={"api_key": "sk-test", "oauth_models": ["gpt-private-1"]},
        home=tmp_path,
        transport=_transport(handler),
    )

    assert listing.source == model_discovery.SOURCE_CURATED
    assert "gpt-private-1" not in listing.models


async def test_a_malformed_override_is_ignored_not_fatal(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    listing = await model_discovery.resolve_models(
        "deepseek",
        api_key="k",
        credentials={"api_key": "k", "models": "deepseek-chat"},
        home=tmp_path,
        transport=_transport(handler),
    )

    assert listing.source == model_discovery.SOURCE_CURATED


# ---------------------------------------------------------------------------
# rung 3 — the disk cache
# ---------------------------------------------------------------------------


async def test_the_last_good_listing_survives_an_outage(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return _json({"data": [{"id": "kimi-live-1"}]})
        return httpx.Response(502, text="bad gateway")

    first = await model_discovery.resolve_models(
        "kimi", api_key="k", home=tmp_path, transport=_transport(handler)
    )
    assert first.source == model_discovery.SOURCE_LIVE
    assert model_discovery.cache_path(tmp_path, "kimi", None).exists()

    model_discovery.cache_clear()  # the in-process one; the disk cache remains
    second = await model_discovery.resolve_models(
        "kimi", api_key="k", refresh=True, home=tmp_path, transport=_transport(handler)
    )

    assert second.source == model_discovery.SOURCE_CACHE
    assert second.models == ["kimi-live-1"]
    assert second.error


async def test_an_empty_listing_is_never_cached(tmp_path: Path) -> None:
    """Pinning an empty answer would turn one flaky minute into a dead picker."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json({"data": []})

    await model_discovery.resolve_models(
        "kimi", api_key="k", home=tmp_path, transport=_transport(handler)
    )

    assert not model_discovery.cache_path(tmp_path, "kimi", None).exists()


async def test_the_cache_is_keyed_by_auth_method(tmp_path: Path) -> None:
    """An API key and a ChatGPT session see genuinely different catalogs."""
    assert model_discovery.cache_path(tmp_path, "openai", "chatgpt") != model_discovery.cache_path(
        tmp_path, "openai", None
    )


# ---------------------------------------------------------------------------
# rung 4 — curated, merged with models.dev
# ---------------------------------------------------------------------------


async def test_a_chatgpt_account_falls_back_to_the_curated_codex_list(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="expired")

    listing = await model_discovery.resolve_models(
        "openai",
        auth_method="chatgpt",
        credentials={"auth_method": "chatgpt", "access_token": _jwt()},
        home=tmp_path,
        transport=_transport(handler),
    )

    assert listing.source == model_discovery.SOURCE_CURATED
    assert listing.models[: len(CODEX_MODELS)] == list(CODEX_MODELS)


async def test_google_oauth_uses_the_curated_code_assist_list_without_asking(
    tmp_path: Path,
) -> None:
    """Code Assist publishes no catalog — gemini-cli ships static constants —
    so nothing is requested and ``curated`` is the honest answer, not a
    degraded one."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "models.dev":
            return _json({})
        raise AssertionError(f"Code Assist has no listing endpoint: {request.url}")

    listing = await model_discovery.resolve_models(
        "gemini",
        auth_method="google_oauth",
        credentials={"auth_method": "google_oauth", "access_token": "ya29.x"},
        home=tmp_path,
        transport=_transport(handler),
    )

    assert listing.source == model_discovery.SOURCE_CURATED
    assert listing.models[: len(CODE_ASSIST_MODELS)] == list(CODE_ASSIST_MODELS)
    assert "publishes no model listing" in listing.detail
    assert listing.error is None


async def test_the_curated_list_is_extended_with_models_dev(tmp_path: Path) -> None:
    """A model released after this build still reaches the picker."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "models.dev":
            return _json(
                {
                    "google": {
                        "models": {
                            "gemini-99-pro": {"tool_call": True},
                            "gemini-embedding-9": {"tool_call": False},
                        }
                    }
                }
            )
        return httpx.Response(503, text="down")

    listing = await model_discovery.resolve_models(
        "gemini",
        auth_method="google_oauth",
        credentials={"auth_method": "google_oauth"},
        home=tmp_path,
        transport=_transport(handler),
    )

    assert listing.source == model_discovery.SOURCE_CURATED
    # Curated first — those are the ids this build is known to route — then the
    # public catalog's extras.  Models that cannot call tools are not offered.
    assert listing.models[0] == CODE_ASSIST_MODELS[0]
    assert "gemini-99-pro" in listing.models
    assert "gemini-embedding-9" not in listing.models


async def test_models_dev_is_cached_on_disk(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "models.dev":
            return _json({"deepseek": {"models": {"deepseek-99": {"tool_call": True}}}})
        return httpx.Response(503, text="down")

    for _ in range(2):
        listing = await model_discovery.resolve_models(
            "deepseek", api_key="k", home=tmp_path, transport=_transport(handler)
        )
        assert "deepseek-99" in listing.models

    assert [url for url in calls if "models.dev" in url] == ["https://models.dev/api.json"]
    assert (Paths(home=tmp_path).cache_dir / "models-dev.json").exists()


async def test_an_unreachable_models_dev_leaves_the_curated_list_alone(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    listing = await model_discovery.resolve_models(
        "deepseek", api_key="k", home=tmp_path, transport=_transport(handler)
    )

    assert listing.models == list(PRESETS["deepseek"].models)
    assert listing.source == model_discovery.SOURCE_CURATED


@pytest.mark.parametrize("vendor", list(PRESETS))
async def test_every_vendor_resolves_to_something_pickable(vendor: str, tmp_path: Path) -> None:
    """No vendor may leave the picker empty when everything else fails."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    listing = await model_discovery.resolve_models(
        vendor, api_key="k", home=tmp_path, transport=_transport(handler)
    )

    assert listing.source in {
        model_discovery.SOURCE_CACHE,
        model_discovery.SOURCE_CURATED,
    }
    # ``local`` is the one vendor with nothing to curate: what a self-hosted
    # server serves is whatever was loaded into it.
    assert listing.models or vendor == "local"
    assert listing.detail


# ---------------------------------------------------------------------------
# the surfaces
# ---------------------------------------------------------------------------


async def test_registry_reports_the_source(tmp_path: Path) -> None:
    settings = Settings()
    settings.providers["deepseek"] = {"api_key": "k", "models": ["deepseek-pinned"]}
    registry = ProviderRegistry(settings, Paths(home=tmp_path))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    listing = await registry.model_listing("deepseek", transport=_transport(handler))

    assert listing.models == ["deepseek-pinned"]
    assert listing.source == model_discovery.SOURCE_SETTINGS
    assert await registry.list_models("deepseek") == ["deepseek-pinned"]


def test_provider_models_result_carries_the_source() -> None:
    from snowpea_core.server.protocol import ProviderModelsResult

    fields = ProviderModelsResult.model_fields
    assert "source" in fields and "detail" in fields


# ---------------------------------------------------------------------------
# what the live list is allowed to contain
# ---------------------------------------------------------------------------


def test_an_unknown_codex_id_is_routed_as_chosen() -> None:
    """The live catalog is where new models appear first.

    A subscriber whose account offers ``gpt-5.6-sol`` picked it, and routing
    replaced it with the backend default because this build's curated list had
    never heard of it — the picker and the router disagreeing about the same
    account.  Only ids that belong to the *API-key* endpoint may be dropped.
    """
    assert model_discovery.rejected_for_oauth("openai", "chatgpt", "gpt-6-astra") is True
    assert model_discovery.rejected_for_oauth("openai", "chatgpt", "gpt-5.6-sol") is False
    assert model_discovery.rejected_for_oauth("openai", "chatgpt", CODEX_MODELS[0]) is False
    assert model_discovery.rejected_for_oauth("openai", "chatgpt", "") is True
    assert model_discovery.rejected_for_oauth("gemini", "google_oauth", "gemini-9-ultra") is False


async def test_a_codex_session_keeps_a_live_model_id(tmp_path: Path) -> None:
    """End to end: the provider the registry builds carries the chosen id."""
    settings = Settings()
    settings.providers["openai"] = {
        "auth_method": "chatgpt",
        "access_token": _jwt(),
        "model": "gpt-5.6-sol",
    }
    registry = ProviderRegistry(settings, Paths(home=tmp_path))

    provider = registry.get("openai", "gpt-5.6-sol")

    assert getattr(provider, "model", None) == "gpt-5.6-sol"


async def test_provider_list_serves_the_catalog_discovered_live(tmp_path: Path) -> None:
    """``provider.list`` is synchronous, so it reads the cache rather than the
    network — but it must not contradict what the picker just showed."""
    settings = Settings()
    settings.providers["openai"] = {"auth_method": "chatgpt", "access_token": _jwt()}
    registry = ProviderRegistry(settings, Paths(home=tmp_path))

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json(CODEX_PAYLOAD)

    listing = await registry.model_listing("openai", transport=_transport(handler))
    assert listing.source == model_discovery.SOURCE_LIVE

    info = {item.vendor: item for item in registry.list()}
    assert info["openai"].models == listing.models


def test_offline_models_prefers_the_override_then_the_cache(tmp_path: Path) -> None:
    path = model_discovery.cache_path(tmp_path, "openai", "chatgpt")
    model_discovery.write_cache_file(path, ["cached-1"])

    assert model_discovery.offline_models(
        "openai", "chatgpt", {"oauth_models": ["pinned-1"]}, home=tmp_path
    ) == ["pinned-1"]
    assert model_discovery.offline_models("openai", "chatgpt", {}, home=tmp_path) == ["cached-1"]
    assert model_discovery.offline_models("openai", "chatgpt", {}) == list(CODEX_MODELS)


async def test_the_wizard_picker_shows_the_live_chatgpt_catalog(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The wizard is the surface the user complained about: after a ChatGPT
    login it must offer the account's own models and say where they came
    from."""
    from snowpea_core.setup import ui, wizard
    from snowpea_core.setup.state import WizardState

    async def catalog(_credentials: dict[str, Any], **_kwargs: Any) -> list[str]:
        return ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5"]

    monkeypatch.setattr(model_discovery, "codex_catalog", catalog)
    monkeypatch.setattr(ui, "ask_text", lambda *_a, **_k: "")
    state = WizardState.from_settings(Settings())
    state.select_vendor("openai")
    wizard._apply_login(  # noqa: SLF001
        state, {"auth_method": "chatgpt", "access_token": _jwt(), "refresh_token": "r"}
    )

    wizard._ask_for_model(state, interactive=True, home=tmp_path)  # noqa: SLF001

    out = capsys.readouterr().out
    assert "listing models from your ChatGPT account…" in out
    assert "from your ChatGPT account (3 models)" in out
    assert "gpt-5.6-sol" in out and "gpt-5.6-luna" in out
    # Enter takes the first row, and it is the live one — not a curated id.
    assert state.model == "gpt-5.6-sol"
    assert "could not list models" not in out
