"""Security review 8, part B — settings: endpoint fields by key shape, provider
accounts, the home by identity, project files never written over, and a
settings file that does not validate failing CLOSED.

* H8-1: an endpoint-shaped field of a free-form block (``search.credentials.*``,
  ``browser.credentials.*``, ``providers.*``, ``gateway.*``) is an ENDPOINT — a
  remote login never writes one; the browser's Settings page keeps its rule.
  The leaf test walks what the search and browser providers READ, so a provider
  that starts reading a new key as an address fails here until it is covered.
* M-1: a provider's key or sign-in record is a local person's for a remote login.
* H8-3: the home is judged by identity (a case-insensitive file system is
  simulated by standing in for ``pathcase.same_file``: real macOS was not run);
  ``ProjectSettings.save`` refuses the global file by identity at write time and
  a project file that does not validate.
* Low: a ``security`` section that does not validate fails closed; ``web`` keeps
  remote access off; the degraded state is surfaced.

Every URL here is DATA: nothing is fetched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config import project as project_config
from snowpea_core.config.paths import Paths
from snowpea_core.config.project import ProjectSettings, ProjectSettingsUnsafe, home_problem
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors, settings_gate, settings_handlers
from snowpea_core.server import settings_leaves as leaves
from snowpea_core.server.app_server import Core, provider_configure_handler
from snowpea_core.server.errors import DETAIL_REASONS, RpcError
from snowpea_core.server.protocol import ProviderConfigureParams, SettingsSetParams
from snowpea_core.tools import browser_providers, search_providers
from snowpea_core.util import pathcase

pytestmark = pytest.mark.real_mode_guard

EVIL = "https://collector.invalid/v1"


def _conn(kind: str = "ide", *, remote: bool = False, version: str = "0.3.0",
          client_id: str | None = None, closed: bool = False) -> Any:
    return SimpleNamespace(authenticated=True, closed=closed, client_kind=kind,
                           remote_login=remote, client_version=version,
                           client_id=client_id or f"{kind}-1", surface_id=f"{kind}-s")


#: Every connection kind the gates tell apart: name -> (connection, actor).
CALLERS: dict[str, tuple[Any, str | None]] = {
    "person": (_conn("ide"), None),
    "remote_login": (_conn("web", remote=True), None),
    "connector": (_conn("connector"), None),
    "legacy": (_conn("browser", version="0.2.0", client_id="b-legacy"), None),
    "browser": (_conn("browser", client_id="b-1"), None),
    "browser+actor": (_conn("browser", client_id="b-1"), "person"),
    "closed": (_conn("ide", closed=True), None),
    "none": (None, None),
}
#: Who may write an ENDPOINT-shaped field (global scope): a local person, and the
#: browser's Settings page as before (marker, or a legacy browser).
ENDPOINT_WRITERS = {"person", "legacy", "browser+actor"}


def _gate(kind: str, patch: dict[str, Any], *, project: bool = False) -> str:
    conn, actor = CALLERS[kind]
    core = SimpleNamespace(settings=Settings(), paths=SimpleNamespace(home=Path("/srv/sp-home")))
    try:
        settings_handlers.guard_patch(conn, patch, project=project, actor=actor, core=core)
    except RpcError as exc:
        assert exc.code in (errors.JARVIS_PERSON_ONLY, errors.INVALID_PARAMS), (kind, exc.code)
        return "refused"
    return "ok"


def _nest(dotted: str, value: Any) -> dict[str, Any]:
    node: Any = value
    for part in reversed(dotted.split(".")):
        node = {part: node}
    return node


# -- H8-1: endpoint-shaped fields -----------------------------------------------------------

#: Field names shaped like an address, and some that are not.
SHAPED = ["url", "URL", "base_url", "baseUrl", "SEARXNG_URL", "firecrawl_url", "endpoint",
          "apiEndpoint", "host", "proxy", "https_proxy", "verification_uri"]
NOT_SHAPED = ["api_key", "apiKey", "model", "project_id", "token", "region"]


@pytest.mark.parametrize("name", SHAPED)
def test_an_address_shaped_name_is_an_endpoint(name: str) -> None:
    assert leaves.endpoint_key(name)


@pytest.mark.parametrize("name", NOT_SHAPED)
def test_other_names_are_not(name: str) -> None:
    assert not leaves.endpoint_key(name)


@pytest.mark.parametrize("kind", sorted(CALLERS))
@pytest.mark.parametrize("block", sorted(leaves.ENDPOINT_SHAPED))
@pytest.mark.parametrize("name", ["url", "baseUrl", "SEARXNG_URL", "endpoint", "proxy"])
def test_who_may_write_an_endpoint_field(block: str, name: str, kind: str) -> None:
    dotted = block.replace("*", "x") + "." + name
    assert leaves.ENDPOINT_SHAPED[block] == leaves.BROWSER_PAGE
    want = "ok" if kind in ENDPOINT_WRITERS else "refused"
    assert _gate(kind, _nest(dotted, EVIL)) == want, (dotted, kind)
    # Nobody but a local person writes it in the project scope.
    assert _gate(kind, _nest(dotted, EVIL), project=True) == (
        "ok" if kind == "person" else "refused"), (dotted, kind)


def test_a_remote_login_still_writes_the_rest_of_a_credentials_block() -> None:
    for patch in ({"search": {"credentials": {"tavily": {"api_key": "k"}}}},
                  {"browser": {"credentials": {"browserbase": {"api_key": "k"}}}},
                  {"search": {"provider": "searxng"}},
                  {"search": {"credentials": {"searxng": None}}}):  # only takes away
        assert _gate("remote_login", patch) == "ok", patch


def test_the_refusal_names_the_key_and_says_local_only() -> None:
    conn, _ = CALLERS["remote_login"]
    with pytest.raises(RpcError) as caught:
        settings_handlers.guard_patch(
            conn, {"search": {"credentials": {"searxng": {"url": EVIL}}}}, project=False,
            core=SimpleNamespace(settings=Settings(), paths=None))
    assert caught.value.data["reason"] == "local_only"
    assert caught.value.data["keys"] == ["search.credentials.searxng.url"]


class Reads(dict[str, Any]):
    """A credentials block that records every key a provider asks it for."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.asked: set[str] = set()

    def get(self, key: Any, default: Any = None) -> Any:
        self.asked.add(str(key))
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        self.asked.add(str(key))
        return super().__getitem__(key)


def _search_settings(provider_id: str, block: dict[str, Any]) -> Any:
    return SimpleNamespace(search=SimpleNamespace(credentials={provider_id: block}))


def _browser_settings(provider_id: str, block: dict[str, Any]) -> Any:
    return SimpleNamespace(browser=SimpleNamespace(credentials={provider_id: block}))


def _search_endpoint(provider: Any, settings: Any) -> str | None:
    """Where ``provider`` would send its requests with ``settings``."""
    creds = search_providers.credentials_for(provider.meta, settings)
    base = getattr(provider, "_base", None)
    if callable(base):
        provider.bind(settings)
        try:
            return base()
        finally:
            provider.bind(None)
    return creds.base_url


def _endpoint_reads() -> list[tuple[str, str, str]]:
    """``(block, provider id, key)`` for every key a provider reads AS AN ADDRESS."""
    found: list[tuple[str, str, str]] = []
    for provider in search_providers.all_providers():
        probe = Reads()
        search_providers.credentials_for(provider.meta, _search_settings(provider.meta.id, probe))
        for key in sorted(probe.asked):
            got = _search_endpoint(provider, _search_settings(provider.meta.id, {key: EVIL}))
            if got and got.startswith(EVIL.rstrip("/")):
                found.append(("search.credentials.*", provider.meta.id, key))
    for meta in browser_providers.metas():
        probe = Reads()
        browser_providers.credentials_for(meta.id, _browser_settings(meta.id, probe))
        for key in sorted(probe.asked):
            settings = _browser_settings(meta.id, {key: EVIL})
            if browser_providers.credentials_for(meta.id, settings).get("base_url") == EVIL:
                found.append(("browser.credentials.*", meta.id, key))
    return found


def test_the_walk_finds_what_providers_read_as_an_address(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("SEARXNG_URL", "FIRECRAWL_URL", "FIRECRAWL_CLOUD_URL"):
        monkeypatch.delenv(name, raising=False)
    found = _endpoint_reads()
    keys = {(block, key) for block, _pid, key in found}
    # The ones known today: any change shows up here and in the next test.
    assert {("search.credentials.*", "url"), ("search.credentials.*", "base_url"),
            ("search.credentials.*", "baseUrl")} <= keys
    assert {pid for block, pid, _ in found if block == "search.credentials.*"} >= {
        "searxng", "firecrawl_selfhost", "firecrawl"}


def test_every_key_a_provider_reads_as_an_address_is_an_endpoint(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only the schema: the providers' own reads.  A provider that starts
    reading a new key as its address fails here until that key is classified."""
    for name in ("SEARXNG_URL", "FIRECRAWL_URL", "FIRECRAWL_CLOUD_URL"):
        monkeypatch.delenv(name, raising=False)
    unclassified = []
    for block, provider_id, key in _endpoint_reads():
        if block not in leaves.ENDPOINT_SHAPED or not leaves.endpoint_key(key):
            unclassified.append((block, provider_id, key))
            continue
        dotted = block.replace("*", provider_id) + "." + key
        assert _gate("remote_login", _nest(dotted, EVIL)) == "refused", dotted
    assert not unclassified, unclassified


def test_an_unclassified_free_form_block_fails() -> None:
    """The schema walk: a new ``dict[str, Any]`` block is caught until FREE_FORM
    says what governs it."""
    from pydantic import BaseModel, Field

    class Telemetry(BaseModel):
        exporters: dict[str, Any] = Field(default_factory=dict)

    class Grown(Settings):
        telemetry: Telemetry = Field(default_factory=Telemetry)

    found = leaves.free_form_leaves(Grown)
    assert "telemetry.exporters.*" in found and "telemetry.exporters.*" not in leaves.FREE_FORM


def test_every_free_form_block_is_classified() -> None:
    schema = leaves.free_form_leaves()
    assert schema == set(leaves.FREE_FORM), (
        f"classify in settings_leaves.FREE_FORM: {sorted(schema - set(leaves.FREE_FORM))}; "
        f"stale: {sorted(set(leaves.FREE_FORM) - schema)}")


def test_the_other_open_blocks_are_known() -> None:
    """audio, media, telemetry, updates, tools.*: no free-form endpoint block today."""
    schema = leaves.free_form_leaves()
    for section in ("audio", "media", "telemetry", "updates", "tools"):
        assert not [leaf for leaf in schema if leaf.split(".")[0] == section], section


# -- M-1: a provider's account ---------------------------------------------------------------


@pytest.mark.parametrize("field", settings_gate.PROVIDER_CREDENTIAL_FIELDS)
@pytest.mark.parametrize("kind", sorted(CALLERS))
def test_who_may_set_a_providers_account_by_settings_set(field: str, kind: str) -> None:
    want = "ok" if kind in ("person", "legacy", "browser+actor") else "refused"
    assert _gate(kind, {"providers": {"openai": {field: "k"}}}) == want, (field, kind)


@pytest.fixture
def core(tmp_path: Path) -> Core:
    return Core(settings=Settings(), paths=Paths.create(tmp_path / "dot" / ".snowpea"), token="t")


@pytest.mark.parametrize(
    ("kind", "config", "refused"),
    [
        ("remote_login", {"api_key": "k"}, True),
        ("remote_login", {"access_token": "t", "auth_method": "chatgpt"}, True),
        ("remote_login", {"refresh_token": "t"}, True),
        ("remote_login", {"model": "m"}, False),
        ("remote_login", {"api_key": None}, False),  # clearing only takes away
        ("person", {"api_key": "k"}, False),
        ("browser+actor", {"api_key": "k"}, False),
    ],
)
async def test_provider_configure_with_a_credential(
    core: Core, monkeypatch: pytest.MonkeyPatch, kind: str, config: dict[str, Any],
    refused: bool
) -> None:
    from snowpea_core.server import app_server

    saved: list[dict[str, Any]] = []

    async def persist(_core: Any, _vendor: str, cfg: dict[str, Any]) -> None:
        saved.append(cfg)

    monkeypatch.setattr(app_server, "_persist_provider", persist)
    conn, actor = CALLERS[kind]
    call = provider_configure_handler(
        conn, ProviderConfigureParams(vendor="openai", config=config, actor=actor), core)
    if not refused:
        await call
        assert saved == [config]
        return
    with pytest.raises(RpcError) as caught:
        await call
    assert caught.value.data == {"reason": "local_only", "kind": "remote_login",
                                 "what": "credential"}
    assert saved == []


def test_the_leaf_table_says_the_same() -> None:
    assert leaves.COMMAND_LIKE["providers.*.api_key"] == (leaves.SECRET, leaves.BROWSER_PAGE)


# -- H8-3: the home by identity --------------------------------------------------------------


def _fold_same(a: Any, b: Any) -> bool:
    """A case- and Unicode-insensitive file system, as macOS APFS answers."""
    return pathcase.fold_text(os.path.normpath(str(a))) == pathcase.fold_text(
        os.path.normpath(str(b)))


def test_the_home_by_another_spelling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "Users" / "me" / ".snowpea"
    home.mkdir(parents=True)
    shouting = Path(str(tmp_path / "USERS" / "ME"))
    # On Linux the spelling is another folder: no problem.
    assert home_problem(shouting, home) is None
    monkeypatch.setattr(pathcase, "same_file", _fold_same)
    assert home_problem(shouting, home) == "parent"
    assert home_problem(shouting / ".SNOWPEA", home) == "home"
    assert home_problem(shouting / ".snowpéa", home) is None  # another name entirely
    assert home_problem(tmp_path / "work", home) is None


async def test_settings_set_by_another_spelling_is_refused(
    core: Core, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path(core.paths.home)
    core.settings.save(core.paths)
    before = core.paths.settings_json.read_bytes()
    monkeypatch.setattr(pathcase, "same_file", _fold_same)
    upper = Path(str(home.parent).upper())
    for kind in sorted(CALLERS):
        conn, actor = CALLERS[kind]
        params = SettingsSetParams(scope="project", workdir=str(upper),
                                   patch={"defaultMode": "plan"}, actor=actor)
        with pytest.raises(RpcError) as caught:
            await settings_handlers.settings_set_handler(conn, params, core)
        assert caught.value.code == errors.INVALID_PARAMS, kind
        assert caught.value.data["reason"] == "snowpea_home", kind
    assert core.paths.settings_json.read_bytes() == before


def test_save_refuses_the_global_file_by_identity(tmp_path: Path) -> None:
    """A hard link is the same file by another name — realpath does not see it."""
    home = tmp_path / "home" / ".snowpea"
    paths = Paths.create(home)
    project_config.register_home(home)
    Settings().save(paths)
    before = paths.settings_json.read_bytes()
    project = tmp_path / "proj"
    (project / ".snowpea").mkdir(parents=True)
    os.link(paths.settings_json, project / ".snowpea" / "settings.json")
    assert home_problem(project) is None  # by path it looks like a project
    with pytest.raises(ProjectSettingsUnsafe) as caught:
        ProjectSettings(defaultMode="auto").save(project)
    assert caught.value.reason == "snowpea_home"
    assert paths.settings_json.read_bytes() == before


@pytest.mark.parametrize("text", ['{"defaultMode": "sideways"}', "[1, 2]", "{not json",
                                  '{"allowlist": [{"nope": 1}]}'])
async def test_an_invalid_project_file_is_never_written_over(
    core: Core, tmp_path: Path, text: str
) -> None:
    project = tmp_path / "proj"
    (project / ".snowpea").mkdir(parents=True)
    path = project / ".snowpea" / "settings.json"
    path.write_text(text, "utf-8")
    assert ProjectSettings.load(project) == ProjectSettings()  # read as defaults ...
    with pytest.raises(ProjectSettingsUnsafe) as unsafe:
        ProjectSettings(defaultMode="plan").save(project)  # ... but never written so
    assert unsafe.value.reason == "invalid"
    conn, _ = CALLERS["person"]
    params = SettingsSetParams(scope="project", workdir=str(project),
                               patch={"defaultMode": "plan"})
    with pytest.raises(RpcError) as caught:
        await settings_handlers.settings_set_handler(conn, params, core)
    assert caught.value.code == errors.INVALID_PARAMS
    assert caught.value.data == {"reason": "project_settings_invalid", "field": "workdir"}
    assert path.read_text("utf-8") == text


def test_a_valid_project_file_is_written_as_before(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    ProjectSettings(defaultMode="plan").save(project)
    ProjectSettings(defaultMode="accept").save(project)
    assert ProjectSettings.load(project).defaultMode == "accept"


def test_the_new_reason_is_enumerated() -> None:
    assert DETAIL_REASONS["project_settings_invalid"][0] == errors.INVALID_PARAMS


def test_fs_folding_is_the_shared_one() -> None:
    from snowpea_core.server import fs_handlers

    assert fs_handlers._fold("Ｃafé") == pathcase.fold_text("Ｃafé")


# -- Low: a settings file that does not validate fails closed --------------------------------


def _load(tmp_path: Path, text: str) -> Settings:
    paths = Paths.create(tmp_path / "home")
    paths.settings_json.write_text(text, "utf-8")
    return Settings.load(paths)


def test_an_invalid_security_section_fails_closed(tmp_path: Path) -> None:
    loaded = _load(tmp_path, json.dumps({"security": {"blockedCommands": "everything"},
                                         "agent": {"busy": "queue"}}))
    assert loaded.degraded_sections() == ["security"]
    # Security review 9, F3: the lists a fresh install has, not every preset.
    assert loaded.security.blockedCommands.presets == ["essential"]
    assert loaded.security.protectedPaths.presets == ["essential"]
    assert loaded.security.unknownCode == "ask"
    assert loaded.agent.busy == "queue"  # the rest is kept


def test_an_invalid_web_section_keeps_remote_access_off(tmp_path: Path) -> None:
    loaded = _load(tmp_path, json.dumps({"web": {"remote": {"enabled": True, "port": "x"}}}))
    assert loaded.degraded_sections() == ["web"]
    assert loaded.web.remote.enabled is False


@pytest.mark.parametrize("text", ["{not json", "[]", '"x"'])
def test_an_unreadable_file_is_every_section(tmp_path: Path, text: str) -> None:
    loaded = _load(tmp_path, text)
    assert loaded.degraded_sections() == ["*"]
    assert loaded.security.blockedCommands.presets == ["essential"]
    assert loaded.web.remote.enabled is False
    assert (tmp_path / "home" / "settings.json.invalid").read_text("utf-8") == text


def test_a_valid_file_is_not_degraded(tmp_path: Path) -> None:
    loaded = _load(tmp_path, json.dumps({"security": {"unknownCode": "allow"}}))
    assert loaded.degraded_sections() == []
    assert loaded.security.unknownCode == "allow"


async def test_the_degraded_state_is_surfaced(tmp_path: Path) -> None:
    from snowpea_core.server.app_server import info_handler
    from snowpea_core.server.protocol import (
        PROTOCOL_VERSION,
        Empty,
        HelloParams,
        SecurityGetParams,
    )
    from snowpea_core.server.rpc import RpcConnection
    from snowpea_core.server.security_handlers import get_handler
    from snowpea_core.server.transport_ws import hello_handler
    from snowpea_core.server.web_remote_handlers import _status

    async def nothing(_frame: dict[str, Any]) -> None:
        return None

    paths = Paths.create(tmp_path / "home")
    paths.settings_json.write_text(json.dumps({"security": {"blockedCommands": 7}}), "utf-8")
    core = Core(settings=Settings.load(paths), paths=paths, token="t")
    person = RpcConnection(nothing)
    hello = await hello_handler(person, HelloParams(
        token="t", clientKind="ide", clientVersion="1.0.0", protocolVersion=PROTOCOL_VERSION),
        core)
    assert hello.settingsDegraded == ["security"]
    assert (await info_handler(person, Empty(), core)).settingsDegraded == ["security"]
    assert (await _status(core)).settingsDegraded == ["security"]
    got = await get_handler(person, SecurityGetParams(), core)
    assert any("security section of settings.json is not valid" in p for p in got.problems)
    remote = RpcConnection(nothing)
    remote.remote_login = True
    remote.remote_capability = lambda _token: True
    hello = await hello_handler(remote, HelloParams(
        token="cap", clientKind="web", clientVersion="1.0.0", protocolVersion=PROTOCOL_VERSION),
        core)
    assert hello.settingsDegraded == []  # a local person's surfaces only
