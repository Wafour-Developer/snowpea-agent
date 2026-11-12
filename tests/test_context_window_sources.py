"""A model's context window is never unknown, and says where it came from (1.42.0)."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent import loop
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import context_windows
from snowpea_core.providers.base import ChatMessage
from snowpea_core.providers.registry import ProviderRegistry

LOCAL = {"providers": {"local": {"base_url": "http://gw.example/v1", "model": "k-exaone_v2.5"}}}


@pytest.fixture
def registry(tmp_path: Path) -> ProviderRegistry:
    context_windows.cache_clear()
    paths = Paths.create(tmp_path / "home")
    reg = ProviderRegistry(Settings.model_validate(copy.deepcopy(LOCAL)), paths)
    yield reg
    context_windows.cache_clear()
    context_windows.bind_store(None)


def test_nothing_names_a_window_so_it_is_assumed(registry: ProviderRegistry) -> None:
    info = registry.window_info("local", "k-exaone_v2.5")
    assert (info.window, info.source) == (131072, "assumed")


def _pair(registry: ProviderRegistry, model: str) -> tuple[int, str]:
    info = registry.window_info("local", model)
    return info.window, info.source


def test_the_sources_are_tried_in_order(registry: ProviderRegistry) -> None:
    base, model = "http://gw.example/v1", "k-exaone_v2.5"
    # server-reported
    context_windows.remember_discovered("local", base, model, 262144)
    assert _pair(registry, model) == (262144, "server")
    # a refusal that named a window beats the server's own list
    registry.learn_context_window("local", model, 100_000)
    assert _pair(registry, model) == (100_000, "error")
    # the vendor-wide setting beats both, the per-model one beats that
    registry.settings.providers["local"]["context_window"] = 65_536
    assert _pair(registry, model) == (65_536, "settings")
    registry.settings.providers["local"]["context_windows"] = {model: 32_768}
    assert _pair(registry, model) == (32_768, "settings")
    # another model of the same vendor keeps the vendor-wide value
    assert _pair(registry, "other-model") == (65_536, "settings")


def test_a_table_entry_is_a_catalog_window(registry: ProviderRegistry) -> None:
    reg = ProviderRegistry(Settings.model_validate({"providers": {"anthropic": {}}}))
    info = reg.window_info("anthropic", "claude-sonnet-4-5")
    assert info.source == "catalog" and info.window >= 200_000


def test_an_accepted_prompt_raises_the_window(registry: ProviderRegistry) -> None:
    registry.note_prompt_accepted("local", "k-exaone_v2.5", 150_000)
    info = registry.window_info("local", "k-exaone_v2.5")
    assert (info.window, info.source, info.lower) == (150_000, "learned", 150_000)


def test_a_refused_prompt_lowers_it_below_the_refusal(registry: ProviderRegistry) -> None:
    registry.note_prompt_refused("local", "k-exaone_v2.5", 100_000)
    info = registry.window_info("local", "k-exaone_v2.5")
    assert info.source == "learned" and info.upper == 100_000
    assert info.window < 100_000
    # the smallest refusal wins, a bigger one later changes nothing
    registry.note_prompt_refused("local", "k-exaone_v2.5", 120_000)
    assert registry.window_info("local", "k-exaone_v2.5").upper == 100_000


def test_a_setting_is_not_moved_by_what_requests_taught(registry: ProviderRegistry) -> None:
    registry.settings.providers["local"]["context_window"] = 500_000
    registry.note_prompt_refused("local", "k-exaone_v2.5", 100_000)
    info = registry.window_info("local", "k-exaone_v2.5")
    assert (info.window, info.source, info.upper) == (500_000, "settings", 100_000)


def test_what_was_learned_survives_a_restart(tmp_path: Path) -> None:
    context_windows.cache_clear()
    paths = Paths.create(tmp_path / "home")
    first = ProviderRegistry(Settings.model_validate(copy.deepcopy(LOCAL)), paths)
    first.learn_context_window("local", "k-exaone_v2.5", 90_000)
    first.note_prompt_accepted("local", "k-exaone_v2.5", 40_000)
    first.note_prompt_refused("local", "k-exaone_v2.5", 95_000)
    # a fresh process: nothing in memory, only the file
    context_windows._ERROR_WINDOWS.clear()
    context_windows._BOUNDS.clear()
    context_windows._STORE["loaded"] = False
    second = ProviderRegistry(Settings.model_validate(copy.deepcopy(LOCAL)), paths)
    info = second.window_info("local", "k-exaone_v2.5")
    assert (info.window, info.source) == (90_000, "error")
    assert (info.lower, info.upper) == (40_000, 95_000)
    context_windows.bind_store(None)


def test_per_model_windows_merge_and_null_drops_one(registry: ProviderRegistry) -> None:
    registry.configure("local", {"context_windows": {"a": 65_536, "b": 32_768}})
    registry.configure("local", {"context_windows": {"b": None, "c": 16_384}})
    assert registry.vendor_config("local")["context_windows"] == {"a": 65_536, "c": 16_384}
    registry.configure("local", {"context_windows": {"a": None, "c": None}})
    assert "context_windows" not in registry.vendor_config("local")


def test_provider_models_reports_one_record_per_model(registry: ProviderRegistry) -> None:
    from snowpea_core.server.protocol import ProviderModelsResult
    from snowpea_core.server.session_handlers import _window_fields

    registry.note_prompt_accepted("local", "k-exaone_v2.5", 150_000)
    core = SimpleNamespace(providers=registry)
    fields = _window_fields(core, "local", ["k-exaone_v2.5", "glm-5.3-flash"])  # type: ignore[arg-type]
    models = ["k-exaone_v2.5", "glm-5.3-flash"]
    result = ProviderModelsResult(vendor="local", models=models, **fields)
    record = result.contextWindows["k-exaone_v2.5"]
    assert record.contextWindow == 150_000 and record.contextWindowSource == "learned"
    assert record.contextWindowBounds is not None and record.contextWindowBounds.lower == 150_000
    assert result.contextWindows["glm-5.3-flash"].contextWindowBounds is None


# ---- the bare-400 length test ----------------------------------------------


def _history(old_chars: int) -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="start " + "x" * old_chars),
        ChatMessage(role="tool", content="r" * old_chars, tool_call_id="c1"),
        ChatMessage(role="assistant", content="a" * old_chars),
        ChatMessage(role="user", content="u" * 10),
        ChatMessage(role="assistant", content="tail one"),
        ChatMessage(role="user", content="tail two"),
        ChatMessage(role="assistant", content="tail three"),
    ]


def test_the_trial_copy_is_about_half_and_the_original_is_untouched() -> None:
    original = _history(40_000)
    sizes = [len(m.content) for m in original]
    trial = loop.shrunk_for_trial(original)
    assert [len(m.content) for m in original] == sizes
    assert sum(len(m.content) for m in trial) < sum(sizes) * 0.6
    assert [m.content for m in trial[-loop.TRIAL_KEEP_TAIL :]] == [
        m.content for m in original[-loop.TRIAL_KEEP_TAIL :]
    ]


def test_a_bare_400_is_recognised_but_not_a_decode_error() -> None:
    from snowpea_core.providers.base import ProviderError

    bare = ProviderError("invalid_request", '{"error":{"message":"invalid request to model"}}')
    assert loop.is_opaque_rejection(bare)
    assert not loop.is_opaque_rejection(ProviderError("invalid_request", "bad temperature"))


class _Provider:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls = 0

    async def stream(self, messages: Any, specs: Any, **kwargs: Any):  # noqa: ANN201
        self.calls += 1
        assert kwargs.get("max_tokens") == 1
        if not self.ok:
            raise RuntimeError("still 400")
        return
        yield  # pragma: no cover - makes this an async generator


def _core_and_session(registry: ProviderRegistry) -> tuple[Any, Any]:
    core = SimpleNamespace(providers=registry)
    session = SimpleNamespace(
        id="s-test",
        provider="local",
        model="k-exaone_v2.5",
        context_ratio=1.0,
        interrupt=SimpleNamespace(is_set=lambda: False),
    )
    return core, session


@pytest.mark.asyncio
async def test_a_trial_that_goes_through_means_length(
    registry: ProviderRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("snowpea_core.providers.openai_compat.OPAQUE_REPLAY_PAUSE_SEC", 0)
    core, session = _core_and_session(registry)
    provider = _Provider(ok=True)
    history = _history(200_000)
    assert await loop._opaque_was_length(core, session, provider, history, [], RuntimeError())
    assert provider.calls == 1
    assert registry.window_info("local", "k-exaone_v2.5").upper is not None


@pytest.mark.asyncio
async def test_a_trial_that_fails_too_means_not_length(
    registry: ProviderRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("snowpea_core.providers.openai_compat.OPAQUE_REPLAY_PAUSE_SEC", 0)
    core, session = _core_and_session(registry)
    provider = _Provider(ok=False)
    assert not await loop._opaque_was_length(
        core, session, provider, _history(200_000), [], RuntimeError()
    )
    assert registry.window_info("local", "k-exaone_v2.5").upper is None


@pytest.mark.asyncio
async def test_a_small_request_is_not_tested(registry: ProviderRegistry) -> None:
    core, session = _core_and_session(registry)
    provider = _Provider(ok=True)
    history = _history(100)
    assert not await loop._opaque_was_length(core, session, provider, history, [], RuntimeError())
    assert provider.calls == 0


def test_a_settings_window_says_which_setting_won(registry: ProviderRegistry) -> None:
    registry.settings.providers["local"]["context_window"] = 65_536
    assert registry.window_info("local", "any").scope == "vendor"
    registry.settings.providers["local"]["context_windows"] = {"any": 32_768}
    assert registry.window_info("local", "any").scope == "model"
    assert registry.window_info("local", "other").scope == "vendor"
    registry.settings.providers["local"].pop("context_window")
    registry.settings.providers["local"].pop("context_windows")
    assert registry.window_info("local", "any").scope is None


def test_unlisted_models_get_a_record_too(registry: ProviderRegistry) -> None:
    from snowpea_core.server.session_handlers import _window_fields, _window_models

    registry.settings.models.default = "local:my-finetune"
    registry.settings.models.categories["fast"] = "glm:glm-5.3-flash"  # another vendor
    registry.settings.models.categories["deep"] = "local:big-one"
    core = SimpleNamespace(providers=registry, settings=registry.settings)
    models = _window_models(core, "local", ["listed"], ["typed-by-hand", "listed"])  # type: ignore[arg-type]
    assert models == [
        "listed",
        "k-exaone_v2.5",  # the vendor's configured model
        "my-finetune",
        "big-one",
        "typed-by-hand",
    ]
    records = _window_fields(core, "local", models)["contextWindows"]  # type: ignore[arg-type]
    assert records["my-finetune"]["contextWindowSource"] == "assumed"
    assert "glm-5.3-flash" not in records


@pytest.mark.asyncio
async def test_pinning_a_window_does_not_make_the_vendor_the_default(tmp_path: Path) -> None:
    from snowpea_core.server.app_server import _persist_provider

    context_windows.cache_clear()
    paths = Paths.create(tmp_path / "home")
    settings = Settings.model_validate(copy.deepcopy(LOCAL))

    async def noop(_scopes: list[str]) -> None:
        return None

    core = SimpleNamespace(
        settings=settings,
        paths=paths,
        providers=ProviderRegistry(settings, paths),
        mark_settings_saved=lambda: None,
        notify_settings_changed=noop,
    )
    await _persist_provider(core, "local", {"context_windows": {"m": 65_536}})  # type: ignore[arg-type]
    assert "default" not in settings.providers
    await _persist_provider(core, "local", {"model": "m"})  # type: ignore[arg-type]
    assert settings.providers["default"] == "local"
    context_windows.bind_store(None)
