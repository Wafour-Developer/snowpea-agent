"""1.56.0: the per-session Jarvis switch (owner decision 2026-11-17).

Jarvis sees only the sessions a person shares with it.  Default off
(``jarvis.sessions.default``); a session grant turns it on; Jarvis's own new
sessions start on; children and forks inherit.  Off, a session does not exist
for Jarvis: not in the overview, refused by every tool, no route (so no Jarvis
decision and no engine auto-allow), no notice, no wake.  Also the Jarvis
browser session (``session_create {browser: true}``) and the host asks that are
always the person's.

Every daemon runs on a throwaway home under ``tmp_path`` with scripted
providers; nothing calls a model, nothing touches ``~/.snowpea``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from _support import connect, delegate_everything, make_daemon

from snowpea_core.config.settings import Settings
from snowpea_core.jarvis.delegation import Caller, DecisionRefused
from snowpea_core.jarvis.service import SWITCH_MARKER
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    CAPABILITIES,
    EVENTS,
    ApprovalAskParams,
    ApprovalRequest,
    JarvisSessionSetParams,
)
from snowpea_core.session.history import message_text
from snowpea_core.tools.host_tools import HOST_TOOLS
from snowpea_core.tools.registry import ToolContext

PERSON = Caller(kind="connection", surface="ide")


def _settings(tmp_path: Path, **jarvis: Any) -> dict[str, Any]:
    return {"jarvis": {"folder": str(tmp_path / "JarvisFolder"), "enabled": True, **jarvis}}


class _Quiet:
    vendor = "fake"
    model = "fake-1"

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=5, output_tokens=1))
        yield StreamEvent(kind="text_delta", text="ok")
        yield StreamEvent(kind="done", stop_reason="end_turn")


class _Repl:
    """Calls the browser's ``repl`` once, then answers."""

    vendor = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.calls += 1
        last = message_text(messages[-1])
        names = {getattr(tool, "name", None) for tool in tools}
        if "repl" in names and "tool" not in str(getattr(messages[-1], "role", "")) and (
                "open the page" in last):
            yield StreamEvent(kind="tool_call", tool_call=ToolCall(
                id=f"c{self.calls}", name="repl", arguments={"code": "return 1 + 1"}))
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        yield StreamEvent(kind="text_delta", text="done")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _daemon(tmp_path: Path, provider: Any = None, **jarvis: Any) -> Any:
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, **jarvis))
    chosen = provider or _Quiet()
    daemon.core.providers.get = lambda _p, _m: chosen
    return daemon


def _ctx(core: Any, *, human: bool = True) -> ToolContext:
    jarvis = core.jarvis.session()
    assert jarvis is not None
    turn = "t-human" if human else "t-wake"
    jarvis.current_turn = turn
    jarvis.turn_origins[turn] = "human" if human else "system"
    return ToolContext(session=jarvis, core=core, backend=None)  # type: ignore[arg-type]


async def _tool(core: Any, name: str, args: dict[str, Any], *, human: bool = True) -> Any:
    return await core.jarvis.session().session_tools[name].run(_ctx(core, human=human), args)


async def _wait(predicate: Any, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


def _request(session_id: str, request_id: str = "ap-1", tool: str = "shell",
             args: dict[str, Any] | None = None) -> ApprovalRequest:
    return ApprovalRequest(requestId=request_id, sessionId=session_id, tool=tool,
                           args=args or {"command": "npm run build"}, risk="medium",
                           timeoutSec=0)


def _pending(core: Any, session_id: str) -> list[Any]:
    return [entry.request for entry in core.approvals._pending.values()
            if entry.request.sessionId == session_id]


def _person(**extra: Any) -> Any:
    base = {"authenticated": True, "closed": False, "client_kind": "tui", "remote_login": False}
    base.update(extra)
    return SimpleNamespace(**base)


# -- contract -------------------------------------------------------------------------


def test_capability_event_and_default() -> None:
    assert "jarvisSessionSwitch" in CAPABILITIES
    assert "jarvis.session.changed" in EVENTS
    assert Settings().jarvis.sessions.default == "off"


# -- the gate matrix ----------------------------------------------------------------------


async def test_off_session_does_not_exist_for_jarvis(http: aiohttp.ClientSession,
                                                     tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        delegate_everything(core)  # even a global full grant does not reach an off session
        client = await connect(http, daemon)
        off = await core.sessions.create(project, mode="auto")
        jarvis = core.jarvis
        assert core.jarvis_delegation.switch(off.id) is False
        rows = await client.ok("session.list", {})
        row = next(r for r in rows["sessions"] if r["sessionId"] == off.id)
        assert row["jarvisOn"] is False and row["jarvisLocked"] is None
        overview = await client.ok("jarvis.overview", {})
        assert off.id not in {r["sessionId"] for r in overview["rows"]}
        assert overview["hiddenCount"] >= 1
        # No route at all: exactly as with Jarvis mode off.
        assert jarvis.route_approval(_request(off.id), off) is None
        assert jarvis.route_question({"questions": [{"question": "how?"}],
                                      "sessionId": off.id}, off) is None
        # The engine refuses a decision for it, whatever the grant.
        classify, project_root = jarvis.classify_request(_request(off.id), off)
        with pytest.raises(DecisionRefused):
            jarvis.engine.validate_decision(classify, "allow", project=project_root)
        assert jarvis.engine.route(classify, project=project_root).reason == "switch-off"
        # Every tool says it does not exist.
        for name, args in (
            ("session_read", {"sessionId": off.id}),
            ("session_prompt", {"sessionId": off.id, "text": "hi"}),
            ("session_steer", {"sessionId": off.id, "text": "hi"}),
            ("session_interrupt", {"sessionId": off.id}),
            ("ledger_read", {"sessionId": off.id}),
        ):
            result = await _tool(core, name, args)
            assert not result.ok and "no session" in result.error, (name, result.error)
        listed = await _tool(core, "session_list", {})
        assert off.id not in (listed.output or "")
        # No digest: no notice, no wake.
        assert off.id not in jarvis.intake.digests
        await jarvis.intake.notify("session.event", {
            "sessionId": off.id, "kind": "turn.done", "payload": {"reason": "error"}})
        assert not jarvis.intake._notices and not jarvis.intake._routed
        # The context table Jarvis reads does not mention it.
        from snowpea_core.jarvis.overview import _render_context

        assert off.id not in _render_context(core, jarvis.session())
    finally:
        await daemon.stop()


async def test_on_session_is_supervised_as_before(http: aiohttp.ClientSession,
                                                  tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        delegate_everything(core)
        client = await connect(http, daemon)
        on = await core.sessions.create(project, mode="auto")
        result = await client.ok("jarvis.session.set", {"sessionId": on.id, "enabled": True})
        assert result["enabled"] is True and result["session"]["jarvisOn"] is True
        changed = await client.wait_notification("jarvis.session.changed")
        assert changed == {"sessionId": on.id, "external": None, "enabled": True, "by": "person"}
        overview = await client.ok("jarvis.overview", {})
        assert on.id in {r["sessionId"] for r in overview["rows"]}
        assert core.jarvis.route_approval(_request(on.id, "ap-2"), on) == "jarvis"
        read = await _tool(core, "session_read", {"sessionId": on.id})
        assert read.ok, read.error
        assert on.id in core.jarvis.intake.digests
    finally:
        await daemon.stop()


async def test_switching_with_pending_requests(http: aiohttp.ClientSession,
                                               tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        delegate_everything(core)
        client = await connect(http, daemon)
        session = await core.sessions.create(project, mode="accept")
        await client.ok("jarvis.session.set", {"sessionId": session.id, "enabled": True})
        asked = asyncio.ensure_future(core.approvals.request(
            session, "shell", {"command": "npm run build"}, unattended=True,
            tags=("exec",)))
        await _wait(lambda: bool(_pending(core, session.id)))
        request = _pending(core, session.id)[0]
        assert request.route == "jarvis"
        # Off: what waits for Jarvis there is the person's at once (route null).
        await client.ok("jarvis.session.set", {"sessionId": session.id, "enabled": False})
        assert _pending(core, session.id)[0].route is None
        assert session.id not in core.jarvis.intake.digests
        assert not any(item.session_id == session.id for item in core.jarvis.intake._routed)
        # Grants stay: off does not revoke them.
        assert core.jarvis_delegation.list_grants(scope="global")
        # On again: routed as if just asked (and announced as session_on).
        await client.ok("jarvis.session.set", {"sessionId": session.id, "enabled": True})
        assert _pending(core, session.id)[0].route == "jarvis"
        await core.approvals.respond(request.requestId, "deny", "once", by="test")
        await asyncio.wait_for(asked, 5)
    finally:
        await daemon.stop()


async def test_new_pending_request_of_an_off_session_has_no_route(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        delegate_everything(core)
        session = await core.sessions.create(project, mode="accept")
        asked = asyncio.ensure_future(core.approvals.request(
            session, "read_file", {"path": str(project / "a.txt")}, unattended=True,
            tags=("read",)))
        await _wait(lambda: bool(_pending(core, session.id)))
        # A safe read would be the engine's alone if the session were shared.
        request = _pending(core, session.id)[0]
        assert request.route is None
        await asyncio.sleep(0.2)
        assert not asked.done()  # nobody auto-allowed it
        await core.approvals.respond(request.requestId, "deny", "once", by="test")
        await asyncio.wait_for(asked, 5)
    finally:
        await daemon.stop()


# -- who may switch ------------------------------------------------------------------------


async def test_person_gate(tmp_path: Path) -> None:
    from snowpea_core.server import settings_gate
    from snowpea_core.server.jarvis_handlers import jarvis_session_set_handler
    from snowpea_core.server.person_gate import method_refusal

    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        session = await core.sessions.create(project, mode="auto")
        params = JarvisSessionSetParams(sessionId=session.id, enabled=True)
        for conn in (None, _person(client_kind="browser"), _person(client_kind="remote")):
            with pytest.raises(RpcError) as caught:
                method_refusal("jarvis.session.set", conn)
            assert caught.value.code == "jarvis_person_only"
            with pytest.raises(RpcError):
                await jarvis_session_set_handler(conn, params, core)
        # A connector and a browser are refused by the dispatcher's tables too.
        with pytest.raises(RpcError):
            settings_gate.kind_method_refusal("jarvis.session.set", {},
                                              _person(client_kind="remote"), core)
        with pytest.raises(RpcError):
            settings_gate.browser_method_refusal(
                "jarvis.session.set", {}, _person(client_kind="browser"), core) or \
                settings_gate.kind_method_refusal("jarvis.session.set", {},
                                                  _person(client_kind="browser"), core)
        # A remote web login is a person here.
        remote = _person(remote_login=True, client_kind="web")
        settings_gate.kind_method_refusal("jarvis.session.set", {}, remote, core)
        method_refusal("jarvis.session.set", remote)
        result = await jarvis_session_set_handler(remote, params, core)
        assert result.enabled is True
        # Jarvis has no tool for it.
        assert not any("switch" in name or name == "session_share"
                       for name in core.jarvis.session().session_tools)
    finally:
        await daemon.stop()


async def test_refused_while_jarvis_is_off_and_when_locked(tmp_path: Path) -> None:
    from snowpea_core.server.jarvis_handlers import jarvis_session_set_handler

    hidden = tmp_path / "hidden"
    (hidden / ".snowpea").mkdir(parents=True)
    (hidden / ".snowpea" / "settings.json").write_text(json.dumps({"jarvis": {"expose": False}}))
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        session = await core.sessions.create(hidden, mode="auto")
        with pytest.raises(RpcError) as caught:
            await jarvis_session_set_handler(
                _person(), JarvisSessionSetParams(sessionId=session.id, enabled=True), core)
        assert caught.value.data["reason"] == "project_hidden"
        # Project expose off wins over a stored switch.
        core.jarvis_delegation.set_switch(session.id, True, by="test")
        assert core.jarvis.sees(session) is False
        assert core.jarvis.standing_of(session) == (True, "project_hidden")
        # Jarvis's own session is locked on.
        own = core.jarvis.session()
        assert core.jarvis.standing_of(own) == (True, "jarvis_session")
        await core.jarvis.disable()
        with pytest.raises(RpcError) as off:
            await jarvis_session_set_handler(
                _person(), JarvisSessionSetParams(sessionId=session.id, enabled=True), core)
        assert off.value.code == "jarvis_disabled"
    finally:
        await daemon.stop()


async def test_browser_rule(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path, exposeBrowser=False)
    try:
        core = daemon.core
        session = await core.sessions.create(project, mode="auto")
        session.host_tools_from = "browser-1"
        core.jarvis_delegation.set_switch(session.id, True, by="test")
        assert core.jarvis.standing_of(session) == (True, "browser_hidden")
        assert core.jarvis.sees(session) is False
        core.settings.jarvis.exposeBrowser = True
        assert core.jarvis.sees(session) is True
    finally:
        await daemon.stop()


async def test_settings_tool_cannot_change_the_default() -> None:
    from snowpea_core.jarvis.remote_grant import tool_refusal

    before = Settings()
    after = Settings.model_validate({"jarvis": {"sessions": {"default": "on"}}})
    assert tool_refusal(before, after) is not None
    assert tool_refusal(before, Settings()) is None


# -- grants, Jarvis's own sessions, inheritance --------------------------------------


async def test_session_grant_turns_on_project_grant_does_not(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        engine = core.jarvis_delegation
        a = await core.sessions.create(project, mode="auto")
        b = await core.sessions.create(project, mode="auto")
        engine.set_grant("project", str(project), "full", caller=PERSON)
        assert engine.switch(b.id) is False and not core.jarvis.sees(b)
        engine.set_grant("session", a.id, "staged", caller=PERSON, project=str(project))
        assert engine.switch(a.id) is True and core.jarvis.sees(a)
        await core.jarvis.settle()
        # Jarvis cannot even propose a session grant for a session it does not see.
        result = await _tool(core, "delegation_set",
                             {"scope": "session", "target": b.id, "level": "full"})
        assert not result.ok and "no session" in result.error
    finally:
        await daemon.stop()


async def test_jarvis_created_session_starts_on_with_a_ledger_line(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        await core.sessions.create(project, mode="auto")  # registers the project
        await core.jarvis.projects.roots()
        created = await _tool(core, "session_create", {"project": str(project), "title": "t"})
        assert created.ok, created.error
        session_id = created.meta["sessionId"]
        assert core.jarvis_delegation.switch(session_id) is True
        entries, _ = core.jarvis_delegation.ledger.decisions(session_id=session_id)
        assert any("shared with Jarvis: on" in entry.summary for entry in entries)
    finally:
        await daemon.stop()


async def test_children_and_forks_inherit(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        engine = core.jarvis_delegation
        on = await core.sessions.create(project, mode="auto")
        off = await core.sessions.create(project, mode="auto")
        engine.set_switch(on.id, True, by="test")
        child_on = await core.sessions.create(project, mode="auto", parent_session_id=on.id,
                                              kind="scheduled")
        child_off = await core.sessions.create(project, mode="auto", parent_session_id=off.id,
                                               kind="scheduled")
        assert engine.switch(child_on.id) is True
        assert engine.switch(child_off.id) is False
        sub = await core.sessions.create(project, mode="auto", parent_session_id=on.id,
                                         kind="subagent")
        assert core.jarvis.sees(sub) is True  # follows its parent at every look
        core.jarvis.inherit_switch("s-fork1", on.id)
        core.jarvis.inherit_switch("s-fork2", off.id)
        assert engine.switch("s-fork1") is True and engine.switch("s-fork2") is False
    finally:
        await daemon.stop()


async def test_default_on_setting(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path, sessions={"default": "on"})
    try:
        core = daemon.core
        session = await core.sessions.create(project, mode="auto")
        assert core.jarvis_delegation.switch(session.id) is True
    finally:
        await daemon.stop()


async def test_external_refs_have_the_switch(tmp_path: Path) -> None:
    daemon = await _daemon(tmp_path)
    try:
        jarvis = daemon.core.jarvis
        ref = {"cli": "codex", "id": "th-1"}
        assert jarvis.standing_of_external(ref, None) == (False, None)
        assert jarvis.sees_ids(None, ref) is False
        assert jarvis.sees_ids("ext:codex:th-1", None) is False
        jarvis.set_session_switch(external=ref, enabled=True)
        assert jarvis.sees_ids(None, ref) is True
        assert jarvis.sees_ids("ext:codex:th-1", None) is True
        proxy = SimpleNamespace(id="ext:codex:th-1", workdir="/", project=None,
                                external=SimpleNamespace(cli="codex", id="th-1"))
        assert jarvis.sees(proxy) is True
    finally:
        await daemon.stop()


async def test_ledger_read_hides_an_off_sessions_entries(http: aiohttp.ClientSession,
                                                         tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        client = await connect(http, daemon)
        session = await core.sessions.create(project, mode="auto")
        core.jarvis.record_decision(kind="prompt", decided_by="jarvis", summary="SECRETWORD",
                                    session_id=session.id, project=str(project))
        ledger = await client.ok("jarvis.ledger", {})
        assert any(e["summary"] == "SECRETWORD" for e in ledger["entries"])  # the person's view
        read = await _tool(core, "ledger_read", {})
        assert "SECRETWORD" not in (read.output or "")
        core.jarvis_delegation.set_switch(session.id, True, by="test")
        read = await _tool(core, "ledger_read", {})
        assert "SECRETWORD" in (read.output or "")
    finally:
        await daemon.stop()


# -- migration --------------------------------------------------------------------------------


async def test_migration_once(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        jarvis = core.jarvis
        marker = jarvis.home / SWITCH_MARKER
        assert marker.is_file()  # written at the first start
        delegated = await core.sessions.create(project, mode="auto")
        plain = await core.sessions.create(project, mode="auto")
        # As an older daemon left it: a session grant, no switch rows, no marker.
        engine = core.jarvis_delegation
        engine.set_grant("session", delegated.id, "staged", caller=PERSON)
        with engine._lock:
            engine._conn().execute("DELETE FROM switches")
        engine._switch_cache.clear()
        marker.unlink()
        before = len(jarvis.escalations.page(limit=50)[0])
        assert await jarvis.migrate_switches() is True
        assert engine.switch(delegated.id) is True
        assert engine.switch(plain.id) is None and not jarvis.sees(plain)
        notices = jarvis.escalations.page(limit=50)[0]
        assert len(notices) == before + 1
        assert notices[0]["title"] == "Jarvis sees only sessions you switch on"
        assert await jarvis.migrate_switches() is False  # once
        assert len(jarvis.escalations.page(limit=50)[0]) == before + 1
    finally:
        await daemon.stop()


# -- the Jarvis browser session -------------------------------------------------------------


class _Browser:
    def __init__(self) -> None:
        self.surface_id = "surface-browser"
        self.client_id = "browser-1"
        self.client_kind = "browser"
        self.authenticated = True
        self.closed = False
        self.on_close: list[Any] = []
        self.invoked: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None):
        self.invoked.append({"method": method, **params})
        return {"ok": True, "output": "2"}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        return None


HOST_TOOL_NAMES = ("repl", "browser_navigate", "browser_click", "browser_type",
                   "browser_press", "use_tab", "payment", "subscription")


def _register_browser(core: Any) -> _Browser:
    conn = _Browser()
    HOST_TOOLS.register(core, conn, [
        {"name": name, "description": name, "permission": "read",
         "inputSchema": {"type": "object", "properties": {}}}
        for name in HOST_TOOL_NAMES
    ])
    return conn


async def test_browser_session_needs_a_browser(tmp_path: Path) -> None:
    daemon = await _daemon(tmp_path)
    try:
        result = await _tool(daemon.core, "session_create",
                             {"browser": True, "prompt": "open the page"})
        assert not result.ok and "the browser is not running" in result.error
    finally:
        await daemon.stop()


async def test_browser_session_is_created_on_the_alias_and_runs_repl(tmp_path: Path) -> None:
    provider = _Repl()
    daemon = await _daemon(tmp_path, provider)
    conn = _register_browser(daemon.core)
    try:
        core = daemon.core
        created = await _tool(core, "session_create",
                              {"browser": True, "prompt": "open the page and read the price"})
        assert created.ok, created.error
        session = core.sessions.get(created.meta["sessionId"])
        assert session.host_tools_from == "browser"
        assert session.origin_surface == "jarvis"
        assert session.title == "open the page and read the price"
        assert core.jarvis_delegation.switch(session.id) is True
        assert core.jarvis.sees(session)
        assert "repl" in HOST_TOOLS.for_session(session)
        await _wait(lambda: any(c.get("name") == "repl" or c.get("tool") == "repl"
                                for c in conn.invoked), 10)
        await _wait(lambda: session.turn_task is None or session.turn_task.done(), 10)
        # Jarvis's words never carry a human origin, the first turn nor a follow-up.
        assert set(session.turn_origins.values()) <= {"jarvis"}
        follow = await _tool(core, "session_prompt", {"sessionId": session.id, "text": "again"})
        assert follow.ok, follow.error
        await _wait(lambda: session.turn_task is None or session.turn_task.done(), 10)
        assert set(session.turn_origins.values()) <= {"jarvis"}
        events = await core.store.events_of_kinds(session.id, ("message.user",), 0)
        assert events and all(
            (e["payload"].get("origin") or {}).get("kind") == "jarvis" for e in events)
        entries, _ = core.jarvis_delegation.ledger.decisions(session_id=session.id)
        assert any("browser session (shared with Jarvis: on)" in e.summary for e in entries)
        row = next(r for r in core.jarvis.overview_rows() if r.sessionId == session.id)
        assert row.hostToolsFrom == "browser"
    finally:
        HOST_TOOLS.drop_connection(conn)
        HOST_TOOLS._gone.clear()
        await daemon.stop()


async def test_one_browser_session_at_a_time_and_end_on_disconnect(tmp_path: Path) -> None:
    class _Slow(_Quiet):
        async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
            await asyncio.sleep(30)
            yield StreamEvent(kind="done", stop_reason="end_turn")

    daemon = await _daemon(tmp_path, _Slow())
    conn = _register_browser(daemon.core)
    try:
        core = daemon.core
        first = await _tool(core, "session_create", {"browser": True, "prompt": "work"})
        assert first.ok, first.error
        session = core.sessions.get(first.meta["sessionId"])
        await _wait(lambda: session.turn_live, 5)
        second = await _tool(core, "session_create", {"browser": True, "prompt": "more"})
        assert not second.ok and "one at a time" in second.error
        HOST_TOOLS.drop_connection(conn)
        await _wait(lambda: core.sessions.get(session.id) is None
                    or core.sessions.get(session.id).closed_at is not None, 10)
        entries, _ = core.jarvis_delegation.ledger.decisions(session_id=session.id)
        assert any("browser disconnected" in e.summary for e in entries)
    finally:
        HOST_TOOLS.drop_connection(conn)
        HOST_TOOLS._gone.clear()
        await daemon.stop()


async def test_browser_session_refused_with_expose_browser_off(tmp_path: Path) -> None:
    daemon = await _daemon(tmp_path, exposeBrowser=False)
    conn = _register_browser(daemon.core)
    try:
        result = await _tool(daemon.core, "session_create", {"browser": True, "prompt": "x"})
        assert not result.ok and "exposeBrowser" in result.error
    finally:
        HOST_TOOLS.drop_connection(conn)
        HOST_TOOLS._gone.clear()
        await daemon.stop()


# -- host asks in a shared browser session ---------------------------------------------------

#: (tool, permission, risk) of the host asks the browser raises.
DELEGABLE_ASKS = [
    ("repl.send", "send", None),
    ("repl.upload", "send", None),
    ("repl.script", "send", None),
    ("repl.navigate", "send", None),
    ("repl.fetch", "send", None),
    ("browser_navigate", "send", None),
    ("browser_click", "send", None),
    ("browser_type", "send", None),
    ("browser_press", "send", None),
]
PERSON_ONLY_ASKS = [
    ("payment.confirm", "send", "payment"),
    ("subscription.resume", "send", None),
    ("use_tab", "send", None),
]


async def _host_ask(core: Any, conn: Any, session: Any, tool: str, permission: str,
                    risk: str | None) -> tuple[asyncio.Future[Any], ApprovalRequest]:
    from snowpea_core.server.host_handlers import approval_ask_handler

    params = ApprovalAskParams(sessionId=session.id, tool=tool, permission=permission,
                               reason="the page asks", args={"detail": {"origin":
                                                                        "https://shop.example"}},
                               risk=risk)
    before = {r.requestId for r in _pending(core, session.id)}
    task = asyncio.ensure_future(approval_ask_handler(conn, params, core))
    await _wait(lambda: any(r.requestId not in before for r in _pending(core, session.id)), 5)
    request = next(r for r in _pending(core, session.id) if r.requestId not in before)
    return task, request


@pytest.mark.parametrize("level", ["staged", "full", "full+exception"])
async def test_host_asks_follow_the_engine(tmp_path: Path, level: str) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    conn = _register_browser(daemon.core)
    try:
        core = daemon.core
        session = await core.sessions.create(project, mode="accept", origin_surface="jarvis")
        session.host_tools_from = "browser"
        engine = core.jarvis_delegation
        if level == "staged":
            engine.set_switch(session.id, True, by="test")
        else:
            engine.set_grant("session", session.id, "full",
                             ["secret_send"] if level == "full+exception" else [],
                             caller=PERSON, project=str(project))
        await core.jarvis.settle()
        for tool, permission, risk in DELEGABLE_ASKS:
            task, request = await _host_ask(core, conn, session, tool, permission, risk)
            expected = "jarvis" if level == "full+exception" else "person"
            assert request.route == expected, (tool, level, request.route)
            assert not request.personOnly
            await core.approvals.respond(request.requestId, "deny", "once", by="test")
            await asyncio.wait_for(task, 5)
        for tool, permission, risk in PERSON_ONLY_ASKS:
            task, request = await _host_ask(core, conn, session, tool, permission, risk)
            assert request.route == "person", (tool, level)
            assert core.jarvis.engine.routed(request.requestId).reason == "person-only"
            await core.approvals.respond(request.requestId, "deny", "once", by="test")
            await asyncio.wait_for(task, 5)
    finally:
        HOST_TOOLS.drop_connection(conn)
        HOST_TOOLS._gone.clear()
        await daemon.stop()


async def test_jarvis_answers_a_host_ask_once_only(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    conn = _register_browser(daemon.core)
    try:
        core = daemon.core
        session = await core.sessions.create(project, mode="accept", origin_surface="jarvis")
        session.host_tools_from = "browser"
        core.jarvis_delegation.set_grant("session", session.id, "full", ["secret_send"],
                                         caller=PERSON, project=str(project))
        await core.jarvis.settle()
        task, request = await _host_ask(core, conn, session, "repl.send", "send", None)
        assert request.route == "jarvis"
        classify, root = core.jarvis.classified(request.requestId)
        for scope in ("session", "project", "always", "site"):
            with pytest.raises(DecisionRefused):
                core.jarvis.engine.validate_decision(classify, "allow", scope=scope,
                                                     request_id=request.requestId, project=root)
        decided = await _tool(core, "approval_decide",
                              {"requestId": request.requestId, "decision": "allow"},
                              human=False)
        assert decided.ok, decided.error
        answer = await asyncio.wait_for(task, 5)
        assert answer.decision == "allow" and answer.scope == "once"
    finally:
        HOST_TOOLS.drop_connection(conn)
        HOST_TOOLS._gone.clear()
        await daemon.stop()
