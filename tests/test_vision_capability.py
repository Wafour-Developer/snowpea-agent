"""Can this model see? — the four rungs, and the one that asks the server.

The bug: a local vision model loaded under a name no allowlist knows
(``flash-next-mtp``) had every image replaced by "(this model cannot see
images)", while the server would have answered fine.  The fix is a chain — a
configured override, the public catalog, the model name, and for a self-hosted
server only, one optimistic attempt whose answer is remembered (CORE-vision).
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import content as content_parts
from snowpea_core.providers import vision as vision_scale
from snowpea_core.providers.base import ChatMessage
from snowpea_core.providers.presets import PRESETS
from snowpea_core.providers.registry import ProviderRegistry

#: A one-pixel PNG, so an attachment block has real bytes behind it.
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def _forget_learned() -> Iterator[None]:
    """The learned-capability memory is process-global."""
    vision_scale.MEMORY.clear()
    vision_scale.MEMORY.home = None
    yield
    vision_scale.MEMORY.clear()
    vision_scale.MEMORY.home = None


@pytest.fixture
def image(tmp_path: Path) -> Path:
    path = tmp_path / "shot.png"
    path.write_bytes(PIXEL)
    return path


def _messages(image: Path) -> list[ChatMessage]:
    """One user turn carrying text and an image attachment."""
    return [
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
        )
    ]


class FakeServer:
    """An OpenAI-compatible endpoint that either takes images or refuses them."""

    def __init__(self, *, accepts_images: bool, status: int = 400, detail: str = "") -> None:
        self.accepts_images = accepts_images
        self.status = status
        self.detail = detail or "Invalid content: expected a string, got a list"
        #: One entry per request: True when it carried image parts.
        self.requests: list[bool] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                had_images = any(
                    isinstance(message.get("content"), list)
                    for message in payload.get("messages", [])
                )
                server.requests.append(had_images)
                if had_images and not server.accepts_images:
                    body = json.dumps({"error": {"message": server.detail}}).encode()
                    self.send_response(server.status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                chunk = json.dumps({"choices": [{"delta": {"content": "ok"}}]})
                body = f"data: {chunk}\n\ndata: [DONE]\n\n".encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                """Silence the default stderr access log."""

        return Handler


def _local_settings(base_url: str, **extra: Any) -> Settings:
    settings = Settings()
    settings.providers["hon2"] = {
        "preset": "local",
        "base_url": base_url,
        "model": "flash-next-mtp",
        **extra,
    }
    return settings


# ---------------------------------------------------------------------------
# the name rung, unchanged
# ---------------------------------------------------------------------------


def test_the_name_rung_still_answers_what_it_always_did() -> None:
    assert content_parts.vision_from_name("openai", "gpt-4o") is True
    assert content_parts.vision_from_name("anthropic", "anything") is True
    assert content_parts.vision_from_name("local", "qwen2.5-vl-7b") is True
    # The model that started this: a real vision model no allowlist knows.
    assert content_parts.vision_from_name("local", "flash-next-mtp") is False
    # The old public name is kept, and still means the name rung.
    assert content_parts.supports_vision("local", "flash-next-mtp") is False


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------


def test_a_settings_override_beats_every_guess() -> None:
    settings = _local_settings("http://hon2:8000/v1", vision=True)
    assert ProviderRegistry(settings).vision_for("hon2") is True
    settings.providers["hon2"]["vision"] = False
    assert ProviderRegistry(settings).vision_for("hon2") is False


def test_a_per_model_override_beats_the_per_server_one() -> None:
    settings = _local_settings(
        "http://hon2:8000/v1",
        vision=True,
        models={"flash-next-mtp": {"vision": False}},
    )
    registry = ProviderRegistry(settings)
    assert registry.vision_for("hon2", "flash-next-mtp") is False
    # A model the map says nothing about falls back to the server rule.
    assert registry.vision_for("hon2", "other-model") is True


def test_an_override_can_turn_a_hosted_model_off() -> None:
    """A gateway in front of gpt-4o that strips images is the user's to declare."""
    settings = Settings()
    settings.providers["openai"] = {"api_key": "sk-test", "vision": False}
    assert ProviderRegistry(settings).vision_for("openai", "gpt-4o") is False


def test_the_public_catalog_answers_when_it_has_the_card(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path)
    paths.cache_dir.mkdir(parents=True, exist_ok=True)
    (paths.cache_dir / "models-dev.json").write_text(
        json.dumps(
            {
                "saved_at": 10**10,
                "catalog": {
                    "deepseek": {
                        "models": {
                            "deepseek-vl": {"modalities": {"input": ["text", "image"]}},
                            "deepseek-chat": {"modalities": {"input": ["text"]}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    settings = Settings()
    settings.providers["deepseek"] = {"api_key": "sk-test"}
    registry = ProviderRegistry(settings, paths)
    # Neither name matches a hint; the catalog is what separates them.
    assert registry.vision_for("deepseek", "deepseek-vl") is True
    assert registry.vision_for("deepseek", "deepseek-chat") is False


def test_a_hosted_model_nothing_knows_stays_text_only() -> None:
    """A 400 from a hosted vendor costs a request and teaches nothing."""
    settings = Settings()
    settings.providers["deepseek"] = {"api_key": "sk-test"}
    assert ProviderRegistry(settings).vision_for("deepseek", "deepseek-chat") is False


def test_an_unknown_local_model_is_left_for_the_probe() -> None:
    registry = ProviderRegistry(_local_settings("http://hon2:8000/v1"))
    assert registry.vision_for("hon2") is None


def test_a_local_model_the_name_knows_needs_no_probe() -> None:
    settings = _local_settings("http://hon2:8000/v1")
    settings.providers["hon2"]["model"] = "qwen2.5-vl-7b"
    assert ProviderRegistry(settings).vision_for("hon2") is True


# ---------------------------------------------------------------------------
# the rejection detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (400, "Invalid content: expected a string, got a list"),
        (400, "this model does not support image_url content parts"),
        (422, "multimodal input is not supported"),
        (415, "unsupported content type"),
    ],
)
def test_a_refusal_of_the_image_parts_is_recognised(status: int, detail: str) -> None:
    assert vision_scale.is_rejection(status, detail) is True


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (400, "max_tokens must be a positive integer"),
        (401, "invalid api key"),
        (429, "rate limited"),
        (500, "internal image processing failure"),
        (503, "image service unavailable"),
    ],
)
def test_another_failure_is_never_read_as_blindness(status: int, detail: str) -> None:
    """Remembering the wrong 400 would blind a model that can see, silently."""
    assert vision_scale.is_rejection(status, detail) is False


# ---------------------------------------------------------------------------
# try once
# ---------------------------------------------------------------------------


async def test_a_server_that_takes_images_is_never_probed_again(
    tmp_path: Path, image: Path
) -> None:
    server = FakeServer(accepts_images=True)
    try:
        settings = _local_settings(server.base_url)
        registry = ProviderRegistry(settings, Paths.create(tmp_path))
        assert registry.vision_for("hon2") is None

        provider = registry.build("hon2")
        text = "".join([event.text async for event in provider.stream(_messages(image), [])])
        assert text == "ok"
        # One request, and it carried the image.
        assert server.requests == [True]
        # Learned, persisted, and no longer a question.
        assert registry.vision_for("hon2") is True
    finally:
        server.close()


async def test_a_server_that_refuses_images_falls_back_and_still_answers(
    tmp_path: Path, image: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The turn must finish. A refused image is a degraded answer, not a lost one."""
    server = FakeServer(accepts_images=False)
    try:
        settings = _local_settings(server.base_url)
        registry = ProviderRegistry(settings, Paths.create(tmp_path))
        provider = registry.build("hon2")

        with caplog.at_level("WARNING"):
            text = "".join([event.text async for event in provider.stream(_messages(image), [])])

        assert text == "ok"
        # The probe, then the same call with the text fallback.
        assert server.requests == [True, False]
        assert "falling back to the text description" in caplog.text
        assert registry.vision_for("hon2") is False
    finally:
        server.close()


async def test_the_refusal_is_remembered_so_the_next_turn_pays_nothing(
    tmp_path: Path, image: Path
) -> None:
    server = FakeServer(accepts_images=False)
    try:
        settings = _local_settings(server.base_url)
        registry = ProviderRegistry(settings, Paths.create(tmp_path))

        async for _ in registry.build("hon2").stream(_messages(image), []):
            pass
        server.requests.clear()

        # A fresh provider, built after the answer was learned.
        async for _ in registry.build("hon2").stream(_messages(image), []):
            pass
        assert server.requests == [False]
    finally:
        server.close()


async def test_the_answer_survives_a_restart(tmp_path: Path, image: Path) -> None:
    """The one refused request is paid once, not once per daemon start."""
    server = FakeServer(accepts_images=False)
    try:
        paths = Paths.create(tmp_path)
        settings = _local_settings(server.base_url)
        async for _ in ProviderRegistry(settings, paths).build("hon2").stream(_messages(image), []):
            pass
        assert (paths.cache_dir / vision_scale.CACHE_FILE).exists()

        # A new process: the in-memory store is empty, the file is not.
        vision_scale.MEMORY.clear()
        vision_scale.MEMORY.home = None
        restarted = ProviderRegistry(_local_settings(server.base_url), paths)
        assert restarted.vision_for("hon2") is False
    finally:
        server.close()


async def test_a_turn_with_no_image_never_probes(tmp_path: Path) -> None:
    """There is nothing to learn from a request that carries no picture."""
    server = FakeServer(accepts_images=False)
    try:
        registry = ProviderRegistry(_local_settings(server.base_url), Paths.create(tmp_path))
        provider = registry.build("hon2")
        async for _ in provider.stream([ChatMessage(role="user", content="hello")], []):
            pass
        assert server.requests == [False]
        # Still unknown: nothing was asked, so nothing was answered.
        assert registry.vision_for("hon2") is None
    finally:
        server.close()


async def test_an_unrelated_400_is_reported_rather_than_remembered(
    tmp_path: Path, image: Path
) -> None:
    server = FakeServer(accepts_images=False, detail="max_tokens must be a positive integer")
    try:
        from snowpea_core.providers.base import ProviderError

        registry = ProviderRegistry(_local_settings(server.base_url), Paths.create(tmp_path))
        provider = registry.build("hon2")
        with pytest.raises(ProviderError, match="HTTP 400"):
            async for _ in provider.stream(_messages(image), []):
                pass
        # One attempt, no retry, and nothing learned about vision.
        assert server.requests == [True]
        assert registry.vision_for("hon2") is None
    finally:
        server.close()


async def test_an_override_stops_the_probe_before_it_starts(tmp_path: Path, image: Path) -> None:
    server = FakeServer(accepts_images=False)
    try:
        settings = _local_settings(server.base_url, vision=False)
        registry = ProviderRegistry(settings, Paths.create(tmp_path))
        async for _ in registry.build("hon2").stream(_messages(image), []):
            pass
        # Straight to the fallback: no refused request at all.
        assert server.requests == [False]
    finally:
        server.close()


def test_the_provider_sends_images_when_the_registry_says_it_may(image: Path) -> None:
    """The adapter honours the resolved answer rather than re-deriving one."""
    from snowpea_core.providers.normalize import build_openai_request

    body = build_openai_request(
        PRESETS["local"], "flash-next-mtp", _messages(image), [], max_tokens=64, vision=True
    )
    parts = body["messages"][0]["content"]
    assert [part["type"] for part in parts] == ["text", "image_url"]

    text_only = build_openai_request(
        PRESETS["local"], "flash-next-mtp", _messages(image), [], max_tokens=64, vision=False
    )
    assert isinstance(text_only["messages"][0]["content"], str)
    assert content_parts.NO_VISION_NOTE in text_only["messages"][0]["content"]


# ---------------------------------------------------------------------------
# what the surfaces show
# ---------------------------------------------------------------------------


def test_the_listing_marks_only_what_is_known() -> None:
    settings = Settings()
    settings.providers["openai"] = {"api_key": "sk-test"}
    settings.providers["hon2"] = {"preset": "local", "base_url": "http://hon2:8000/v1"}
    registry = ProviderRegistry(settings)

    assert registry.vision_map("openai", ["gpt-4o", "o3"]) == {"gpt-4o": True, "o3": True}
    # An unknown local model is absent rather than False: a surface draws no
    # badge for it, not a crossed-out one.
    assert registry.vision_map("hon2", ["flash-next-mtp"]) == {}
    assert registry.vision_map("hon2", ["qwen2.5-vl-7b"]) == {"qwen2.5-vl-7b": True}


def test_the_wizard_badges_the_models_that_can_see() -> None:
    from snowpea_core.setup import wizard
    from snowpea_core.setup.state import WizardState

    state = WizardState.from_settings(Settings())
    state.select_vendor("openai")
    badges = wizard._vision_badges(  # noqa: SLF001 - the unit under test
        state, PRESETS["openai"], ["gpt-4o", "o3", "text-davinci-003"]
    )
    assert set(badges) == {"gpt-4o", "o3"}
    assert all("\N{EYE}" in badge for badge in badges.values())


def test_a_settings_override_reaches_the_wizard_badge() -> None:
    from snowpea_core.setup import wizard
    from snowpea_core.setup.state import WizardState

    settings = _local_settings("http://hon2:8000/v1", vision=True)
    state = WizardState.from_settings(settings)
    state.select_vendor("hon2")
    badges = wizard._vision_badges(  # noqa: SLF001 - the unit under test
        state, PRESETS["local"], ["flash-next-mtp"]
    )
    assert "flash-next-mtp" in badges


def test_a_pinned_model_list_is_still_read_as_a_list() -> None:
    """``providers.<vendor>.models`` has two shapes; neither may break the other."""
    from snowpea_core.providers import models as model_discovery

    as_list = {"models": ["a", "b"]}
    assert model_discovery.override_models(as_list, oauth=False) == ["a", "b"]
    assert vision_scale.settings_override(as_list, "a") is None

    as_map = {"models": {"a": {"vision": True}}}
    assert vision_scale.settings_override(as_map, "a") is True
    # The object form pins the same catalog through its keys.
    assert model_discovery.override_models(as_map, oauth=False) == ["a"]


async def test_the_override_survives_provider_configure(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    """``provider.configure`` filters its keys; ``vision`` has to be one of them.

    It was not, so ``snowpea provider add-local --vision`` wrote a block with
    the flag silently dropped and the probe ran anyway.
    """
    from _support import connect, make_daemon

    home = tmp_path / "home"
    daemon = await make_daemon(home)
    try:
        client = await connect(http, daemon)
        await client.ok(
            "provider.configure",
            {
                "vendor": "hon2",
                "config": {
                    "preset": "local",
                    "base_url": "http://hon2:8000/v1",
                    "model": "flash-next-mtp",
                    "vision": True,
                },
            },
        )
        saved = json.loads((home / "settings.json").read_text(encoding="utf-8"))
        assert saved["providers"]["hon2"]["vision"] is True

        settings = Settings.load(Paths(home=home))
        assert ProviderRegistry(settings).vision_for("hon2") is True
        await client.stop()
    finally:
        await daemon.stop()
