"""Model discovery: listing, the lazy auto-pick, ``/model`` and the wizard.

The bug these cover: with vendor ``local`` the preset default model is the
placeholder ``local-model``, which a vLLM server answers with
``HTTP 404: The model 'local-model' does not exist``.  Nothing may send that id
to a server — the endpoint is asked for its real list instead.

A tiny threaded HTTP server stands in for the OpenAI-compatible endpoint, so
the whole stack down to the socket is the production code path.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.commands import model_cmd
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import models as model_discovery
from snowpea_core.providers.base import ProviderError
from snowpea_core.providers.presets import PRESETS, preset_for
from snowpea_core.providers.registry import ProviderRegistry
from snowpea_core.setup import wizard
from snowpea_core.setup.state import WizardState

MODEL_IDS = ("Qwen/Qwen3-8B", "Qwen/Qwen3-32B")


@pytest.fixture(autouse=True)
def _clear_model_cache() -> Iterator[None]:
    """The listing cache is process-global; no test may inherit another's."""
    model_discovery.cache_clear()
    yield
    model_discovery.cache_clear()


class FakeServer:
    """An OpenAI-compatible ``/v1/models`` endpoint, and what it was asked.

    A thread, not an asyncio app, so the synchronous wizard tests can use the
    same fixture as the asynchronous provider ones.
    """

    def __init__(self, ids: tuple[str, ...]) -> None:
        self.ids = ids
        #: One entry per request: the ``authorization`` header as it arrived.
        self.requests: list[str] = []
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
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
                if self.path != "/v1/models":
                    self.send_error(404)
                    return
                server.requests.append(self.headers.get("authorization", ""))
                body = json.dumps(
                    {"object": "list", "data": [{"id": name} for name in server.ids]}
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                """Silence the default stderr access log."""

        return Handler


@pytest.fixture
def server() -> Iterator[FakeServer]:
    """A real HTTP server listing :data:`MODEL_IDS` on ``/v1/models``."""
    fake = FakeServer(MODEL_IDS)
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def empty_server() -> Iterator[str]:
    """A server that answers ``/v1/models`` with an empty list."""
    fake = FakeServer(())
    try:
        yield fake.base_url
    finally:
        fake.close()


def _registry(tmp_path: Path, base_url: str, **extra: Any) -> ProviderRegistry:
    paths = Paths.create(tmp_path)
    settings = Settings()
    settings.providers["local"] = {"base_url": base_url, **extra}
    settings.providers["default"] = "local"
    return ProviderRegistry(settings, paths)


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


async def test_list_models_reads_openai_compatible_endpoint(server: FakeServer) -> None:
    ids = await model_discovery.list_models(PRESETS["local"], base_url=server.base_url)
    assert ids == list(MODEL_IDS)


async def test_list_models_sends_the_key_and_caches(server: FakeServer) -> None:
    first = await model_discovery.list_models(
        PRESETS["local"], api_key="sk-test", base_url=server.base_url
    )
    second = await model_discovery.list_models(
        PRESETS["local"], api_key="sk-test", base_url=server.base_url
    )
    assert first == second == list(MODEL_IDS)
    # One round trip for two calls, and the key travelled as a bearer token.
    assert server.requests == ["Bearer sk-test"]


async def test_list_models_refresh_bypasses_the_cache(server: FakeServer) -> None:
    await model_discovery.list_models(PRESETS["local"], base_url=server.base_url)
    await model_discovery.list_models(PRESETS["local"], base_url=server.base_url, refresh=True)
    assert len(server.requests) == 2


async def test_list_models_raises_when_the_server_is_down(tmp_path: Path) -> None:
    with pytest.raises(ProviderError) as caught:
        await model_discovery.list_models(
            PRESETS["local"], base_url="http://127.0.0.1:1/v1", timeout=1.0
        )
    assert caught.value.code == "internal"
    assert "local" in str(caught.value)


def test_placeholder_detection() -> None:
    assert model_discovery.is_placeholder(PRESETS["local"].default_model)
    assert model_discovery.is_placeholder("")
    assert model_discovery.is_placeholder(None)
    assert not model_discovery.is_placeholder("Qwen/Qwen3-8B")
    assert not model_discovery.is_placeholder(PRESETS["anthropic"].default_model)


# ---------------------------------------------------------------------------
# lazy auto-pick
# ---------------------------------------------------------------------------


async def test_resolve_model_auto_picks_and_persists(
    tmp_path: Path, server: FakeServer
) -> None:
    registry = _registry(tmp_path, server.base_url)
    assert registry.model_for("local") == "local-model"

    picked = await registry.resolve_model("local")

    assert picked == MODEL_IDS[0]
    assert registry.settings.providers["local"]["model"] == MODEL_IDS[0]
    # The choice reached settings.json, so the next daemon starts there.
    reloaded = Settings.load(Paths.create(tmp_path))
    assert reloaded.providers["local"]["model"] == MODEL_IDS[0]


async def test_resolve_model_keeps_a_configured_model(
    tmp_path: Path, server: FakeServer
) -> None:
    registry = _registry(tmp_path, server.base_url, model="my-own-model")
    assert await registry.resolve_model("local") == "my-own-model"
    assert server.requests == []


async def test_empty_listing_raises_model_not_configured(
    tmp_path: Path, empty_server: str
) -> None:
    registry = _registry(tmp_path, empty_server)
    with pytest.raises(ProviderError) as caught:
        await registry.resolve_model("local")
    assert caught.value.code == "model_not_configured"
    assert "/model <name>" in str(caught.value)


async def test_a_listing_of_only_placeholders_is_no_listing(tmp_path: Path) -> None:
    """Ollama-style servers echo the alias back; ``local-model`` is never a pick."""
    fake = FakeServer(("local-model",))
    try:
        registry = _registry(tmp_path, fake.base_url)
        with pytest.raises(ProviderError) as caught:
            await registry.resolve_model("local")
    finally:
        fake.close()
    assert caught.value.code == "model_not_configured"


async def test_replay_mode_leaves_the_fixture_model_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixtures name their own model; discovery must not rewrite them."""
    from snowpea_core.providers.openai_compat import OpenAICompatProvider

    monkeypatch.setenv("SNOWPEA_PROVIDER_MODE", "replay")
    provider = OpenAICompatProvider(
        PRESETS["local"], model="local-model", base_url="http://127.0.0.1:1/v1"
    )
    assert await provider._ensure_model() == "local-model"  # noqa: SLF001 - unit under test


async def test_unreachable_server_raises_model_not_configured(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "http://127.0.0.1:1/v1")
    with pytest.raises(ProviderError) as caught:
        await registry.resolve_model("local")
    assert caught.value.code == "model_not_configured"


async def test_provider_never_sends_the_placeholder(tmp_path: Path, server: FakeServer) -> None:
    """The adapter the registry builds resolves the model before its first call."""
    registry = _registry(tmp_path, server.base_url)
    provider = registry.build("local")
    assert provider.model == "local-model"

    resolved = await provider._ensure_model()  # noqa: SLF001 - the unit under test

    assert resolved == MODEL_IDS[0]
    assert provider.model == MODEL_IDS[0]


async def test_provider_without_a_resolver_refuses_the_placeholder() -> None:
    from snowpea_core.providers.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider(
        PRESETS["local"], model="local-model", base_url="http://127.0.0.1:1/v1"
    )
    with pytest.raises(ProviderError) as caught:
        await provider._ensure_model()  # noqa: SLF001 - the unit under test
    assert caught.value.code == "model_not_configured"


def test_errors_name_the_vendor_and_the_model() -> None:
    from snowpea_core.providers.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider(
        PRESETS["local"], model="my-model", base_url="http://127.0.0.1:1/v1"
    )
    assert provider._tag() == "local (my-model)"  # noqa: SLF001 - the unit under test


# ---------------------------------------------------------------------------
# /model
# ---------------------------------------------------------------------------


class _Hub:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit_event(self, _session_id: str, event: Any) -> None:
        self.events.append(event)


class _Core:
    """The slice of ``Core`` ``/model`` touches.

    It grew with CORE-model-assignment: the command now lists model profiles
    and pins the session through ``SessionManager.set_model`` so the choice is
    persisted, rather than only mutating ``session.model`` in memory.
    """

    def __init__(self, registry: ProviderRegistry, workdir: Path) -> None:
        from snowpea_core.session.manager import SessionManager

        self.providers = registry
        self.hub = _Hub()
        self.settings = registry.settings
        self.paths = Paths.create(workdir)
        self.store = None
        self.sessions = SessionManager(settings=self.settings)
        self.saved = 0

    def mark_settings_saved(self) -> None:
        self.saved += 1


class _Session:
    def __init__(self, workdir: Path) -> None:
        self.id = "s-1"
        self.provider = "local"
        self.model: str | None = None
        self.workdir = workdir
        self.agent: str | None = None


def _ctx(registry: ProviderRegistry, workdir: Path | None = None) -> Any:
    from snowpea_core.commands.registry import CommandContext

    directory = workdir or Path(registry.paths.home if registry.paths else ".")
    return CommandContext(
        core=_Core(registry, directory),  # type: ignore[arg-type]
        session=_Session(directory),  # type: ignore[arg-type]
        turn_id="t-1",
    )


def _texts(ctx: Any) -> list[str]:
    """The text of every ``message.done`` the command emitted."""
    return [str(payload.get("text", "")) for _kind, payload in ctx.core.hub.events]


async def test_model_command_lists_with_the_current_one_marked(
    tmp_path: Path, server: FakeServer
) -> None:
    registry = _registry(tmp_path, server.base_url, model=MODEL_IDS[1])
    ctx = _ctx(registry)

    await model_cmd.cmd_model(ctx, "")

    said = "\n".join(_texts(ctx))
    assert MODEL_IDS[0] in said
    assert f"* 2. {MODEL_IDS[1]}" in said


async def test_model_command_sets_and_persists(tmp_path: Path, server: FakeServer) -> None:
    registry = _registry(tmp_path, server.base_url)
    ctx = _ctx(registry)

    await model_cmd.cmd_model(ctx, MODEL_IDS[1])

    assert ctx.session.model == MODEL_IDS[1]
    assert f"model: local/{MODEL_IDS[1]}" in "\n".join(_texts(ctx))
    assert Settings.load(Paths.create(tmp_path)).providers["local"]["model"] == MODEL_IDS[1]


async def test_model_command_accepts_a_row_number(tmp_path: Path, server: FakeServer) -> None:
    registry = _registry(tmp_path, server.base_url)
    ctx = _ctx(registry)

    await model_cmd.cmd_model(ctx, "2")

    assert ctx.session.model == MODEL_IDS[1]


async def test_model_command_survives_a_down_server(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "http://127.0.0.1:1/v1")
    ctx = _ctx(registry)

    await model_cmd.cmd_model(ctx, "")

    assert "could not list models" in "\n".join(_texts(ctx))


def test_model_command_is_registered() -> None:
    from snowpea_core.commands.registry import CommandRegistry, register_builtin_commands

    registry = register_builtin_commands(CommandRegistry())
    assert registry.get("model") is not None


# ---------------------------------------------------------------------------
# the wizard
# ---------------------------------------------------------------------------


def _wizard_state(base_url: str) -> WizardState:
    state = WizardState()
    state.vendor = "local"
    state.variant = "vllm"
    state.base_url = base_url
    return state


def test_wizard_defaults_to_the_first_model(
    monkeypatch: pytest.MonkeyPatch, server: FakeServer
) -> None:
    asked: list[str] = []

    def ask_text(prompt: str, **_kwargs: Any) -> str:
        asked.append(prompt)
        return ""  # Enter

    monkeypatch.setattr(wizard.ui, "ask_text", ask_text)
    state = _wizard_state(server.base_url)

    wizard._ask_for_model(state, interactive=True)  # noqa: SLF001 - the unit under test

    assert state.model == MODEL_IDS[0]
    assert asked and MODEL_IDS[0] in asked[0]


def test_wizard_takes_a_typed_number(
    monkeypatch: pytest.MonkeyPatch, server: FakeServer
) -> None:
    monkeypatch.setattr(wizard.ui, "ask_text", lambda *_a, **_k: "2")
    state = _wizard_state(server.base_url)

    wizard._ask_for_model(state, interactive=True)  # noqa: SLF001 - the unit under test

    assert state.model == MODEL_IDS[1]


def test_wizard_takes_free_text(monkeypatch: pytest.MonkeyPatch, server: FakeServer) -> None:
    monkeypatch.setattr(wizard.ui, "ask_text", lambda *_a, **_k: "some/other-model")
    state = _wizard_state(server.base_url)

    wizard._ask_for_model(state, interactive=True)  # noqa: SLF001 - the unit under test

    assert state.model == "some/other-model"


def test_wizard_continues_when_the_server_is_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(wizard.ui, "ask_text", lambda *_a, **_k: "")
    state = _wizard_state("http://127.0.0.1:1/v1")

    wizard._ask_for_model(state, interactive=True)  # noqa: SLF001 - the unit under test

    assert state.model is None
    assert "could not list models" in capsys.readouterr().out


def test_wizard_asks_nothing_when_not_interactive(server: FakeServer) -> None:
    state = _wizard_state(server.base_url)
    wizard._ask_for_model(state, interactive=False)  # noqa: SLF001 - the unit under test
    assert state.model is None


def test_local_variants_still_ship_the_placeholder() -> None:
    """The preset is a placeholder by design; discovery is what replaces it."""
    for variant in ("vllm", "ollama", "lmstudio"):
        assert model_discovery.is_placeholder(preset_for("local", variant).default_model)


# ---------------------------------------------------------------------------
# provider.models over RPC
# ---------------------------------------------------------------------------


def test_provider_models_is_in_the_protocol() -> None:
    from snowpea_core.server.protocol import IMPLEMENTED_METHODS, METHODS

    assert "provider.models" in METHODS
    assert "provider.models" in IMPLEMENTED_METHODS


async def test_provider_models_handler(tmp_path: Path, server: FakeServer) -> None:
    from snowpea_core.server.protocol import ProviderModelsParams
    from snowpea_core.server.session_handlers import provider_models_handler

    core = _Core(_registry(tmp_path, server.base_url), tmp_path)
    result = await provider_models_handler(
        None,  # type: ignore[arg-type]
        ProviderModelsParams(vendor="local"),
        core,  # type: ignore[arg-type]
    )
    assert result.vendor == "local"
    assert result.models == list(MODEL_IDS)
    assert result.current == "local-model"


async def test_run_sync_works_inside_a_running_loop(server: FakeServer) -> None:
    """The wizard's helper must survive being called from async CLI code."""
    assert asyncio.get_running_loop() is not None
    listed = wizard._run_sync(  # noqa: SLF001 - the unit under test
        model_discovery.list_models(PRESETS["local"], base_url=server.base_url)
    )
    assert list(listed) == list(MODEL_IDS)


# ---------------------------------------------------------------------------
# /model and model profiles (CORE-model-assignment B-P3-1)
# ---------------------------------------------------------------------------


def _with_profiles(registry: ProviderRegistry) -> ProviderRegistry:
    from snowpea_core.config.project import ModelProfile

    registry.settings.models.profiles = {
        "fast": ModelProfile(provider="local", model=MODEL_IDS[0]),
        "deep": ModelProfile(provider="local", model=MODEL_IDS[1]),
    }
    registry.settings.models.default = "fast"
    return registry


async def test_model_listing_shows_the_configured_profiles(
    tmp_path: Path, server: FakeServer
) -> None:
    """The listing used to show only the vendor's models, never the profiles."""
    ctx = _ctx(_with_profiles(_registry(tmp_path, server.base_url)), tmp_path)

    await model_cmd.cmd_model(ctx, "")

    said = "\n".join(_texts(ctx))
    assert "Model profiles:" in said
    assert "* fast" in said, "models.default is marked"
    assert "deep" in said


async def test_model_pins_the_session_to_a_profile_id(
    tmp_path: Path, server: FakeServer
) -> None:
    ctx = _ctx(_with_profiles(_registry(tmp_path, server.base_url)), tmp_path)

    await model_cmd.cmd_model(ctx, "deep")

    assert (ctx.session.provider, ctx.session.model) == ("local", MODEL_IDS[1])
    assert "model: local/" in "\n".join(_texts(ctx))
    # The surfaces hear about it, so a HUD fed once by session/ready updates.
    assert any(kind == "model.changed" for kind, _payload in ctx.core.hub.events)


async def test_model_default_writes_the_global_default(
    tmp_path: Path, server: FakeServer
) -> None:
    ctx = _ctx(_with_profiles(_registry(tmp_path, server.base_url)), tmp_path)

    await model_cmd.cmd_model(ctx, "default deep")

    assert ctx.core.settings.models.default == "deep"
    assert Settings.load(Paths.create(tmp_path)).models.default == "deep"
    assert "default model profile is now deep" in "\n".join(_texts(ctx))


async def test_model_default_refuses_an_unknown_profile(
    tmp_path: Path, server: FakeServer
) -> None:
    ctx = _ctx(_with_profiles(_registry(tmp_path, server.base_url)), tmp_path)

    await model_cmd.cmd_model(ctx, "default nope")

    assert ctx.core.settings.models.default == "fast"
    assert "unknown profile" in "\n".join(_texts(ctx))
