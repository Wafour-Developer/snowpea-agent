"""Codex transport and tool gates agree on vision (m32 feedback).

A ChatGPT-signed-in gpt-6.1 session captured screenshots through
``view_image`` (the gate asks ``ProviderRegistry.vision_for``) and then the
Codex transport replaced them with the no-vision note, because it asked only
the model-name hints, which stopped at ``gpt-5``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import codex_transport, openai_oauth
from snowpea_core.providers import content as content_parts
from snowpea_core.providers import models as model_discovery
from snowpea_core.providers import vision as vision_scale
from snowpea_core.providers.anthropic_native import messages_to_anthropic
from snowpea_core.providers.base import ChatMessage
from snowpea_core.providers.normalize import messages_to_gemini
from snowpea_core.providers.openai_compat import OpenAICompatProvider
from snowpea_core.providers.registry import ProviderRegistry

PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The Codex live-list info and the learned vision memory are process-global."""
    monkeypatch.setattr(model_discovery, "_CODEX_INFO", {})
    monkeypatch.setattr(model_discovery, "_LISTED_VISION", {})
    vision_scale.MEMORY.clear()
    vision_scale.MEMORY.home = None
    yield
    vision_scale.MEMORY.clear()
    vision_scale.MEMORY.home = None


def _token(account: str = "acct_123") -> str:
    def segment(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    claims = {openai_oauth.AUTH_CLAIM: {"chatgpt_account_id": account}}
    return f"{segment({'alg': 'none'})}.{segment(claims)}.sig"


def _messages(tmp_path: Path) -> list[ChatMessage]:
    image = tmp_path / "shot.png"
    image.write_bytes(PIXEL)
    return [
        ChatMessage(role="system", content="You are Snowpea."),
        ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "what is in this?"},
                {
                    "type": "image",
                    "name": image.name,
                    "mime": "image/png",
                    "path": str(image),
                    "text": f"[image: {image.name}]",
                },
            ],
        ),
    ]


def _chatgpt_registry(tmp_path: Path, model: str, **extra: Any) -> ProviderRegistry:
    settings = Settings()
    settings.providers["openai"] = {
        "auth_method": "chatgpt",
        "access_token": _token(),
        "refresh_token": "rt",
        "model": model,
        **extra,
    }
    return ProviderRegistry(settings, Paths.create(tmp_path / "home"))


def _codex_sends_images(provider: Any, messages: list[ChatMessage]) -> bool:
    body = provider.build_request(messages, [])
    for item in body["input"]:
        content = item.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "input_image" for part in content
        ):
            return True
    return False


def _codex_text(provider: Any, messages: list[ChatMessage]) -> str:
    return json.dumps(provider.build_request(messages, [])["input"])


def test_gpt6_is_named_as_a_vision_model() -> None:
    assert content_parts.supports_vision("openai", "gpt-6.1-sol")


def test_a_gpt6_codex_session_keeps_its_image_parts(tmp_path: Path) -> None:
    registry = _chatgpt_registry(tmp_path, "gpt-6.1-sol")
    provider = registry.build("openai")
    assert isinstance(provider, codex_transport.CodexProvider)
    assert registry.vision_for("openai", "gpt-6.1-sol") is True
    messages = _messages(tmp_path)
    assert _codex_sends_images(provider, messages)
    assert content_parts.NO_VISION_NOTE not in _codex_text(provider, messages)


def test_a_model_vision_for_refuses_still_gets_the_note(tmp_path: Path) -> None:
    registry = _chatgpt_registry(tmp_path, "gpt-6.1-sol", vision=False)
    provider = registry.build("openai")
    assert registry.vision_for("openai", "gpt-6.1-sol") is False
    messages = _messages(tmp_path)
    assert not _codex_sends_images(provider, messages)
    assert "this model cannot see images" in _codex_text(provider, messages)


def test_the_codex_list_input_modalities_win_over_the_name(tmp_path: Path) -> None:
    model_discovery._remember_codex_info(
        [
            {"slug": "gpt-6.1-sol", "input_modalities": ["text"]},
            {"slug": "mystery-model", "input_modalities": ["text", "image"]},
            {"slug": "gpt-5.2"},
        ]
    )
    assert model_discovery.codex_model_info("gpt-6.1-sol")["vision"] is False
    assert model_discovery.codex_model_info("mystery-model")["vision"] is True
    # An entry without modalities says nothing; the name decides.
    assert model_discovery.codex_model_info("gpt-5.2")["vision"] is None

    messages = _messages(tmp_path)
    text_only = _chatgpt_registry(tmp_path, "gpt-6.1-sol")
    assert text_only.vision_for("openai", "gpt-6.1-sol") is False
    assert not _codex_sends_images(text_only.build("openai"), messages)

    sees = _chatgpt_registry(tmp_path, "mystery-model")
    assert sees.vision_for("openai", "mystery-model") is True
    assert _codex_sends_images(sees.build("openai"), messages)

    # A bare transport (no registry answer) reads the same list.
    bare = codex_transport.CodexProvider(
        {"auth_method": "chatgpt", "access_token": _token()}, model="mystery-model"
    )
    assert _codex_sends_images(bare, messages)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"input_modalities": ["text", "image"]}, True),  # Codex model list
        ({"input_modalities": ["text"]}, False),
        ({"architecture": {"input_modalities": ["text", "image", "file"]}}, True),  # OpenRouter
        ({"architecture": {"input_modalities": ["text"]}}, False),
        ({"modalities": {"input": ["text", "image"]}}, True),  # models.dev shape
        ({"modalities": {"vision": False, "audio": False}}, False),  # llama.cpp /props
        ({"capabilities": ["completion", "vision"]}, True),  # Ollama /api/show
        ({"capabilities": ["completion", "tools"]}, False),
        ({"capabilities": {"image_input": {"supported": True}}}, True),
        ({"type": "vlm"}, True),  # LM Studio /api/v0/models
        ({"type": "llm"}, False),
        ({"supports_image_in": True}, True),  # Moonshot /v1/models
        ({"id": "gpt-4o", "object": "model", "owned_by": "openai"}, None),
        ({"type": "model", "id": "claude-x"}, None),
    ],
)
def test_listing_rows_are_read_for_image_input(row: dict[str, Any], expected: bool | None) -> None:
    assert model_discovery.vision_from_listing(row) is expected


def test_the_listing_beats_settings_catalog_and_name(tmp_path: Path) -> None:
    """Order: listing, then settings, then models.dev, then the name."""
    settings = Settings()
    settings.providers["openrouter"] = {"api_key": "sk-test", "vision": True}
    registry = ProviderRegistry(settings, Paths.create(tmp_path / "home"))
    base = model_discovery.listing_base(model_discovery.PRESETS_BY_VENDOR["openrouter"], None)
    model_discovery._remember_listed_vision(
        base,
        [{"id": "openai/gpt-6.1", "architecture": {"input_modalities": ["text"]}}],
    )
    # The name matches a hint and settings say yes; the listing says text only.
    assert registry.vision_for("openrouter", "openai/gpt-6.1") is False
    # A model the listing did not describe falls to settings.
    assert registry.vision_for("openrouter", "other/model") is True


async def test_a_live_listing_is_cached_with_its_vision(tmp_path: Path) -> None:
    import httpx

    payload = {
        "data": [
            {"id": "vendor/sees", "architecture": {"input_modalities": ["text", "image"]}},
            {"id": "vendor/blind", "architecture": {"input_modalities": ["text"]}},
        ]
    }
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    home = tmp_path / "home"
    listing = await model_discovery.resolve_models(
        "openrouter", api_key="sk-test", home=home, transport=transport, refresh=True
    )
    assert listing.source == model_discovery.SOURCE_LIVE
    # A fresh process: nothing in memory, the disk cache still answers.
    model_discovery._LISTED_VISION.clear()
    settings = Settings()
    settings.providers["openrouter"] = {"api_key": "sk-test"}
    registry = ProviderRegistry(settings, Paths.create(home))
    assert registry.vision_for("openrouter", "vendor/sees") is True
    assert registry.vision_for("openrouter", "vendor/blind") is False


async def test_window_discovery_records_ollama_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.providers import context_windows

    async def get_json(url: str, *_a: Any) -> dict[str, Any] | None:
        if url.endswith("/api/ps"):
            return {"models": [{"name": "gemma3:4b", "context_length": 8192}]}
        return None

    async def post_json(url: str, body: dict[str, Any], *_a: Any) -> dict[str, Any] | None:
        assert url.endswith("/api/show") and body == {"model": "gemma3:4b"}
        return {"capabilities": ["completion", "vision"]}

    monkeypatch.setattr(context_windows, "_get_json", get_json)
    monkeypatch.setattr(context_windows, "_post_json", post_json)
    base = "http://127.0.0.1:11434/v1"
    window = await context_windows.discover_window(base, "gemma3:4b")
    assert window == 8192
    assert model_discovery.listed_vision(base) == {"gemma3:4b": True}

    settings = Settings()
    settings.providers["box"] = {"preset": "local", "base_url": base, "model": "gemma3:4b"}
    registry = ProviderRegistry(settings, Paths.create(tmp_path / "home"))
    assert registry.vision_for("box", "gemma3:4b") is True


@pytest.mark.parametrize(
    ("vendor", "model", "config"),
    [
        ("openai", "gpt-6.1-sol", {"auth_method": "chatgpt", "access_token": _token()}),
        ("openai", "gpt-5.2-codex", {"auth_method": "chatgpt", "access_token": _token()}),
        ("openai", "gpt-6.1", {"api_key": "sk-test"}),
        ("openai", "gpt-4o", {"api_key": "sk-test"}),
        ("deepseek", "deepseek-chat", {"api_key": "sk-test"}),
        ("anthropic", "claude-sonnet-4-5", {"api_key": "sk-ant-test"}),
        ("gemini", "gemini-2.5-pro", {"api_key": "test"}),
    ],
)
def test_gates_and_transport_agree(
    tmp_path: Path, vendor: str, model: str, config: dict[str, Any]
) -> None:
    settings = Settings()
    settings.providers[vendor] = {"model": model, **config}
    registry = ProviderRegistry(settings, Paths.create(tmp_path / "home"))
    gate = registry.vision_for(vendor, model)
    provider = registry.build(vendor, model)
    messages = _messages(tmp_path)
    if isinstance(provider, codex_transport.CodexProvider):
        sends = _codex_sends_images(provider, messages)
    elif isinstance(provider, OpenAICompatProvider):
        sends = bool(provider._vision)
    elif vendor == "anthropic":
        _system, converted = messages_to_anthropic(messages)
        sends = "'type': 'image'" in str(converted)
    else:
        _system, contents = messages_to_gemini(messages)
        sends = "inlineData" in str(contents)
    assert sends is (gate is True), (vendor, model, gate)
