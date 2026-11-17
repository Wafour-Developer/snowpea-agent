"""A delegation grant from a remote web login (1.56.0, ``jarvis.delegation.remoteGrant``).

Off (the default): a remote login gives no grant, as security review 8 (H8-2)
left it — only the refusal now says where a local person turns it on.  On: the
remote password again (checked like a sign-in and counted with its failures),
one session or project, no guarded classes, at most 24 h; the grant is marked
and ledgered; Jarvis, a connector, a browser and in-process callers still give
nothing; the switch itself is a local person's (settings.set, a file edit, the
agent's settings tool); a degraded document reads it as off; the password is
never in a log, the ledger, the store or an event.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from _support import RpcClient, make_daemon

from snowpea_core.config import hot_reload
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.jarvis import remote_grant
from snowpea_core.jarvis.delegation import (
    JARVIS,
    SYSTEM,
    Caller,
    DelegationEngine,
    DelegationError,
    PersonOnly,
    RemoteGrantRefused,
)
from snowpea_core.providers.base import ChatMessage, StreamEvent, Usage
from snowpea_core.server import errors, settings_gate, settings_handlers, web_remote
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import PROTOCOL_VERSION
from snowpea_core.tools.registry import ToolContext

PASSWORD = "Remote-Pass-7f3c!correct"
WRONG = "Remote-Pass-7f3c!wrong"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
REMOTE = Caller(kind="connection", surface="web", remote=True)
CONFIRMED = Caller(kind="connection", surface="web", remote=True, reauthenticated=True)
LOCAL = Caller(kind="connection", surface="ide")


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _settings(on: bool) -> Settings:
    settings = Settings()
    settings.jarvis.delegation.remoteGrant = on
    return settings


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _engine(tmp_path: Path, settings: Settings) -> DelegationEngine:
    return DelegationEngine(tmp_path / "home", lambda: settings, now=lambda: NOW)


# -- the engine: off ---------------------------------------------------------------------------


def test_off_a_remote_login_gives_nothing_and_is_told_where_to_turn_it_on(
    tmp_path: Path, project: Path
) -> None:
    engine = _engine(tmp_path, _settings(False))
    for caller in (REMOTE, CONFIRMED):  # a password changes nothing while it is off
        with pytest.raises(PersonOnly, match="local connection") as caught:
            engine.set_grant("session", "s-1", "full", caller=caller)
        assert "Settings > Jarvis > Delegation" in str(caught.value)
        assert "jarvis.delegation.remoteGrant" in str(caught.value)
    # Guarded classes: the refusal of before, word for word.
    with pytest.raises(PersonOnly, match="handing guarded classes") as caught:
        engine.set_grant("project", str(project), "staged", ["deploy"], caller=REMOTE)
    assert not isinstance(caught.value, RemoteGrantRefused)
    assert engine.list_grants() == []
    engine.close()


# -- the engine: on ----------------------------------------------------------------------------


def test_on_a_remote_grant_needs_the_password_again(tmp_path: Path) -> None:
    engine = _engine(tmp_path, _settings(True))
    with pytest.raises(RemoteGrantRefused) as caught:
        engine.set_grant("session", "s-1", "full", caller=REMOTE)
    assert caught.value.details() == {"reason": "reauth_required", "kind": "remote_login"}
    assert engine.list_grants() == []
    engine.close()


def test_on_a_confirmed_remote_grant_is_one_session_or_project_for_24_hours(
    tmp_path: Path, project: Path
) -> None:
    engine = _engine(tmp_path, _settings(True))
    grant, _ = engine.set_grant("session", "s-1", "full", caller=CONFIRMED)
    assert grant.remoteLogin is True and grant.level == "full" and grant.allow == []
    assert grant.expiresAt is not None and grant.expiresAt.startswith("2026-11-18T12:00:00")
    # A shorter expiry is kept; a staged project grant too.
    one_hour = _iso(NOW + timedelta(hours=1))
    other, _ = engine.set_grant("project", str(project), "staged", caller=CONFIRMED,
                                expires_at=one_hour)
    assert other.remoteLogin and other.expiresAt.startswith("2026-11-17T13:00:00")
    # A local grant is not marked.
    local, _ = engine.set_grant("session", "s-2", "full", caller=LOCAL)
    assert local.remoteLogin is False and local.expiresAt is None
    engine.close()


@pytest.mark.parametrize(
    ("scope", "allow", "expires", "what"),
    [
        ("global", [], None, "scope"),
        ("project", ["deploy"], None, "allow"),
        ("session", ["outside_destructive"], None, "allow"),
        ("session", ["secret_send"], None, "allow"),
        ("session", [], timedelta(hours=25), "expiry"),
        ("project", [], timedelta(days=30), "expiry"),
    ],
)
def test_on_each_limit_holds_in_the_engine(
    tmp_path: Path, project: Path, scope: str, allow: list[str],
    expires: timedelta | None, what: str,
) -> None:
    engine = _engine(tmp_path, _settings(True))
    target = None if scope == "global" else ("s-1" if scope == "session" else str(project))
    with pytest.raises(RemoteGrantRefused) as caught:
        engine.set_grant(scope, target, "full", allow, caller=CONFIRMED,
                         expires_at=_iso(NOW + expires) if expires else None)
    assert caught.value.details() == {"reason": "remote_grant_limit", "kind": "remote_login",
                                      "what": what}
    assert engine.list_grants() == []
    engine.close()


def test_on_an_expiry_a_little_past_24_hours_is_clamped(tmp_path: Path) -> None:
    """A client clock a little ahead asks for "24 h": it gets exactly 24 h."""
    engine = _engine(tmp_path, _settings(True))
    grant, _ = engine.set_grant("session", "s-1", "staged", caller=CONFIRMED,
                                expires_at=_iso(NOW + timedelta(hours=24, minutes=3)))
    assert grant.expiresAt.startswith("2026-11-18T12:00:00")
    with pytest.raises(DelegationError):  # in the past: as before
        engine.set_grant("session", "s-2", "staged", caller=CONFIRMED,
                         expires_at=_iso(NOW - timedelta(minutes=1)))
    engine.close()


def test_nobody_but_a_remote_person_turns_the_flag_into_a_grant(tmp_path: Path) -> None:
    """Jarvis, core itself, a connector, a browser: the flag is not theirs to use."""
    engine = _engine(tmp_path, _settings(True))
    for caller in (
        Caller(kind="jarvis", surface="jarvis", reauthenticated=True),
        Caller(kind="system", surface="core", reauthenticated=True),
        Caller(kind="connection", surface="connector", remote=True, reauthenticated=True),
        Caller(kind="connection", surface="browser", reauthenticated=True),
        JARVIS,
        SYSTEM,
    ):
        with pytest.raises(PersonOnly):
            engine.set_grant("session", "s-1", "full", caller=caller)
    assert engine.list_grants() == []
    engine.close()


def test_a_remote_grant_is_ledgered_and_revoked_from_a_remote_login(tmp_path: Path) -> None:
    engine = _engine(tmp_path, _settings(True))
    grant, _ = engine.set_grant("session", "s-1", "full", caller=CONFIRMED)
    records = [json.loads(line) for path in engine.ledger.files()
               for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    sets = [r for r in records if r.get("type") == "grant" and r.get("action") == "set"]
    assert sets and sets[-1]["grant"]["remoteLogin"] is True
    decisions = [r for r in records if r.get("type") == "decision"
                 and r.get("reason") == "remote-grant"]
    assert len(decisions) == 1 and decisions[0]["importance"] == "high"
    assert decisions[0]["grantId"] == grant.id
    # Revoking only narrows: a remote login may, without the password.
    assert engine.revoke(grant.id, caller=REMOTE) is True
    engine.close()


def test_a_store_from_before_remote_grants_opens(tmp_path: Path) -> None:
    import sqlite3

    home = tmp_path / "home"
    (home / "jarvis").mkdir(parents=True)
    db = sqlite3.connect(home / "jarvis" / "delegation.db")
    db.executescript(
        "CREATE TABLE grants (id TEXT PRIMARY KEY, scope TEXT NOT NULL, target TEXT, "
        "project TEXT, level TEXT NOT NULL, allow TEXT NOT NULL DEFAULT '', granted_by TEXT "
        "NOT NULL, proposed_by TEXT NOT NULL DEFAULT 'person', granted_at TEXT NOT NULL, "
        "expires_at TEXT, note TEXT NOT NULL DEFAULT '', revoked_at TEXT, revoked_by TEXT, "
        "expired_noticed INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO grants (id, scope, target, level, granted_by, granted_at) VALUES "
        "('jg-old', 'session', 's-9', 'full', 'ide', '2026-11-17T11:00:00.000Z');"
    )
    db.commit()
    db.close()
    engine = DelegationEngine(home, lambda: _settings(True), now=lambda: NOW)
    (old,) = engine.list_grants()
    assert old.id == "jg-old" and old.remoteLogin is False
    engine.close()


# -- the switch is a local person's -------------------------------------------------------------


def test_degraded_settings_read_the_switch_as_off(tmp_path: Path) -> None:
    on = _settings(True)
    assert remote_grant.enabled(on)
    for sections in (["jarvis"], ["*"], ["jarvis.delegation"]):
        broken = _settings(True)
        broken.mark_degraded(sections, close=False)
        assert not remote_grant.enabled(broken), sections
    # A jarvis section that does not validate loads at its defaults: off.
    paths = Paths.create(tmp_path / "home")
    paths.settings_json.write_text(json.dumps(
        {"jarvis": {"delegation": {"remoteGrant": True, "projectTtlHours": "many"}}}),
        encoding="utf-8")
    loaded = Settings.load(paths)
    assert "jarvis" in loaded.degraded_sections() and not remote_grant.enabled(loaded)
    assert not remote_grant.enabled(None)


def _conn(kind: str) -> Any:
    client_kind = {"person": "ide", "remote": "web", "connector": "connector"}.get(kind, "browser")
    return SimpleNamespace(authenticated=True, closed=False, client_kind=client_kind,
                           remote_login=kind == "remote", client_version="0.3.0",
                           client_id=f"{kind}-1", surface_id=f"{kind}-s")


@pytest.mark.parametrize("kind", ["remote", "connector", "browser"])
@pytest.mark.parametrize("value", [True, False, None])
def test_settings_set_of_the_switch_is_refused_to_every_non_local_kind(
    kind: str, value: Any
) -> None:
    core = SimpleNamespace(settings=_settings(value is False),
                           paths=SimpleNamespace(home=Path("/srv/sp-home")))
    for patch in ({"jarvis": {"delegation": {"remoteGrant": value}}},
                  {"jarvis": {"delegation": None}}, {"jarvis": None}):
        with pytest.raises(RpcError) as caught:
            settings_handlers.guard_patch(_conn(kind), patch, project=False,
                                          actor="person" if kind == "browser" else None,
                                          core=core)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
        if kind == "remote":
            assert caught.value.data["reason"] in ("local_only", "person_only")


def test_a_file_edit_of_the_switch_waits_for_a_local_person() -> None:
    held, waiting = hot_reload.hold_back(_settings(False), _settings(True))
    assert held.jarvis.delegation.remoteGrant is False
    assert waiting and any(key == "jarvis" or key.startswith("jarvis") for key in waiting)
    assert any(pattern[:3] == ("jarvis", "delegation", "remoteGrant") or pattern == ("jarvis",)
               for pattern in hot_reload.gated_patterns())


async def test_the_settings_tool_never_flips_the_switch(tmp_path: Path) -> None:
    from snowpea_core.tools.settings_tools import settings_set

    paths = Paths.create(tmp_path / "home")
    for start, value in ((False, True), (True, False)):
        settings = _settings(start)
        core = SimpleNamespace(settings=settings, paths=paths)
        ctx = ToolContext(session=SimpleNamespace(workdir=tmp_path), core=core,  # type: ignore[arg-type]
                          backend=None)
        for key, given in (("jarvis.delegation.remoteGrant", value),
                           ("jarvis.delegation", {"remoteGrant": value}),
                           ("jarvis", {"delegation": {"remoteGrant": value}})):
            result = await settings_set(ctx, {"key": key, "value": given})
            assert not result.ok and "local person" in (result.error or ""), key
        assert core.settings.jarvis.delegation.remoteGrant is start
        assert not paths.settings_json.exists()


# -- the dispatcher ------------------------------------------------------------------------------


def test_the_dispatcher_lets_a_remote_login_through_only_with_the_switch_on() -> None:
    method = "jarvis.delegation.set"
    off = SimpleNamespace(settings=_settings(False))
    with pytest.raises(RpcError) as caught:
        settings_gate.kind_method_refusal(method, {}, _conn("remote"), off)
    # Byte for byte as before but the message: the same details.
    assert caught.value.data == {"reason": "local_only", "kind": "remote_login", "what": method}
    assert "Settings > Jarvis > Delegation" in caught.value.message
    on = SimpleNamespace(settings=_settings(True))
    settings_gate.kind_method_refusal(method, {}, _conn("remote"), on)  # through
    for kind in ("connector", "browser"):
        with pytest.raises(RpcError):
            settings_gate.kind_method_refusal(method, {}, _conn(kind), on)
            settings_gate.browser_method_refusal(method, {"actor": "person"}, _conn(kind), on)
    # The other local-only methods do not follow the switch.
    for other in settings_gate.REMOTE_LOGIN_LOCAL_ONLY - {method}:
        with pytest.raises(RpcError):
            settings_gate.kind_method_refusal(other, {}, _conn("remote"), on)


# -- the remote listener: the password again, under the sign-in limits ---------------------------


def _auth(paths: Paths) -> web_remote.RemoteAuth:
    web_remote.set_password(paths, PASSWORD)
    auth = web_remote.RemoteAuth("owner", 12, paths)
    auth.failure_delay = 0.0

    async def no_wait(_seconds: float) -> None:
        return None

    auth.sleep = no_wait  # type: ignore[assignment]
    return auth


async def test_reauthenticate_counts_with_the_sign_in(tmp_path: Path) -> None:
    auth = _auth(Paths.create(tmp_path / "home"))
    cookie = auth.open_session(via="password")
    assert await auth.reauthenticate(cookie, "10.0.0.7", PASSWORD) == "ok"
    for _ in range(web_remote.MAX_FAILURES_PER_IP):
        assert await auth.reauthenticate(cookie, "10.0.0.7", WRONG) == "failed"
    # Over the per-address limit: answered "too many", and a sign-in from there is too.
    outcome = await auth.reauthenticate(cookie, "10.0.0.7", WRONG)
    assert outcome[0] == "rate_limited" and outcome[1] >= 1
    assert auth.retry_after("10.0.0.7") >= 1
    assert auth.retry_after("10.0.0.8") == 0  # another address is not held up
    # A right password still passes (the limits cost time, never lock out).
    assert await auth.reauthenticate(cookie, "10.0.0.7", PASSWORD) == "ok"
    # Malformed and empty count too.
    assert await auth.reauthenticate(cookie, "10.0.0.9", "") == "failed"
    assert await auth.reauthenticate(cookie, "10.0.0.9", None) == "failed"


async def test_a_link_login_cannot_reauthenticate(tmp_path: Path) -> None:
    auth = _auth(Paths.create(tmp_path / "home"))
    link = auth.open_session()  # the access link (or no password at the time)
    assert await auth.reauthenticate(link, "10.0.0.7", PASSWORD) == "no_password"
    assert await auth.reauthenticate("no-such-login", "10.0.0.7", PASSWORD) == "logged_out"
    # A password login whose password was cleared since is logged out.
    password = auth.open_session(via="password")
    web_remote.clear_password(auth.paths)
    assert await auth.reauthenticate(password, "10.0.0.7", PASSWORD) == "logged_out"


async def test_reauthenticate_needs_the_listeners_check() -> None:
    for conn in (SimpleNamespace(), SimpleNamespace(remote_reauth=None)):
        with pytest.raises(RemoteGrantRefused) as caught:
            await remote_grant.reauthenticate(conn, PASSWORD)
        assert caught.value.reason == "reauth_unavailable"
    with pytest.raises(RemoteGrantRefused) as caught:
        await remote_grant.reauthenticate(SimpleNamespace(), None)
    assert caught.value.reason == "reauth_required"


def test_a_validation_error_never_echoes_the_password() -> None:
    from pydantic import ValidationError

    from snowpea_core.server.protocol import JarvisDelegationSetParams
    from snowpea_core.server.rpc import _without_secrets

    raw = {"scope": "session", "target": "s-1", "level": "everything", "reauth": PASSWORD,
           "extra": PASSWORD}
    with pytest.raises(ValidationError) as caught:
        JarvisDelegationSetParams.model_validate(raw)
    assert PASSWORD not in json.dumps(_without_secrets(caught.value, raw), default=str)


# -- end to end: a daemon with its remote listener ----------------------------------------------


class _Quiet:
    vendor = "fake"
    model = "fake-1"

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=5, output_tokens=1))
        yield StreamEvent(kind="text_delta", text="ok")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _remote_daemon(tmp_path: Path, *, on: bool) -> Any:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    web_remote.set_password(Paths(home=home), PASSWORD)
    daemon = await make_daemon(home, {
        "jarvis": {"enabled": True, "folder": str(tmp_path / "JarvisFolder"),
                   "delegation": {"remoteGrant": on}},
        "web": {"remote": {"enabled": True, "host": "127.0.0.1", "port": 0,
                           "username": "owner"}},
    })
    daemon.core.providers.get = lambda _p, _m: _Quiet()
    auth = daemon.remote_auth
    auth.failure_delay = 0.0

    async def no_wait(_seconds: float) -> None:
        return None

    auth.sleep = no_wait
    return daemon


async def _remote_client(http: aiohttp.ClientSession, daemon: Any) -> RpcClient:
    base = f"http://127.0.0.1:{daemon.remote_status['port']}"
    async with http.post(f"{base}/web/login",
                         json={"username": "owner", "password": PASSWORD}) as ok:
        assert ok.status == 200
    async with http.get(f"{base}/web/session") as session:
        capability = (await session.json())["token"]
    ws = await http.ws_connect(f"{base}/ws", headers={"Origin": base})
    client = RpcClient(ws, approval_mode="ignore")
    client.start()
    await client.ok("system.hello", {"token": capability, "clientVersion": "test",
                                     "clientKind": "web", "protocolVersion": PROTOCOL_VERSION})
    return client


def _details(frame: dict[str, Any]) -> dict[str, Any]:
    data = (frame.get("error") or {}).get("data") or {}
    details = data.get("details")
    return details if isinstance(details, dict) else {}


def _reason(frame: dict[str, Any]) -> str | None:
    return _details(frame).get("reason")


def _everything_on_disk(home: Path) -> bytes:
    found = b""
    for path in sorted(home.rglob("*")):
        if path.is_file():
            found += path.read_bytes()
    return found


async def test_end_to_end_off_is_refused_with_where_to_turn_it_on(tmp_path: Path) -> None:
    daemon = await _remote_daemon(tmp_path, on=False)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            remote = await _remote_client(http, daemon)
            frame = await remote.call("jarvis.delegation.set", {
                "scope": "session", "target": "s-1", "level": "full", "reauth": PASSWORD})
            assert frame["error"]["data"]["code"] == errors.JARVIS_PERSON_ONLY
            assert _reason(frame) == "local_only"
            assert "Settings > Jarvis > Delegation" in json.dumps(frame)
            assert PASSWORD not in json.dumps(frame)
            # Nor can it turn the switch on.
            frame = await remote.call("settings.set", {
                "patch": {"jarvis": {"delegation": {"remoteGrant": True}}}})
            assert frame["error"]["data"]["code"] == errors.JARVIS_PERSON_ONLY
            assert daemon.core.settings.jarvis.delegation.remoteGrant is False
            await remote.stop()
        assert daemon.core.jarvis.engine.list_grants() == []
    finally:
        await daemon.stop()


async def test_end_to_end_on(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _remote_daemon(tmp_path, on=True)
    home = tmp_path / "home"
    try:
        engine = daemon.core.jarvis.engine
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            remote = await _remote_client(http, daemon)
            base = {"scope": "session", "target": "s-1", "level": "full"}
            # No password: asked for it, nothing counted.
            frame = await remote.call("jarvis.delegation.set", base)
            assert _reason(frame) == "reauth_required"
            # Beyond the limits: refused before any password is checked.
            for params, what in (
                ({"scope": "global", "level": "full"}, "scope"),
                ({**base, "allow": ["deploy"]}, "allow"),
                ({**base, "expiresAt": _iso(datetime.now(UTC) + timedelta(hours=30))}, "expiry"),
            ):
                frame = await remote.call("jarvis.delegation.set", {**params, "reauth": WRONG})
                assert _reason(frame) == "remote_grant_limit", frame
                assert _details(frame)["what"] == what
            assert daemon.remote_auth.retry_after("127.0.0.1") == 0
            # A wrong password: refused, and counted with the sign-in failures.
            frame = await remote.call("jarvis.delegation.set", {**base, "reauth": WRONG})
            assert _reason(frame) == "reauth_failed"
            assert len(daemon.remote_auth._ip_failures["127.0.0.1"]) == 1
            # The right one: a session grant, marked, for 24 h.
            done = await remote.ok("jarvis.delegation.set", {**base, "reauth": PASSWORD})
            grant = done["grant"]
            assert grant["remoteLogin"] is True and grant["scope"] == "session"
            ends = datetime.fromisoformat(grant["expiresAt"].replace("Z", "+00:00"))
            assert timedelta(hours=23, minutes=58) < ends - datetime.now(UTC) <= timedelta(
                hours=24)
            # Listed as such; revoking from the remote login is allowed.
            listed = await remote.ok("jarvis.delegation.list", {})
            assert [g["remoteLogin"] for g in listed["grants"]] == [True]
            assert (await remote.ok("jarvis.delegation.revoke", {"id": grant["id"]}))["revoked"]
            # A flood of wrong passwords: "too many attempts".
            reasons = []
            for _ in range(web_remote.MAX_FAILURES_PER_IP + 1):
                frame = await remote.call("jarvis.delegation.set", {**base, "reauth": WRONG})
                reasons.append(_reason(frame))
            assert reasons[-1] == "reauth_rate_limited", reasons
            assert _details(frame)["retryAfter"] >= 1
            # ... which the sign-in page feels too.
            port = daemon.remote_status["port"]
            async with http.post(f"http://127.0.0.1:{port}/web/login",
                                 json={"username": "owner", "password": WRONG}) as login:
                assert login.status == 429
            # The switch stays a local person's.
            frame = await remote.call("settings.set", {
                "patch": {"jarvis": {"delegation": {"remoteGrant": False}}}})
            assert frame["error"]["data"]["code"] == errors.JARVIS_PERSON_ONLY
            seen = json.dumps(remote.notifications)
            await remote.stop()
        assert engine.list_grants() == []
        # The password is nowhere: not in a log record, an event, the ledger or the store.
        assert PASSWORD not in seen and WRONG not in seen
        for record in caplog.records:
            text = record.getMessage() + repr(record.args)
            assert PASSWORD not in text and WRONG not in text, record.getMessage()
        engine.close()
        stored = _everything_on_disk(home)
        assert PASSWORD.encode() not in stored and WRONG.encode() not in stored
        assert b"remote-grant" in stored  # the ledger has the remote grant
    finally:
        await daemon.stop()


async def test_end_to_end_jarvis_proposal_confirmed_from_a_remote_login(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _remote_daemon(tmp_path, on=True)
    try:
        core = daemon.core
        engine = core.jarvis.engine
        jarvis = core.jarvis.session()
        tool = jarvis.session_tools["delegation_set"]

        def ctx() -> ToolContext:
            jarvis.current_turn = "t-human"
            jarvis.turn_origins["t-human"] = "human"
            return ToolContext(session=jarvis, core=core, backend=None)  # type: ignore[arg-type]

        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            remote = await _remote_client(http, daemon)
            # Jarvis proposes a project grant with an exception item: not a remote's.
            proposal = asyncio.ensure_future(tool.run(ctx(), {
                "scope": "project", "target": str(project), "level": "full",
                "allow": ["deploy"]}))
            request = (await remote.wait_notification("approval.pending"))["request"]
            frame = await remote.call("approval.respond", {
                "requestId": request["requestId"], "decision": "allow", "reauth": PASSWORD})
            assert _reason(frame) == "remote_grant_limit"
            assert _details(frame)["what"] == "allow"
            assert core.approvals.list()  # still pending, for a local person
            await remote.ok("approval.respond", {"requestId": request["requestId"],
                                                 "decision": "deny"})
            assert (await asyncio.wait_for(proposal, 5)).meta["granted"] is False
            remote.notifications.clear()

            # A session-or-project proposal: no password, a wrong one, then the right one.
            proposal = asyncio.ensure_future(tool.run(ctx(), {
                "scope": "project", "target": str(project), "level": "full",
                "expiresAt": _iso(datetime.now(UTC) + timedelta(days=3))}))
            request = (await remote.wait_notification("approval.pending"))["request"]
            answer = {"requestId": request["requestId"], "decision": "allow"}
            assert _reason(await remote.call("approval.respond", answer)) == "reauth_required"
            assert _reason(await remote.call("approval.respond",
                                             {**answer, "reauth": WRONG})) == "reauth_failed"
            eight = datetime.now(UTC) + timedelta(hours=8)
            await remote.ok("approval.respond", {**answer, "reauth": PASSWORD,
                                                 "grantExpiresAt": _iso(eight)})
            result = await asyncio.wait_for(proposal, 5)
            assert result.ok and result.meta["granted"] is True, result.error
            (grant,) = engine.list_grants()
            assert grant.remoteLogin and grant.proposedBy == "jarvis" and grant.allow == []
            ends = datetime.fromisoformat(grant.expiresAt.replace("Z", "+00:00"))
            assert abs((ends - eight).total_seconds()) < 2  # the person's choice, not 3 days
            # Jarvis itself cannot answer a proposal, whatever it carries.
            with pytest.raises(RpcError):
                await core.approvals.respond("nope", "allow", by="jarvis", reauth=PASSWORD)
            seen = json.dumps(remote.notifications)
            await remote.stop()
        assert PASSWORD not in seen
        for record in caplog.records:
            assert PASSWORD not in record.getMessage() + repr(record.args)
        assert PASSWORD.encode() not in _everything_on_disk(tmp_path / "home")
    finally:
        await daemon.stop()


async def test_end_to_end_off_the_proposal_result_tells_jarvis_where(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _remote_daemon(tmp_path, on=False)
    try:
        core = daemon.core
        jarvis = core.jarvis.session()
        jarvis.current_turn = "t-human"
        jarvis.turn_origins["t-human"] = "human"
        ctx = ToolContext(session=jarvis, core=core, backend=None)  # type: ignore[arg-type]
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            remote = await _remote_client(http, daemon)
            proposal = asyncio.ensure_future(jarvis.session_tools["delegation_set"].run(ctx, {
                "scope": "project", "target": str(project), "level": "full"}))
            request = (await remote.wait_notification("approval.pending"))["request"]
            # Off: the answer is taken as before (a password is ignored) ...
            await remote.ok("approval.respond", {"requestId": request["requestId"],
                                                 "decision": "allow", "reauth": PASSWORD})
            result = await asyncio.wait_for(proposal, 5)
            await remote.stop()
        # ... and the grant is refused, with what the person can do about it.
        assert not result.ok
        assert "Settings > Jarvis > Delegation" in (result.error or "")
        assert "remote password" in (result.error or "")
        assert core.jarvis.engine.list_grants() == []
    finally:
        await daemon.stop()
