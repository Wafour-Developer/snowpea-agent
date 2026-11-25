"""M42: Jarvis manages remote machines (1.78.0, jarvisRemote)."""

# ruff: noqa: E402, I001

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import aiohttp
import pytest

pytest_plugins = ["test_m40_machines"]

from _support import connect
from test_jarvis_core import FakeClock

from snowpea_core.jarvis.delegation import Caller

PERSON = Caller(kind="connection", surface="ide")

from snowpea_core.cli.daemon_client import DaemonInfo
from snowpea_core.machines.gates import gate_jarvis_machine_call, run_jarvis_proxy_gates
from snowpea_core.machines.jarvis_remote import (
    RemoteJarvisRelay,
    delegation_key,
    jarvis_proxy_call,
    qualify,
)
from snowpea_core.machines.manager import MachineManager
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import PROTOCOL_VERSION, TerminalCreateParams
from snowpea_core.tools.registry import ToolContext

# A stand-in for the codex/claude TUI: CI machines have neither CLI installed.
FAKE_CLI = r'''#!/usr/bin/env python3
import sys
sys.stdout.write("\x1b]7501;state=idle\x1b\\cli> ")
sys.stdout.flush()
for line in sys.stdin:
    sys.stdout.write("got:" + line.strip() + "\r\ncli> ")
    sys.stdout.flush()
'''


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Terminal profiles find a fake CLI (``profiles.cli_state``), never a real one."""
    from snowpea_core.external import claude, codex
    from snowpea_core.terminal import profiles

    script = tmp_path / "fake-cli-bin" / "fake-cli"
    script.parent.mkdir()
    script.write_text(FAKE_CLI, encoding="utf-8")
    script.chmod(0o755)

    async def state(_core, cli: str) -> profiles.CliState:
        modes = codex.NATIVE_MODES if cli == "codex" else claude.NATIVE_MODES
        return profiles.CliState(str(script), modes, [])

    monkeypatch.setattr(profiles, "cli_state", state)


async def _pair_connected(local, remote) -> str:
    info = DaemonInfo(port=remote.port, pid=0, token=remote.token, protocolVersion=PROTOCOL_VERSION)
    local.core.machines = MachineManager(local.core, direct=lambda _id: info)
    local.core.machines.bind_loop(asyncio.get_running_loop())
    mid = local.core.machines.registry.add(
        "me@box",
        label="Work box",
        key_path=None,
        pasted_key=None,
        remote_cwd=None,
        auto_connect=False,
    ).id
    local.core.machines.connect(mid)
    for _ in range(80):
        if next(m for m in local.core.machines.rows() if m.id == mid).status == "connected":
            break
        await asyncio.sleep(0.05)
    await local.core.jarvis.enable()
    return mid


def _ctx(core) -> ToolContext:
    jarvis = core.jarvis.session()
    assert jarvis is not None
    turn = "t-test"
    jarvis.current_turn = turn
    jarvis.turn_origins[turn] = "human"
    return ToolContext(session=jarvis, core=core, backend=None)  # type: ignore[arg-type]


async def _tool(core, name: str, args: dict):
    tool = core.jarvis.session().session_tools[name]
    return await tool.run(_ctx(core), args)


@pytest.mark.asyncio
async def test_jarvis_proxy_lists_remote_terminal_when_switch_on(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    async with aiohttp.ClientSession() as http:
        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "shell", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        await rclient.ok("terminal.setJarvis", {"terminalId": term_id, "enabled": True})
        listed = await jarvis_proxy_call(local.core, mid, "terminal.list", {})
        rows = listed.get("terminals") or []
        assert any(r.get("terminalId") == term_id and r.get("jarvisOn") for r in rows)
        read = await jarvis_proxy_call(
            local.core, mid, "terminal.read", {"terminalId": term_id, "lines": 5}
        )
        assert "text" in read
        qid = qualify(mid, term_id)
        assert qid.startswith(mid)
        table = await _tool(local.core, "terminal_list", {})
        assert qid in table.output and "Work box" in table.output
        await rclient.ok("terminal.close", {"terminalId": term_id, "force": True})
        await rclient.stop()


@pytest.mark.asyncio
async def test_switch_off_terminal_absent_from_remote_list(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    async with aiohttp.ClientSession() as http:
        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "shell", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        listed = await jarvis_proxy_call(local.core, mid, "terminal.list", {})
        rows = listed.get("terminals") or []
        row = next((r for r in rows if r.get("terminalId") == term_id), {})
        assert not row.get("jarvisOn")
        qid = qualify(mid, term_id)
        read = await _tool(local.core, "terminal_read", {"id": qid})
        assert not read.ok
        await rclient.stop()


@pytest.mark.asyncio
async def test_remote_wake_names_machine(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    intake = local.core.jarvis.intake
    assert intake is not None
    intake._routed.clear()
    async with aiohttp.ClientSession() as http:
        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "shell", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        await rclient.ok("terminal.setJarvis", {"terminalId": term_id, "enabled": True})
        await jarvis_proxy_call(local.core, mid, "terminal.list", {})
        relay = RemoteJarvisRelay(
            machine_id=mid,
            label="Work box",
            cache=local.core.machines._jarvis_cache,  # noqa: SLF001
        )
        await relay.handle(
            local.core,
            "terminal.status",
            {"terminalId": term_id, "status": {"state": "blocked", "kind": "permission"}},
        )
        from snowpea_core.jarvis.delegation import Caller

        PERSON = Caller(kind="connection", surface="ide")
        local.core.jarvis.engine.set_grant(
            "session", delegation_key(mid, term_id), "full", caller=PERSON
        )
        relay.states.clear()
        intake._routed.clear()
        await relay.handle(
            local.core,
            "terminal.status",
            {"terminalId": term_id, "status": {"state": "blocked", "kind": "permission"}},
        )
        assert intake._routed
        assert "Work box" in intake._routed[-1].text
        assert intake._routed[-1].session_id == delegation_key(mid, term_id)
        await rclient.stop()


@pytest.mark.asyncio
async def test_jarvis_cannot_turn_switch_on_via_engine(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    async with aiohttp.ClientSession() as http:
        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "shell", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        key = delegation_key(mid, term_id)
        local.core.jarvis.engine.set_switch(key, False, by="person", quiet=True)
        assert not local.core.jarvis.engine.set_switch(key, True, by="jarvis")
        await rclient.ok("terminal.setJarvis", {"terminalId": term_id, "enabled": True})
        await jarvis_proxy_call(local.core, mid, "terminal.list", {})
        assert local.core.machines._jarvis_cache.jarvis_on(mid, term_id)  # noqa: SLF001
        await rclient.stop()


@pytest.mark.asyncio
async def test_local_session_switch_off_invisible_to_jarvis(tmp_path: Path) -> None:
    from _support import make_daemon

    jarvis_dir = tmp_path / "JarvisFolder"
    daemon = await make_daemon(
        tmp_path / "home",
        {"jarvis": {"enabled": True, "folder": str(jarvis_dir)}},
    )
    try:
        await daemon.core.jarvis.enable()
        assert daemon.core.jarvis.running
        project = tmp_path / "p"
        project.mkdir()
        hidden = await daemon.core.sessions.create(project, mode="accept")
        rows = daemon.core.jarvis.overview_rows()
        assert not any(r.sessionId == hidden.id for r in rows)
        visible = await daemon.core.sessions.create(project, mode="accept")
        daemon.core.jarvis.engine.set_switch(visible.id, True, by="person")
        await daemon.core.jarvis.settle()
        rows = daemon.core.jarvis.overview_rows()
        assert any(r.sessionId == visible.id for r in rows)
    finally:
        await daemon.stop()


def test_jarvis_proxy_refuses_trust_dangerous_and_snowpea_tui() -> None:
    from types import SimpleNamespace

    from test_m40_security import _core

    core = _core()
    core.jarvis = SimpleNamespace(running=True)
    with pytest.raises(RpcError) as caught:
        run_jarvis_proxy_gates(
            "terminal.create",
            TerminalCreateParams(profile="shell", cwd="/p", trust=True),
            core,
            "mid",
        )
    assert caught.value.code == errors.UNAUTHORIZED
    with pytest.raises(RpcError):
        gate_jarvis_machine_call(
            "terminal.create",
            TerminalCreateParams(profile="snowpea", cwd="/p"),
            core,
            "mid",
        )


@pytest.mark.asyncio
async def test_remote_terminal_send_refuses_shell(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    async with aiohttp.ClientSession() as http:
        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "shell", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        await rclient.ok("terminal.setJarvis", {"terminalId": term_id, "enabled": True})
        await jarvis_proxy_call(local.core, mid, "terminal.list", {})
        qid = qualify(mid, term_id)
        sent = await _tool(
            local.core,
            "terminal_send",
            {"id": qid, "text": "echo hi"},
        )
        assert not sent.ok
        assert "shell" in (sent.error or "").lower()
        await rclient.stop()


@pytest.mark.asyncio
async def test_reconnect_resubscribes_jarvis_relay(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    manager = local.core.machines
    ensure = manager.ensure_jarvis_relay
    await ensure(mid)
    first = manager._jarvis_relays.get(mid)  # noqa: SLF001
    assert first is not None
    await manager.close_jarvis_client(mid)
    await ensure(mid)
    second = manager._jarvis_relays.get(mid)  # noqa: SLF001
    assert second is not None and second is not first


def test_protocol_advertises_jarvis_remote() -> None:
    from snowpea_core.server import protocol

    assert protocol.PROTOCOL_VERSION == "1.79.0"
    assert "jarvisRemote" in protocol.CAPABILITIES


@pytest.mark.asyncio
async def test_local_terminal_human_input_pauses_then_quiet_resumes(tmp_path: Path) -> None:
    from _support import make_daemon
    from snowpea_core.jarvis.service import JarvisService
    from snowpea_core.jarvis.terminals import key_of

    jarvis_dir = tmp_path / "JarvisFolder"
    daemon = await make_daemon(
        tmp_path / "home",
        {"jarvis": {"enabled": True, "folder": str(jarvis_dir)}},
    )
    clock = FakeClock()
    old = daemon.core.jarvis
    if old is not None:
        await old.stop()
    service = JarvisService(daemon.core, clock=clock)
    daemon.core.jarvis = service
    await service.start()
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            await client.ok("jarvis.enable")
            created = await client.ok("terminal.create", {"profile": "shell", "noProject": True})
            tid = created["terminal"]["terminalId"]
            key = key_of(daemon.core.terminals.get(tid))
            service.engine.set_switch(key, True, by="person")
            service.engine.set_grant("session", key, "full", caller=PERSON)
            payload = base64.b64encode(b"x").decode("ascii")
            await client.ok("terminal.write", {"terminalId": tid, "data": payload, "human": True})
            assert service.engine.pause_state(key).paused
            from snowpea_core.jarvis.classify import ClassifyRequest

            blocked = service.engine.route(
                ClassifyRequest(
                    tool="shell",
                    args={"command": "ls"},
                    tags=("exec",),
                    session_id=key,
                )
            )
            assert "paused" in (blocked.reason or "")
            clock.advance(11 * 60)
            assert not service.engine.pause_state(key).paused
            state = service.engine.takeover(key, caller=PERSON)
            assert state.taken_over
            assert service.engine.handback(key, caller=PERSON).paused is False
            await client.stop()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_remote_terminal_write_pauses_machine_qualified_key(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    async with aiohttp.ClientSession() as http:
        lclient = await connect(http, local)
        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "shell", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        await rclient.ok("terminal.setJarvis", {"terminalId": term_id, "enabled": True})
        key = delegation_key(mid, term_id)
        local.core.jarvis.engine.set_grant("session", key, "full", caller=PERSON)
        payload = base64.b64encode(b"a").decode("ascii")
        await lclient.ok(
            "terminal.write",
            {"terminalId": term_id, "machine": mid, "data": payload, "human": True},
        )
        assert local.core.jarvis.engine.pause_state(key).paused
        await rclient.stop()
        await lclient.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_cli")
async def test_jarvis_created_terminal_starts_with_switch_on(tmp_path: Path) -> None:
    from _support import make_daemon
    from snowpea_core.jarvis.terminals import key_of

    jarvis_dir = tmp_path / "JarvisFolder"
    project = tmp_path / "p"
    project.mkdir()
    (project / ".snowpea").mkdir()
    (project / ".snowpea" / "settings.json").write_text('{"jarvis":{"expose":true}}')
    daemon = await make_daemon(
        tmp_path / "home",
        {"jarvis": {"enabled": True, "folder": str(jarvis_dir)}},
    )
    try:
        await daemon.core.jarvis.enable()
        from snowpea_core.tools.registry import ToolContext

        jarvis = daemon.core.jarvis.session()
        assert jarvis is not None
        jarvis.current_turn = "t"
        jarvis.turn_origins["t"] = "human"
        ctx = ToolContext(session=jarvis, core=daemon.core, backend=None)  # type: ignore[arg-type]
        from snowpea_core.jarvis.terminals import terminal_create

        created = await terminal_create(ctx, {"profile": "codex", "project": str(project)})
        assert created.ok, created.error
        tid = str((created.meta or {}).get("terminalId") or "")
        key = key_of(daemon.core.terminals.get(tid))
        assert daemon.core.jarvis.engine.switch(key)
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# remote snowpea sessions (pass 3)
# ---------------------------------------------------------------------------


async def _remote_session(rclient, *, on: bool) -> str:
    """A session on the remote daemon; its Jarvis switch on or off (a person's call there)."""
    created = await rclient.ok("session.create", {"workdir": "/tmp", "noProject": True})
    sid = created["sessionId"]
    if on:
        await rclient.ok("jarvis.session.set", {"sessionId": sid, "enabled": True})
    return sid


def _overview_ids(core) -> list[str | None]:
    return [row.sessionId for row in core.jarvis.overview_rows()]


@pytest.mark.asyncio
async def test_remote_snowpea_session_listed_when_switch_on_invisible_off(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    await remote.core.jarvis.enable()
    async with aiohttp.ClientSession() as http:
        rclient = await connect(http, remote)
        sid = await _remote_session(rclient, on=False)
        qid = qualify(mid, sid)
        await jarvis_proxy_call(local.core, mid, "session.list", {})
        assert qid not in _overview_ids(local.core)
        await rclient.ok("jarvis.session.set", {"sessionId": sid, "enabled": True})
        await jarvis_proxy_call(local.core, mid, "session.list", {})
        rows = [r for r in local.core.jarvis.overview_rows() if r.sessionId == qid]
        assert len(rows) == 1
        assert rows[0].machine == mid and rows[0].machineLabel == "Work box"
        table = await _tool(local.core, "session_list", {})
        assert qid in table.output and "Work box" in table.output
        await rclient.ok("jarvis.session.set", {"sessionId": sid, "enabled": False})
        await jarvis_proxy_call(local.core, mid, "session.list", {})
        assert qid not in _overview_ids(local.core)
        await rclient.stop()


@pytest.mark.asyncio
async def test_remote_session_read_prompt_interrupt_through_gates(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    await remote.core.jarvis.enable()
    async with aiohttp.ClientSession() as http:
        rclient = await connect(http, remote)
        sid_on = await _remote_session(rclient, on=True)
        sid_off = await _remote_session(rclient, on=False)
        qid_on, qid_off = qualify(mid, sid_on), qualify(mid, sid_off)
        await jarvis_proxy_call(local.core, mid, "session.list", {})

        trajectory = await _tool(local.core, "session_read", {"sessionId": qid_on,
                                                              "view": "trajectory"})
        assert trajectory.ok, trajectory.error
        digest = await _tool(local.core, "session_read", {"sessionId": qid_on})
        assert digest.ok, digest.error
        assert "Work box" in digest.output
        assert not (await _tool(local.core, "session_read", {"sessionId": qid_off})).ok
        assert not (await _tool(local.core, "session_prompt",
                                {"sessionId": qid_off, "text": "hi"})).ok

        # The gates: a switched-off session is not found, and a switch is never Jarvis's.
        with pytest.raises(RpcError) as hidden:
            await jarvis_proxy_call(local.core, mid, "session.events", {"sessionId": sid_off})
        assert hidden.value.code == errors.NOT_FOUND
        with pytest.raises(RpcError) as switch:
            await jarvis_proxy_call(
                local.core, mid, "jarvis.session.set", {"sessionId": sid_off, "enabled": True}
            )
        assert switch.value.code == errors.JARVIS_PERSON_ONLY

        sent = await _tool(local.core, "session_prompt", {"sessionId": qid_on,
                                                          "text": "report status"})
        assert sent.ok, sent.error
        turn_id = (sent.meta or {}).get("turnId")
        assert turn_id
        seen = False
        for _ in range(100):
            events = (await rclient.ok("session.events", {"sessionId": sid_on}))["events"]
            seen = any(
                e["kind"] == "turn.started" and (e.get("payload") or {}).get("turnId") == turn_id
                for e in events
            )
            if seen:
                break
            await asyncio.sleep(0.05)
        assert seen
        stopped = await _tool(local.core, "session_interrupt", {"sessionId": qid_on})
        assert stopped.ok, stopped.error
        ledger = Path(local.core.paths.home) / "jarvis" / "ledger.jsonl"
        entries = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
        assert any(
            e["type"] == "access" and e.get("sessionId") == qid_on
            and "machine:Work box" in (e.get("hostToolsFrom") or "")
            for e in entries
        )
        await rclient.stop()


@pytest.mark.asyncio
async def test_remote_session_wake_names_machine(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    await remote.core.jarvis.enable()
    intake = local.core.jarvis.intake
    assert intake is not None
    async with aiohttp.ClientSession() as http:
        rclient = await connect(http, remote)
        sid = await _remote_session(rclient, on=True)
        key = delegation_key(mid, sid)
        relay = RemoteJarvisRelay(
            machine_id=mid,
            label="Work box",
            cache=local.core.machines._jarvis_cache,  # noqa: SLF001
        )
        await relay.refresh(local.core)
        local.core.jarvis.engine.set_grant("session", key, "full", caller=PERSON)
        intake._routed.clear()

        async def changed(status: str) -> None:
            await relay.handle(
                local.core,
                "sessions.changed",
                {"reason": "turn", "sessionId": sid, "status": status},
            )

        await changed("running")
        assert not intake._routed
        await changed("idle")
        assert len(intake._routed) == 1
        done = intake._routed[-1]
        assert "Work box" in done.text and "finished" in done.text
        assert done.session_id == key
        assert key in intake.digests and "Work box" in intake.digests[key].title

        await changed("awaiting_approval")
        blocked = intake._routed[-1]
        assert "Work box" in blocked.text and "blocked" in blocked.text
        assert blocked.importance == "high" and blocked.session_id == key

        # The same turn end through session.event is not woken twice.
        intake._routed.clear()
        await relay.handle(local.core, "session.event", {"sessionId": sid, "kind": "turn.started",
                                                         "payload": {}})
        await relay.handle(
            local.core,
            "session.event",
            {"sessionId": sid, "kind": "turn.done", "payload": {"reason": "complete"}},
        )
        await changed("idle")
        assert [item.reason for item in intake._routed] == ["done"]

        # Switched off there: nothing wakes Jarvis, the digest is gone.
        await rclient.ok("jarvis.session.set", {"sessionId": sid, "enabled": False})
        await relay.refresh(local.core)
        intake._routed.clear()
        await changed("awaiting_question")
        assert not intake._routed
        assert key not in intake.digests
        await rclient.stop()


@pytest.mark.asyncio
async def test_remote_session_human_prompt_pauses_through_proxy(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    await remote.core.jarvis.enable()
    async with aiohttp.ClientSession() as http:
        lclient = await connect(http, local)
        rclient = await connect(http, remote)
        sid = await _remote_session(rclient, on=True)
        key = delegation_key(mid, sid)
        await jarvis_proxy_call(local.core, mid, "session.list", {})
        local.core.jarvis.engine.set_grant("session", key, "full", caller=PERSON)

        # Jarvis's own prompt never pauses it.
        await _tool(local.core, "session_prompt", {"sessionId": qualify(mid, sid), "text": "go"})
        assert not local.core.jarvis.engine.pause_state(key).paused

        # The person's, through the machine proxy, does.
        await lclient.ok(
            "session.prompt",
            {"sessionId": sid, "machine": mid, "text": "I take over", "whenBusy": "queue"},
        )
        assert local.core.jarvis.engine.pause_state(key).paused
        await rclient.stop()
        await lclient.stop()


@pytest.mark.asyncio
async def test_remote_session_event_human_message_pauses_jarvis_message_does_not(
    daemon_pair,
) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    await remote.core.jarvis.enable()
    async with aiohttp.ClientSession() as http:
        rclient = await connect(http, remote)
        sid = await _remote_session(rclient, on=True)
        key = delegation_key(mid, sid)
        await jarvis_proxy_call(local.core, mid, "session.list", {})
        relay = RemoteJarvisRelay(
            machine_id=mid,
            label="Work box",
            cache=local.core.machines._jarvis_cache,  # noqa: SLF001
        )
        engine = local.core.jarvis.engine
        await relay.handle(
            local.core,
            "session.event",
            {"sessionId": sid, "kind": "message.user",
             "payload": {"text": "from Jarvis", "origin": {"kind": "jarvis", "surface": "jarvis"}}},
        )
        assert not engine.pause_state(key).paused
        await relay.handle(
            local.core,
            "session.event",
            {"sessionId": sid, "kind": "message.user",
             "payload": {"text": "from me", "origin": {"kind": "human"}}},
        )
        assert engine.pause_state(key).paused
        await rclient.stop()


# ---------------------------------------------------------------------------
# remote external sessions (pass 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_cli")
async def test_remote_external_session_visibility_tools_and_wake(daemon_pair, monkeypatch) -> None:
    from snowpea_core.jarvis.tools import PROMPT_PREFIX
    from snowpea_core.server.external_handlers import ensure_manager as external_manager

    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    await remote.core.jarvis.enable()
    ref = {"cli": "claude", "id": "abc123"}
    qid = qualify(mid, "ext:claude:abc123")
    row = {
        "ref": ref, "title": "Remote work", "cwd": "/tmp/remote-project", "project": None,
        "status": "idle", "host": "none", "canSend": True, "controlled": True,
        "mode": "default", "preview": "hello", "updatedAt": "2026-01-01T00:00:00Z",
    }
    typed: list[str] = []

    async def fake_list(**_kw):
        return {"sessions": [dict(row)], "nextBefore": None}

    async def fake_get(_cli, _sid):
        return {"session": dict(row)}

    async def fake_read(_cli, _sid, before_seq=None, limit=200):
        event = {"seq": 1, "kind": "message.done", "ts": "2026-01-01T00:00:00Z",
                 "payload": {"role": "assistant", "text": "hello from the remote CLI"}}
        return {"events": [event], "hasMore": False}

    async def fake_send(_cli, _sid, text, **_kw):
        typed.append(text)
        return {"turnId": "turn-1"}

    async def fake_interrupt(_cli, _sid):
        typed.append("<interrupt>")

    manager = external_manager(remote.core)
    monkeypatch.setattr(manager, "list_sessions", fake_list)
    monkeypatch.setattr(manager, "get", fake_get)
    monkeypatch.setattr(manager, "read", fake_read)
    monkeypatch.setattr(manager, "send", fake_send)
    monkeypatch.setattr(manager, "interrupt", fake_interrupt)

    # This computer has no external sessions of its own (and, on CI, no CLI installed).
    async def no_local_sessions(**_kw):
        return {"sessions": [], "nextBefore": None}

    monkeypatch.setattr(external_manager(local.core), "list_sessions", no_local_sessions)

    intake = local.core.jarvis.intake
    assert intake is not None
    async with aiohttp.ClientSession() as http:
        rclient = await connect(http, remote)

        # Switch off: listed by the machine, invisible to Jarvis.
        await jarvis_proxy_call(local.core, mid, "external.list", {"allProjects": True})
        assert not [r for r in local.core.jarvis.overview_rows() if r.external]
        refused = await _tool(local.core, "external_read", {"sessionId": qid})
        assert not refused.ok

        await rclient.ok("jarvis.session.set", {"external": ref, "enabled": True})
        await jarvis_proxy_call(local.core, mid, "external.list", {"allProjects": True})
        rows = [r for r in local.core.jarvis.overview_rows() if r.external]
        assert len(rows) == 1
        assert rows[0].sessionId is None and rows[0].external.id == "abc123"
        assert rows[0].machine == mid and rows[0].machineLabel == "Work box"
        listed = await _tool(local.core, "external_list", {})
        assert qid in listed.output and "Work box" in listed.output
        overview = await _tool(local.core, "session_list", {})
        assert qid in overview.output

        read = await _tool(local.core, "external_read", {"sessionId": qid})
        assert read.ok, read.error
        assert "hello from the remote CLI" in read.output
        sent = await _tool(local.core, "external_send", {"sessionId": qid, "text": "continue"})
        assert sent.ok, sent.error
        assert typed == [PROMPT_PREFIX + "continue"]
        stopped = await _tool(local.core, "external_interrupt", {"sessionId": qid})
        assert stopped.ok, stopped.error
        assert typed[-1] == "<interrupt>"

        # Wake on status changes, naming the machine.
        relay = RemoteJarvisRelay(
            machine_id=mid,
            label="Work box",
            cache=local.core.machines._jarvis_cache,  # noqa: SLF001
        )
        await relay.refresh(local.core)
        local.core.jarvis.engine.set_grant("session", qid, "full", caller=PERSON)
        intake._routed.clear()
        for status in ("running", "idle"):
            await relay.handle(local.core, "external.state", {"ref": ref, "status": status})
        assert len(intake._routed) == 1
        assert "Work box" in intake._routed[-1].text and intake._routed[-1].session_id == qid
        await relay.handle(local.core, "external.state", {"ref": ref, "status": "error"})
        assert intake._routed[-1].importance == "high"

        # Switched off again: gone from the overview and refused by the gate.
        await rclient.ok("jarvis.session.set", {"external": ref, "enabled": False})
        await jarvis_proxy_call(local.core, mid, "external.list", {"allProjects": True})
        assert not [r for r in local.core.jarvis.overview_rows() if r.external]
        with pytest.raises(RpcError) as caught:
            await jarvis_proxy_call(local.core, mid, "external.read", {"ref": ref})
        assert caught.value.code == errors.NOT_FOUND
        await rclient.stop()