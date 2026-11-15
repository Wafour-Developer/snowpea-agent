"""M19 WP3: the Jarvis service over the wire (protocol 1.53.0, capability ``jarvis``).

Every daemon here runs on a throwaway home under ``tmp_path`` with the scripted
echo provider, so nothing touches a model or the developer's ``~/.snowpea``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import connect, delegate_everything, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.agent import named, runtime_context
from snowpea_core.providers.base import ChatMessage, StreamEvent, Usage
from snowpea_core.server.protocol import CAPABILITIES, PENDING_CAPABILITIES, SessionSummary
from snowpea_core.session.history import message_text
from snowpea_core.session.workspaces import canonical


class _Echo:
    vendor = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.seen: list[list[ChatMessage]] = []

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.seen.append(list(messages))
        last = (message_text(messages[-1]).splitlines() or [""])[0]
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=10, output_tokens=2))
        yield StreamEvent(kind="text_delta", text=f"echo {last}")
        yield StreamEvent(kind="done", stop_reason="end_turn")


def _settings(tmp_path: Path, **jarvis: Any) -> dict[str, Any]:
    return {"jarvis": {"folder": str(tmp_path / "JarvisFolder"), **jarvis}}


async def _error_code(client: Any, method: str, params: dict[str, Any] | None = None) -> str:
    frame = await client.call(method, params or {})
    assert frame.get("error"), frame
    return str(frame["error"]["data"]["code"])


async def _jarvis_row(client: Any, *, include_closed: bool = False) -> dict[str, Any] | None:
    rows = (await client.ok("session.list", {"includeClosed": include_closed}))["sessions"]
    found = [row for row in rows if row.get("role") == "jarvis"]
    assert len(found) <= 1
    return found[0] if found else None


def _hide(project: Path) -> None:
    folder = project / ".snowpea"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(json.dumps({"jarvis": {"expose": False}}))


# -- capability and contract -------------------------------------------------


def test_jarvis_capability_is_advertised() -> None:
    assert "jarvis" in CAPABILITIES
    assert "jarvis" not in PENDING_CAPABILITIES


# -- lifecycle -----------------------------------------------------------------


async def test_enable_disable_and_restore_across_a_restart(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    folder = tmp_path / "JarvisFolder"
    daemon = await make_daemon(home, _settings(tmp_path))
    try:
        daemon.core.providers.get = lambda _p, _m: _Echo()
        client = await connect(http, daemon)
        status = await client.ok("jarvis.status")
        assert status["enabled"] is False and status["running"] is False
        assert status["sessionId"] is None
        assert status["folder"] == str(folder.resolve())
        assert not folder.exists()
        # Off: the methods that need it say so.
        assert await _error_code(client, "jarvis.overview") == "jarvis_disabled"

        status = await client.ok("jarvis.enable", {})
        assert status["enabled"] and status["running"]
        session_id = status["sessionId"]
        assert session_id and folder.is_dir()
        assert status["budget"]["wakesPerHour"] == 30
        state = await client.wait_notification("jarvis.state")
        assert state["reason"] == "enabled" and state["status"]["running"] is True
        changed = await client.wait_notification("settings.changed")
        assert changed["keys"] == ["jarvis"]
        stored = json.loads((home / "settings.json").read_text())
        assert stored["jarvis"]["enabled"] is True

        # Idempotent.
        again = await client.ok("jarvis.enable", {})
        assert again["sessionId"] == session_id

        row = await _jarvis_row(client)
        assert row is not None
        assert row["sessionId"] == session_id
        assert row["kind"] == "agent" and row["agent"] == "jarvis"
        assert row["jarvis"] is None
        assert row["workdir"] == str(folder.resolve())
        agents = (await client.ok("agent.list"))["agents"]
        named_jarvis = [a for a in agents if a["kind"] == "named" and a["name"] == "jarvis"]
        assert named_jarvis and named_jarvis[0]["namespace"] == "agent:jarvis"
        session = daemon.core.sessions.get(session_id)
        assert session is not None and session.memory_namespace == "agent:jarvis"
        assert session.unattended is True

        # A turn, so there is history to restore.
        turn = agent_loop.start_turn(daemon.core, session, "remember the plan")
        await asyncio.wait_for(session.turn_task, 30)
        assert turn
        history_len = len(session.history.snapshot())
        assert history_len >= 2
        await client.stop()
    finally:
        await daemon.stop()

    # Restart: Jarvis comes back with the same session and its history.
    daemon = await make_daemon(home)
    try:
        client = await connect(http, daemon)
        status = await client.ok("jarvis.status")
        assert status["enabled"] and status["running"]
        assert status["sessionId"] == session_id
        session = daemon.core.sessions.get(session_id)
        assert session is not None
        assert len(session.history.snapshot()) == history_len
        assert session.memory_namespace == "agent:jarvis"

        status = await client.ok("jarvis.disable")
        assert status["enabled"] is False and status["running"] is False
        assert status["sessionId"] == session_id
        assert daemon.core.sessions.get(session_id) is None
        assert await _jarvis_row(client) is None
        parked = await _jarvis_row(client, include_closed=True)
        assert parked is not None and parked["sessionId"] == session_id
        assert not [
            a for a in (await client.ok("agent.list"))["agents"] if a["name"] == "jarvis"
        ]
        assert await _error_code(client, "jarvis.overview") == "jarvis_disabled"
        await client.stop()
    finally:
        await daemon.stop()

    # Restart while off: nothing is reopened; enabling again keeps the session.
    daemon = await make_daemon(home)
    try:
        client = await connect(http, daemon)
        status = await client.ok("jarvis.status")
        assert status["enabled"] is False and status["running"] is False
        assert status["sessionId"] == session_id
        assert daemon.core.sessions.get(session_id) is None
        status = await client.ok("jarvis.enable", {})
        assert status["sessionId"] == session_id
        session = daemon.core.sessions.get(session_id)
        assert session is not None and len(session.history.snapshot()) == history_len
        await client.stop()
    finally:
        await daemon.stop()


async def test_enable_sets_model_and_folder(http: aiohttp.ClientSession, tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path))
    try:
        client = await connect(http, daemon)
        frame = await client.call("jarvis.enable", {"model": "no-such-profile"})
        assert frame["error"]["data"]["code"] == "invalid_params"
        assert (await client.ok("jarvis.status"))["enabled"] is False
        frame = await client.call("jarvis.enable", {"folder": "relative/path"})
        assert frame["error"]["data"]["code"] == "invalid_params"

        other = tmp_path / "Elsewhere"
        status = await client.ok("jarvis.enable", {"folder": str(other)})
        assert status["folder"] == str(other.resolve()) and other.is_dir()
        session = daemon.core.sessions.get(status["sessionId"])
        assert session is not None and Path(session.workdir) == other.resolve()

        # Moving the folder later moves the running session with it.
        third = tmp_path / "Third"
        status = await client.ok("jarvis.enable", {"folder": str(third)})
        assert Path(session.workdir) == third.resolve() and third.is_dir()
        stored = await daemon.core.store.session(session.id)
        assert stored["workdir"] == str(third.resolve())
    finally:
        await daemon.stop()


async def test_settings_edit_follows(http: aiohttp.ClientSession, tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path))
    try:
        client = await connect(http, daemon)
        await client.ok("settings.set", {"patch": {"jarvis": {"enabled": True}}})
        for _ in range(100):
            if daemon.core.jarvis.running:
                break
            await asyncio.sleep(0.02)
        assert daemon.core.jarvis.running
        await client.ok("settings.set", {"patch": {"jarvis": {"enabled": False}}})
        for _ in range(100):
            if not daemon.core.jarvis.running:
                break
            await asyncio.sleep(0.02)
        assert not daemon.core.jarvis.running
    finally:
        await daemon.stop()


# -- the reserved name -----------------------------------------------------------


async def test_the_name_jarvis_is_reserved(http: aiohttp.ClientSession, tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path))
    try:
        registry = named.registry(daemon.core)
        with pytest.raises(named.NamedAgentError, match="reserved"):
            await registry.create("jarvis", None, workdir=tmp_path)
        with pytest.raises(named.NamedAgentError, match="reserved"):
            await registry.create("Jarvis", None, workdir=tmp_path)
        client = await connect(http, daemon)
        await client.ok("jarvis.enable", {})
        frame = await client.call("agent.delete", {"name": "jarvis"})
        assert frame["error"]["data"]["code"] == "unauthorized"
        assert daemon.core.jarvis.session() is not None
        # Other names are untouched.
        agent = await registry.create("helper", None, workdir=tmp_path)
        assert agent.memory_namespace == "agent:helper"
    finally:
        await daemon.stop()


# -- exposure, overview, older clients ----------------------------------------------


async def test_exposure_hides_a_project_everywhere(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    shown = tmp_path / "shown"
    hidden = tmp_path / "hidden"
    shown.mkdir()
    hidden.mkdir()
    _hide(hidden)
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, enabled=True))
    try:
        core = daemon.core
        client = await connect(http, daemon)
        a = await core.sessions.create(shown, mode="auto")
        b = await core.sessions.create(hidden, mode="auto")
        rows = (await client.ok("jarvis.overview", {}))["rows"]
        ids = {row["sessionId"] for row in rows}
        assert a.id in ids and b.id not in ids
        assert core.jarvis.session_id not in ids
        status = await client.ok("jarvis.status")
        assert canonical(hidden) in status["hiddenProjects"]
        assert canonical(shown) not in status["hiddenProjects"]
        listed = {r["sessionId"]: r for r in (await client.ok("session.list"))["sessions"]}
        assert listed[a.id]["jarvis"]["exposed"] is True
        assert listed[a.id]["jarvis"]["level"] == "staged"
        assert listed[b.id]["jarvis"]["exposed"] is False
        assert listed[b.id]["jarvis"]["level"] is None
        # A takeover there only records the pause; Jarvis still neither sees nor decides.
        taken = await client.ok("jarvis.takeover", {"sessionId": b.id})
        assert taken["state"]["exposed"] is False and taken["state"]["level"] is None
        projects = {p["root"]: p for p in (await client.ok("jarvis.project.list"))["projects"]}
        assert projects[canonical(hidden)]["exposed"] is False
        assert projects[canonical(shown)]["exposed"] is True
        # Jarvis's own folder is not a project.
        assert canonical(core.jarvis.folder()) not in projects
    finally:
        await daemon.stop()


async def test_older_clients_see_an_ordinary_named_agent(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, enabled=True))
    try:
        client = await connect(http, daemon, client_version="1.52.0")
        row = await _jarvis_row(client)
        assert row is not None
        # A 1.52 client's model has neither field: what remains is a named agent.
        old = {k: v for k, v in row.items() if k not in ("role", "jarvis")}
        parsed = SessionSummary.model_validate(old)
        assert parsed.kind == "agent" and parsed.agent == "jarvis" and parsed.role is None
        # Unknown notifications are plain JSON-RPC notifications an old client skips.
        assert all(f["method"] != "jarvis.state" or "id" not in f for f in client.notifications)
    finally:
        await daemon.stop()


async def test_takeover_and_handback(http: aiohttp.ClientSession, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, enabled=True))
    try:
        client = await connect(http, daemon)
        session = await daemon.core.sessions.create(project, mode="auto")
        result = await client.ok("jarvis.takeover", {"sessionId": session.id})
        assert result["state"] == {
            "exposed": True,
            "level": "staged",
            "pausedUntil": None,
            "takenOver": True,
            "levelScope": "default",
            "grantId": None,
        }
        paused = (await client.ok("jarvis.status"))["paused"]
        assert paused == [
            {
                "sessionId": session.id,
                "external": None,
                "project": canonical(project),
                "reason": "takeover",
                "until": None,
            }
        ]
        limited = await client.ok("jarvis.takeover", {"sessionId": session.id, "minutes": 5})
        assert limited["state"]["takenOver"] is False and limited["state"]["pausedUntil"]
        back = await client.ok("jarvis.handback", {"sessionId": session.id})
        assert back["state"]["pausedUntil"] is None and back["state"]["takenOver"] is False
        assert (await client.ok("jarvis.status"))["paused"] == []
        reasons = [p["reason"] for p in client.of_method("jarvis.state")]
        assert "paused" in reasons and "resumed" in reasons
        external = await client.ok(
            "jarvis.takeover", {"external": {"cli": "codex", "id": "t-1"}, "minutes": 3}
        )
        assert external["state"]["pausedUntil"]
        frame = await client.call(
            "jarvis.takeover", {"sessionId": daemon.core.jarvis.session_id}
        )
        assert frame["error"]["data"]["code"] == "invalid_params"
        frame = await client.call("jarvis.takeover", {"sessionId": "s-nope"})
        assert frame["error"]["data"]["code"] == "not_found"
    finally:
        await daemon.stop()


# -- project names -------------------------------------------------------------------


async def test_project_names_and_aliases(http: aiohttp.ClientSession, tmp_path: Path) -> None:
    alpha = tmp_path / "alpha-repo"
    beta = tmp_path / "beta-repo"
    alpha.mkdir()
    beta.mkdir()
    home = tmp_path / "home"
    daemon = await make_daemon(home, _settings(tmp_path))
    try:
        core = daemon.core
        await core.sessions.create(alpha, mode="auto")
        await core.sessions.create(beta, mode="auto")
        client = await connect(http, daemon)
        projects = {p["root"]: p for p in (await client.ok("jarvis.project.list"))["projects"]}
        assert projects[canonical(alpha)]["name"] == "alpha-repo"
        assert projects[canonical(alpha)]["named"] is False

        renamed = await client.ok("jarvis.project.rename", {"root": str(alpha), "name": "Shop"})
        assert renamed["project"]["name"] == "Shop" and renamed["project"]["named"] is True
        aliased = await client.ok(
            "jarvis.project.alias", {"root": str(alpha), "add": ["store", "쇼핑몰"]}
        )
        assert aliased["project"]["aliases"] == ["store", "쇼핑몰"]
        frame = await client.call("jarvis.project.rename", {"root": str(beta), "name": "shop"})
        assert frame["error"]["data"]["code"] == "invalid_params"
        frame = await client.call("jarvis.project.alias", {"root": str(beta), "add": ["STORE"]})
        assert frame["error"]["data"]["code"] == "invalid_params"
        frame = await client.call("jarvis.project.rename", {"root": "relative", "name": "x"})
        assert frame["error"]["data"]["code"] == "invalid_params"
        assert core.jarvis.projects.resolve("쇼핑몰") == canonical(alpha)
        assert core.jarvis.projects.resolve("beta-repo") is None or core.jarvis.projects.resolve(
            "beta-repo"
        ) == canonical(beta)
        removed = await client.ok(
            "jarvis.project.alias", {"root": str(alpha), "remove": ["STORE"]}
        )
        assert removed["project"]["aliases"] == ["쇼핑몰"]
        assert (home / "jarvis" / "projects.json").is_file()
        await client.stop()
    finally:
        await daemon.stop()
    daemon = await make_daemon(home)
    try:
        client = await connect(http, daemon)
        projects = {p["root"]: p for p in (await client.ok("jarvis.project.list"))["projects"]}
        assert projects[canonical(alpha)]["name"] == "Shop"
        assert projects[canonical(alpha)]["aliases"] == ["쇼핑몰"]
        cleared = await client.ok("jarvis.project.rename", {"root": str(alpha), "name": None})
        assert cleared["project"]["name"] == "alpha-repo"
    finally:
        await daemon.stop()


# -- the per-turn overview ------------------------------------------------------------


async def test_each_jarvis_turn_sees_the_overview(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, enabled=True))
    try:
        core = daemon.core
        echo = _Echo()
        core.providers.get = lambda _p, _m: echo
        worker = await core.sessions.create(project, mode="auto")
        worker.title = "Fix the login bug"
        jarvis = core.jarvis.session()
        assert jarvis is not None
        items = dict(runtime_context.REGISTRY.collect(core, jarvis))
        assert "jarvis-overview" in items
        assert worker.id in items["jarvis-overview"]
        assert "Fix the login bug" in items["jarvis-overview"]
        # Nobody else gets it.
        assert "jarvis-overview" not in dict(runtime_context.REGISTRY.collect(core, worker))
        agent_loop.start_turn(core, jarvis, "what is going on?")
        await asyncio.wait_for(jarvis.turn_task, 30)
        sent = "\n".join(message_text(m) for m in echo.seen[-1])
        assert "Jarvis overview: 1 supervised session(s)" in sent
    finally:
        await daemon.stop()


async def test_routed_request_wakes_jarvis_with_a_system_turn(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """The real tick loop: a request routed to Jarvis starts its turn about 2 s later."""
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, enabled=True))
    try:
        core = daemon.core
        echo = _Echo()
        core.providers.get = lambda _p, _m: echo
        service = core.jarvis
        # Fully delegated by default: a risky command goes to Jarvis.
        delegate_everything(core)
        client = await connect(http, daemon)
        worker = await core.sessions.create(project, mode="auto")
        jarvis = service.session()
        assert jarvis is not None
        core.hub.subscribe(client_recorder := _Events(), jarvis.id)
        ask = asyncio.ensure_future(
            core.approvals.request(
                worker, "shell", {"command": "make build"}, unattended=True, timeout_sec=30,
                tags=("exec",),
            )
        )
        started = await client_recorder.wait("turn.started", timeout=10)
        rows = (await client.ok("jarvis.overview", {}))["rows"]
        row = next(r for r in rows if r["sessionId"] == worker.id)
        assert row["status"] == "awaiting_approval" and row["pendingApprovals"] == 1
        assert row["waitingOnPerson"] == 0  # routed to Jarvis
        assert started["payload"]["reason"] == "jarvis-intake"
        assert started["payload"]["initiator"] == "system"
        await asyncio.wait_for(jarvis.turn_task, 30)
        sent = "\n".join(message_text(m) for m in echo.seen[-1])
        assert "make build" in sent and "Waiting for your decision" in sent
        assert core.approvals.list()[0].route == "jarvis"
        assert service.intake.budget.wakes_this_hour() == 1
        await core.approvals.respond(ask_id(core, worker.id), "deny")
        await asyncio.wait_for(ask, 5)
    finally:
        await daemon.stop()


def ask_id(core: Any, session_id: str) -> str:
    entries = [r for r in core.approvals.list() if r.sessionId == session_id]
    assert entries
    return entries[0].requestId


class _Events:
    closed = False

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "session.event":
            self.events.append(params)

    async def wait(self, kind: str, timeout: float) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for event in self.events:
                if event["kind"] == kind:
                    return event
            await asyncio.sleep(0.02)
        raise AssertionError(f"no {kind}; saw {[e['kind'] for e in self.events]}")
