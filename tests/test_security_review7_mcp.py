"""Security review 7, part B — H-3: stored credentials go only where a local person sent them.

An MCP entry's ``${NAME}`` is filled from ``credentials.json`` and then the
daemon's environment when the server starts.  A remote web login could add a
url server whose header carried ``${SOME_TOKEN}``, or point a saved server's
url at its own host, and the daemon sent the secret there.

Now, for a remote login: an entry with a ``${VAR}`` is refused, and so is any
change to a SAVED entry's ``url``, ``headers`` or ``oauth`` — through
``mcp.add``, ``mcp.update``, ``mcp.test`` with overrides, ``settings.set
mcp.servers.*`` and the ``/mcp`` commands; probing a saved entry unchanged
stays allowed.  For everyone: no entry expands a ``SNOWPEA_*`` environment
variable, and an entry a remote login saved (``savedBy``) never expands a
``credentials.json`` value.

The secrets below are made-up strings; nothing here opens a connection — the
probe and the server start are replaced by recorders.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.credentials import CredentialStore
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors, mcp_handlers, settings_gate, settings_handlers
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    McpAddParams,
    McpEntryFields,
    McpTestParams,
    McpUpdateParams,
    SettingsSetParams,
)
from snowpea_core.tools import mcp_client, mcp_config
from snowpea_core.tools.mcp_client import McpError, McpServerConfig, resolve_config

pytestmark = pytest.mark.real_mode_guard

SECRET = "tok-7f3a-not-a-real-secret"
OWN = "https://mcp.example.com/sse"
ELSEWHERE = "https://collector.example.net/in"
WITH_VAR = {"Authorization": "Bearer ${TELEGRAM_BOT_TOKEN}"}


def _conn(kind: str | None = "ide", *, remote: bool = False, version: str = "0.3.0",
          client_id: str | None = None, closed: bool = False) -> Any:
    return SimpleNamespace(authenticated=True, closed=closed, client_kind=kind,
                           remote_login=remote, client_version=version,
                           client_id=client_id or f"{kind}-1", surface_id=f"{kind}-s")


PERSON = _conn("ide")
REMOTE = _conn("web", remote=True)
#: name -> (connection, actor): every kind the gates tell apart.
CALLERS: dict[str, tuple[Any, str | None]] = {
    "person": (PERSON, None),
    "remote_login": (REMOTE, None),
    "connector": (_conn("connector"), None),
    "legacy": (_conn("browser", version="0.2.0", client_id="b-legacy"), None),
    "browser": (_conn("browser", client_id="b-1"), None),
    "browser+actor": (_conn("browser", client_id="b-1"), "person"),
    "closed": (_conn("ide", closed=True), None),
    "none": (None, None),
}


@pytest.fixture
def core(tmp_path: Path) -> Core:
    made = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    CredentialStore(made.paths).save({"TELEGRAM_BOT_TOKEN": SECRET})
    return made


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Nothing connects: what would be probed or started is recorded, resolved."""
    seen: list[Any] = []

    async def probe(config: Any) -> tuple[bool, list[Any], str | None, int]:
        seen.append(config)
        return True, [], None, 1

    async def register(_core: Any, config: Any) -> None:
        seen.append(config)

    monkeypatch.setattr(mcp_handlers, "probe", probe)
    monkeypatch.setattr(mcp_client, "register_config", register)
    mcp_client.MANAGER.listeners = []
    return seen


def _local_only(caught: pytest.ExceptionInfo[RpcError], what: str) -> None:
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert caught.value.data["reason"] == "local_only"
    assert caught.value.data["kind"] == "remote_login"
    assert caught.value.data["what"] == what


def _nothing_left(configs: list[Any], home: Path) -> None:
    """No recorded config would send the stored secret anywhere but where the
    owner saved it for (``OWN``)."""
    for config in configs:
        if not isinstance(config, McpServerConfig):
            continue
        try:
            resolved = resolve_config(config, home)
        except McpError:
            continue  # it does not start at all
        carried = str(resolved.headers) + str(resolved.env) + str(resolved.args)
        assert SECRET not in carried or resolved.url == OWN, resolved.url


# -- the rule, as a table ----------------------------------------------------------------------

SAVED = {"url": OWN, "headers": WITH_VAR}


@pytest.mark.parametrize(
    ("entry", "saved", "what"),
    [
        # New entries: a url server is a remote login's to add — without a ${VAR}.
        ({"url": OWN}, None, None),
        ({"url": OWN, "headers": {"X-Plain": "v"}}, None, None),
        ({"url": OWN, "headers": WITH_VAR}, None, "variable"),
        ({"url": ELSEWHERE, "headers": WITH_VAR}, None, "variable"),
        ({"url": OWN, "oauth": {"clientId": "c", "clientSecret": "${TELEGRAM_BOT_TOKEN}"}},
         None, "variable"),
        ({"url": OWN, "headers": {"X": "$${LITERAL}"}}, None, None),  # the literal text
        # A saved entry: unchanged (a probe, a re-save, switching it off) is fine ...
        (SAVED, SAVED, None),
        ({**SAVED, "disabled": True, "timeoutSec": 5}, SAVED, None),
        # ... where its requests go, and what they carry, is a local person's.
        ({**SAVED, "url": ELSEWHERE}, SAVED, "target"),
        ({"url": ELSEWHERE}, SAVED, "target"),
        ({**SAVED, "headers": {**WITH_VAR, "X-Extra": "v"}}, SAVED, "target"),
        ({"url": OWN}, SAVED, "target"),  # dropping its headers is a change too
        ({**SAVED, "oauth": {"clientId": "c"}}, SAVED, "target"),
        ({"url": OWN}, {"url": OWN}, None),
        ({"url": ELSEWHERE}, {"url": OWN}, "target"),
    ],
)
def test_what_a_remote_login_may_do_to_an_mcp_entry(
    entry: dict[str, Any], saved: dict[str, Any] | None, what: str | None
) -> None:
    def judge(conn: Any) -> None:
        settings_gate.remote_mcp_refusal(conn, None, method="mcp.update", entry=entry,
                                         saved=saved)

    for name in ("person", "legacy", "browser", "browser+actor", "connector", "none"):
        judge(CALLERS[name][0])  # only a remote login is judged here (the tables judge the rest)
    if what is None:
        judge(REMOTE)
        return
    with pytest.raises(RpcError) as caught:
        judge(REMOTE)
    _local_only(caught, what)


def test_a_closed_connection_changes_no_entry() -> None:
    with pytest.raises(RpcError) as caught:
        settings_gate.remote_mcp_refusal(CALLERS["closed"][0], None, method="mcp.add",
                                         entry={"url": OWN})
    assert caught.value.data["kind"] == "closed"


# -- every way in ------------------------------------------------------------------------------


async def _person_saves(core: Core, scope: str = "global") -> Path:
    await mcp_handlers.mcp_add_handler(
        PERSON, McpAddParams(name="mine", url=OWN, headers=WITH_VAR, scope=scope, test=False),
        core)
    return mcp_config.config_path(scope, core.paths.home, None)


async def test_mcp_add_from_a_remote_login(core: Core, started: list[Any]) -> None:
    path = mcp_config.config_path("global", core.paths.home, None)
    with pytest.raises(RpcError) as caught:
        await mcp_handlers.mcp_add_handler(
            REMOTE, McpAddParams(name="leak", url=ELSEWHERE, headers=WITH_VAR, scope="global"),
            core)
    _local_only(caught, "variable")
    assert "leak" not in mcp_config.read_servers(path) and started == []
    # A url server with nothing to fill in: allowed, and marked as not the owner's.
    await mcp_handlers.mcp_add_handler(
        REMOTE, McpAddParams(name="plain", url=OWN, scope="global"), core)
    assert mcp_config.read_servers(path)["plain"] == {"url": OWN, "savedBy": "remote_login"}
    # Replacing the owner's saved server with one that points elsewhere: refused.
    await _person_saves(core)
    with pytest.raises(RpcError) as caught:
        await mcp_handlers.mcp_add_handler(
            REMOTE, McpAddParams(name="mine", url=ELSEWHERE, scope="global", replace=True), core)
    _local_only(caught, "target")
    assert mcp_config.read_servers(path)["mine"] == {"url": OWN, "headers": WITH_VAR}
    _nothing_left(started, core.paths.home)
    await mcp_client.MANAGER.close_all()


async def test_mcp_update_from_a_remote_login(core: Core, started: list[Any]) -> None:
    path = await _person_saves(core)
    for patch in (McpEntryFields(url=ELSEWHERE),
                  McpEntryFields(headers={"X-Copy": "${TELEGRAM_BOT_TOKEN}"}),
                  McpEntryFields(headers={"X-Plain": "v"}),
                  McpEntryFields(oauth={"clientId": "c"})):
        with pytest.raises(RpcError) as caught:
            await mcp_handlers.mcp_update_handler(
                REMOTE, McpUpdateParams(name="mine", scope="global", patch=patch), core)
        assert caught.value.data["reason"] == "local_only"
        assert caught.value.data["what"] in ("target", "variable")
    with pytest.raises(RpcError):
        await mcp_handlers.mcp_update_handler(
            REMOTE, McpUpdateParams(name="mine", scope="global", patch=McpEntryFields(),
                                    headersRemove=["Authorization"]), core)
    assert mcp_config.read_servers(path)["mine"] == {"url": OWN, "headers": WITH_VAR}
    # Switching it off and on, and its timeouts, stay a remote login's.
    await mcp_handlers.mcp_update_handler(
        REMOTE, McpUpdateParams(name="mine", scope="global",
                                patch=McpEntryFields(disabled=True)), core)
    saved = mcp_config.read_servers(path)["mine"]
    assert saved["disabled"] is True and saved["url"] == OWN and "savedBy" not in saved
    _nothing_left(started, core.paths.home)
    await mcp_client.MANAGER.close_all()


async def test_mcp_test_from_a_remote_login(core: Core, started: list[Any]) -> None:
    await _person_saves(core)
    started.clear()
    # The saved entry as it is: probed, with the owner's variable, at the owner's url.
    await mcp_handlers.mcp_test_handler(REMOTE, McpTestParams(name="mine"), core)
    (probed,) = started
    assert probed.url == OWN and probed.source == "global"
    assert resolve_config(probed, core.paths.home).headers["Authorization"].endswith(SECRET)
    started.clear()
    # ... with an override that moves it, or a draft that names a variable: refused.
    for params in (McpTestParams(name="mine", url=ELSEWHERE),
                   McpTestParams(name="mine", headers={"X": "v"}),
                   McpTestParams(url=ELSEWHERE, headers=WITH_VAR),
                   McpTestParams(name="new", url=ELSEWHERE, headers=WITH_VAR)):
        with pytest.raises(RpcError) as caught:
            await mcp_handlers.mcp_test_handler(REMOTE, params, core)
        assert caught.value.data["reason"] == "local_only"
    assert started == []
    await mcp_handlers.mcp_test_handler(REMOTE, McpTestParams(url=ELSEWHERE), core)  # plain


async def test_settings_set_mcp_servers_from_a_remote_login(core: Core) -> None:
    core.settings = Settings.model_validate(
        {"mcp": {"servers": {"mine": {"url": OWN, "headers": WITH_VAR}}}})
    core.settings.save(core.paths)
    refused = [
        {"mcp": {"servers": {"mine": {"url": ELSEWHERE}}}},
        {"mcp": {"servers": {"mine": {"headers": {"X": "v"}}}}},
        {"mcp": {"servers": {"mine": {"oauth": {"clientId": "c"}}}}},
        {"mcp": {"servers": {"leak": {"url": ELSEWHERE, "headers": WITH_VAR}}}},
        {"mcp": {"servers": {"leak": {"url": ELSEWHERE,
                                      "oauth": {"clientSecret": "${TELEGRAM_BOT_TOKEN}"}}}}},
    ]
    for patch in refused:
        with pytest.raises(RpcError) as caught:
            await settings_handlers.settings_set_handler(
                REMOTE, SettingsSetParams(scope="global", patch=patch), core)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
        assert caught.value.data["reason"] == "local_only", patch
        assert Settings.load(core.paths).mcp.servers == {
            "mine": {"url": OWN, "headers": WITH_VAR}}
    # A new url server with nothing to fill in, a switch, and taking one away.
    for patch in ({"mcp": {"servers": {"plain": {"url": OWN}}}},
                  {"mcp": {"servers": {"mine": {"disabled": True}}}},
                  {"mcp": {"servers": {"plain": None}}}):
        await settings_handlers.settings_set_handler(
            REMOTE, SettingsSetParams(scope="global", patch=patch), core)
    assert Settings.load(core.paths).mcp.servers["mine"]["url"] == OWN
    # A local person writes all of it, as before.
    for patch in refused:
        await settings_handlers.settings_set_handler(
            PERSON, SettingsSetParams(scope="global", patch=patch), core)


async def test_the_mcp_command_from_a_remote_login(core: Core, tmp_path: Path,
                                                   started: list[Any]) -> None:
    from snowpea_core.commands import mcp_cmd

    path = await _person_saves(core)
    session = await core.sessions.create(tmp_path)
    ctx = SimpleNamespace(core=core, session=session, conn=REMOTE, actor=None)
    said: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    ctx.say = say
    with pytest.raises(RpcError) as caught:
        await mcp_cmd._add(ctx, {"name": "leak", "url": ELSEWHERE, "headers": dict(WITH_VAR),
                                 "scope": "global", "workdir": str(tmp_path)})
    _local_only(caught, "variable")
    with pytest.raises(RpcError) as caught:
        await mcp_cmd._add(ctx, {"name": "mine", "url": ELSEWHERE, "scope": "global",
                                 "workdir": str(tmp_path), "replace": True})
    _local_only(caught, "target")
    assert set(mcp_config.read_servers(path)) == {"mine"}
    _nothing_left(started, core.paths.home)


# -- what expands, for everyone ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "saved_by", "allow", "expands"),
    [
        ("global", None, (), True),  # the user's own file: as before
        ("settings", None, (), True),
        ("global", "remote_login", (), False),  # not the user's own entry
        ("settings", "remote_login", (), False),
        ("global", "remote_login", ("TELEGRAM_BOT_TOKEN",), False),  # never from credentials
        ("project", None, (), False),
        ("draft", None, (), False),
    ],
)
def test_who_gets_a_stored_credential(core: Core, source: str, saved_by: str | None,
                                      allow: tuple[str, ...], expands: bool) -> None:
    entry: dict[str, Any] = {"url": OWN, "headers": WITH_VAR}
    if saved_by:
        entry["savedBy"] = saved_by
    config = McpServerConfig.parse("s", entry, source)
    assert config is not None and config.saved_by == saved_by
    if expands:
        assert resolve_config(config, core.paths.home, allow).headers["Authorization"] == (
            f"Bearer {SECRET}")
        return
    with pytest.raises(McpError) as caught:
        resolve_config(config, core.paths.home, allow)
    assert SECRET not in str(caught.value)


def test_an_entry_a_remote_login_saved_expands_only_allowed_environment(
    core: Core, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_TEAM_REGION", "eu-west")
    entry = {"url": OWN, "headers": {"X-Region": "${MY_TEAM_REGION}"}, "savedBy": "remote_login"}
    config = McpServerConfig.parse("s", entry, "global")
    assert config is not None
    with pytest.raises(McpError):
        resolve_config(config, core.paths.home)
    # The owner allowed that name (settings.mcp.allowVars, a local person's key).
    allowed = resolve_config(config, core.paths.home, ["MY_TEAM_REGION"])
    assert allowed.headers["X-Region"] == "eu-west"
    assert config.to_entry()["savedBy"] == "remote_login"  # it survives a rewrite


@pytest.mark.parametrize("name", ["SNOWPEA_WEB_PASSWORD", "SNOWPEA_HOME", "snowpea_token"])
@pytest.mark.parametrize("source", ["global", "settings", "project", "draft"])
def test_snowpeas_own_variables_never_expand(core: Core, monkeypatch: pytest.MonkeyPatch,
                                             name: str, source: str) -> None:
    monkeypatch.setenv(name, SECRET)
    config = McpServerConfig.parse("s", {"url": OWN, "headers": {"X": f"${{{name}}}"}}, source)
    assert config is not None
    with pytest.raises(McpError) as caught:
        resolve_config(config, core.paths.home, [name])  # even when someone allow-listed it
    assert SECRET not in str(caught.value)
    # A default still applies: the variable reads as unset.
    fallback = McpServerConfig.parse(
        "s", {"url": OWN, "headers": {"X": f"${{{name}:-none}}"}}, "global")
    assert fallback is not None
    assert resolve_config(fallback, core.paths.home).headers["X"] == "none"


async def test_a_local_persons_edit_makes_the_entry_theirs(core: Core,
                                                           started: list[Any]) -> None:
    path = mcp_config.config_path("global", core.paths.home, None)
    await mcp_handlers.mcp_add_handler(
        REMOTE, McpAddParams(name="plain", url=OWN, scope="global", test=False), core)
    assert mcp_config.read_servers(path)["plain"]["savedBy"] == "remote_login"
    # The remote login cannot take the mark off (``unset`` does not even name it) ...
    with pytest.raises(ValueError):
        McpUpdateParams(name="plain", patch=McpEntryFields(), unset=["savedBy"])
    await mcp_handlers.mcp_update_handler(
        REMOTE, McpUpdateParams(name="plain", scope="global",
                                patch=McpEntryFields(disabled=True)), core)
    assert mcp_config.read_servers(path)["plain"]["savedBy"] == "remote_login"
    # ... a local person who edits it adopts it.
    await mcp_handlers.mcp_update_handler(
        PERSON, McpUpdateParams(name="plain", scope="global",
                                patch=McpEntryFields(headers=dict(WITH_VAR))), core)
    saved = mcp_config.read_servers(path)["plain"]
    assert "savedBy" not in saved and saved["headers"] == WITH_VAR
    await mcp_client.MANAGER.close_all()


@pytest.mark.parametrize("name", sorted(CALLERS))
async def test_a_probe_failure_reaches_only_a_local_person_in_full(
    core: Core, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    """Low 5: the text of a failed start can quote a path or what the server said."""
    detail = "spawn /home/owner/private/tool failed: token=abc123"

    async def probe(_config: Any) -> tuple[bool, list[Any], str | None, int]:
        return False, [], detail, 1

    monkeypatch.setattr(mcp_handlers, "probe", probe)
    conn, _actor = CALLERS[name]
    shown = mcp_handlers._shown_error(conn, detail)
    if name in ("person", "none"):
        assert shown == detail
        return
    assert shown is not None and "private" not in shown and "abc123" not in shown
    assert "id " in shown and detail in caplog.text  # the text is in the log, under the id
    # What a caller needs to act on stays: a sign-in is wanted, or it timed out.
    assert mcp_handlers._shown_error(conn, mcp_client.NEEDS_AUTH_TEXT) == (
        mcp_client.NEEDS_AUTH_TEXT)
