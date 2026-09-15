"""The default model a vendor row offers, and which rung produced it.

``providers/default_models.py`` answers three rungs deep — the account's own
listing, the public models.dev catalog, then the build's preset string — and
says which one replied.  These tests pin the precedence, the timeout that keeps
a slow vendor from holding up the setup screen, the fields ``setup.catalog``
carries, and the preset table itself.  Nothing here touches the network:
models.dev is served by :class:`httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.providers import default_models
from snowpea_core.providers import models as model_discovery
from snowpea_core.providers.presets import PRESETS
from snowpea_core.setup.catalog import vendor_catalog


@pytest.fixture(autouse=True)
def _clear_model_cache() -> Iterator[None]:
    model_discovery.cache_clear()
    yield
    model_discovery.cache_clear()


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


#: A models.dev document with one lab in it, shaped like the real one.
CATALOG: dict[str, Any] = {
    "anthropic": {
        "models": {
            "claude-old-4": {
                "tool_call": True,
                "release_date": "2025-01-01",
                "modalities": {"input": ["text"], "output": ["text"]},
            },
            "claude-new-9": {
                "tool_call": True,
                "release_date": "2026-09-01",
                "modalities": {"input": ["text"], "output": ["text"]},
            },
            "claude-new-9-haiku": {
                "tool_call": True,
                "release_date": "2026-09-20",
                "modalities": {"input": ["text"], "output": ["text"]},
            },
            "claude-new-10-preview": {
                "tool_call": True,
                "release_date": "2026-10-01",
                "modalities": {"input": ["text"], "output": ["text"]},
            },
            "claude-embed-2": {"tool_call": False, "release_date": "2026-09-30"},
            "claude-voice-1": {
                "tool_call": True,
                "release_date": "2026-09-29",
                "modalities": {"input": ["text"], "output": ["audio"]},
            },
        }
    }
}


def _models_dev(handler: Any = None) -> httpx.MockTransport:
    """A transport that serves :data:`CATALOG` for models.dev and 503 elsewhere."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "models.dev":
            return httpx.Response(200, json=CATALOG)
        if handler is not None:
            return handler(request)
        return httpx.Response(503, text="down")

    return _transport(route)


# ---------------------------------------------------------------------------
# the pure pick
# ---------------------------------------------------------------------------


def test_the_newest_chat_model_wins() -> None:
    """Newest release date, but only among the ids an agent turn could use."""
    assert default_models.newest_catalog_model("anthropic", CATALOG) == "claude-new-9"


def test_a_preview_loses_to_a_stable_sibling() -> None:
    """``claude-new-10-preview`` is newer and is still not what a wizard offers."""
    picked = default_models.newest_catalog_model("anthropic", CATALOG)
    assert picked is not None
    assert "preview" not in picked


def test_a_preview_wins_when_it_is_all_there_is() -> None:
    catalog = {"anthropic": {"models": {"claude-x-preview": {"tool_call": True}}}}
    assert default_models.newest_catalog_model("anthropic", catalog) == "claude-x-preview"


def test_a_flagship_beats_a_newer_sibling_of_the_same_generation() -> None:
    """``-haiku`` shipped 19 days later; the family's flagship is still the default."""
    assert default_models.is_flagship("claude-new-9-haiku") is False
    assert default_models.is_flagship("claude-new-9") is True
    assert default_models.newest_catalog_model("anthropic", CATALOG) == "claude-new-9"


def test_a_cheaper_tier_wins_once_the_flagship_is_a_generation_behind() -> None:
    """Past the window there is no "same generation" left to prefer."""
    catalog = {
        "anthropic": {
            "models": {
                "claude-old": {"tool_call": True, "release_date": "2025-01-01"},
                "claude-new-flash": {"tool_call": True, "release_date": "2026-09-01"},
            }
        }
    }
    assert default_models.newest_catalog_model("anthropic", catalog) == "claude-new-flash"


def test_an_unknown_vendor_has_no_catalog_answer() -> None:
    assert default_models.newest_catalog_model("local", CATALOG) is None
    assert default_models.newest_catalog_model("gemini", CATALOG) is None


def test_openrouter_stays_inside_the_namespace_its_preset_points_at() -> None:
    """OpenRouter resells every lab; "whoever shipped last" is not its default."""
    catalog = {
        "openrouter": {
            "models": {
                "sakana/fugu-max": {"tool_call": True, "release_date": "2026-09-30"},
                "anthropic/claude-new-9": {"tool_call": True, "release_date": "2026-09-01"},
            }
        }
    }
    assert default_models.newest_catalog_model("openrouter", catalog) == "anthropic/claude-new-9"


# ---------------------------------------------------------------------------
# precedence: live > models.dev > preset
# ---------------------------------------------------------------------------


async def test_a_live_listing_beats_the_catalog(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.anthropic.com":
            return httpx.Response(200, json={"data": [{"id": "claude-account-only-9"}]})
        return httpx.Response(503, text="down")

    answer = await default_models.default_model_for(
        "anthropic",
        configured=True,
        api_key="sk-test",
        home=tmp_path,
        transport=_models_dev(handler),
    )

    assert answer.model == "claude-account-only-9"
    assert answer.source == default_models.DEFAULT_SOURCE_LIVE
    assert answer.label == "default: claude-account-only-9 (from your account)"


async def test_models_dev_answers_when_there_is_no_credential(tmp_path: Path) -> None:
    """An unconfigured vendor is never asked — it would be a guaranteed 401."""
    answer = await default_models.default_model_for(
        "anthropic", home=tmp_path, transport=_models_dev()
    )

    assert answer.model == "claude-new-9"
    assert answer.source == default_models.DEFAULT_SOURCE_MODELS_DEV
    assert answer.detail == "from models.dev"


async def test_models_dev_answers_when_the_account_does_not(tmp_path: Path) -> None:
    answer = await default_models.default_model_for(
        "anthropic",
        configured=True,
        api_key="sk-test",
        home=tmp_path,
        transport=_models_dev(),
    )

    assert answer.model == "claude-new-9"
    assert answer.source == default_models.DEFAULT_SOURCE_MODELS_DEV


async def test_the_preset_is_the_last_word(tmp_path: Path) -> None:
    """Nothing answers: the build's own string, reported as the build's own string."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    answer = await default_models.default_model_for(
        "anthropic", home=tmp_path, transport=_transport(handler)
    )

    assert answer.model == PRESETS["anthropic"].default_model
    assert answer.source == default_models.DEFAULT_SOURCE_PRESET
    assert answer.detail == "built-in default"


async def test_a_slow_vendor_falls_through_to_the_catalog(tmp_path: Path) -> None:
    """The wizard's first screen must not wait on a vendor that hangs."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "models.dev":
            return httpx.Response(200, json=CATALOG)
        await asyncio.sleep(5)
        return httpx.Response(200, json={"data": [{"id": "never-arrives"}]})

    answer = await default_models.default_model_for(
        "anthropic",
        configured=True,
        api_key="sk-test",
        home=tmp_path,
        timeout=0.05,
        transport=_transport(handler),
    )

    assert answer.model == "claude-new-9"
    assert answer.source == default_models.DEFAULT_SOURCE_MODELS_DEV


async def test_a_live_listing_is_ranked_by_the_catalog(tmp_path: Path) -> None:
    """``/v1/models`` answers in no useful order, so models.dev orders it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.anthropic.com":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "claude-old-4"},
                        {"id": "claude-new-9"},
                        {"id": "claude-new-10-preview"},
                    ]
                },
            )
        return httpx.Response(503, text="down")

    # Prime the on-disk catalog the ranking reads, exactly as a first run does.
    await default_models.models_dev_document(home=tmp_path, transport=_models_dev())
    answer = await default_models.default_model_for(
        "anthropic",
        configured=True,
        api_key="sk-test",
        home=tmp_path,
        transport=_transport(handler),
    )

    assert answer.source == default_models.DEFAULT_SOURCE_LIVE
    assert answer.model == "claude-new-9"


async def test_the_offline_rung_reads_the_cache_and_never_the_network(tmp_path: Path) -> None:
    cache = Paths(home=tmp_path).cache_dir / "models-dev.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"saved_at": 0, "catalog": CATALOG}), encoding="utf-8")

    answer = default_models.default_model_offline("anthropic", home=tmp_path)

    assert answer.model == "claude-new-9"
    assert answer.source == default_models.DEFAULT_SOURCE_MODELS_DEV


# ---------------------------------------------------------------------------
# the catalog rows
# ---------------------------------------------------------------------------


def test_the_vendor_rows_carry_the_default_and_its_source() -> None:
    picked = {
        "anthropic": default_models.DefaultModel(
            vendor="anthropic",
            model="claude-account-only-9",
            source=default_models.DEFAULT_SOURCE_LIVE,
        )
    }

    rows = {item.id: item for item in vendor_catalog(defaults=picked)}

    assert rows["anthropic"].default_model == "claude-account-only-9"
    assert rows["anthropic"].default_model_source == "from your account"
    assert "default: claude-account-only-9 (from your account)" in rows["anthropic"].tags
    assert rows["anthropic"].description.startswith("claude-account-only-9 (from your account)")
    # A vendor nothing was resolved for still shows a default, never a blank.
    assert rows["glm"].default_model == PRESETS["glm"].default_model
    # A self-hosted server serves whatever was loaded into it; no catalog knows.
    assert rows["local"].default_model == ""
    assert rows["local"].description == PRESETS["local"].base_url


def test_every_vendor_row_shows_some_default() -> None:
    for item in vendor_catalog():
        if item.id == "local":
            continue
        assert item.default_model, f"{item.id} has no default model"
        assert item.default_model_source, f"{item.id} does not say where its default came from"


# ---------------------------------------------------------------------------
# the preset table
# ---------------------------------------------------------------------------

#: The defaults this work replaced.  They are what a user saw in ``snowpea
#: setup`` long after every one of them had been superseded, so the table is
#: pinned against their return.
STALE_DEFAULTS = frozenset(
    {
        "claude-sonnet-4-5",
        "gpt-4.1",
        "gemini-2.5-pro",
        "grok-4",
        "glm-4.6",
        "minimax-m2",
        "kimi-k2",
        "deepseek-chat",
        "qwen3-max",
    }
)


@pytest.mark.parametrize("vendor", list(PRESETS))
def test_every_preset_default_is_the_first_of_its_own_model_list(vendor: str) -> None:
    preset = PRESETS[vendor]
    assert preset.models, f"{vendor} lists no models"
    assert preset.default_model == preset.models[0]


@pytest.mark.parametrize("vendor", list(PRESETS))
def test_no_preset_carries_a_superseded_default(vendor: str) -> None:
    assert PRESETS[vendor].default_model not in STALE_DEFAULTS


@pytest.mark.parametrize("vendor", list(PRESETS))
def test_every_preset_default_has_a_known_context_window(vendor: str) -> None:
    """A default whose window is unknown shows the HUD a ``?`` from the first turn."""
    if vendor == "local":  # the placeholder is replaced by discovery
        return
    assert PRESETS[vendor].context_window() is not None
