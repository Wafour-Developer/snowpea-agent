"""Security review 7, part A: the tamper guard (P2), the settings that wait for a
local person, and the trust findings (M7-6 … M7-8, L7-3, L7-6, 6A L-1).

Nothing here runs a hook, a server or a command: the tests change files the way
a program outside snowpea would and look at what the stores then say.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.security import tamper
from snowpea_core.security.tamper import FileGuard
from snowpea_core.server import errors, trust_handlers
from snowpea_core.server.app_server import Core, Daemon, accept_disk_change_handler
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import SettingsAcceptDiskChangeParams
from snowpea_core.trust import projects as trust
from snowpea_core.trust.projects import TrustStore, store_for

pytestmark = [pytest.mark.real_project_trust, pytest.mark.real_mode_guard]


def _conn(kind: str = "ide", *, remote: bool = False) -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind=kind,
                           remote_login=remote, client_version="0.3.0", client_id=f"{kind}-1",
                           surface_id=f"{kind}-s")


@pytest.fixture
def core(tmp_path: Path) -> Core:
    made = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    trust.set_default_home(made.paths.home)
    trust_handlers._TAMPERED.clear()
    return made


def _hooks(project: Path, command: str = "echo hi") -> Path:
    path = project / ".snowpea" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}}),
        encoding="utf-8")
    return path


def _trust(store: TrustStore, project: Path, scopes: list[str] | None = None) -> None:
    store.set(project, scopes, by="ide", digests=store.status(project)["digests"])


def _audit(home: Path) -> list[dict[str, Any]]:
    path = home / "logs" / "security.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# -- P2: one file ---------------------------------------------------------------------


def test_a_file_changed_behind_the_guard_is_put_back(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    # Review 8, L8-1 throttles repeated notices; every change is told here.
    monkeypatch.setattr(tamper, "NOTICE_WINDOW_S", 0.0)
    path = tmp_path / "home" / "trust" / "record.json"
    path.parent.mkdir(parents=True)
    path.write_text("one", encoding="utf-8")
    guard = FileGuard(path, home=tmp_path / "home", what="a record")
    told: list[tuple[str, str]] = []
    guard.listeners.append(lambda where, happened, *_: told.append((where, happened)))
    # Not armed (no daemon): nothing is guarded.
    path.write_text("zero", encoding="utf-8")
    assert guard.check() is False and path.read_text(encoding="utf-8") == "zero"
    guard.arm()                                   # the daemon starts: disk as it is
    assert guard.check() is False
    path.write_text("two!", encoding="utf-8")     # something else writes it
    assert guard.check() is True
    assert path.read_text(encoding="utf-8") == "zero"
    assert told == [(str(path), "changed")]
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    path.unlink()                                 # … or removes it
    assert guard.check() is True and path.read_text(encoding="utf-8") == "zero"
    assert told[-1] == (str(path), "removed")
    kinds = [(line["kind"], line["happened"]) for line in _audit(tmp_path / "home")]
    assert kinds == [("tamper", "changed"), ("tamper", "removed")]


def test_the_daemons_own_write_is_not_tampering(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    guard = FileGuard(path)
    guard.arm()                                   # no file yet
    path.write_text("mine", encoding="utf-8")
    guard.wrote(b"mine")                          # review 8: the bytes it wrote, not a re-read
    assert guard.check() is False and path.read_text(encoding="utf-8") == "mine"
    os.utime(path, (1, 1))                        # touched, same bytes: nothing to undo
    assert guard.check() is False
    other = tmp_path / "other.json"
    absent = FileGuard(other)
    absent.arm()
    other.write_text("planted", encoding="utf-8")  # a file that was never the daemon's
    assert absent.check() is True and not other.exists()


# -- P2: the trust store ---------------------------------------------------------------


def test_trust_written_behind_the_daemons_back_is_not_honoured(core: Core,
                                                               tmp_path: Path) -> None:
    store = store_for(core.paths.home)
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    _hooks(mine)
    _hooks(theirs, "echo planted")
    _trust(store, mine)
    told: list[tuple[str, str]] = []
    store.tamper_listeners[:] = [lambda where, happened, *_: told.append((where, happened))]
    store.guard()
    before = store.path.read_bytes()
    forged = json.loads(before)
    forged["projects"][str(trust.project_root(theirs))] = {
        "scopes": ["hooks"], "by": "ide", "at": "2026-01-01T00:00:00Z",
        "files": {".snowpea/hooks.json": trust.digest_of(theirs / ".snowpea" / "hooks.json")}}
    store.path.write_text(json.dumps(forged), encoding="utf-8")
    assert not store.active(theirs, "hooks", ".snowpea/hooks.json")
    assert store.active(mine, "hooks", ".snowpea/hooks.json")
    assert store.path.read_bytes() == before
    assert told == [(str(store.path), "changed")]
    # The daemon's own change afterwards is not flagged, and is the new copy.
    _trust(store, theirs)
    assert store.active(theirs, "hooks", ".snowpea/hooks.json") and len(told) == 1
    store.revoke(theirs, by="ide")
    assert not store.active(theirs, "hooks") and len(told) == 1


def test_the_record_and_the_marker_removed_while_running_come_back(core: Core,
                                                                   tmp_path: Path) -> None:
    store = store_for(core.paths.home)
    project = tmp_path / "proj"
    _hooks(project)
    assert store.migrate([]) == []                # writes the marker
    _trust(store, project)
    store.guard()
    store.path.unlink()
    store.marker.unlink()
    assert store.active(project, "hooks", ".snowpea/hooks.json")
    assert store.path.exists() and store.marker.exists() and store.problem() is None
    # The legacy external-CLI file is guarded too: it cannot appear and grant.
    store.legacy.parent.mkdir(parents=True, exist_ok=True)
    store.legacy.write_text(json.dumps({"projects": {str(tmp_path / "x"): {"trusted": True}}}),
                            encoding="utf-8")
    assert store.scopes(tmp_path / "x") == [] and not store.legacy.exists()


def test_a_restart_takes_the_disk_as_it_is(core: Core, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _hooks(project)
    running = TrustStore(core.paths.home)
    running.guard()
    # The daemon is down: what is written now is what the next start finds.
    stopped = TrustStore(core.paths.home)
    _trust(stopped, project)
    restarted = TrustStore(core.paths.home)
    restarted.guard()
    assert restarted.active(project, "hooks", ".snowpea/hooks.json")


async def test_the_person_is_told_about_a_tampered_trust_record(core: Core,
                                                               tmp_path: Path) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(method: str, payload: dict[str, Any], **_: Any) -> None:
        sent.append((method, payload))

    core.hub = SimpleNamespace(notify=notify)
    trust_handlers.wire_trust(core)               # the daemon starts: the guard is armed
    store = store_for(core.paths.home)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"version": 1, "projects": {
        str(tmp_path): {"scopes": ["hooks", "mode"], "files": {}}}}), encoding="utf-8")
    assert store.scopes(tmp_path) == []
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert [(method, payload["reason"], payload["workdir"]) for method, payload in sent] == [
        ("project.trust.changed", "tamper", "")]
    from snowpea_core.server.protocol import ProjectTrustChangedNotification, SecurityGetParams
    from snowpea_core.server.security_handlers import get_handler

    ProjectTrustChangedNotification.model_validate(sent[0][1])
    result = await get_handler(_conn("ide"), SecurityGetParams(), core)
    assert any("outside snowpea" in line and "put back" in line for line in result.problems)


# -- P2: the delegation store ------------------------------------------------------------


def _engine(home: Path) -> Any:
    from snowpea_core.jarvis.delegation import DelegationEngine

    return DelegationEngine(home, Settings())


def _grant(engine: Any, level: str = "full") -> Any:
    from snowpea_core.jarvis.delegation import Caller

    return engine.set_grant("global", None, level, [],
                            caller=Caller(kind="connection", surface="ide"))[0]


def test_a_grant_written_behind_the_daemons_back_is_not_honoured(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tamper, "NOTICE_WINDOW_S", 0.0)
    engine = _engine(tmp_path / "home")
    told: list[tuple[str, str]] = []
    engine.tamper_listeners.append(lambda where, happened, *_: told.append((where, happened)))
    engine.guard()
    assert engine.list_grants() == []
    grant = _grant(engine)                        # the daemon's own write
    assert [g.id for g in engine.list_grants()] == [grant.id] and told == []
    from snowpea_core.jarvis.delegation import Caller

    engine.revoke(grant.id, caller=Caller(kind="connection", surface="ide"))
    live = [g for g in engine.list_grants() if g.revokedAt is None]
    assert live == [] and told == []
    # Another program opens the store and takes the revocation back.
    other = sqlite3.connect(engine.db_path)
    other.execute("UPDATE grants SET revoked_at = NULL")
    other.commit()
    other.close()
    live = [g for g in engine.list_grants() if g.revokedAt is None]
    assert live == [] and told == [(str(engine.db_path), "changed")]
    check = sqlite3.connect(engine.db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM grants WHERE revoked_at IS NULL"
                             ).fetchone()[0] == 0
    finally:
        check.close()
    # … or swaps the whole file for one of its own.
    planted = tmp_path / "planted.db"
    made = sqlite3.connect(planted)
    made.executescript("CREATE TABLE grants (id TEXT); INSERT INTO grants VALUES ('x');")
    made.commit()
    made.close()
    os.replace(planted, engine.db_path)
    assert engine.list_grants() == []             # still revoked: the daemon's copy
    assert told[-1] == (str(engine.db_path), "changed") and len(told) == 2
    engine.close()
    check = sqlite3.connect(engine.db_path)
    try:
        rows = check.execute("SELECT id, revoked_at FROM grants").fetchall()
    finally:
        check.close()
    assert [row[0] for row in rows] == [grant.id] and rows[0][1] is not None


def test_the_delegation_store_is_taken_as_it_is_at_start(tmp_path: Path) -> None:
    first = _engine(tmp_path / "home")
    grant = _grant(first)                         # no guard: e.g. the daemon was down
    first.close()
    second = _engine(tmp_path / "home")
    told: list[tuple[str, str]] = []
    second.tamper_listeners.append(lambda where, happened, *_: told.append((where, happened)))
    second.guard()
    assert [g.id for g in second.list_grants()] == [grant.id] and told == []
    second.close()


async def test_a_tampered_delegation_store_is_told_as_a_jarvis_escalation(
        core: Core) -> None:
    from snowpea_core.jarvis.delegation import engine_for
    from snowpea_core.server.protocol import JarvisEscalationNotification

    sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(method: str, payload: dict[str, Any], **_: Any) -> None:
        sent.append((method, payload))

    core.hub = SimpleNamespace(notify=notify)
    engine = engine_for(core)                     # armed by the daemon's own accessor
    _grant(engine, "staged")
    other = sqlite3.connect(engine.db_path)
    other.execute("UPDATE grants SET level = 'full'")
    other.commit()
    other.close()
    assert [g.level for g in engine.list_grants()] == ["staged"]
    for _ in range(5):
        await asyncio.sleep(0)
    escalations = [payload for method, payload in sent if method == "jarvis.escalation"]
    assert len(escalations) == 1 and escalations[0]["reason"] == "error"
    assert "outside snowpea" in escalations[0]["summary"]
    JarvisEscalationNotification.model_validate(escalations[0])
    engine.close()


# -- settings.json edited on disk: security settings wait for a local person ---------------


def _edit_settings(core: Core, change: Any) -> None:
    path = core.paths.settings_json
    data = core.settings.model_dump(mode="json")
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, None)


@pytest.fixture
def settings_core(core: Core) -> Core:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(method: str, payload: dict[str, Any], **_: Any) -> None:
        sent.append((method, payload))

    core.hub = SimpleNamespace(notify=notify)
    core.sent = sent  # type: ignore[attr-defined]
    core.settings.save(core.paths)
    core.settings = Settings.load(core.paths)     # what a daemon holds after its start
    core.mark_settings_saved()
    return core


def _forge(data: dict[str, Any]) -> None:
    data["security"]["unknownCode"] = "allow"
    data["security"]["blockedCommands"] = {"presets": [], "custom": []}
    data["jarvis"]["enabled"] = True
    data["web"]["remote"]["enabled"] = True
    data["agents"]["max_concurrent"] = 7       # an ordinary setting: applies as before


async def test_security_settings_edited_on_disk_wait_for_a_local_person(
        settings_core: Core) -> None:
    core = settings_core
    _edit_settings(core, _forge)
    reloaded, keys = await core.reload_settings()
    assert reloaded and keys == ["agents"]
    assert core.settings.agents.max_concurrent == 7
    assert core.settings.security.unknownCode == "ask"
    assert core.settings.jarvis.enabled is False and core.settings.web.remote.enabled is False
    assert sorted(core.settings_waiting) == ["jarvis", "security", "web.remote"]
    changed = [payload for method, payload in core.sent if method == "settings.changed"]
    assert changed[-1] == {"scope": "global", "keys": ["agents"],
                           "waitingKeys": ["jarvis", "security", "web.remote"]}
    from snowpea_core.server.protocol import SecurityGetParams
    from snowpea_core.server.security_handlers import get_handler

    result = await get_handler(_conn("ide"), SecurityGetParams(), core)
    assert any("settings.json changed on disk" in line for line in result.problems)
    # Nobody but a local person applies it.
    for conn in (_conn("web", remote=True), _conn("browser"), _conn("connector")):
        with pytest.raises(RpcError) as refused:
            await accept_disk_change_handler(conn, SettingsAcceptDiskChangeParams(), core)
        assert refused.value.code != errors.INVALID_PARAMS
    assert core.settings.security.unknownCode == "ask"
    answer = await accept_disk_change_handler(_conn("ide"), SettingsAcceptDiskChangeParams(),
                                              core)
    assert answer.applied == ["jarvis", "security", "web.remote"] and answer.discarded == []
    assert core.settings.security.unknownCode == "allow" and core.settings_waiting == {}
    assert core.settings.web.remote.enabled is True
    changed = [payload for method, payload in core.sent if method == "settings.changed"]
    assert changed[-1]["waitingKeys"] == []


async def test_discarding_writes_the_daemons_values_back(settings_core: Core) -> None:
    core = settings_core
    _edit_settings(core, lambda data: data["security"].update(unknownCode="allow"))
    reloaded, keys = await core.reload_settings()
    assert (reloaded, keys) == (False, [])
    assert core.settings_waiting == {"security": core.settings_waiting["security"]}
    changed = [payload for method, payload in core.sent if method == "settings.changed"]
    assert changed[-1] == {"scope": "global", "keys": [], "waitingKeys": ["security"]}
    answer = await accept_disk_change_handler(
        _conn("tui"), SettingsAcceptDiskChangeParams(accept=False), core)
    assert answer.applied == [] and answer.discarded == ["security"]
    assert core.settings.security.unknownCode == "ask" and core.settings_waiting == {}
    on_disk = json.loads(core.paths.settings_json.read_text(encoding="utf-8"))
    assert on_disk["security"]["unknownCode"] == "ask"
    assert await core.reload_settings() == (False, [])


async def test_the_daemons_own_save_makes_nothing_wait(settings_core: Core) -> None:
    core = settings_core
    _edit_settings(core, lambda data: data["security"].update(unknownCode="allow"))
    await core.reload_settings()
    assert "security" in core.settings_waiting
    # settings.set (and every other in-daemon write) saves what the daemon holds.
    core.settings.save(core.paths)
    core.mark_settings_saved()
    assert core.settings_waiting == {} and await core.reload_settings() == (False, [])
    assert json.loads(core.paths.settings_json.read_text(encoding="utf-8"))[
        "security"]["unknownCode"] == "ask"


def test_a_start_takes_settings_as_they_are(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    data = Settings().model_dump(mode="json")
    data["security"]["unknownCode"] = "allow"
    paths.settings_json.write_text(json.dumps(data), encoding="utf-8")
    assert Settings.load(paths).security.unknownCode == "allow"


# -- M7-8: both the record and the marker gone while the daemon was down ------------------


class _Meta:
    """The session store's two methods the migration uses."""

    def __init__(self, workdirs: list[str]) -> None:
        self.values: dict[str, str] = {}
        self.workdirs = workdirs

    def meta_get(self, key: str) -> str | None:
        return self.values.get(key)

    def meta_set(self, key: str, value: str) -> None:
        self.values.setdefault(key, value)

    def session_workdirs(self, **_: Any) -> list[str]:
        return list(self.workdirs)


def test_losing_the_record_and_the_marker_does_not_migrate_again(core: Core,
                                                                 tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _hooks(project)
    core.store = _Meta([str(project)])            # type: ignore[assignment]
    trust_handlers.wire_trust(core)               # the first start with project trust
    store = store_for(core.paths.home)
    assert store.active(project, "hooks", ".snowpea/hooks.json")
    assert trust_handlers.MIGRATED_KEY in core.store.values  # type: ignore[union-attr]
    # The daemon is down; both files go.  The next start must not trust anything.
    fresh = TrustStore(core.paths.home)
    fresh.path.unlink()
    fresh.marker.unlink()
    trust._STORES.clear()
    trust_handlers._STORE_PROBLEM.clear()
    trust_handlers.wire_trust(core)
    store = store_for(core.paths.home)
    assert not store.active(project, "hooks", ".snowpea/hooks.json")
    assert store.marker.exists() and store.migrated()
    assert trust_handlers._STORE_PROBLEM and "no project is trusted" in (
        trust_handlers._STORE_PROBLEM[0])
    trust_handlers._STORE_PROBLEM.clear()


def test_a_home_from_before_this_release_gets_the_record(core: Core, tmp_path: Path) -> None:
    store = store_for(core.paths.home)
    assert store.migrate([]) == []                # the marker of an earlier release
    core.store = _Meta([])                        # type: ignore[assignment]
    trust_handlers.wire_trust(core)
    assert core.store.values[trust_handlers.MIGRATED_KEY] == store.migrated_at()  # type: ignore[union-attr]


def test_the_session_store_keeps_such_facts(tmp_path: Path) -> None:
    from snowpea_core.session.store import Store

    store = Store.open(Paths.create(tmp_path / "home"))
    try:
        assert store.meta_get("trust.migrated") is None
        store.meta_set("trust.migrated", "2026-11-16T00:00:00.000Z")
        store.meta_set("trust.migrated", "later")  # recorded once
        assert store.meta_get("trust.migrated") == "2026-11-16T00:00:00.000Z"
    finally:
        asyncio.run(store.close()) if asyncio.iscoroutinefunction(store.close) else store.close()


# -- M7-6: what a pin covers ---------------------------------------------------------------


def test_what_a_command_runs_outside_the_project_is_said(tmp_path: Path) -> None:
    base = trust.project_root(tmp_path / "proj")
    (base / "tools").mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "run.sh"
    outside.parent.mkdir()
    outside.write_text("echo out\n", encoding="utf-8")
    (base / "tools" / "inside.sh").write_text("echo in\n", encoding="utf-8")
    (base / "tools" / "link.sh").symlink_to(outside)

    def runs(line: str) -> tuple[list[str], str | None]:
        return trust.runs_of(base, line.split(), raw=line, folder=base)

    assert runs("sh tools/inside.sh") == (["tools/inside.sh"], None)
    assert runs(f"sh {outside}") == ([], "outside-project")
    assert runs(f"{outside} --flag") == ([], "outside-project")
    assert runs("sh tools/link.sh") == ([], "symlink")
    assert runs("tools/link.sh") == ([], "symlink")
    assert runs("sh build") == ([], "missing")     # no slash, no dot: still the script
    (base / "build").write_text("echo build\n", encoding="utf-8")
    assert runs("sh build") == (["build"], None)
    assert runs("/usr/bin/env true") == ([], None)  # the system's own programs are let be
    for reason in ("outside-project", "symlink"):
        assert reason in trust.NOT_PINNED_REASONS


def test_the_status_says_what_a_pin_does_not_cover(core: Core, tmp_path: Path) -> None:
    from snowpea_core.server.protocol import ProjectTrustStatus

    project = tmp_path / "proj"
    outside = tmp_path / "elsewhere.sh"
    outside.write_text("echo out\n", encoding="utf-8")
    _hooks(project, f"sh {outside}")
    status = store_for(core.paths.home).status(project)
    assert status["limits"] and "not" in status["limits"][0]
    assert [row["reason"] for row in status["notPinned"]] == ["outside-project"]
    ProjectTrustStatus.model_validate(status)


# -- 6A L-1 as decided, M7-7, L7-3: project agents and nested projects ---------------------


def _agent(project: Path, name: str, body: str = "Be brief.", front: str = "") -> Path:
    path = project / ".snowpea" / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {name} agent\n{front}---\n\n{body}\n",
                    encoding="utf-8")
    return path


def _loader(core: Core, *workdirs: Path) -> Any:
    from snowpea_core.skills.loader import SkillLoader

    loader = SkillLoader(core, workdirs=list(workdirs))
    loader.scan()
    return loader


def _names(loader: Any, workdir: Path) -> list[str]:
    return sorted(agent.name for agent in loader.agents_for(workdir) if agent.project)


def test_every_project_agent_waits_for_the_agents_trust(core: Core, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _agent(project, "helper")                     # no permission, no built-in's name
    store = store_for(core.paths.home)
    status = store.status(project)
    assert [(f["path"], f["scope"], f["state"]) for f in status["files"]] == [
        (".snowpea/agents/helper.md", "agents", "untrusted")]
    assert _names(_loader(core, project), project) == []
    from snowpea_core.agent.definition import discover_definitions

    assert [d.name for d in discover_definitions(project, core.paths.home)
            if d.source == "project"] == []
    _trust(store, project, ["agents"])
    assert _names(_loader(core, project), project) == ["helper"]
    assert [d.name for d in discover_definitions(project, core.paths.home)
            if d.source == "project"] == ["helper"]


def test_the_pin_is_compared_with_the_bytes_that_were_loaded(core: Core,
                                                             tmp_path: Path) -> None:
    project = tmp_path / "proj"
    path = _agent(project, "helper", "Loaded text.")
    store = store_for(core.paths.home)
    _trust(store, project, ["agents"])
    loader = _loader(core, project)               # loads "Loaded text."
    assert _names(loader, project) == ["helper"]
    # The file changes, and the person trusts the project as it is NOW: the pin is
    # the new content.  The definition held in memory is the old one — not active.
    path.write_text(path.read_text(encoding="utf-8").replace("Loaded text.", "Other text."),
                    encoding="utf-8")
    _trust(store, project, ["agents"])
    assert _names(loader, project) == []
    assert _names(_loader(core, project), project) == ["helper"]   # loaded again: the pinned one


def test_an_agent_snowpea_wrote_for_the_person_is_pinned_as_written(core: Core,
                                                                    tmp_path: Path) -> None:
    from snowpea_core.agent.definition import AgentDefinition, write_definition

    project = tmp_path / "proj"
    project.mkdir()
    defn = AgentDefinition(name="mine", description="mine", prompt="Be brief.")
    # Review 8, M8-1: pinned only when a local person asked (``pin=True``).
    write_definition(defn, project, pin=True)
    assert _names(_loader(core, project), project) == ["mine"]
    # One that asks for a permission of its own still waits for the person.
    bold = AgentDefinition(name="bold", description="bold", prompt="x", permission="auto")
    write_definition(bold, project, pin=True)
    assert _names(_loader(core, project), project) == ["mine"]


def test_an_outer_projects_entries_need_its_trust_inside_a_nested_project(
        core: Core, tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "packages" / "inner"
    for base, text in ((outer, "outer"), (inner, "inner")):
        path = base / ".snowpea" / "commands" / "ship.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\ndescription: {text}\n---\n\nFrom {text}.\n", encoding="utf-8")
    only = outer / ".snowpea" / "commands" / "outer-only.md"
    only.write_text("---\ndescription: only\n---\n\nOnly outer.\n", encoding="utf-8")
    loader = _loader(core, outer, inner)
    # The innermost project's entry wins a name both have.
    assert "From inner." in loader.find("ship", inner).doc.body
    assert "From outer." in loader.find("ship", outer).doc.body
    # The outer project's own entry is not seen from the nested one until trusted.
    assert loader.find("outer-only", inner) is None
    assert loader.find("outer-only", outer) is not None
    store_for(core.paths.home).set(outer, ["commands"], by="ide")
    assert loader.find("outer-only", inner) is not None
    assert "From inner." in loader.find("ship", inner).doc.body


# -- L7-6: what a tool's child process is given ---------------------------------------------


def test_a_tool_child_gets_none_of_snowpeas_own_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.exec.local import child_env

    for name in ("SNOWPEA_TOKEN", "SNOWPEA_WEB_PASSWORD", "SNOWPEA_REGISTRY_TOKEN",
                 "SNOWPEA_DAEMON_CAPABILITY", "SNOWPEA_SESSION_SECRET"):
        monkeypatch.setenv(name, "s3cret")
    monkeypatch.setenv("SNOWPEA_HOME", "/home/u/.snowpea")
    monkeypatch.setenv("GITHUB_TOKEN", "the-persons-own")
    env = child_env({"EXTRA": "1"})
    assert not [name for name in env if name.startswith("SNOWPEA_") and env[name] == "s3cret"]
    assert env["SNOWPEA_HOME"] == "/home/u/.snowpea" and env["EXTRA"] == "1"
    assert env["GITHUB_TOKEN"] == "the-persons-own"   # not snowpea's: left as it was


def test_the_daemon_does_not_put_its_token_in_its_environment() -> None:
    """L7-6, established by reading: nothing in the core assigns a SNOWPEA_* login
    variable, and the web password is dropped at start."""
    import re

    import snowpea_core

    root = Path(snowpea_core.__file__).parent
    assigns = re.compile(r"os\.environ\[\s*[\"']SNOWPEA_[A-Z_]*(TOKEN|PASSWORD|SECRET|CAPABILITY)"
                         r"[A-Z_]*[\"']\s*\]\s*=|os\.putenv\(\s*[\"']SNOWPEA_[A-Z_]*(TOKEN|"
                         r"PASSWORD|SECRET|CAPABILITY)")
    found = [str(path.relative_to(root)) for path in root.rglob("*.py")
             if "vendor" not in path.parts
             and assigns.search(path.read_text(encoding="utf-8", errors="replace"))]
    assert found == []
    assert 'os.environ.pop("SNOWPEA_WEB_PASSWORD", None)' in (
        root / "server" / "app_server.py").read_text(encoding="utf-8")


# -- the untrusted-project notice reaches a client that subscribed after create ------------

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "basic.json"


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            if instance.core is not None:
                await instance.stop()


def _untrusted(client: RpcClient, session_id: str) -> list[dict[str, Any]]:
    return [event for event in client.events
            if event.get("sessionId") == session_id and event["kind"] == "error"
            and event["payload"].get("code") == trust_handlers.UNTRUSTED_CODE]


async def test_the_untrusted_notice_comes_with_the_first_turn_exactly_once(
        daemon: Daemon, tmp_path: Path, http: aiohttp.ClientSession) -> None:
    project = tmp_path / "proj"
    _hooks(project)
    client = await connect(http, daemon, timeout=20.0)
    created = await client.ok("session.create", {"workdir": str(project), "mode": "accept"})
    session_id = str(created["sessionId"])
    # Nothing was sent while the session was being created (no client could have
    # known its id yet); the notice waits for the first turn.
    assert _untrusted(client, session_id) == []
    first = await client.ok("session.prompt", {"sessionId": session_id, "text": "hello"})
    await client.wait(lambda event: event["kind"] == "turn.done"
                      and event["payload"]["turnId"] == first["turnId"])
    notices = _untrusted(client, session_id)
    assert len(notices) == 1 and "hooks" in notices[0]["payload"]["message"]
    kinds = [event["kind"] for event in client.events if event.get("sessionId") == session_id]
    assert kinds.index("error") < kinds.index("turn.started")
    second = await client.ok("session.prompt", {"sessionId": session_id, "text": "again"})
    await client.wait(lambda event: event["kind"] == "turn.done"
                      and event["payload"]["turnId"] == second["turnId"])
    assert len(_untrusted(client, session_id)) == 1
    # Kept in the session's events for a replay, once.
    replay = await client.ok("session.events", {"sessionId": session_id, "afterSeq": 0})
    stored = [event for event in replay["events"] if event["kind"] == "error"
              and event["payload"].get("code") == trust_handlers.UNTRUSTED_CODE]
    assert len(stored) == 1
