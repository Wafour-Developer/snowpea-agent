"""Jarvis mode (M19 WP2): grants, routing, pauses, the re-check, and the RPCs."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import RpcClient, make_daemon

from snowpea_core.config.settings import Settings
from snowpea_core.jarvis.classify import ClassifyRequest
from snowpea_core.jarvis.delegation import (
    JARVIS,
    SYSTEM,
    Caller,
    DecisionRefused,
    DelegationEngine,
    DelegationError,
    PersonOnly,
    engine_for,
)
from snowpea_core.server import protocol
from snowpea_core.server.protocol import PROTOCOL_VERSION

PERSON = Caller(kind="connection", surface="ide")
PROJECT = "/srv/jarvis-test/proj"


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def engine(tmp_path: Path, settings: Settings, clock: Clock) -> DelegationEngine:
    built = DelegationEngine(tmp_path / "home", lambda: settings, now=clock)
    yield built  # type: ignore[misc]
    built.close()


def _req(command: str, session: str = "s-1", workdir: str = PROJECT, **extra: Any) -> Any:
    return ClassifyRequest(tool="shell", args={"command": command}, tags=("exec",),
                           workdir=workdir, session_id=session, home="/srv/jarvis-test/home",
                           user_home="/srv/jarvis-test/user", **extra)


# -- grants and their precedence ------------------------------------------------


def test_scope_precedence(engine: DelegationEngine) -> None:
    assert engine.effective("s-1", PROJECT).source == "default"
    assert engine.effective("s-1", PROJECT).level == "staged"
    engine.set_grant("global", None, "full", caller=PERSON)
    assert engine.effective("s-1", PROJECT).level == "full"
    project, _ = engine.set_grant("project", PROJECT, "staged", ["deploy"], caller=PERSON)
    found = engine.effective("s-1", PROJECT + "/sub/dir")
    assert (found.level, found.source, found.allow) == ("staged", "project", {"deploy"})
    deeper, _ = engine.set_grant("project", PROJECT + "/sub", "full", caller=PERSON)
    assert engine.effective("s-1", PROJECT + "/sub/dir").grant == deeper
    assert engine.effective("s-1", PROJECT).grant == project  # a sibling folder is not "sub"
    session, _ = engine.set_grant("session", "s-1", "staged", caller=PERSON)
    assert engine.effective("s-1", PROJECT + "/sub/dir").grant == session
    assert engine.effective("s-2", "/elsewhere").source == "global"
    external, _ = engine.set_grant("session", "ext:codex:t-1", "full", ["deploy"], caller=PERSON)
    found = engine.effective(external={"cli": "codex", "id": "t-1"}, project="/elsewhere")
    assert found.grant == external


def test_the_default_level_follows_settings(engine: DelegationEngine, settings: Settings) -> None:
    settings.jarvis.delegation.default = "full"
    assert engine.effective("s-1", PROJECT).level == "full"


def test_malformed_grants_are_refused(engine: DelegationEngine) -> None:
    for bad in (
        lambda: engine.set_grant("global", None, "full", ["deploy"], caller=PERSON),
        lambda: engine.set_grant("global", "/x", "full", caller=PERSON),
        lambda: engine.set_grant("project", "relative/path", "full", caller=PERSON),
        lambda: engine.set_grant("project", None, "full", caller=PERSON),
        lambda: engine.set_grant("session", "has space", "full", caller=PERSON),
        lambda: engine.set_grant("session", "", "full", caller=PERSON),
        lambda: engine.set_grant("galaxy", None, "full", caller=PERSON),
        lambda: engine.set_grant("global", None, "everything", caller=PERSON),
        lambda: engine.set_grant("project", PROJECT, "full", ["nukes"], caller=PERSON),
        lambda: engine.set_grant("project", PROJECT, "full", caller=PERSON,
                                 expires_at="2020-01-01T00:00:00Z"),
        lambda: engine.set_grant("project", PROJECT, "full", caller=PERSON,
                                 expires_at="next tuesday"),
    ):
        with pytest.raises(DelegationError):
            bad()
    assert engine.list_grants() == []


def test_a_project_grant_expires_after_its_default_ttl(
    engine: DelegationEngine, clock: Clock, settings: Settings
) -> None:
    changes: list[tuple[str, str]] = []
    engine.grant_listeners.append(lambda reason, grant: changes.append((reason, grant.id)))
    grant, _ = engine.set_grant("project", PROJECT, "full", caller=PERSON)
    assert grant.expiresAt == "2026-11-16T09:00:00.000Z"
    clock.advance(hours=23, minutes=59)
    assert engine.effective("s-1", PROJECT).level == "full"
    clock.advance(minutes=2)
    assert engine.effective("s-1", PROJECT).source == "default"
    assert engine.list_grants() == []
    assert [g.id for g in engine.list_grants(include_expired=True)] == [grant.id]
    engine.sweep_expired()  # announced once only
    assert changes == [("set", grant.id), ("expired", grant.id)]
    settings.jarvis.delegation.projectTtlHours = 2
    short, _ = engine.set_grant("project", PROJECT, "full", caller=PERSON)
    assert short.expiresAt == "2026-11-16T11:01:00.000Z"


def test_explicit_expiry_and_session_grants_without_one(
    engine: DelegationEngine, clock: Clock
) -> None:
    grant, _ = engine.set_grant("session", "s-1", "full", caller=PERSON,
                                expires_at="2026-11-15T10:00:00+00:00")
    assert grant.expiresAt == "2026-11-15T10:00:00.000Z"
    clock.advance(minutes=61)
    assert engine.effective("s-1").source == "default"
    lasting, _ = engine.set_grant("session", "s-1", "full", caller=PERSON)
    assert lasting.expiresAt is None
    clock.advance(days=400)
    assert engine.effective("s-1").grant == lasting
    assert engine.end_session("s-1") == [lasting.id]
    assert engine.effective("s-1").source == "default"


def test_setting_again_replaces(engine: DelegationEngine) -> None:
    first, replaced = engine.set_grant("project", PROJECT, "staged", caller=PERSON)
    assert replaced is None
    second, replaced = engine.set_grant("project", PROJECT + "/", "full", ["deploy"],
                                        caller=PERSON, note="ship it")
    assert replaced == first.id
    assert [g.id for g in engine.list_grants()] == [second.id]
    assert second.note == "ship it" and second.grantedBy == "ide"
    assert engine.list_grants("project", PROJECT)[0].id == second.id


def test_revoke(engine: DelegationEngine) -> None:
    grant, _ = engine.set_grant("project", PROJECT, "full", caller=PERSON)
    assert engine.revoke(grant.id, caller=PERSON) is True
    assert engine.revoke(grant.id, caller=PERSON) is False
    assert engine.revoke("jg-missing", caller=PERSON) is False
    assert engine.effective("s-1", PROJECT).source == "default"


def test_narrowing_revokes_are_open_widening_ones_are_the_persons(
    engine: DelegationEngine,
) -> None:
    project, _ = engine.set_grant("project", PROJECT, "full", ["deploy"], caller=PERSON)
    restriction, _ = engine.set_grant("session", "s-1", "staged", caller=PERSON,
                                      project=PROJECT)
    assert engine.revoke_widens(restriction.id) is True
    with pytest.raises(PersonOnly):
        engine.revoke(restriction.id, caller=JARVIS)
    assert engine.effective("s-1", PROJECT).level == "staged"
    # Jarvis may give back authority it holds: the project grant only narrows.
    assert engine.revoke_widens(project.id) is False
    assert engine.revoke(project.id, caller=JARVIS) is True
    # Fewer guarded classes than the fallback is a restriction too.
    engine.set_grant("project", PROJECT, "full", ["deploy", "secret_send"], caller=PERSON)
    narrow, _ = engine.set_grant("session", "s-1", "full", ["deploy"], caller=PERSON,
                                 project=PROJECT)
    assert engine.revoke_widens(narrow.id) is True
    # A session grant with no recorded project is judged against every project grant.
    unknown, _ = engine.set_grant("session", "s-9", "staged", caller=PERSON)
    assert engine.revoke_widens(unknown.id) is True
    assert engine.revoke(unknown.id, caller=PERSON) is True


@pytest.mark.parametrize(
    ("caller", "allowed"),
    [
        (PERSON, True),
        (Caller(kind="connection", surface=None), True),
        (Caller(kind="connection", surface="tui"), True),
        (Caller(kind="connection", surface="web"), True),
        (Caller(kind="connection", surface="browser"), False),
        (Caller(kind="connection", surface="connector"), False),
        (Caller(kind="connection", surface="something-new"), False),
        (Caller.from_connection(None), False),
        (JARVIS, False),
        (SYSTEM, False),
        (Caller.messenger("telegram", bound_person=True), True),
        (Caller.messenger("telegram", bound_person=False), False),
    ],
)
def test_only_the_person_sets_grants(
    engine: DelegationEngine, caller: Caller, allowed: bool
) -> None:
    if allowed:
        grant, _ = engine.set_grant("project", PROJECT, "full", ["deploy"], caller=caller)
        assert grant.grantedBy == caller.label
    else:
        with pytest.raises(PersonOnly):
            engine.set_grant("project", PROJECT, "full", ["deploy"], caller=caller)
        assert engine.list_grants() == []


def test_messenger_grants_follow_the_setting(engine: DelegationEngine, settings: Settings) -> None:
    settings.jarvis.messenger.allowGrants = False
    with pytest.raises(PersonOnly, match="allowGrants"):
        engine.set_grant("session", "s-1", "full", caller=Caller.messenger(
            "slack", bound_person=True))


def test_a_remote_web_login_may_not_hand_over_guarded_classes(engine: DelegationEngine) -> None:
    remote = Caller(kind="connection", surface="web", remote=True)
    with pytest.raises(PersonOnly, match="local connection"):
        engine.set_grant("project", PROJECT, "staged", ["secret_send"], caller=remote)
    grant, _ = engine.set_grant("project", PROJECT, "full", caller=remote)
    assert grant.level == "full"


def test_grants_live_outside_settings(tmp_path: Path, clock: Clock) -> None:
    settings = Settings()
    engine = DelegationEngine(tmp_path / "home", lambda: settings, now=clock)
    grant, _ = engine.set_grant("session", "s-1", "staged", caller=PERSON)
    assert engine.db_path == tmp_path / "home" / "jarvis" / "delegation.db"
    # Whatever a worker writes into settings.json, grants do not move.
    forged = Settings.model_validate(
        {"jarvis": {"delegation": {"default": "full"}, "messenger": {"allowGrants": True}}}
    )
    settings = forged
    engine._settings = lambda: settings  # a settings reload
    assert engine.list_grants() == [grant]
    assert engine.effective("s-1").level == "staged"
    engine.close()
    reopened = DelegationEngine(tmp_path / "home", lambda: settings, now=clock)
    assert reopened.list_grants() == [grant]
    reopened.close()


# -- routing ------------------------------------------------------------------------


def test_routing_by_class_and_level(engine: DelegationEngine) -> None:
    assert engine.route(_req("git status")).route == "auto"
    risky = engine.route(_req("npm install"))
    assert (risky.route, risky.reason, risky.level) == ("person", "risky:staged", "staged")
    deploy = engine.route(_req("git push"))
    assert (deploy.route, deploy.missing) == ("person", ("deploy",))
    engine.set_grant("project", PROJECT, "full", caller=PERSON)
    assert engine.route(_req("npm install")).route == "jarvis"
    assert engine.route(_req("git push")).route == "person"
    grant, _ = engine.set_grant("session", "s-1", "staged", ["deploy"], caller=PERSON,
                                project=PROJECT)
    allowed = engine.route(_req("git push"))
    assert (allowed.route, allowed.grant_id, allowed.reason) == ("jarvis", grant.id,
                                                                  "guarded:allowed")
    # Allowing one class does not cover a request in two.
    both = engine.route(_req("git push --force"))
    assert (both.route, both.missing) == ("person", ("outside_destructive",))
    # Under staged, risky still goes to the person, even with guarded grants.
    assert engine.route(_req("npm install")).route == "person"
    # An opaque line needs every class.
    assert engine.route(_req("bash -c 'git push'")).missing == (
        "outside_destructive", "secret_send")


def test_never_and_the_jarvis_session(engine: DelegationEngine) -> None:
    engine.set_grant("project", PROJECT, "full", ["deploy", "outside_destructive",
                                                  "secret_send"], caller=PERSON)
    assert engine.route(_req("cat ~/.snowpea/settings.json")).route == "person"
    assert engine.route(_req("ls", jarvis_session=True)).route == "person"
    engine.jarvis_session_id = "s-jarvis"
    own = engine.route(_req("npm install", session="s-jarvis"))
    assert (own.route, own.verdict.classification) == ("person", "never")
    assert engine.route(_req("ls", browser_owned=True)).route == "person"
    assert engine.route(_req("npm install")).route == "jarvis"


def test_hidden_projects_budget_and_no_session(engine: DelegationEngine) -> None:
    engine.set_grant("global", None, "full", caller=PERSON)
    assert engine.route(_req("npm install"), exposed=False).reason == "not-exposed"
    assert engine.route(_req("git status"), exposed=False).route == "person"
    assert engine.route(_req("npm install"), budget_exhausted=True).route == "person"
    no_session = ClassifyRequest(tool="shell", args={"command": "ls"}, tags=("exec",))
    assert engine.route(no_session).route == "person"


# -- pauses -------------------------------------------------------------------------


def test_typing_pauses_jarvis_for_the_quiet_period(
    engine: DelegationEngine, clock: Clock, settings: Settings
) -> None:
    engine.set_grant("project", PROJECT, "full", caller=PERSON)
    state = engine.note_human_input("s-1", project=PROJECT, surface="ide")
    assert (state.paused, state.reason, state.until) == (True, "typing",
                                                         "2026-11-15T09:10:00.000Z")
    assert engine.route(_req("npm install")).reason == "paused:typing"
    assert engine.route(_req("git status")).route == "person"  # the person is here
    clock.advance(minutes=6)
    engine.note_human_input("s-1")  # each input moves the end
    clock.advance(minutes=6)
    assert engine.route(_req("npm install")).route == "person"
    clock.advance(minutes=5)
    assert engine.route(_req("npm install")).route == "jarvis"
    assert engine.paused() == []
    settings.jarvis.quietMinutes = 3
    assert engine.note_human_input("s-1").until == "2026-11-15T09:20:00.000Z"
    view = engine.session_state("s-1", project=PROJECT, enabled=True)
    assert (view.level, view.pausedUntil, view.takenOver) == ("full",
                                                             "2026-11-15T09:20:00.000Z", False)


def test_takeover_and_handback(engine: DelegationEngine, clock: Clock) -> None:
    engine.set_grant("session", "s-1", "full", caller=PERSON)  # no expiry
    heard: list[tuple[str, bool]] = []
    engine.pause_listeners.append(lambda key, state: heard.append((key, state.paused)))
    state = engine.takeover("s-1", project=PROJECT, caller=PERSON)
    assert state.taken_over and state.until is None
    # Typing during an open-ended takeover changes nothing.
    assert engine.note_human_input("s-1").taken_over
    clock.advance(days=3)
    assert engine.route(_req("npm install")).reason == "paused:takeover"
    paused = engine.paused()
    assert [(p.sessionId, p.reason, p.until) for p in paused] == [("s-1", "takeover", None)]
    view = engine.session_state("s-1", enabled=True)
    assert view.takenOver is True and view.pausedUntil is None
    # It survives a restart.
    again = DelegationEngine(engine.home, engine._settings, now=clock)
    assert again.pause_state("s-1").taken_over
    again.close()
    with pytest.raises(PersonOnly):
        engine.handback("s-1", caller=JARVIS)
    with pytest.raises(PersonOnly):
        engine.handback("s-1", caller=Caller(kind="connection", surface="browser"))
    assert engine.handback("s-1", caller=PERSON).paused is False
    assert engine.route(_req("npm install")).route == "jarvis"
    assert heard == [("s-1", True), ("s-1", False)]


def test_a_timed_takeover_ends_by_itself(engine: DelegationEngine, clock: Clock) -> None:
    state = engine.takeover(external={"cli": "claude", "id": "abc"}, minutes=30, caller=PERSON)
    assert state.until == "2026-11-15T09:30:00.000Z" and not state.taken_over
    view = engine.session_state(external={"cli": "claude", "id": "abc"}, enabled=False)
    assert view.pausedUntil == "2026-11-15T09:30:00.000Z" and view.level is None
    assert [p.external.id for p in engine.paused() if p.external] == ["abc"]
    clock.advance(minutes=31)
    assert engine.paused() == []
    with pytest.raises(DelegationError):
        engine.takeover("s-1", minutes=0, caller=PERSON)
    with pytest.raises(DelegationError):
        engine.takeover("s-1", minutes=10081, caller=PERSON)


# -- the server-side re-check -------------------------------------------------------


def test_validate_decision(engine: DelegationEngine) -> None:
    engine.set_grant("project", PROJECT, "full", caller=PERSON)
    risky = _req("npm install")
    engine.route(risky, request_id="ap-1")
    assert engine.validate_decision(risky, "allow", request_id="ap-1").route == "jarvis"
    assert engine.validate_decision(risky, "deny", request_id="ap-1").route == "jarvis"
    for bad in (
        lambda: engine.validate_decision(risky, "maybe", request_id="ap-1"),
        lambda: engine.validate_decision(risky, "allow", scope="always", request_id="ap-1"),
        lambda: engine.validate_decision(risky, "allow", scope="session", request_id="ap-1"),
        lambda: engine.validate_decision(risky, "allow", request_id="ap-unknown"),
    ):
        with pytest.raises(DecisionRefused):
            bad()
    guarded = _req("git push")
    engine.route(guarded, request_id="ap-2")
    with pytest.raises(DecisionRefused, match="routed to person"):
        engine.validate_decision(guarded, "allow", request_id="ap-2")
    # Without a remembered route the engine still re-classifies.
    with pytest.raises(DecisionRefused):
        engine.validate_decision(guarded, "allow")
    with pytest.raises(DecisionRefused):
        engine.validate_decision(_req("cat .snowpea/settings.json"), "deny")
    engine.forget("ap-1")
    assert engine.routed("ap-1") is None


def test_validate_decision_sees_changes_since_the_route(engine: DelegationEngine) -> None:
    grant, _ = engine.set_grant("project", PROJECT, "full", caller=PERSON)
    risky = _req("npm install")
    engine.route(risky, request_id="ap-1")
    engine.revoke(grant.id, caller=PERSON)
    with pytest.raises(DecisionRefused, match="now routed to person"):
        engine.validate_decision(risky, "allow", request_id="ap-1")
    engine.set_grant("project", PROJECT, "full", caller=PERSON)
    engine.route(risky, request_id="ap-2")
    engine.note_human_input("s-1")
    with pytest.raises(DecisionRefused, match="paused"):
        engine.validate_decision(risky, "allow", request_id="ap-2")
    engine.handback("s-1", caller=PERSON)
    with pytest.raises(DecisionRefused, match="budget"):
        engine.validate_decision(risky, "allow", request_id="ap-2", budget_exhausted=True)
    engine.jarvis_session_id = "s-1"
    with pytest.raises(DecisionRefused, match="never"):
        engine.validate_decision(risky, "allow", request_id="ap-2")


def test_question_routing(engine: DelegationEngine) -> None:
    assert engine.route_question("s-1") == "jarvis"
    assert engine.route_question("s-1", exposed=False) == "person"
    assert engine.route_question("s-1", browser_owned=True) == "person"
    assert engine.route_question(None) == "person"
    engine.jarvis_session_id = "s-j"
    assert engine.route_question("s-j") == "person"
    engine.note_human_input("s-1")
    assert engine.route_question("s-1") == "person"
    with pytest.raises(DecisionRefused):
        engine.validate_question_answer("s-1")
    engine.validate_question_answer("s-2")


def test_every_change_lands_in_the_ledger(engine: DelegationEngine) -> None:
    grant, _ = engine.set_grant("project", PROJECT, "full", caller=PERSON)
    engine.set_grant("project", PROJECT, "staged", caller=PERSON)
    engine.takeover("s-1", caller=PERSON)
    engine.handback("s-1", caller=PERSON)
    records = list(engine.ledger.entries())
    assert [(r["type"], r.get("action")) for r in reversed(records)] == [
        ("grant", "set"), ("grant", "replaced"), ("grant", "set"),
        ("pause", "takeover"), ("pause", "handback"),
    ]
    assert records[-1]["grant"]["id"] == grant.id


# -- over RPC --------------------------------------------------------------------------


async def _client(http: aiohttp.ClientSession, daemon: Any, kind: str | None) -> RpcClient:
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    client = RpcClient(ws)
    client.start()
    hello: dict[str, Any] = {"token": daemon.token, "clientVersion": "test",
                             "protocolVersion": PROTOCOL_VERSION}
    if kind is not None:
        hello["clientKind"] = kind
    result = await client.ok("system.hello", hello)
    assert "jarvisDelegation" in result["capabilities"]
    return client


def _code(frame: dict[str, Any]) -> str:
    return str(frame["error"]["data"]["code"])


async def test_rpc_grants_are_person_only(tmp_path: Path, http: aiohttp.ClientSession) -> None:
    daemon = await make_daemon(tmp_path / "home")
    person = browser = connector = None
    try:
        person = await _client(http, daemon, None)
        browser = await _client(http, daemon, "browser")
        connector = await _client(http, daemon, "connector")
        root = str(tmp_path / "proj")
        for client in (browser, connector):
            frame = await client.call("jarvis.delegation.set",
                                      {"scope": "project", "target": root, "level": "full"})
            assert _code(frame) == "jarvis_person_only"
        assert (await person.ok("jarvis.delegation.list"))["grants"] == []
        frame = await person.call("jarvis.delegation.set",
                                  {"scope": "global", "level": "full", "allow": ["deploy"]})
        assert _code(frame) == "invalid_params"
        result = await person.ok("jarvis.delegation.set", {
            "scope": "project", "target": root, "level": "full", "allow": ["deploy"],
            "note": "release week",
        })
        grant = result["grant"]
        assert grant["grantedBy"] == "local" and grant["allow"] == ["deploy"]
        assert result["replaced"] is None
        changed = await browser.wait_notification("jarvis.delegation.changed")
        assert changed["reason"] == "set" and changed["grant"]["id"] == grant["id"]
        listed = await browser.ok("jarvis.delegation.list", {"scope": "project"})
        assert [g["id"] for g in listed["grants"]] == [grant["id"]]
        assert listed["default"] == "staged"
        # A restriction under it: lifting that is the person's, even over RPC.
        restriction = (await person.ok("jarvis.delegation.set", {
            "scope": "session", "target": "s-x", "level": "staged"}))["grant"]
        frame = await browser.call("jarvis.delegation.revoke", {"id": restriction["id"]})
        assert _code(frame) == "jarvis_person_only"
        # Narrowing is open to any connection.
        assert (await browser.ok("jarvis.delegation.revoke", {"id": grant["id"]}))["revoked"]
        assert (await person.ok("jarvis.delegation.revoke", {"id": grant["id"]})) == {
            "revoked": False}
        frame = await person.call("jarvis.delegation.revoke", {"id": "jg-nope"})
        assert _code(frame) == "not_found"
        assert (await person.ok("jarvis.delegation.list", {"includeExpired": True}))["grants"]
    finally:
        for client in (person, browser, connector):
            if client is not None:
                await client.stop()
        await daemon.stop()


async def test_rpc_takeover_handback_and_ledger(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    person = browser = None
    try:
        core = daemon.core
        project = tmp_path / "proj"
        project.mkdir()
        session = await core.sessions.create(project, mode="auto")
        person = await _client(http, daemon, "tui")
        browser = await _client(http, daemon, "browser")
        state = (await browser.ok("jarvis.takeover", {"sessionId": session.id}))["state"]
        assert state["takenOver"] is True and state["exposed"] is True
        assert state["level"] is None  # Jarvis mode is off
        frame = await browser.call("jarvis.handback", {"sessionId": session.id})
        assert _code(frame) == "jarvis_person_only"
        timed = (await person.ok("jarvis.takeover",
                                 {"sessionId": session.id, "minutes": 5}))["state"]
        assert timed["takenOver"] is False and timed["pausedUntil"]
        state = (await person.ok("jarvis.handback", {"sessionId": session.id}))["state"]
        assert state == {"exposed": True, "level": None, "pausedUntil": None,
                         "takenOver": False}
        assert _code(await person.call("jarvis.takeover", {"sessionId": "nope"})) == "not_found"
        both = {"sessionId": session.id, "external": {"cli": "codex", "id": "t"}}
        assert _code(await person.call("jarvis.takeover", both)) == "invalid_params"
        assert _code(await person.call("jarvis.takeover", {})) == "invalid_params"
        ext = (await person.ok("jarvis.takeover",
                               {"external": {"cli": "codex", "id": "t"}}))["state"]
        assert ext["takenOver"] is True

        engine = engine_for(core)
        assert engine is engine_for(core)
        engine.ledger.record_decision(kind="approval", decided_by="auto", summary="ran ls",
                                      session_id=session.id, decision="allow", tool="shell")
        decided = await person.wait_notification("jarvis.decision")
        assert decided["entry"]["summary"] == "ran ls"
        for n in range(3):
            engine.ledger.record_decision(kind="approval", decided_by="jarvis",
                                          summary=f"npm install {n}", session_id="other")
        page = await person.ok("jarvis.ledger", {"limit": 2})
        assert [e["summary"] for e in page["entries"]] == ["npm install 2", "npm install 1"]
        rest = await person.ok("jarvis.ledger", {"limit": 10, "before": page["nextBefore"]})
        assert [e["summary"] for e in rest["entries"]] == ["npm install 0", "ran ls"]
        assert rest["nextBefore"] is None
        mine = await person.ok("jarvis.ledger", {"sessionId": session.id})
        assert [e["decidedBy"] for e in mine["entries"]] == ["auto"]
        assert _code(await person.call("jarvis.ledger", {"before": "bogus"})) == "invalid_params"
    finally:
        for client in (person, browser):
            if client is not None:
                await client.stop()
        await daemon.stop()


async def test_a_settings_write_does_not_touch_grants(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    home = tmp_path / "home"
    daemon = await make_daemon(home)
    person = None
    try:
        person = await _client(http, daemon, None)
        grant = (await person.ok("jarvis.delegation.set", {
            "scope": "session", "target": "s-1", "level": "staged"}))["grant"]
        settings_file = home / "settings.json"
        current = json.loads(settings_file.read_text()) if settings_file.exists() else {}
        current["jarvis"] = {"delegation": {"default": "full"}}
        settings_file.write_text(json.dumps(current))
        await person.ok("system.reloadSettings")
        listed = await person.ok("jarvis.delegation.list")
        assert [g["id"] for g in listed["grants"]] == [grant["id"]]
        assert listed["default"] == "full"
        engine = engine_for(daemon.core)
        assert engine.effective("s-1").level == "staged"
        # And a worker's write to the store itself is never Jarvis's to allow.
        for path in (settings_file, engine.db_path):
            write = ClassifyRequest(tool="write_file", args={"path": str(path)},
                                    tags=("write",), workdir=str(tmp_path), home=str(home),
                                    session_id="s-1")
            assert engine.route(write).verdict.classification == "never"
    finally:
        if person is not None:
            await person.stop()
        await daemon.stop()


async def test_handlers_see_a_remote_login(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from snowpea_core.server.errors import RpcError
    from snowpea_core.server.jarvis_delegation_handlers import delegation_set_handler
    from snowpea_core.server.protocol import JarvisDelegationSetParams
    from snowpea_core.server.rpc import RpcConnection

    async def nothing(_frame: dict[str, Any]) -> None:
        return None

    assert RpcConnection(nothing).remote_login is False
    daemon = await make_daemon(tmp_path / "home")
    try:
        remote = SimpleNamespace(client_kind="web", remote_login=True)
        params = JarvisDelegationSetParams(scope="project", target=str(tmp_path), level="full",
                                           allow=["deploy"])
        with pytest.raises(RpcError) as caught:
            await delegation_set_handler(remote, params, daemon.core)  # type: ignore[arg-type]
        assert caught.value.code == "jarvis_person_only"
        params = JarvisDelegationSetParams(scope="project", target=str(tmp_path), level="full")
        result = await delegation_set_handler(remote, params, daemon.core)  # type: ignore[arg-type]
        assert result.grant.grantedBy == "web"
        with pytest.raises(RpcError):
            await delegation_set_handler(None, params, daemon.core)
    finally:
        await daemon.stop()


async def test_the_remote_listener_marks_its_connections(tmp_path: Path) -> None:
    from snowpea_core.server import transport_ws

    seen: list[Any] = []

    class Dispatcher:
        core = None

        async def handle_message(self, conn: Any, _raw: Any) -> None:
            seen.append(conn)

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    def build(track: bool) -> web.Application:
        app = web.Application()
        app[transport_ws.DISPATCHER_KEY] = Dispatcher()
        app[transport_ws.CONNECTIONS_KEY] = set()
        app[transport_ws.SOCKETS_KEY] = set()
        tracked: set[Any] = set()

        async def handler(request: web.Request) -> Any:
            return await transport_ws.websocket_handler(
                request, track=tracked if track else None
            )

        app.router.add_get("/ws", handler)
        return app

    for track in (False, True):
        async with TestClient(TestServer(build(track))) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_str("{}")
            for _ in range(100):
                if len(seen) == (2 if track else 1):
                    break
                await asyncio.sleep(0.01)
            await ws.close()
    assert [conn.remote_login for conn in seen] == [False, True]


def test_capability_moved_out_of_pending() -> None:
    assert "jarvisDelegation" in protocol.CAPABILITIES
    assert "jarvisDelegation" not in protocol.PENDING_CAPABILITIES
    wp2 = {"jarvis.ledger", "jarvis.delegation.list", "jarvis.delegation.set",
           "jarvis.delegation.revoke", "jarvis.takeover", "jarvis.handback"}
    assert wp2 <= protocol.IMPLEMENTED_METHODS


def test_the_engine_judges_paths_against_its_own_home(engine: DelegationEngine) -> None:
    engine.set_grant("global", None, "full", caller=PERSON)
    write = ClassifyRequest(tool="write_file", args={"path": str(engine.home / "token")},
                            tags=("write",), workdir=PROJECT, session_id="s-1")
    routed = engine.route(write)
    assert (routed.route, routed.verdict.classification) == ("person", "never")
