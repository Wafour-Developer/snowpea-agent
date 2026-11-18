"""Hidden models (protocol 1.60.0, capability ``hiddenModels``).

Owner request: Settings › 모델 lets a person take a model off a provider's
list.  Model lists are mostly live discovery, so taking one off means hiding
it everywhere (``providers.<vendor>.hiddenModels``) — except a hand-added id
(``providers.<vendor>.models``), which is simply unlisted.  Hiding never
rewrites what uses the model and never stops a run.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import httpx
import pytest
from _support import connect, fake_provider, make_daemon

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import models as model_discovery
from snowpea_core.server import errors
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    CAPABILITIES,
    PROTOCOL_VERSION,
    Empty,
    ProviderModelParams,
    ProviderModelsParams,
    SettingsSetParams,
)
from snowpea_core.server.session_handlers import provider_list_handler, provider_models_handler
from snowpea_core.server.settings_handlers import (
    provider_model_remove_handler,
    provider_model_unhide_handler,
    settings_set_handler,
)

FIXTURES = Path(__file__).parent / "fixtures"


class Hub:
    def __init__(self) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any], **_kw: Any) -> None:
        self.notes.append((method, params))

    def of(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.notes if m == method]


def _conn(kind: str = "ide", *, remote: bool = False, version: str = "0.3.0") -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind=kind,
                           remote_login=remote, client_version=version, client_id=f"{kind}-1")


PERSON = _conn()


def _core(tmp_path: Path, document: dict[str, Any] | None = None) -> Any:
    made = Core(settings=Settings.model_validate(document or {}),
                paths=Paths.create(tmp_path / "home"), token="t")
    made.providers.bind(made.settings, made.paths)  # what the daemon does at start
    hub = Hub()
    made.hub.notify = hub.notify  # type: ignore[method-assign]
    made.hub_spy = hub  # type: ignore[attr-defined]
    return made


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("SNOWPEA_MODELS_DEV", "0")
    monkeypatch.delenv("SNOWPEA_PROVIDER", raising=False)
    model_discovery.cache_clear()
    yield
    model_discovery.cache_clear()


async def _remove(core: Any, vendor: str, model: str, conn: Any = PERSON, **extra: Any) -> Any:
    return await provider_model_remove_handler(
        conn, ProviderModelParams(vendor=vendor, model=model, **extra), core)


async def _unhide(core: Any, vendor: str, model: str, conn: Any = PERSON, **extra: Any) -> Any:
    return await provider_model_unhide_handler(
        conn, ProviderModelParams(vendor=vendor, model=model, **extra), core)


def _row(result: Any, vendor: str) -> Any:
    return next(info for info in result.providers if info.vendor == vendor)


def _saved(core: Any) -> dict[str, Any]:
    return json.loads(core.paths.settings_json.read_text(encoding="utf-8"))


# -- the contract ----------------------------------------------------------------------------


def test_protocol_1_60_0_advertises_hidden_models() -> None:
    assert PROTOCOL_VERSION == "1.64.0"
    assert "hiddenModels" in CAPABILITIES


# -- filtering: resolve_models, offline listing, provider.models, provider.list --------------


async def test_resolve_models_drops_hidden_ids_from_the_live_list(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "m-a"}, {"id": "m-b"}, {"id": "m-c"}]})

    listing = await model_discovery.resolve_models(
        "local", base_url="http://127.0.0.1:9/v1", home=tmp_path,
        credentials={"base_url": "http://127.0.0.1:9/v1", "hiddenModels": ["m-b"]},
        transport=httpx.MockTransport(handler),
    )
    assert listing.source == model_discovery.SOURCE_LIVE
    assert listing.models == ["m-a", "m-c"]
    # The cache keeps the vendor's whole answer: unhiding needs no round trip.
    cached = model_discovery.read_cache_file(model_discovery.cache_path(tmp_path, "local", None))
    assert cached == ["m-a", "m-b", "m-c"]


async def test_every_rung_drops_hidden_ids(tmp_path: Path) -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    preset = model_discovery.PRESETS_BY_VENDOR["anthropic"]
    first = list(preset.models)[0]
    curated = await model_discovery.resolve_models(
        "anthropic", api_key="k", credentials={"hiddenModels": [first]}, home=tmp_path,
        transport=httpx.MockTransport(down),
    )
    assert curated.source == model_discovery.SOURCE_CURATED
    assert first not in curated.models and curated.models
    pinned = await model_discovery.resolve_models(
        "anthropic", api_key="k", home=tmp_path, transport=httpx.MockTransport(down),
        credentials={"models": ["x-1", "x-2"], "hiddenModels": ["x-2"]},
    )
    assert pinned.source == model_discovery.SOURCE_SETTINGS and pinned.models == ["x-1"]
    model_discovery.write_cache_file(
        model_discovery.cache_path(tmp_path, "anthropic", None), ["c-1", "c-2"])
    cached = await model_discovery.resolve_models(
        "anthropic", api_key="k", home=tmp_path, transport=httpx.MockTransport(down),
        credentials={"hiddenModels": ["c-1"]},
    )
    assert cached.source == model_discovery.SOURCE_CACHE and cached.models == ["c-2"]
    assert model_discovery.offline_models(
        "anthropic", None, {"hiddenModels": ["c-2"]}, home=tmp_path) == ["c-1"]


def test_hidden_ids_match_exactly_and_keep_their_slash() -> None:
    config = {"hiddenModels": [" openai/gpt-4o ", "openai/gpt-4o", 7, ""]}
    assert model_discovery.hidden_models(config) == ["openai/gpt-4o"]
    ids = ["openai/gpt-4o", "gpt-4o", "anthropic/claude"]
    assert model_discovery.without_hidden(ids, config) == ["gpt-4o", "anthropic/claude"]
    assert model_discovery.hidden_models({"hiddenModels": "gpt-4o"}) == []


async def test_provider_list_and_models_leave_hidden_ids_out(tmp_path: Path) -> None:
    core = _core(tmp_path, {"providers": {"anthropic": {"models": ["a-1", "a-2", "a-3"],
                                                        "hiddenModels": ["a-2"]}}})
    row = _row(await provider_list_handler(PERSON, Empty(), core), "anthropic")
    assert row.models == ["a-1", "a-3"] and row.hiddenModels == ["a-2"]

    async def listing(vendor: str, **_kw: Any) -> Any:
        return await model_discovery.resolve_models(
            vendor, credentials=core.providers.vendor_config(vendor), home=tmp_path,
            transport=httpx.MockTransport(lambda _r: httpx.Response(503)), api_key="k")

    core.providers.model_listing = listing  # type: ignore[method-assign]
    result = await provider_models_handler(PERSON, ProviderModelsParams(vendor="anthropic"), core)
    assert result.models == ["a-1", "a-3"]


async def test_the_tui_model_command_lists_without_hidden_ids(tmp_path: Path) -> None:
    from snowpea_core.providers.registry import ProviderRegistry

    registry = ProviderRegistry(Settings.model_validate(
        {"providers": {"anthropic": {"api_key": "k", "models": ["a-1", "a-2"],
                                     "hiddenModels": ["a-1"]}}}), Paths.create(tmp_path / "h"))
    listing = await registry.model_listing(
        "anthropic", transport=httpx.MockTransport(lambda _r: httpx.Response(503)))
    assert listing.models == ["a-2"]


# -- provider.model.remove / unhide ----------------------------------------------------------


async def test_remove_hides_a_discovered_model_and_unhide_brings_it_back(tmp_path: Path) -> None:
    core = _core(tmp_path)
    assert (await _remove(core, "anthropic", "claude-x")).action == "hidden"
    assert (await _remove(core, "anthropic", "claude-y")).action == "hidden"
    assert (await _remove(core, "anthropic", "claude-x")).action == "hidden"  # idempotent
    assert _saved(core)["providers"]["anthropic"]["hiddenModels"] == ["claude-x", "claude-y"]
    changed = core.hub_spy.of("settings.changed")
    assert changed, "settings.changed fires: the write went through settings.set"
    assert (await _unhide(core, "anthropic", "claude-x")).ok is True
    assert core.settings.providers["anthropic"]["hiddenModels"] == ["claude-y"]
    await _unhide(core, "anthropic", "claude-y")
    assert "hiddenModels" not in core.settings.providers["anthropic"]
    assert (await _unhide(core, "anthropic", "never-hidden")).ok is True


async def test_remove_unlists_a_hand_added_model_instead_of_hiding_it(tmp_path: Path) -> None:
    core = _core(tmp_path, {"providers": {
        "anthropic": {"models": ["h-1", "h-2"]},
        "openai": {"models": {"v-1": {"vision": True}, "v-2": {}}},
        "gemini": {"models": ["only"]},
    }})
    assert (await _remove(core, "anthropic", "h-1")).action == "unlisted"
    assert core.settings.providers["anthropic"]["models"] == ["h-2"]
    assert "hiddenModels" not in core.settings.providers["anthropic"]
    assert (await _remove(core, "openai", "v-1")).action == "unlisted"
    assert core.settings.providers["openai"]["models"] == {"v-2": {}}
    assert (await _remove(core, "gemini", "only")).action == "unlisted"
    assert "models" not in core.settings.providers["gemini"]


async def test_remove_refuses_an_unknown_vendor_and_a_blank_model(tmp_path: Path) -> None:
    core = _core(tmp_path)
    with pytest.raises(RpcError) as caught:
        await _remove(core, "no-such-vendor", "m")
    assert caught.value.code == errors.NOT_FOUND
    for model in ("", "   ", "x" * 300, "a\nb"):
        with pytest.raises(RpcError) as caught:
            await _remove(core, "anthropic", model)
        assert caught.value.code == errors.INVALID_PARAMS


# -- in use: flagged, never rewritten, still runs --------------------------------------------


async def test_hidden_models_in_use_are_flagged_and_left_alone(tmp_path: Path) -> None:
    core = _core(tmp_path, {
        "providers": {"openai": {"api_key": "k", "model": "gpt-v"}},
        "models": {
            "default": "main",
            "profiles": {"main": {"provider": "openai", "model": "gpt-d"},
                         "quick": {"provider": "openai", "model": "gpt-f"}},
            "categories": {"fast": "quick", "deep": "openai:gpt-d"},
        },
        "agents": {"models": {"reviewer": "quick"}},
    })
    running = SimpleNamespace(id="s-1", turn_live=True, provider="openai", model="gpt-r")
    idle = SimpleNamespace(id="s-2", turn_live=False, provider="openai", model="gpt-r")
    core.sessions.all = lambda: [running, idle]  # type: ignore[method-assign]
    before = core.settings.model_dump(mode="json")
    for model in ("gpt-d", "gpt-f", "gpt-r", "gpt-unused"):
        assert (await _remove(core, "openai", model)).action == "hidden"
    after = core.settings.model_dump(mode="json")
    assert after["models"] == before["models"] and after["agents"] == before["agents"]
    row = _row(await provider_list_handler(PERSON, Empty(), core), "openai")
    uses = {use.model: use.usedBy for use in row.hiddenInUse}
    assert uses == {
        "gpt-d": ["default", "category:deep"],
        "gpt-f": ["category:fast", "agent:reviewer"],
        "gpt-r": ["session:s-1"],
    }
    assert row.defaultModel == "gpt-d"
    # A hidden default still runs: nothing on the request path consults the list.
    assert await core.providers.resolve_model("openai") == "gpt-d"
    assert core.providers.build("openai").model == "gpt-d"


async def test_a_session_on_a_hidden_default_model_still_runs(tmp_path: Path) -> None:
    settings = {"providers": {"openai": {"api_key": "k", "model": "gpt-hidden"}}}
    with fake_provider(FIXTURES / "providers" / "fake" / "basic.json"):
        daemon = await make_daemon(tmp_path / "home", settings)
        try:
            async with aiohttp.ClientSession() as http:
                client = await connect(http, daemon)
                try:
                    removed = await client.ok(
                        "provider.model.remove", {"vendor": "openai", "model": "gpt-hidden"})
                    assert removed == {"action": "hidden"}
                    listed = await client.ok("provider.list")
                    row = next(p for p in listed["providers"] if p["vendor"] == "openai")
                    assert row["hiddenModels"] == ["gpt-hidden"]
                    assert row["hiddenInUse"] == [{"model": "gpt-hidden", "usedBy": ["default"]}]
                    await client.wait_notification("settings.changed")
                    created = await client.ok(
                        "session.create", {"workdir": str(tmp_path / "work"), "mode": "auto"})
                    turn = await client.ok(
                        "session.prompt", {"sessionId": created["sessionId"], "text": "hello"})
                    assert await client.wait_turn(turn["turnId"]) == "complete"
                    assert daemon.core is not None
                    assert daemon.core.settings.providers["openai"]["model"] == "gpt-hidden"
                finally:
                    await client.stop()
        finally:
            await daemon.stop()


# -- settings.set: the value, the scope, who may write it ------------------------------------


async def _set(core: Any, conn: Any, patch: dict[str, Any], **extra: Any) -> Any:
    return await settings_set_handler(conn, SettingsSetParams(patch=patch, **extra), core)


async def test_settings_set_validates_hidden_models(tmp_path: Path) -> None:
    core = _core(tmp_path)
    await _set(core, PERSON, {"providers": {"anthropic": {"hiddenModels": ["a", "b"]}}})
    assert core.settings.providers["anthropic"]["hiddenModels"] == ["a", "b"]
    for bad in ("a", [1], [""], ["x" * 300], {"a": True}):
        with pytest.raises(RpcError) as caught:
            await _set(core, PERSON, {"providers": {"anthropic": {"hiddenModels": bad}}})
        assert caught.value.code == errors.INVALID_PARAMS
        assert caught.value.data["key"] == "providers.anthropic.hiddenModels"
    await _set(core, PERSON, {"providers": {"anthropic": {"hiddenModels": None}}})
    assert "hiddenModels" not in core.settings.providers["anthropic"]


async def test_the_project_scope_cannot_hide_a_model(tmp_path: Path) -> None:
    core = _core(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(RpcError) as caught:
        await _set(core, PERSON, {"providers": {"anthropic": {"hiddenModels": ["a"]}}},
                   scope="project", workdir=str(work))
    assert caught.value.code == errors.INVALID_PARAMS
    assert caught.value.data == {"reason": "global_only",
                                 "key": "providers.anthropic.hiddenModels"}
    for conn in (_conn("browser"), _conn("web", remote=True)):
        with pytest.raises(RpcError):
            await _set(core, conn, {"providers": {"anthropic": {"hiddenModels": ["a"]}}},
                       scope="project", workdir=str(work), actor="person")


async def test_a_marked_browser_and_a_remote_login_write_hidden_and_hand_added_models(
    tmp_path: Path,
) -> None:
    core = _core(tmp_path)
    browser = _conn("browser")
    await _set(core, browser, {"providers": {"anthropic": {"hiddenModels": ["a"],
                                                           "models": ["h"]}}}, actor="person")
    remote = _conn("web", remote=True)
    await _set(core, remote, {"providers": {"anthropic": {"hiddenModels": ["a", "b"]}}})
    assert core.settings.providers["anthropic"] == {"hiddenModels": ["a", "b"], "models": ["h"]}
    assert (await _remove(core, "anthropic", "c", browser, actor="person")).action == "hidden"
    assert (await _remove(core, "anthropic", "h", remote)).action == "unlisted"
    await _unhide(core, "anthropic", "a", browser, actor="person")
    await _unhide(core, "anthropic", "b", remote)
    assert core.settings.providers["anthropic"]["hiddenModels"] == ["c"]


async def test_the_rpcs_refuse_unmarked_and_legacy_browsers_connectors_and_in_process(
    tmp_path: Path,
) -> None:
    core = _core(tmp_path)
    refused = (
        (_conn("browser"), {}),
        (_conn("browser", version="0.2.0"), {"actor": "person"}),
        (_conn("browser", version="0.2.0"), {}),
        (_conn("connector"), {}),
        (None, {}),
    )
    for conn, extra in refused:
        for call in (_remove, _unhide):
            with pytest.raises(RpcError) as caught:
                await call(core, "anthropic", "m", conn, **extra)
            assert caught.value.code == errors.JARVIS_PERSON_ONLY, (conn, extra)
    assert "anthropic" not in core.settings.providers
    # An unmarked browser's settings.set of the key is refused too.
    with pytest.raises(RpcError) as caught:
        await _set(core, _conn("browser"), {"providers": {"anthropic": {"hiddenModels": ["a"]}}})
    assert caught.value.code == errors.JARVIS_PERSON_ONLY


async def test_jarvis_on_keeps_its_own_provider_a_local_persons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from snowpea_core.server import settings_gate

    core = _core(tmp_path)
    monkeypatch.setattr(settings_gate, "jarvis_authority",
                        lambda _core: (("providers", "anthropic"),))
    with pytest.raises(RpcError) as caught:
        await _remove(core, "anthropic", "m", _conn("web", remote=True))
    assert caught.value.data["reason"] == "jarvis_on"
    assert (await _remove(core, "anthropic", "m")).action == "hidden"


# -- the CLI: snowpea provider models list|hide|unhide, provider remove --------------------


async def _cli(home: Path, *argv: str) -> int:
    from snowpea_core.cli import commands as cli_commands
    from snowpea_core.cli.main import build_parser

    return await cli_commands.dispatch(build_parser().parse_args(list(argv)), home)


async def test_the_cli_hides_lists_and_unhides(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    settings = {"providers": {"anthropic": {"api_key": "k", "models": ["hand-1", "hand-2"],
                                            "model": "hand-2"}}}
    daemon = await make_daemon(tmp_path / "home", settings)
    home = daemon.paths.home
    try:
        assert await _cli(home, "provider", "models", "hide", "anthropic", "hand-1") == 0
        assert "taken off the hand-added list" in capsys.readouterr().out
        assert await _cli(home, "provider", "models", "hide", "anthropic", "claude-z") == 0
        assert "claude-z hidden" in capsys.readouterr().out
        assert await _cli(home, "provider", "models", "hide", "anthropic", "hand-2") == 0
        capsys.readouterr()
        assert await _cli(home, "provider", "models", "hide", "anthropic", "zz") == 0
        capsys.readouterr()
        # hand-2 was the only hand-added id left, so it was unlisted, not hidden.
        assert daemon.core is not None
        block = daemon.core.settings.providers["anthropic"]
        assert block["hiddenModels"] == ["claude-z", "zz"] and "models" not in block
        assert await _cli(home, "provider", "models", "list", "anthropic", "--hidden") == 0
        out = capsys.readouterr().out
        assert "anthropic: claude-z" in out and "anthropic: zz" in out
        assert await _cli(home, "provider", "models", "unhide", "anthropic", "zz") == 0
        assert "zz shown again" in capsys.readouterr().out
        assert await _cli(home, "provider", "models", "list", "--hidden", "--json") == 0
        rows = json.loads(capsys.readouterr().out)
        assert rows == [{"vendor": "anthropic", "hiddenModels": ["claude-z"], "hiddenInUse": []}]
        assert await _cli(home, "provider", "models", "hide", "anthropic") == 2
    finally:
        await daemon.stop()


async def test_the_cli_asks_before_it_disconnects_a_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    daemon = await make_daemon(tmp_path / "home", {"providers": {"anthropic": {"api_key": "k"}}})
    home = daemon.paths.home
    try:
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert await _cli(home, "provider", "remove", "anthropic") == 2
        assert "--yes" in capsys.readouterr().err
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        assert await _cli(home, "provider", "remove", "anthropic") == 2
        out = capsys.readouterr().out
        assert "fall back to the default model" in out and "nothing was removed" in out
        assert daemon.core is not None and "anthropic" in daemon.core.settings.providers
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")
        assert await _cli(home, "provider", "remove", "anthropic") == 0
        assert "anthropic" not in daemon.core.settings.providers
        daemon.core.settings.providers["openai"] = {"api_key": "k"}
        assert await _cli(home, "provider", "remove", "openai", "--yes") == 0
    finally:
        await daemon.stop()
