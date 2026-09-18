"""Named local vendors — several OpenAI-compatible servers, each with a name.

``providers.local`` is one server.  A user with a vLLM box *and* an Ollama
laptop needs two, so any key under ``providers`` whose block carries
``"preset": "local"`` is a local-style vendor of its own: its name is the
vendor id, and everything the built-in ``local`` vendor gets — keyless auth,
``/v1/models`` discovery, the Ollama fallback, placeholder resolution, live
context-window probing — it gets too.

The one thing that must not change is an existing configuration: a settings
file holding nothing but ``providers.local`` has to behave exactly as it did.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import connect, make_daemon

from snowpea_core.config.model_routing import resolve_reference
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import ModelProfile, Settings
from snowpea_core.providers import context_windows
from snowpea_core.providers import models as model_discovery
from snowpea_core.providers.base import ProviderError
from snowpea_core.providers.presets import (
    PRESETS,
    is_local_vendor_config,
    local_vendor_ids,
    preset_for,
    synthesize_local_preset,
    validate_custom_vendor_id,
)
from snowpea_core.providers.registry import ProviderRegistry
from snowpea_core.setup import catalog, ui, wizard
from snowpea_core.setup.state import WizardState

HON2_MODELS = ("flash-next-mtp", "flash-next-base")


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    """Both discovery caches are process-global; no test may inherit another's."""
    model_discovery.cache_clear()
    context_windows.cache_clear()
    yield
    model_discovery.cache_clear()
    context_windows.cache_clear()


class FakeServer:
    """An OpenAI-compatible ``/v1/models`` endpoint that reports a window."""

    def __init__(self, ids: tuple[str, ...], window: int | None = None) -> None:
        self.ids = ids
        self.window = window
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
                server.requests.append(self.path)
                row: dict[str, Any] = {}
                if server.window is not None:
                    row["max_model_len"] = server.window
                body = json.dumps({"data": [{"id": name, **row} for name in server.ids]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                """Silence the default stderr access log."""

        return Handler


@pytest.fixture
def hon2() -> Iterator[FakeServer]:
    fake = FakeServer(HON2_MODELS, window=262144)
    try:
        yield fake
    finally:
        fake.close()


def _settings(**blocks: dict[str, Any]) -> Settings:
    settings = Settings()
    for vendor, block in blocks.items():
        settings.providers[vendor] = block
    return settings


# ---------------------------------------------------------------------------
# settings parsing and validation
# ---------------------------------------------------------------------------


def test_a_block_is_a_local_vendor_only_when_it_says_so() -> None:
    assert is_local_vendor_config({"preset": "local", "base_url": "http://x/v1"})
    # The alias exists because "local" reads oddly for a server on another host.
    assert is_local_vendor_config({"preset": "openai-compatible"})
    assert not is_local_vendor_config({"base_url": "http://x/v1"})
    assert not is_local_vendor_config({"preset": "anthropic"})
    assert not is_local_vendor_config("not a block")


@pytest.mark.parametrize(
    "name",
    ["Hon2", "2hon", "hon 2", "", "hon2:", "a" * 33, "hon.2"],
)
def test_a_bad_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="invalid provider name"):
        validate_custom_vendor_id(name)


@pytest.mark.parametrize("name", ["openai", "anthropic", "local", "qwen"])
def test_a_name_may_not_shadow_a_built_in_vendor(name: str) -> None:
    """``providers.openai`` turning into a self-hosted server would reroute
    every ``openai:`` model reference somewhere the user never intended."""
    with pytest.raises(ValueError, match="built-in provider"):
        validate_custom_vendor_id(name)


@pytest.mark.parametrize("name", ["hon2", "vllm-a", "a", "x_9", "a" * 32])
def test_a_good_name_is_accepted(name: str) -> None:
    assert validate_custom_vendor_id(name) == name


def test_local_vendor_ids_skips_unusable_keys(caplog: pytest.LogCaptureFixture) -> None:
    """One hand-edited typo must not stop the daemon from starting."""
    providers = {
        "default": "hon2",
        "local": {"base_url": "http://localhost:11434/v1"},
        "hon2": {"preset": "local"},
        "Bad Name": {"preset": "local"},
        "openai": {"api_key": "sk-test"},
    }
    with caplog.at_level("WARNING"):
        assert local_vendor_ids(providers) == ["local", "hon2"]
    assert "Bad Name" in caplog.text


# ---------------------------------------------------------------------------
# preset synthesis
# ---------------------------------------------------------------------------


def test_a_named_server_gets_a_preset_built_from_its_block() -> None:
    preset = synthesize_local_preset(
        "hon2",
        {
            "preset": "local",
            "label": "hon2 vLLM",
            "variant": "vllm",
            "base_url": "http://hon2.example.com:8000/v1",
        },
    )
    assert preset.id == "hon2"
    assert preset.label == "hon2 vLLM"
    assert preset.base_url == "http://hon2.example.com:8000/v1"
    assert preset.variant == "vllm"
    assert preset.local_style is True
    # The two quirks the built-in local vendor has, for the same reasons.
    assert preset.key_required is False
    assert preset.supports_parallel_tools is True
    assert model_discovery.is_placeholder(preset.default_model)


def test_a_named_server_without_a_label_or_url_falls_back() -> None:
    preset = synthesize_local_preset("vllm-a", {"preset": "local", "variant": "vllm"})
    assert preset.label == "vllm-a"
    assert preset.base_url == PRESETS["local"].base_url or preset.base_url
    assert preset.base_url == "http://localhost:8000/v1"


def test_preset_for_needs_the_block_to_know_a_named_server() -> None:
    block = {"preset": "local", "base_url": "http://hon2:8000/v1"}
    assert preset_for("hon2", None, block).id == "hon2"
    with pytest.raises(KeyError):
        preset_for("hon2")


def test_the_built_in_local_preset_is_still_local_style() -> None:
    assert PRESETS["local"].local_style is True
    assert preset_for("local", "vllm").local_style is True
    assert PRESETS["anthropic"].local_style is False


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_the_registry_treats_a_named_server_as_local_style() -> None:
    registry = ProviderRegistry(
        _settings(hon2={"preset": "local", "base_url": "http://hon2:8000/v1"})
    )
    assert registry.is_local_style("hon2") is True
    assert registry.is_local_style("local") is True
    assert registry.is_local_style("openai") is False
    assert registry.custom_vendors() == ["hon2"]
    # Keyless: a base_url is the whole setup, exactly as for ``local``.
    assert registry.is_configured("hon2") is True
    assert registry.auth_status("hon2") == "active"
    assert registry.api_key_for("hon2") is None


def test_a_named_server_builds_a_provider_without_a_key() -> None:
    registry = ProviderRegistry(
        _settings(hon2={"preset": "local", "base_url": "http://hon2:8000/v1", "model": "m"})
    )
    provider = registry.build("hon2")
    assert provider.vendor == "hon2"
    assert provider.model == "m"


def test_an_unknown_vendor_is_still_an_error() -> None:
    registry = ProviderRegistry(_settings(nonsense={"base_url": "http://x/v1"}))
    assert registry.custom_vendors() == []
    with pytest.raises(ProviderError, match="unknown provider vendor"):
        registry.preset("nonsense")


def test_configure_refuses_a_new_name_that_declares_nothing() -> None:
    registry = ProviderRegistry(Settings())
    with pytest.raises(ProviderError, match="unknown provider vendor"):
        registry.configure("hon2", {"base_url": "http://hon2:8000/v1"})


def test_configure_refuses_a_name_that_shadows_a_preset_id() -> None:
    registry = ProviderRegistry(Settings())
    with pytest.raises(ProviderError, match="invalid provider name|built-in provider"):
        registry.configure("Hon2", {"preset": "local", "base_url": "http://hon2:8000/v1"})


def test_configure_accepts_a_declared_named_server() -> None:
    registry = ProviderRegistry(Settings())
    registry.configure("hon2", {"preset": "local", "base_url": "http://hon2:8000/v1"})
    assert registry.settings.providers["hon2"]["preset"] == "local"
    # A later patch does not have to repeat the marker.
    registry.configure("hon2", {"model": "flash-next-mtp"})
    assert registry.model_for("hon2") == "flash-next-mtp"


def test_provider_list_reports_the_preset_and_the_custom_flag() -> None:
    registry = ProviderRegistry(
        _settings(hon2={"preset": "local", "base_url": "http://hon2:8000/v1", "label": "Hon2"})
    )
    rows = {info.vendor: info for info in registry.list()}
    assert rows["hon2"].preset == "local"
    assert rows["hon2"].custom is True
    assert rows["hon2"].label == "Hon2"
    assert rows["local"].preset == "local"
    assert rows["local"].custom is False
    assert rows["openai"].preset == "openai"
    assert rows["openai"].custom is False


def test_remove_forgets_the_block_its_profiles_and_its_assignments() -> None:
    settings = _settings(hon2={"preset": "local", "base_url": "http://hon2:8000/v1"})
    settings.providers["default"] = "hon2"
    settings.models.profiles = {
        "hon2:flash-next-mtp": ModelProfile(provider="hon2", model="flash-next-mtp"),
        "openai:gpt-4.1": ModelProfile(provider="openai", model="gpt-4.1"),
    }
    settings.models.default = "hon2:flash-next-mtp"
    settings.agents.models = {"critic": "hon2:flash-next-mtp", "writer": "openai:gpt-4.1"}
    registry = ProviderRegistry(settings)

    assert registry.remove("hon2") is True

    assert "hon2" not in settings.providers
    assert "default" not in settings.providers
    assert set(settings.models.profiles) == {"openai:gpt-4.1"}
    assert settings.models.default is None
    assert settings.agents.models == {"writer": "openai:gpt-4.1"}
    assert registry.remove("hon2") is False


# ---------------------------------------------------------------------------
# model references
# ---------------------------------------------------------------------------


def test_a_named_server_model_reference_resolves_unchanged() -> None:
    settings = _settings(hon2={"preset": "local", "base_url": "http://hon2:8000/v1"})
    route = resolve_reference(settings, "hon2:flash-next-mtp")
    assert (route.provider, route.model) == ("hon2", "flash-next-mtp")
    # A bare name works too, the way a bare preset id does.
    assert resolve_reference(settings, "hon2").provider == "hon2"
    # And a name nothing describes still resolves to nothing.
    assert resolve_reference(settings, "nowhere").provider is None


def test_a_profile_default_selects_the_named_server() -> None:
    settings = _settings(hon2={"preset": "local", "base_url": "http://hon2:8000/v1"})
    settings.models.profiles = {
        "hon2:flash-next-mtp": ModelProfile(provider="hon2", model="flash-next-mtp")
    }
    settings.models.default = "hon2:flash-next-mtp"
    registry = ProviderRegistry(settings)
    assert registry.default_vendor() == "hon2"
    assert registry.model_for("hon2") == "flash-next-mtp"


# ---------------------------------------------------------------------------
# discovery and context windows
# ---------------------------------------------------------------------------


async def test_discovery_and_the_auto_pick_work_per_named_server(
    hon2: FakeServer, tmp_path: Path
) -> None:
    settings = _settings(hon2={"preset": "local", "base_url": hon2.base_url})
    registry = ProviderRegistry(settings, Paths.create(tmp_path))

    listing = await registry.model_listing("hon2")
    assert listing.models == list(HON2_MODELS)
    assert listing.source == model_discovery.SOURCE_LIVE

    # The preset default is the placeholder, so the first use picks a real id
    # and persists it — the ``local`` behaviour, keyed on this vendor's block.
    assert model_discovery.is_placeholder(registry.model_for("hon2"))
    assert await registry.resolve_model("hon2") == HON2_MODELS[0]
    assert settings.providers["hon2"]["model"] == HON2_MODELS[0]


async def test_the_context_window_is_probed_and_cached_per_vendor_and_url(
    hon2: FakeServer, tmp_path: Path
) -> None:
    settings = _settings(hon2={"preset": "local", "base_url": hon2.base_url})
    registry = ProviderRegistry(settings, Paths.create(tmp_path))

    window = await registry.resolve_context_window("hon2", HON2_MODELS[0])
    assert window == 262144
    # Cached against this vendor and this URL, so a second server with the same
    # model name is asked separately rather than inheriting the answer.
    hit, cached = context_windows.cache_get("hon2", hon2.base_url, HON2_MODELS[0])
    assert (hit, cached) == (True, 262144)
    assert context_windows.cache_get("other", hon2.base_url, HON2_MODELS[0])[0] is False
    assert registry.context_window("hon2", HON2_MODELS[0]) == 262144


async def test_two_named_servers_keep_their_own_catalogs(tmp_path: Path) -> None:
    first = FakeServer(("alpha-1",))
    second = FakeServer(("beta-1", "beta-2"))
    try:
        settings = _settings(
            hon2={"preset": "local", "base_url": first.base_url},
            lab={"preset": "local", "base_url": second.base_url},
        )
        registry = ProviderRegistry(settings, Paths.create(tmp_path))
        assert await registry.list_models("hon2") == ["alpha-1"]
        assert await registry.list_models("lab") == ["beta-1", "beta-2"]
    finally:
        first.close()
        second.close()


def test_a_named_server_gets_the_small_model_prompt_layer() -> None:
    from snowpea_core.prompts import compose

    assert compose.vendor_class_for("hon2") == "openai-family"
    assert compose.vendor_class_for("hon2", local_style=True) == "small-local"
    assert compose.vendor_class_for("local") == "small-local"


# ---------------------------------------------------------------------------
# setup: catalog and wizard
# ---------------------------------------------------------------------------


def test_the_setup_catalog_lists_named_servers_after_the_presets() -> None:
    settings = _settings(
        hon2={"preset": "local", "base_url": "http://hon2:8000/v1", "label": "Hon2"}
    )
    items = catalog.vendor_catalog(settings)
    assert [item.id for item in items][: len(PRESETS)] == list(PRESETS)
    row = next(item for item in items if item.id == "hon2")
    assert row.label == "Hon2"
    assert row.tier == "free"
    assert row.key == "self-hosted"
    assert row.active is True
    assert catalog.vendor_auth_tags("hon2") == ("api_key",)


def _menu_answers(monkeypatch: pytest.MonkeyPatch, **by_title: str | None) -> None:
    """Stand in for the arrow menu: one answer per menu, keyed on its title."""

    def pick(title: str, options, **_kwargs):  # type: ignore[no-untyped-def]
        for prefix, answer in by_title.items():
            if title.lower().startswith(prefix.replace("_", " ")):
                return answer
        return None

    monkeypatch.setattr(wizard, "_menu_pick", pick)


def test_the_wizard_offers_the_configured_servers_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Picking ``local`` on the provider screen opens the server list, and the
    existing ``local`` entry is one of its rows rather than a special case."""
    seen: list[list[str]] = []

    def pick(title: str, options, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append([row[0] for row in options])
        return None

    monkeypatch.setattr(wizard, "_menu_pick", pick)
    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: "")
    state = WizardState.from_settings(Settings())
    state.add_local_server("hon2")
    state.provider_configs["local"] = {"base_url": "http://localhost:11434/v1"}
    state.select_vendor("local")

    wizard._ask_for_key(state, interactive=True)  # noqa: SLF001

    assert seen[0] == ["local", "hon2", wizard.ADD_LOCAL, wizard.REMOVE_LOCAL]


def test_the_wizard_adds_a_named_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Add another server… → name → type → URL → key."""
    _menu_answers(monkeypatch, local=wizard.ADD_LOCAL)
    # name, server type (1 = vLLM), base URL, API key
    answers = iter(["hon2", "1", "http://hon2.example.com:8000/v1", ""])
    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: next(answers, ""))
    state = WizardState.from_settings(Settings())
    state.select_vendor("local")

    wizard._ask_for_key(state, interactive=True)  # noqa: SLF001
    state.remember_current_provider()

    block = state.provider_configs["hon2"]
    assert block["preset"] == "local"
    assert block["base_url"] == "http://hon2.example.com:8000/v1"
    assert block["variant"] == "vllm"
    assert state.vendor == "hon2"
    assert state.local_servers() == ["hon2"]


def test_off_a_tty_the_local_flow_configures_the_selected_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scripted run has no menu, so it keeps the single-server behaviour:
    type, URL, key for whichever server is selected — no name is invented."""
    monkeypatch.setattr(ui, "is_interactive", lambda *a, **kw: False)
    answers = iter(["1", "http://localhost:8000/v1", ""])
    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: next(answers, ""))
    state = WizardState.from_settings(Settings())
    state.select_vendor("local")

    wizard._ask_for_key(state, interactive=True)  # noqa: SLF001
    state.remember_current_provider()

    assert state.vendor == "local"
    assert state.provider_configs["local"]["base_url"] == "http://localhost:8000/v1"
    assert "preset" not in state.provider_configs["local"]


def test_the_wizard_writes_a_named_server_to_settings(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path)
    state = WizardState.from_settings(Settings())
    state.add_local_server("hon2", label="Hon2")
    state.base_url = "http://hon2.example.com:8000/v1"
    state.variant = "vllm"
    state.model = "flash-next-mtp"
    state.add_current_model_profile(make_default=True)

    settings = state.write(paths, Settings())

    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["providers"]["hon2"]["preset"] == "local"
    assert saved["providers"]["hon2"]["base_url"] == "http://hon2.example.com:8000/v1"
    assert saved["providers"]["default"] == "hon2"
    assert saved["models"]["default"] == "hon2:flash-next-mtp"
    # And the daemon reads it back as a working vendor.
    registry = ProviderRegistry(settings, paths)
    assert registry.is_local_style("hon2")
    assert registry.model_for("hon2") == "flash-next-mtp"


def test_the_wizard_refuses_a_name_that_collides(monkeypatch: pytest.MonkeyPatch) -> None:
    state = WizardState.from_settings(Settings())
    state.add_local_server("hon2")
    monkeypatch.setattr(ui, "is_interactive", lambda *a, **kw: False)
    # already taken → a preset id → a bad shape → give up with an empty line.
    answers = iter(["hon2", "openai", "Hon2", ""])
    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: next(answers, ""))

    assert wizard._ask_for_local_name(state) is None  # noqa: SLF001


def test_the_wizard_removes_a_named_server(monkeypatch: pytest.MonkeyPatch) -> None:
    state = WizardState.from_settings(Settings())
    state.add_local_server("hon2")
    state.model = "flash-next-mtp"
    state.add_current_model_profile(make_default=True)
    state.assign_agent_model("critic", "hon2:flash-next-mtp")
    assert state.agent_models == {"critic": "hon2:flash-next-mtp"}

    monkeypatch.setattr(ui, "is_interactive", lambda *a, **kw: False)
    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: "hon2")

    wizard._remove_local_server(state, state.local_servers())  # noqa: SLF001

    assert "hon2" not in state.provider_configs
    assert state.removed_providers == ["hon2"]
    assert state.model_profiles == {}
    assert state.agent_models == {}


def test_a_removed_server_is_deleted_from_settings(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path)
    settings = _settings(hon2={"preset": "local", "base_url": "http://hon2:8000/v1"})
    settings.providers["default"] = "hon2"
    settings.save(paths)

    state = WizardState.from_settings(Settings.load(paths))
    assert state.local_servers() == ["hon2"]
    state.remove_provider("hon2")
    state.write(paths, Settings.load(paths))

    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert "hon2" not in saved["providers"]


def test_setup_flags_add_a_named_server(tmp_path: Path) -> None:
    result = wizard.run(
        "blank",
        home=tmp_path,
        vendor="hon2",
        base_url="http://hon2.example.com:8000/v1",
        model="flash-next-mtp",
        interactive=False,
    )
    saved = json.loads((result.settings_path).read_text(encoding="utf-8"))
    assert saved["providers"]["hon2"]["preset"] == "local"
    assert saved["models"]["default"] == "hon2:flash-next-mtp"


def test_setup_still_refuses_a_typo_without_a_url(tmp_path: Path) -> None:
    with pytest.raises(wizard.SetupError, match="unknown vendor"):
        wizard.run("blank", home=tmp_path, vendor="antropic", interactive=False)


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_an_old_config_with_only_providers_local_is_untouched(tmp_path: Path) -> None:
    """The whole point of the marker: a file that predates named servers has
    no ``preset`` key anywhere, and must keep behaving exactly as it did."""
    paths = Paths.create(tmp_path)
    document = {
        "providers": {
            "default": "local",
            "local": {"base_url": "http://localhost:8000/v1", "model": "flash-next-mtp"},
        },
        "models": {
            "profiles": {"local:flash-next-mtp": {"provider": "local", "model": "flash-next-mtp"}},
            "default": "local:flash-next-mtp",
        },
    }
    paths.settings_json.write_text(json.dumps(document), encoding="utf-8")

    settings = Settings.load(paths)
    registry = ProviderRegistry(settings, paths)

    assert registry.default_vendor() == "local"
    assert registry.model_for("local") == "flash-next-mtp"
    assert registry.base_url_for("local") == "http://localhost:8000/v1"
    assert registry.is_local_style("local") is True
    assert registry.custom_vendors() == []
    assert registry.local_vendors() == ["local"]
    assert registry.is_configured("local") is True

    # Re-saving through the wizard's state leaves the document as it was.
    state = WizardState.from_settings(settings)
    state.write(paths, settings)
    saved = json.loads(paths.settings_json.read_text(encoding="utf-8"))
    assert saved["providers"]["local"] == document["providers"]["local"]
    assert "preset" not in saved["providers"]["local"]
    assert saved["providers"]["default"] == "local"


# ---------------------------------------------------------------------------
# RPC round trip
# ---------------------------------------------------------------------------


async def test_the_rpc_surface_adds_lists_and_removes_a_named_server(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    """``provider.configure`` writes the block, ``provider.list`` reports it as
    a custom local vendor, and ``provider.remove`` forgets it again."""
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
                    "label": "Hon2",
                    "variant": "vllm",
                    "base_url": "http://hon2.example.com:8000/v1",
                    "model": "flash-next-mtp",
                },
            },
        )
        rows = {row["vendor"]: row for row in (await client.ok("provider.list"))["providers"]}
        assert rows["hon2"]["custom"] is True
        assert rows["hon2"]["preset"] == "local"
        assert rows["hon2"]["configured"] is True
        assert rows["hon2"]["defaultModel"] == "flash-next-mtp"
        assert rows["openai"]["custom"] is False

        saved = json.loads((home / "settings.json").read_text(encoding="utf-8"))
        assert saved["providers"]["hon2"]["preset"] == "local"

        await client.ok("provider.remove", {"vendor": "hon2"})
        after = {row["vendor"] for row in (await client.ok("provider.list"))["providers"]}
        assert "hon2" not in after
        reread = json.loads((home / "settings.json").read_text(encoding="utf-8"))
        assert "hon2" not in reread["providers"]
        await client.stop()
    finally:
        await daemon.stop()


async def test_the_rpc_surface_refuses_an_undeclared_new_vendor(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        client = await connect(http, daemon)
        frame = await client.call(
            "provider.configure",
            {"vendor": "hon2", "config": {"base_url": "http://hon2:8000/v1"}},
        )
        assert frame["error"]["data"]["code"] == "invalid_params"
        # And removing one that was never configured is a clean not_found.
        missing = await client.call("provider.remove", {"vendor": "hon2"})
        assert missing["error"]["data"]["code"] == "not_found"
        await client.stop()
    finally:
        await daemon.stop()
