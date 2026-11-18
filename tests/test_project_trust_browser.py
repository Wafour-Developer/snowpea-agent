"""A confirmed local browser trusts a folder of its own session (1.62.0,
``browserProjectTrust``), under the rule of browser person approvals.

Every hook command below is DATA: nothing here runs a hook; the files only give
the project something to trust.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors, settings_gate
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import RpcError
from snowpea_core.server.person_gate import method_refusal
from snowpea_core.server.protocol import (
    CAPABILITIES,
    PROTOCOL_VERSION,
    ProjectTrustParams,
    ProjectTrustRevokeParams,
    ProjectTrustSetParams,
)
from snowpea_core.server.trust_handlers import (
    BROWSER_TRUST_TEXT,
    LEGACY_BROWSER_TRUST_TEXT,
    UNTRUSTED_CODE,
    notice_details,
    project_trust_revoke_handler,
    project_trust_set_handler,
    project_trust_status_handler,
    session_notice,
)
from snowpea_core.trust import projects as trust

pytestmark = [pytest.mark.real_project_trust, pytest.mark.real_mode_guard]

#: The browser's own native confirmation.
CONFIRMED: dict[str, Any] = {"actor": "person", "personConfirm": "window"}


def _conn(kind: str = "browser", client_id: str = "b-1", *, version: str = "0.4.0",
          peer: str = "127.0.0.1", remote: bool = False) -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind=kind,
                           remote_login=remote, client_version=version, client_id=client_id,
                           surface_id=f"{client_id}-s", peer=peer)


def _hooks(project: Path) -> Path:
    path = project / ".snowpea" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": "echo data-only"}]}]}}),
        encoding="utf-8")
    return path


def _session(sid: str, workdir: Path, **owners: Any) -> Any:
    values = {"id": sid, "workdir": workdir, "host_tools_from": None, "owned_by": None,
              "origin_client_id": None, "origin_conn": None, "asker_conn": None,
              "parent_session_id": None}
    values.update(owners)
    return SimpleNamespace(**values)


@pytest.fixture
def core(tmp_path: Path) -> Core:
    made = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    trust.set_default_home(made.paths.home)
    return made


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _hooks(root)
    return root


def _live(core: Core, *sessions: Any) -> None:
    known = {s.id: s for s in sessions}
    core.sessions = SimpleNamespace(get=known.get, all=lambda: list(known.values()))  # type: ignore[assignment]


async def _digests(core: Core, conn: Any, project: Path) -> dict[str, str]:
    shown = await project_trust_status_handler(conn, ProjectTrustParams(workdir=str(project)),
                                               core)
    return dict(shown.digests)


async def _set(core: Core, conn: Any, project: Path, digests: dict[str, str],
               **extra: Any) -> Any:
    return await project_trust_set_handler(
        conn, ProjectTrustSetParams(workdir=str(project), digests=digests, **extra), core)


def _reason(caught: pytest.ExceptionInfo[RpcError]) -> str:
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    return str(caught.value.data["reason"])


async def test_a_confirmed_browser_trusts_its_own_sessions_folder(
        core: Core, project: Path) -> None:
    browser = _conn()
    _live(core, _session("s", project, host_tools_from="b-1"))
    digests = await _digests(core, browser, project)
    assert digests == {".snowpea/hooks.json": trust.digest_of(project / ".snowpea/hooks.json")}
    status = await _set(core, browser, project, digests, **CONFIRMED)
    assert status.trusted
    assert set(status.scopes) == set(trust.DEFAULT_SCOPES)
    assert status.by == "browser:b-1" and status.files[0].state == "active"
    lines = (core.paths.logs_dir / "security.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    assert record["event"] == "project_trusted" and record["connection"] == "browser"
    assert record["clientId"] == "b-1" and record["personConfirmWindow"] is True
    assert record["digests"] == 1 and record["by"] == "browser:b-1"


@pytest.mark.parametrize(("conn_kw", "extra", "reason"), [
    ({"version": "0.3.0"}, CONFIRMED, "browser_legacy"),
    ({}, {"actor": "person"}, "browser_unconfirmed"),
    ({}, {"personConfirm": "window"}, "browser_unconfirmed"),
    ({}, {}, "browser_unconfirmed"),
    ({"peer": "192.168.50.17"}, CONFIRMED, "browser_remote"),
    ({}, {**CONFIRMED, "scopes": ["hooks", "mode"]}, "browser_not_allowed"),
    ({}, {**CONFIRMED, "scopes": ["externalCli"]}, "browser_not_allowed"),
])
async def test_a_browser_trust_that_does_not_count(
        core: Core, project: Path, conn_kw: dict[str, Any], extra: dict[str, Any],
        reason: str) -> None:
    browser = _conn(**conn_kw)
    _live(core, _session("s", project, host_tools_from="b-1"))
    digests = await _digests(core, _conn(), project)
    with pytest.raises(RpcError) as caught:
        await _set(core, browser, project, digests, **extra)
    assert _reason(caught) == reason
    assert not trust.store_for(core.paths.home).status(project)["scopes"]


@pytest.mark.parametrize("owner", [
    {"host_tools_from": "b-2"},          # another browser's session
    {"origin_client_id": "ide-1"},       # the IDE's session
    # m27, M1: an IDE session this browser attached to (or claims the id of).
    {"origin_client_id": "b-1", "origin_surface": "ide"},
])
async def test_another_surfaces_session_is_not_the_browsers(
        core: Core, project: Path, owner: dict[str, Any]) -> None:
    browser = _conn()
    _live(core, _session("s", project, **owner))
    with pytest.raises(RpcError) as caught:
        await _digests(core, browser, project)
    assert _reason(caught) == "browser_not_owner"
    digests = await _digests(core, _conn("ide", "ide-1"), project)
    with pytest.raises(RpcError) as caught:
        await _set(core, browser, project, digests, **CONFIRMED)
    assert _reason(caught) == "browser_not_owner"


async def test_being_the_origin_of_an_ide_session_is_not_owning_it(
        core: Core, project: Path) -> None:
    """m27, M1: a browser that resumed or attached to an IDE session is its origin
    connection, but the session is still not the browser's for trust."""
    browser = _conn()
    _live(core, _session("s", project, origin_surface="ide", origin_conn=browser,
                         origin_client_id="b-1"))
    with pytest.raises(RpcError) as caught:
        await _digests(core, browser, project)
    assert _reason(caught) == "browser_not_owner"
    # The same origin on a browser-made session is its own.
    _live(core, _session("s", project, origin_surface="browser", origin_conn=browser))
    assert await _digests(core, browser, project)


async def test_a_folder_with_no_session_of_the_browser_is_not_its(
        core: Core, project: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _live(core, _session("s", other, host_tools_from="b-1"))
    digests = await _digests(core, _conn("ide", "ide-1"), project)
    with pytest.raises(RpcError) as caught:
        await _set(core, _conn(), project, digests, **CONFIRMED)
    assert _reason(caught) == "browser_not_owner"


async def test_stale_digests_are_trust_changed(core: Core, project: Path) -> None:
    browser = _conn()
    _live(core, _session("s", project, owned_by="b-1"))
    digests = await _digests(core, browser, project)
    hooks = project / ".snowpea" / "hooks.json"
    hooks.write_text(hooks.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RpcError) as caught:
        await _set(core, browser, project, digests, **CONFIRMED)
    assert caught.value.code == errors.TRUST_CHANGED
    assert caught.value.data["changed"] == [".snowpea/hooks.json"]


async def test_revoke_follows_the_same_rule(core: Core, project: Path) -> None:
    browser = _conn()
    _live(core, _session("s", project, origin_client_id="b-1", origin_surface="browser"))
    store = trust.store_for(core.paths.home)
    store.set(project, [*trust.DEFAULT_SCOPES, "mode", "externalCli"], by="ide",
              digests=store.status(project)["digests"])
    for conn, extra, reason in (
            (_conn(version="0.3.0"), CONFIRMED, "browser_legacy"),
            (browser, {"actor": "person"}, "browser_unconfirmed"),
            (_conn(peer="10.0.0.9"), CONFIRMED, "browser_remote"),
            (_conn(client_id="b-2"), CONFIRMED, "browser_not_owner"),
            (browser, {**CONFIRMED, "scopes": ["mode"]}, "browser_not_allowed")):
        with pytest.raises(RpcError) as caught:
            await project_trust_revoke_handler(
                conn, ProjectTrustRevokeParams(workdir=str(project), **extra), core)
        assert _reason(caught) == reason
    # Omitted scopes from a browser are the default ones: mode and externalCli stay.
    status = await project_trust_revoke_handler(
        browser, ProjectTrustRevokeParams(workdir=str(project), **CONFIRMED), core)
    assert status.scopes == ["mode", "externalCli"] and status.by == "browser:b-1"
    record = json.loads((core.paths.logs_dir / "security.jsonl").read_text(
        encoding="utf-8").splitlines()[-1])
    assert record["event"] == "project_trust_revoked" and record["clientId"] == "b-1"


def _settings(project: Path, mode: str) -> Path:
    path = project / trust.SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"defaultMode": mode}), encoding="utf-8")
    return path


async def test_a_browser_grant_never_reactivates_mode_for_new_content(
        core: Core, project: Path) -> None:
    """Review H1: a browser grant keeps settings.json's pin while mode is trusted."""
    browser = _conn()
    _live(core, _session("s", project, host_tools_from="b-1"))
    store = trust.store_for(core.paths.home)
    _settings(project, "accept")
    store.set(project, ["mode"], by="ide", digests=store.status(project)["digests"])
    assert trust.mode_trusted(project)
    _settings(project, "auto")
    assert not trust.mode_trusted(project)
    digests = await _digests(core, browser, project)
    # Granting settings would need settings.json pinned anew: refused.
    with pytest.raises(RpcError) as caught:
        await _set(core, browser, project, digests, **CONFIRMED)
    assert _reason(caught) == "browser_not_allowed"
    assert caught.value.data["what"] == trust.SETTINGS_FILE
    status = await _set(core, browser, project, digests, scopes=["hooks"], **CONFIRMED)
    assert not trust.mode_trusted(project)
    assert store.active(project, "hooks", ".snowpea/hooks.json")
    assert set(status.scopes) == {"hooks", "mode"}


async def test_a_browser_grant_keeps_settings_unpinned_when_mode_had_no_file(
        core: Core, project: Path) -> None:
    browser = _conn()
    _live(core, _session("s", project, host_tools_from="b-1"))
    store = trust.store_for(core.paths.home)
    store.set(project, ["mode"], by="ide", digests=store.status(project)["digests"])
    _settings(project, "auto")
    digests = await _digests(core, browser, project)
    await _set(core, browser, project, digests, scopes=["hooks", "mcp"], **CONFIRMED)
    assert not trust.mode_trusted(project)
    assert trust.SETTINGS_FILE not in (store.entry(project) or {})["files"]


async def test_without_mode_a_browser_pins_settings_as_shown(core: Core, project: Path) -> None:
    browser = _conn()
    _live(core, _session("s", project, host_tools_from="b-1"))
    _settings(project, "auto")
    digests = await _digests(core, browser, project)
    await _set(core, browser, project, digests, **CONFIRMED)
    store = trust.store_for(core.paths.home)
    assert store.active(project, "settings", trust.SETTINGS_FILE)
    assert not trust.mode_trusted(project)
    record = json.loads((core.paths.logs_dir / "security.jsonl").read_text(
        encoding="utf-8").splitlines()[-1])
    assert record["scopes"] == list(trust.DEFAULT_SCOPES)  # review L2: what was granted


async def test_a_browser_revoke_that_drops_nothing_changes_nothing(
        core: Core, project: Path) -> None:
    """Review L1."""
    browser = _conn()
    _live(core, _session("s", project, host_tools_from="b-1"))
    store = trust.store_for(core.paths.home)
    store.set(project, ["externalCli"], by="ide")
    before = dict(store.entry(project) or {})
    for scopes in ([], ["hooks"], None):
        extra = {} if scopes is None else {"scopes": scopes}
        status = await project_trust_revoke_handler(
            browser, ProjectTrustRevokeParams(workdir=str(project), **extra, **CONFIRMED), core)
        assert status.by == "ide"
    assert store.entry(project) == before
    assert not (core.paths.logs_dir / "security.jsonl").exists()


async def test_the_browser_path_resolves_the_folder_once(
        core: Core, project: Path, tmp_path: Path) -> None:
    """Review L3: a link to the session's folder is judged and stored as the folder."""
    link = tmp_path / "link"
    link.symlink_to(project, target_is_directory=True)
    browser = _conn()
    _live(core, _session("s", project, host_tools_from="b-1"))
    digests = await _digests(core, browser, link)
    status = await _set(core, browser, link, digests, **CONFIRMED)
    assert status.workdir == str(project.resolve())
    assert trust.store_for(core.paths.home).entry(project) is not None


def _gates(method: str, conn: Any, params: dict[str, Any] | None = None) -> None:
    method_refusal(method, conn)
    settings_gate.browser_method_refusal(method, params or {}, conn, None)
    settings_gate.kind_method_refusal(method, params or {}, conn, None)


@pytest.mark.parametrize("method", ["project.trust.set", "project.trust.revoke"])
def test_connectors_remote_logins_and_in_process_callers_stay_refused(method: str) -> None:
    for conn in (_conn("connector", "c-1"), _conn("web", "w-1", remote=True),
                 _conn("browser", "b-1", remote=True), None):
        with pytest.raises(RpcError) as caught:
            _gates(method, conn, CONFIRMED)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    _gates(method, _conn(), CONFIRMED)  # a browser reaches the handler, which judges it
    _gates(method, _conn("ide", "ide-1"))


async def test_jarvis_and_in_process_callers_cannot_trust(core: Core, project: Path) -> None:
    digests = await _digests(core, _conn("ide", "ide-1"), project)
    for conn in (None, SimpleNamespace(authenticated=True, closed=True, client_kind="ide")):
        with pytest.raises(RpcError) as caught:
            await _set(core, conn, project, digests, **CONFIRMED)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY


async def test_the_untrusted_notice_lets_each_surface_choose(core: Core, project: Path,
                                                             tmp_path: Path) -> None:
    details = notice_details(core, project)
    assert details is not None
    assert details["scopes"] == ["hooks"] and details["browserScopes"] == ["hooks"]
    assert details["canTrustHere"] is True and details["changed"] == []
    assert details["workdir"] == str(project.resolve())
    assert details["browserText"] == BROWSER_TRUST_TEXT
    assert details["legacyBrowserText"] == LEGACY_BROWSER_TRUST_TEXT
    events: list[tuple[str, Any]] = []

    async def emit(session_id: str, event: Any) -> None:
        events.append((session_id, event))

    core.hub = SimpleNamespace(emit_event=emit)  # type: ignore[assignment]
    await session_notice(core, "notice-s1", project)
    (_sid, (kind, data)), = events
    assert kind == "error" and data["code"] == UNTRUSTED_CODE
    assert "snowpea project trust" in data["message"]  # the TUI/IDE line is unchanged
    assert data["details"]["canTrustHere"] is True
    empty = tmp_path / "empty"
    empty.mkdir()
    assert notice_details(core, empty) is None


def test_protocol_and_capability() -> None:
    assert PROTOCOL_VERSION == "1.63.0"
    assert "browserProjectTrust" in CAPABILITIES
