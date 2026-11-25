"""M42 security review regressions (2026-11-25)."""

# ruff: noqa: E402

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest_plugins = ["test_m40_machines", "test_m42_jarvis_remote"]

from datetime import timedelta

from test_m42_jarvis_remote import PERSON, _ctx, _pair_connected, _tool, qualify

from snowpea_core.machines.gates import run_proxy_gates
from snowpea_core.machines.jarvis_remote import RemoteJarvisRelay
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import SessionSetModeParams


@pytest.mark.asyncio
async def test_c1_remote_shell_typing_refused(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    async with __import__("aiohttp").ClientSession() as http:
        from _support import connect

        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "shell", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        await rclient.ok("terminal.setJarvis", {"terminalId": term_id, "enabled": True})
        qid = qualify(mid, term_id)
        sent = await _tool(local.core, "terminal_send", {"id": qid, "text": "echo hi"})
        assert not sent.ok
        assert "shell" in (sent.error or "").lower()
        esc = await _tool(local.core, "terminal_send", {"id": qid, "text": "ok\n!id"})
        assert not esc.ok
        await rclient.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_cli")
async def test_c1_remote_keys_records_decision_with_machine(daemon_pair) -> None:
    """Non-prompt keys on a CLI terminal record a ledger decision naming the machine."""
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    async with __import__("aiohttp").ClientSession() as http:
        from _support import connect

        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "codex", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        await rclient.ok("terminal.setJarvis", {"terminalId": term_id, "enabled": True})
        qid = qualify(mid, term_id)
        pressed = await _tool(local.core, "terminal_keys", {"id": qid, "keys": ["enter"]})
        assert pressed.ok, pressed.error
        ledger = Path(local.core.paths.home) / "jarvis" / "ledger.jsonl"
        decisions = [
            json.loads(line)
            for line in ledger.read_text().splitlines()
            if line.strip() and json.loads(line).get("type") == "decision"
        ]
        assert any("Work box" in (d.get("summary") or "") for d in decisions[-3:])
        await rclient.stop()


@pytest.mark.asyncio
async def test_m1_switch_off_refuses_remote_read(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    async with __import__("aiohttp").ClientSession() as http:
        from _support import connect

        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "shell", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        with pytest.raises(RpcError) as caught:
            from snowpea_core.machines.jarvis_remote import jarvis_proxy_call

            await jarvis_proxy_call(
                local.core, mid, "terminal.read", {"terminalId": term_id, "lines": 5}
            )
        assert caught.value.code == errors.NOT_FOUND
        await rclient.stop()


def test_h1_proxy_session_set_mode_capped_for_remote_login() -> None:
    from test_m40_security import _conn, _core

    core = _core()
    core.settings.web.remote.maxMode = "plan"
    remote = _conn("remote_login")
    params = SessionSetModeParams(sessionId="s1", mode="auto")
    with pytest.raises(RpcError) as caught:
        run_proxy_gates("session.setMode", remote, params, core, "mid")
    assert caught.value.code == errors.JARVIS_PERSON_ONLY


def test_l1_spoofed_parent_jarvis_does_not_enable_switch(tmp_path: Path) -> None:
    import asyncio

    from _support import connect, make_daemon

    async def run() -> None:
        daemon = await make_daemon(tmp_path / "home", {})
        async with __import__("aiohttp").ClientSession() as http:
            client = await connect(http, daemon)
            created = await client.ok(
                "terminal.create", {"profile": "shell", "noProject": True, "parent": "jarvis"}
            )
            tid = created["terminal"]["terminalId"]
            from snowpea_core.jarvis.terminals import key_of

            assert not daemon.core.jarvis.engine.switch(key_of(daemon.core.terminals.get(tid)))
            await client.stop()
        await daemon.stop()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_m5_remote_wake_notice_without_full_delegation(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    intake = local.core.jarvis.intake
    assert intake is not None
    intake._routed.clear()
    intake._notices.clear()
    async with __import__("aiohttp").ClientSession() as http:
        from _support import connect

        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "shell", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        await rclient.ok("terminal.setJarvis", {"terminalId": term_id, "enabled": True})
        from snowpea_core.machines.jarvis_remote import jarvis_proxy_call

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
        assert not intake._routed
        assert intake._notices
        await rclient.stop()


def test_l5_jarvis_cannot_turn_on_existing_switch_row(tmp_path: Path) -> None:
    from snowpea_core.jarvis.delegation import DelegationEngine

    engine = DelegationEngine(tmp_path / "jarvis")
    for by in ("person", "new", "fork", "migration"):
        key = f"s-{by}"
        engine.set_switch(key, False, by=by, quiet=True)
        assert not engine.set_switch(key, True, by="jarvis")


def test_l5_jarvis_fresh_create_may_turn_switch_on(tmp_path: Path) -> None:
    from snowpea_core.jarvis.delegation import DelegationEngine

    engine = DelegationEngine(tmp_path / "jarvis")
    key = "s-brand-new"
    assert engine.set_switch(key, True, by="jarvis", quiet=True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_cli")
async def test_c1b_local_grant_does_not_authorize_remote_terminal(daemon_pair) -> None:
    """Remote typing must not inherit local delegation for the same bare terminal id."""
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    async with __import__("aiohttp").ClientSession() as http:
        from _support import connect

        from snowpea_core.jarvis.remote_terminal import load_remote_terminal
        from snowpea_core.jarvis.terminals import delegated

        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "codex", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        await rclient.ok("terminal.setJarvis", {"terminalId": term_id, "enabled": True})
        qid = qualify(mid, term_id)
        view = await load_remote_terminal(local.core.jarvis, qid)
        local.core.jarvis.engine.set_grant("session", term_id, "full", caller=PERSON)
        assert delegated(local.core.jarvis, view) is False
        ctx = _ctx(local.core)
        ctx.session.turn_origins[ctx.session.current_turn] = "jarvis"
        from snowpea_core.jarvis.terminals import terminal_send
        from snowpea_core.jarvis.tools import Refused

        with pytest.raises(Refused, match="delegat"):
            await terminal_send(ctx, {"id": qid, "text": "hello"})
        await rclient.stop()


@pytest.mark.asyncio
async def test_c1c_remote_snapshot_refuses_missing_status() -> None:
    from snowpea_core.jarvis.remote_terminal import _view_from_row

    row = {"profile": "codex", "alive": True}
    with pytest.raises(ValueError):
        _view_from_row("mid:t1", "mid", "t1", row, "screen")


@pytest.mark.asyncio
async def test_c1c_terminal_keys_expect_state_refused(tmp_path: Path) -> None:
    from _support import connect, make_daemon

    daemon = await make_daemon(tmp_path / "home", {})
    try:
        async with __import__("aiohttp").ClientSession() as http:
            client = await connect(http, daemon)
            created = await client.ok("terminal.create", {"profile": "shell", "noProject": True})
            tid = created["terminal"]["terminalId"]
            from snowpea_core.server.protocol import TerminalKeysParams
            from snowpea_core.server.terminal_handlers import keys_handler

            params = TerminalKeysParams(terminalId=tid, keys=["enter"], expectState="blocked")
            with pytest.raises(RpcError) as caught:
                await keys_handler(client, params, daemon.core)
            assert caught.value.code == errors.INVALID_PARAMS
            await client.stop()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_h1_remote_login_prompt_without_jarvis_relay(daemon_pair, tmp_path: Path) -> None:
    import asyncio

    from _support import connect
    from test_m40_security import _conn

    from snowpea_core.cli.daemon_client import DaemonInfo
    from snowpea_core.machines.manager import MachineManager
    from snowpea_core.server.machine_handlers import proxy_call
    from snowpea_core.server.protocol import PROTOCOL_VERSION, SessionPromptParams

    local, remote = daemon_pair
    if local.core.jarvis is not None and local.core.jarvis.running:
        await local.core.jarvis.disable()
    info = DaemonInfo(port=remote.port, pid=0, token=remote.token, protocolVersion=PROTOCOL_VERSION)
    local.core.machines = MachineManager(local.core, direct=lambda _id: info)
    local.core.machines.bind_loop(asyncio.get_running_loop())
    local.core.settings.web.remote.machines = True
    local.core.settings.web.remote.maxMode = "auto"
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
    async with __import__("aiohttp").ClientSession() as http:
        rclient = await connect(http, remote)
        proj = tmp_path / "remoteproj"
        proj.mkdir()
        (proj / ".snowpea").mkdir()
        session = await rclient.ok("session.create", {"workdir": str(proj)})
        sid = session["sessionId"]
        await rclient.ok("jarvis.enable")
        await rclient.ok("jarvis.session.set", {"sessionId": sid, "enabled": True})
        conn = _conn("remote_login")
        params = SessionPromptParams(sessionId=sid, text="hi")
        result = await proxy_call(conn, "session.prompt", mid, params.model_dump(), local.core)
        assert result.get("turnId") or result.get("besideTurn") is not None
        await rclient.ok("session.setMode", {"sessionId": sid, "mode": "auto"})
        local.core.settings.web.remote.maxMode = "plan"
        with pytest.raises(RpcError) as caught:
            deny = SessionPromptParams(sessionId=sid, text="nope").model_dump()
            await proxy_call(conn, "session.prompt", mid, deny, local.core)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
        await rclient.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_cli")
async def test_m4_remote_terminal_decision_uses_qualified_external_ref(daemon_pair) -> None:
    local, remote = daemon_pair
    mid = await _pair_connected(local, remote)
    async with __import__("aiohttp").ClientSession() as http:
        from _support import connect

        rclient = await connect(http, remote)
        created = await rclient.ok("terminal.create", {"profile": "codex", "noProject": True})
        term_id = created["terminal"]["terminalId"]
        await rclient.ok("terminal.setJarvis", {"terminalId": term_id, "enabled": True})
        qid = qualify(mid, term_id)
        pressed = await _tool(local.core, "terminal_keys", {"id": qid, "keys": ["down"]})
        assert pressed.ok, pressed.error
        ledger = Path(local.core.paths.home) / "jarvis" / "ledger.jsonl"
        decisions = [
            json.loads(line)
            for line in ledger.read_text().splitlines()
            if line.strip() and json.loads(line).get("type") == "decision"
        ]
        last = decisions[-1]
        assert str(last.get("sessionId") or "").startswith(f"{mid}:")
        assert last.get("external") is None
        await rclient.stop()


@pytest.mark.asyncio
async def test_m2_remote_external_pause_honoured(daemon_pair) -> None:
    from snowpea_core.jarvis.tools import Refused, _not_paused
    from snowpea_core.machines.jarvis_remote import RemoteExternalSessionRow

    local, _remote = daemon_pair
    mid = await _pair_connected(local, _remote)
    ext_key = qualify(mid, "ext:codex:abc12345")
    until = (local.core.jarvis.engine.now() + timedelta(hours=1)).replace(microsecond=0).isoformat()
    cache = local.core.machines._jarvis_cache  # noqa: SLF001
    cache._externals[ext_key] = RemoteExternalSessionRow(  # noqa: SLF001
        machine_id=mid,
        cli="codex",
        ext_id="abc12345",
        title="",
        cwd="/work",
        status="idle",
        jarvis_on=True,
        paused_until=until,
        jarvis_paused_until=until,
    )
    with pytest.raises(Refused):
        _not_paused(local.core.jarvis, ext_key)


@pytest.mark.asyncio
async def test_h1_r2_plan_then_auto_remote_prompt_refused(
    daemon_pair, tmp_path: Path
) -> None:
    import asyncio

    from _support import connect
    from test_m40_security import _conn

    from snowpea_core.cli.daemon_client import DaemonInfo
    from snowpea_core.machines.manager import MachineManager
    from snowpea_core.server.machine_handlers import proxy_call
    from snowpea_core.server.protocol import PROTOCOL_VERSION, SessionPromptParams

    local, remote = daemon_pair
    info = DaemonInfo(port=remote.port, pid=0, token=remote.token, protocolVersion=PROTOCOL_VERSION)
    local.core.machines = MachineManager(local.core, direct=lambda _id: info)
    local.core.machines.bind_loop(asyncio.get_running_loop())
    local.core.settings.web.remote.machines = True
    local.core.settings.web.remote.maxMode = "plan"
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
    async with __import__("aiohttp").ClientSession() as http:
        rclient = await connect(http, remote)
        proj = tmp_path / "remoteproj2"
        proj.mkdir()
        (proj / ".snowpea").mkdir()
        session = await rclient.ok("session.create", {"workdir": str(proj)})
        sid = session["sessionId"]
        await rclient.ok(
            "session.setMode",
            {"sessionId": sid, "mode": "plan", "autoAfterPlan": True},
        )
        conn = _conn("remote_login")
        with pytest.raises(RpcError) as caught:
            params = SessionPromptParams(sessionId=sid, text="run it")
            await proxy_call(conn, "session.prompt", mid, params.model_dump(), local.core)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
        await rclient.stop()


@pytest.mark.asyncio
async def test_c1c_terminal_keys_expect_blocked_requires_permission(tmp_path: Path) -> None:
    from _support import connect, make_daemon

    from snowpea_core.server.protocol import TerminalKeysParams
    from snowpea_core.server.terminal_handlers import keys_handler

    daemon = await make_daemon(tmp_path / "home", {})
    try:
        async with __import__("aiohttp").ClientSession() as http:
            client = await connect(http, daemon)
            created = await client.ok("terminal.create", {"profile": "shell", "noProject": True})
            tid = created["terminal"]["terminalId"]
            term = daemon.core.terminals.get(tid)
            term.status.apply_osc("blocked", kind="question")
            params = TerminalKeysParams(terminalId=tid, keys=["enter"], expectState="blocked")
            with pytest.raises(RpcError) as caught:
                await keys_handler(client, params, daemon.core)
            assert caught.value.code == errors.INVALID_PARAMS
            assert (caught.value.data or {}).get("reason") == "expect_state"
            await client.stop()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_c1c_terminal_keys_expect_state_atomic_under_lock(tmp_path: Path) -> None:
    from _support import connect, make_daemon

    from snowpea_core.server.protocol import TerminalKeysParams
    from snowpea_core.server.terminal_handlers import keys_handler

    daemon = await make_daemon(tmp_path / "home", {})
    try:
        async with __import__("aiohttp").ClientSession() as http:
            client = await connect(http, daemon)
            created = await client.ok("terminal.create", {"profile": "shell", "noProject": True})
            tid = created["terminal"]["terminalId"]
            term = daemon.core.terminals.get(tid)
            term.status.apply_osc("working")
            original = term.gate.keys

            async def race_keys(names: list[str], *, guard=None) -> None:
                term.status.apply_osc("idle")
                await original(names, guard=guard)

            term.gate.keys = race_keys  # type: ignore[method-assign]
            params = TerminalKeysParams(terminalId=tid, keys=["enter"], expectState="running")
            with pytest.raises(RpcError) as caught:
                await keys_handler(client, params, daemon.core)
            assert caught.value.code == errors.INVALID_PARAMS
            assert (caught.value.data or {}).get("reason") == "expect_state"
            await client.stop()
    finally:
        await daemon.stop()