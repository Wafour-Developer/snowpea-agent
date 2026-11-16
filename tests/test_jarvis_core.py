"""M19 core integration: the delegation engine wired into the Jarvis service.

The engine routes every approval and question while Jarvis mode is on (the
route is on the request before any client sees it), pauses and grants are the
engine's alone and survive a restart, stalls are noticed with the fake clock,
and escalations that are not a waiting request survive a client reload.
Throwaway homes under ``tmp_path``; no model is called.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import connect, delegate_everything, make_daemon, undelegate

from snowpea_core.cli import main as cli_main
from snowpea_core.jarvis.clock import Clock
from snowpea_core.jarvis.intake import ROUTED_BATCH_SEC
from snowpea_core.jarvis.service import JarvisService
from snowpea_core.session import events
from snowpea_core.session.workspaces import canonical


def _settings(tmp_path: Path, **jarvis: Any) -> dict[str, Any]:
    return {"jarvis": {"folder": str(tmp_path / "JarvisFolder"), **jarvis}}


class FakeClock(Clock):
    def __init__(self) -> None:
        self.mono = 5000.0
        self.wall = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


async def _with_clock(daemon: Any) -> tuple[JarvisService, FakeClock, list[str]]:
    """Replace the daemon's Jarvis service with one on a fake clock, wakes recorded."""
    core = daemon.core
    old = core.jarvis
    if old is not None:
        await old.stop()
    clock = FakeClock()
    service = JarvisService(core, clock=clock)
    core.jarvis = service
    wakes: list[str] = []

    async def deliver(text: str, listener: Any = None, who: Any = None) -> bool:
        wakes.append(text)
        return True

    service.deliver_wake = deliver  # type: ignore[method-assign]
    await service.start()
    # The tick loop runs on real time; these tests drive the intake by hand.
    if service._tick_task is not None:
        service._tick_task.cancel()
        await asyncio.gather(service._tick_task, return_exceptions=True)
        service._tick_task = None
    return service, clock, wakes


async def _approval(core: Any, session: Any, command: str, **kwargs: Any) -> asyncio.Future[Any]:
    task = asyncio.ensure_future(
        core.approvals.request(
            session,
            "shell",
            {"command": command},
            unattended=True,
            timeout_sec=30,
            tags=("exec",),
            **kwargs,
        )
    )
    for _ in range(200):
        if any(r.sessionId == session.id for r in core.approvals.list()):
            break
        await asyncio.sleep(0.01)
    return task


def _pending(core: Any, session_id: str) -> Any:
    found = [r for r in core.approvals.list() if r.sessionId == session_id]
    assert found, "no pending approval"
    return found[-1]


async def _last(client: Any, method: str, request_id: str) -> dict[str, Any]:
    for _ in range(300):
        for frame in reversed(client.of_method(method)):
            request = frame.get("request") or {}
            if request.get("requestId") == request_id:
                return frame
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {method} for {request_id}")


async def _seen(client: Any, method: str, match: Any) -> list[dict[str, Any]]:
    for _ in range(300):
        found = [frame for frame in client.of_method(method) if match(frame)]
        if found:
            return found
        await asyncio.sleep(0.01)
    raise AssertionError(f"no matching {method}")


# -- routing on the wire ---------------------------------------------------------------


async def test_every_request_carries_the_engines_route(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, enabled=True))
    try:
        core = daemon.core
        client = await connect(http, daemon)
        worker = await core.sessions.create(project, mode="auto")
        # Off: no route at all.
        core.settings.jarvis.enabled = False
        await core.jarvis.apply_settings()
        ask = await _approval(core, worker, "make build")
        assert _pending(core, worker.id).route is None
        await core.approvals.respond(_pending(core, worker.id).requestId, "deny")
        await asyncio.wait_for(ask, 5)
        core.settings.jarvis.enabled = True
        await core.jarvis.apply_settings()

        # Staged: a risky command is the person's; the pending frame says so.
        ask = await _approval(core, worker, "make build")
        pending = _pending(core, worker.id)
        frame = await _last(client, "approval.pending", pending.requestId)
        assert frame["request"]["route"] == "person"
        await core.approvals.respond(pending.requestId, "deny")
        await asyncio.wait_for(ask, 5)

        # A safe read is the engine's: allowed as jarvis:auto, recorded, announced.
        decision = await asyncio.wait_for(
            core.approvals.request(
                worker,
                "shell",
                {"command": "ls -la"},
                unattended=True,
                timeout_sec=30,
                tags=("exec",),
            ),
            10,
        )
        assert decision.allowed and decision.by == "jarvis:auto"
        decided = await client.wait_notification("jarvis.decision")
        assert decided["entry"]["decidedBy"] == "auto"
        assert decided["entry"]["classification"] == "safe"
        resolved = await _seen(client, "approval.resolved", lambda f: f["by"] == "jarvis:auto")
        # The engine forgot the answered request.
        assert core.jarvis.engine.routed(resolved[0]["requestId"]) is None

        # Full delegation: risky goes to Jarvis; guarded stays the person's.
        delegate_everything(core)
        ask = await _approval(core, worker, "make build")
        assert _pending(core, worker.id).route == "jarvis"
        await core.approvals.respond(_pending(core, worker.id).requestId, "deny")
        await asyncio.wait_for(ask, 5)
        ask = await _approval(core, worker, "git push origin main")
        assert _pending(core, worker.id).route == "person"
        await core.approvals.respond(_pending(core, worker.id).requestId, "deny")
        await asyncio.wait_for(ask, 5)

        # Questions: Jarvis answers how-to questions at either level.
        from snowpea_core.server.protocol import QuestionItem

        q = asyncio.ensure_future(
            core.questions.ask(worker, [QuestionItem(question="Which db?")], timeout_sec=30)
        )
        for _ in range(200):
            if core.questions.list(worker.id):
                break
            await asyncio.sleep(0.01)
        qid = core.questions.list(worker.id)[0].requestId
        qframe = await _last(client, "question.pending", qid)
        assert qframe["request"]["route"] == "jarvis"
        await core.questions.respond(qframe["request"]["requestId"], [{"text": "sqlite"}])
        await asyncio.wait_for(q, 5)
    finally:
        await daemon.stop()


async def test_jarvis_cannot_answer_its_own_or_browser_requests(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await make_daemon(
        tmp_path / "home",
        _settings(tmp_path, enabled=True),
    )
    try:
        core = daemon.core
        delegate_everything(core)
        jarvis = core.jarvis.session()
        assert jarvis is not None and jarvis.role == "jarvis"
        ask = await _approval(core, jarvis, "make build")
        assert _pending(core, jarvis.id).route == "person"
        await core.approvals.respond(_pending(core, jarvis.id).requestId, "deny")
        await asyncio.wait_for(ask, 5)
        browser = await core.sessions.create(project, mode="auto")
        browser.host_tools_from = "browser-1"
        ask = await _approval(core, browser, "make build")
        assert _pending(core, browser.id).route == "person"
        await core.approvals.respond(_pending(core, browser.id).requestId, "deny")
        await asyncio.wait_for(ask, 5)
    finally:
        await daemon.stop()


# -- pauses and grants: one store, kept across a restart --------------------------------


async def test_pauses_and_grants_survive_a_restart(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    daemon = await make_daemon(home, _settings(tmp_path, enabled=True))
    try:
        core = daemon.core
        client = await connect(http, daemon)
        worker = await core.sessions.create(project, mode="auto")
        worker_id = worker.id
        await client.ok(
            "jarvis.delegation.set",
            {"scope": "project", "target": str(project), "level": "full", "allow": []},
        )
        # The person types into the worker: Jarvis pauses there.
        await core.hub.emit_event(
            worker.id, events.message_user("I'll take it", origin={"kind": "human"})
        )
        state = core.jarvis.session_state(worker.id, canonical(project))
        assert state.pausedUntil and state.level == "full"
        assert state.levelScope == "project" and state.grantId
        ask = await _approval(core, worker, "make build")
        assert _pending(core, worker.id).route == "person"
        await core.approvals.respond(_pending(core, worker.id).requestId, "deny")
        await asyncio.wait_for(ask, 5)
        await client.ok("jarvis.takeover", {"sessionId": worker.id})
    finally:
        await daemon.stop()
    daemon = await make_daemon(home)
    try:
        core = daemon.core
        client = await connect(http, daemon)
        status = await client.ok("jarvis.status")
        assert status["running"]
        assert [p["sessionId"] for p in status["paused"]] == [worker_id]
        assert status["paused"][0]["reason"] == "takeover"
        grants = (await client.ok("jarvis.delegation.list", {}))["grants"]
        assert [g["level"] for g in grants] == ["full"]
        worker = await core.sessions.restore(worker_id)
        assert worker is not None
        ask = await _approval(core, worker, "make build")
        assert _pending(core, worker.id).route == "person"  # still held by the person
        await core.approvals.respond(_pending(core, worker.id).requestId, "deny")
        await asyncio.wait_for(ask, 5)
        await client.ok("jarvis.handback", {"sessionId": worker_id})
        ask = await _approval(core, worker, "make build")
        assert _pending(core, worker.id).route == "jarvis"
        await core.approvals.respond(_pending(core, worker.id).requestId, "deny")
        await asyncio.wait_for(ask, 5)
    finally:
        await daemon.stop()


async def test_a_closed_session_ends_its_grants(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, enabled=True))
    try:
        core = daemon.core
        client = await connect(http, daemon)
        worker = await core.sessions.create(project, mode="auto")
        await client.ok(
            "jarvis.delegation.set",
            {"scope": "session", "target": worker.id, "level": "full", "allow": []},
        )
        assert len((await client.ok("jarvis.delegation.list", {}))["grants"]) == 1
        await core.sessions.close(worker.id)
        assert (await client.ok("jarvis.delegation.list", {}))["grants"] == []
    finally:
        await daemon.stop()


# -- summaries on every row ---------------------------------------------------------------


async def test_role_and_jarvis_ride_on_changes_and_create(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, enabled=True))
    try:
        core = daemon.core
        client = await connect(http, daemon)
        created = await client.ok("session.create", {"workdir": str(project)})
        assert created["jarvis"]["exposed"] is True
        assert created["jarvis"]["level"] == "staged"
        assert created["jarvis"]["levelScope"] == "default"
        assert created.get("role") is None
        changed = await _seen(
            client, "sessions.changed", lambda f: f["sessionId"] == created["sessionId"]
        )
        assert changed[-1]["jarvis"]["level"] == "staged"
        await core.sessions.announce_sessions_changed("renamed", core.jarvis.session_id)
        jarvis_rows = await _seen(
            client, "sessions.changed", lambda f: f["sessionId"] == core.jarvis.session_id
        )
        assert jarvis_rows[-1]["role"] == "jarvis" and jarvis_rows[-1].get("jarvis") is None
    finally:
        await daemon.stop()


async def test_browser_sessions_follow_expose_browser(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, enabled=True))
    try:
        core = daemon.core
        client = await connect(http, daemon)
        browser = await core.sessions.create(project, mode="auto")
        browser.host_tools_from = "browser-1"
        await core.hub.emit_event(browser.id, events.turn_started("t1"))
        await core.hub.emit_event(browser.id, events.message_done("Booked the table."))
        await core.hub.emit_event(browser.id, events.turn_done("t1", "complete"))
        assert core.settings.jarvis.exposeBrowser is True
        row = next(
            r
            for r in (await client.ok("jarvis.overview", {}))["rows"]
            if r["sessionId"] == browser.id
        )
        assert row["exposed"] is True and "Booked the table" in row["digest"]
        core.settings.jarvis.exposeBrowser = False
        row = next(
            r
            for r in (await client.ok("jarvis.overview", {}))["rows"]
            if r["sessionId"] == browser.id
        )
        assert row["exposed"] is False and row["digest"] == "" and row["status"] == "idle"
    finally:
        await daemon.stop()


# -- stalls ------------------------------------------------------------------------------


async def test_stalls_wake_jarvis_for_delegated_sessions(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await make_daemon(
        tmp_path / "home",
        _settings(tmp_path, enabled=True, stallMinutes=5),
    )
    try:
        core = daemon.core
        everything = delegate_everything(core)
        service, clock, wakes = await _with_clock(daemon)
        intake = service.intake
        assert intake is not None
        worker = await core.sessions.create(project, mode="auto")
        # A running turn that goes silent.
        await core.hub.emit_event(worker.id, events.turn_started("t1"))
        clock.advance(4 * 60)
        await intake.tick()
        await core.hub.emit_event(worker.id, events.tool_call("c1", "shell", {"command": "x"}))
        clock.advance(4 * 60)
        await intake.tick()
        clock.advance(ROUTED_BATCH_SEC + 1)
        await intake.tick()
        assert wakes == []  # activity reset the clock
        clock.advance(2 * 60)
        await intake.tick()
        clock.advance(ROUTED_BATCH_SEC + 1)
        await intake.tick()
        assert len(wakes) == 1 and "stalled (running with no activity" in wakes[0]
        # Reported once per stretch of stillness.
        clock.advance(10 * 60)
        await intake.tick()
        clock.advance(ROUTED_BATCH_SEC + 1)
        await intake.tick()
        assert len(wakes) == 1

        # Idle with work its answer says remains.
        await core.hub.emit_event(worker.id, events.turn_started("t2"))
        await core.hub.emit_event(
            worker.id,
            events.message_done("Checkpoint.\nRemaining work:\n1. wire the API\n2. tests"),
        )
        await core.hub.emit_event(worker.id, events.turn_done("t2", "complete"))
        clock.advance(ROUTED_BATCH_SEC + 1)
        await intake.tick()
        assert len(wakes) == 2  # the delegated turn end itself
        clock.advance(5 * 60 + 1)
        await intake.tick()
        clock.advance(ROUTED_BATCH_SEC + 1)
        await intake.tick()
        assert len(wakes) == 3 and "stopped while its last answer says work remains" in wakes[2]

        # Staged sessions are not Jarvis's to restart.
        undelegate(core, everything)
        await core.hub.emit_event(worker.id, events.turn_started("t3"))
        clock.advance(6 * 60)
        await intake.tick()
        clock.advance(ROUTED_BATCH_SEC + 1)
        await intake.tick()
        assert len(wakes) == 3
    finally:
        await daemon.stop()


# -- escalations survive a reload ------------------------------------------------------


async def test_escalations_list_and_dismiss(http: aiohttp.ClientSession, tmp_path: Path) -> None:
    home = tmp_path / "home"
    daemon = await make_daemon(home, _settings(tmp_path, enabled=True))
    try:
        client = await connect(http, daemon)
        service = daemon.core.jarvis
        for n in range(3):
            await service.escalate(
                reason="finished", summary=f"done {n}", importance="normal", session_id=f"s-{n}"
            )
        # A waiting request's escalation is approval.list's, not this list's.
        await service.escalate(
            reason="risky",
            summary="needs you",
            request_kind="approval",
            request_id="ap-1",
            session_id="s-9",
        )
        page = await client.ok("jarvis.escalations.list", {"limit": 2})
        assert [e["summary"] for e in page["escalations"]] == ["done 2", "done 1"]
        rest = await client.ok(
            "jarvis.escalations.list", {"limit": 2, "before": page["nextBefore"]}
        )
        assert [e["summary"] for e in rest["escalations"]] == ["done 0"]
        assert rest["nextBefore"] is None
        first = page["escalations"][0]["id"]
        dismissed = await client.ok("jarvis.escalations.dismiss", {"ids": [first, "je-nope"]})
        assert dismissed["dismissed"] == [first]
        note = await client.wait_notification("jarvis.escalation.dismissed")
        assert note["ids"] == [first]
        frame = await client.call("jarvis.escalations.dismiss", {})
        assert frame["error"]["data"]["code"] == "invalid_params"
    finally:
        await daemon.stop()
    daemon = await make_daemon(home)
    try:
        client = await connect(http, daemon)
        page = await client.ok("jarvis.escalations.list", {})
        assert [e["summary"] for e in page["escalations"]] == ["done 1", "done 0"]
        assert (await client.ok("jarvis.escalations.dismiss", {"all": True}))["dismissed"]
        assert (await client.ok("jarvis.escalations.list", {}))["escalations"] == []
        stored = json.loads((home / "jarvis" / "escalations.json").read_text())
        assert stored == {"open": []}
    finally:
        await daemon.stop()


async def test_a_tamper_escalation_carries_its_own_kind(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, enabled=True))
    try:
        client = await connect(http, daemon)
        service = daemon.core.jarvis
        await service.escalate(reason="error", summary="changed outside", kind="tamper")
        tamper = await client.wait_notification("jarvis.escalation")
        assert tamper["kind"] == "tamper" and tamper["reason"] == "error"
        plain = await service.escalate(reason="finished", summary="done", importance="normal")
        assert plain is not None and "kind" not in plain    # what was sent
        rows = (await client.ok("jarvis.escalations.list", {}))["escalations"]
        assert [(e["summary"], e.get("kind")) for e in rows] == [
            ("done", None), ("changed outside", "tamper")]
    finally:
        await daemon.stop()


# -- snowpea jarvis / snowpea external -------------------------------------------------


@pytest.mark.parametrize("screen", ["jarvis", "external"])
def test_cli_opens_the_tui_on_a_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, screen: str
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(cli_main, "resolve_tui_command", lambda: ["node", "tui.js"])

    class _Info:
        port = 1234
        token = "t"

    async def _ensure(_home: Any) -> _Info:
        return _Info()

    monkeypatch.setattr(cli_main, "ensure_daemon", _ensure)
    monkeypatch.setattr(cli_main.subprocess, "call", lambda command: commands.append(command) or 0)
    assert cli_main.main(["--cwd", str(tmp_path), screen]) == 0
    assert commands and commands[0][-2:] == ["--start", screen]
    commands.clear()
    assert cli_main.main(["--cwd", str(tmp_path)]) == 0
    assert "--start" not in commands[0]
