"""The tmux bridge through the external manager (1.56.0): rows, sends, modes, prompts.

A stub adapter says the session is held by pid 4242; the fake tmux
(``tests/fixtures/fake_tmux.py``) and a fake probe put that pid in pane %3.
No real CLI, no real tmux server.
"""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _claude_support import until
from test_external_claude import Conn, Hub
from test_external_tmux_bridge import FAKE_TMUX, PTS3, RECORDINGS, FakeProbe, World

from snowpea_core.config.settings import Settings
from snowpea_core.external import claude_tmux
from snowpea_core.external.base import (
    CliCapabilities,
    Discovery,
    ExternalAdapter,
    Liveness,
    SessionInfo,
)
from snowpea_core.external.claude import LEGACY_NATIVE as CLAUDE_LEGACY
from snowpea_core.external.claude import NATIVE_MODES as CLAUDE_MODES
from snowpea_core.external.codex import LEGACY_NATIVE as CODEX_LEGACY
from snowpea_core.external.codex import NATIVE_MODES as CODEX_MODES
from snowpea_core.external.manager import ExternalManager
from snowpea_core.external.proc import ExternalError, PinnedExecutable
from snowpea_core.external.registry import AdapterRegistry
from snowpea_core.external.tmux import ProcFacts
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.server import errors
from snowpea_core.server.protocol import ApprovalRequest, ExternalSessionRow

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no tmux on Windows")

SESSION = "0b4f5e6a-1111-4222-8333-444455556666"


class TerminalAdapter(ExternalAdapter):
    """A CLI whose one session is held by pid 4242 in the person's terminal."""

    title = "Stub CLI"
    binary = "stub-cli"

    def __init__(self, cli: str, cwd: str) -> None:
        self.id = cli
        self.cwd = cwd
        self.native_table = CLAUDE_MODES if cli == "claude" else CODEX_MODES
        self.legacy_native = CLAUDE_LEGACY if cli == "claude" else CODEX_LEGACY
        self.capabilities = CliCapabilities(streaming=True, approvals="bridged", live_view=True)
        self.holder = 4242
        self.busy = False

    async def discover(self, path_override: str | None = None) -> Discovery:
        return Discovery(installed=True, executable=PinnedExecutable("/bin/true", "1"),
                         capabilities=self.capabilities)

    async def list_sessions(self, roots: Sequence[str], *, limit: int,
                            before: str | None = None) -> list[SessionInfo]:
        return [SessionInfo(id=SESSION, cwd=self.cwd, title="work")]

    async def get_session(self, session_id: str) -> SessionInfo | None:
        return SessionInfo(id=SESSION, cwd=self.cwd, title="work") if session_id == SESSION \
            else None

    async def read(self, info: SessionInfo) -> list[Any]:
        return []

    async def liveness(self, info: SessionInfo, *, own_pids: set[int]) -> Liveness:
        if self.holder is None:
            return Liveness()
        return Liveness(host="terminal", busy=self.busy, pids=(self.holder,))

    def channel(self, spec: Any, host: Any) -> Any:  # pragma: no cover - never started
        raise AssertionError("the bridge never starts a CLI")


class Env:
    def __init__(self, tmp: Path, world: World, probe: FakeProbe, cli: str) -> None:
        self.project = (tmp / "proj").resolve()
        self.project.mkdir(parents=True, exist_ok=True)
        self.hub = Hub()
        settings = Settings.model_validate({})
        self.approvals = ApprovalQueue(settings, hub=self.hub)  # type: ignore[arg-type]
        self.adapter = TerminalAdapter(cli, str(self.project))
        self.manager = ExternalManager(
            home=tmp / "home", settings=lambda: settings, hub=self.hub,
            approvals=self.approvals, registry=AdapterRegistry([self.adapter]),
            roots=self._roots, poll_interval=0.05, sweep_interval=3600,
        )
        self.manager.tmux_sockets = [world.socket]
        self.manager.tmux_probe = probe
        self.manager.tmux_start_timeout = 0.3
        self.world = world
        self.cli = cli

    async def _roots(self) -> list[str]:
        return [str(self.project)]

    async def open(self, conn: Any) -> dict[str, Any]:
        return await self.manager.open(self.cli, SESSION, conn=conn)


@pytest_asyncio.fixture
async def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    world = World(tmp_path)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(world.socket)
    monkeypatch.setenv("FAKE_TMUX_WORLD", str(world.path))
    monkeypatch.setenv("FAKE_TMUX_LOG", str(world.log))
    monkeypatch.setattr(claude_tmux.proc, "EXTRA_ALLOW_PREFIXES", ["FAKE_"])
    claude_tmux.set_tmux_binary(str(FAKE_TMUX))
    probe = FakeProbe()
    probe.procs[4242] = ProcFacts(start="111", tty_dev=PTS3, pgrp=4242, tpgid=4242)
    envs: list[Env] = []

    def build(cli: str = "claude") -> Env:
        env = Env(tmp_path / f"e{len(envs)}", world, probe, cli) if envs else \
            Env(tmp_path, world, probe, cli)
        envs.append(env)
        return env

    try:
        yield build, world, probe
    finally:
        for env in envs:
            await env.manager.shutdown()
        claude_tmux.set_tmux_binary(None)
        listener.close()


def _remote() -> Conn:
    conn = Conn("web")
    conn.remote_login = True  # type: ignore[attr-defined]
    return conn


async def test_a_session_in_a_tmux_pane_is_bridged(make: Any) -> None:
    build, world, _probe = make
    env = build()
    listed = await env.manager.list_sessions(cwd=str(env.project), limit=10)
    row = ExternalSessionRow.model_validate(listed["sessions"][0]).model_dump()
    assert row["host"] == "tmux" and row["canSend"] and row["controllable"] == "tmux"
    assert row["terminal"]["pane"] == "%3" and row["terminal"]["kind"] == "tmux"
    opened = await env.open(Conn("ide"))
    session = ExternalSessionRow.model_validate(opened["session"]).model_dump()
    assert session["host"] == "tmux" and not opened["readOnly"]
    assert session["terminal"] == {"kind": "tmux", "pane": "%3", "session": "work",
                                   "window": "1", "modeState": "confirmed", "prompt": None,
                                   "terminalId": None, "status": None}
    assert (session["mode"], session["nativeMode"]) == ("default", "default")
    assert session["approvals"] == "person"
    # Opening typed nothing.
    assert world.keys_sent() == []


async def test_not_in_tmux_stays_view_only(make: Any) -> None:
    build, world, probe = make
    env = build()
    world.data["panes"] = []
    world.save()
    opened = await env.open(Conn("ide"))
    assert opened["session"]["host"] == "terminal" and opened["readOnly"]
    assert opened["session"]["terminal"] is None
    with pytest.raises(ExternalError) as caught:
        await env.manager.send("claude", SESSION, "hi", conn=Conn("ide"))
    assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
    assert world.keys_sent() == []


async def test_person_sends_and_the_turn_ends(make: Any) -> None:
    build, world, _probe = make
    env = build()
    conn = Conn("ide")
    await env.open(conn)
    sent = await env.manager.send("claude", SESSION, "fix the build\nplease", conn=conn)
    assert sent["via"] == "tmux"
    loads = [c for c in world.calls() if c["argv"][2] == "load-buffer"]
    assert [c["stdin"] for c in loads] == ["fix the build\nplease"]
    done = await until(lambda: conn.turn_done(sent["turnId"]), 5)
    assert done["payload"]["reason"] == "complete"
    audit = (env.manager.home / "external" / "tmux-sends.jsonl").read_text().splitlines()
    entry = json.loads(audit[-1])
    assert entry["pane"] == "%3" and entry["action"] == "send" and entry["by"] == "person"
    assert "fix the build" not in audit[-1]  # the log has the pane and length, not the text


async def test_busy_cli_refuses_before_anything_is_published(make: Any) -> None:
    build, world, _probe = make
    env = build("codex")
    conn = Conn("ide")
    await env.open(conn)
    env.adapter.busy = True
    with pytest.raises(ExternalError) as caught:
        await env.manager.send("codex", SESSION, "more", conn=conn)
    assert caught.value.code == errors.SESSION_BUSY
    assert caught.value.details == {"reason": "terminal_busy"}
    assert world.keys_sent() == []
    assert not [e for e in conn.events() if e["kind"] == "message.user"]


async def test_who_may_type_into_the_persons_pane(make: Any) -> None:
    build, world, _probe = make
    env = build()
    await env.open(Conn("ide"))
    # Default mode, confirmed on screen: Jarvis and a remote login may send.
    for conn in (None, _remote()):
        sent = await env.manager.send("claude", SESSION, "go on", conn=conn,
                                      origin={"kind": "jarvis"} if conn is None else None)
        await until(lambda s=sent: env.manager._records[("claude", SESSION)].turn_id
                    != s["turnId"], 5)
    # Auto mode on screen (person-only): only the local person.
    world.data["screens"]["%3"] = ["  ⏵⏵ auto mode on (shift+tab to cycle)"]
    world.save()
    env2 = build()
    await env2.open(Conn("ide"))
    before = len(world.keys_sent())
    for conn in (None, _remote(), Conn("connector")):
        with pytest.raises(ExternalError) as caught:
            await env2.manager.send("claude", SESSION, "go on", conn=conn)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    # A mode the screen does not show: not Jarvis either.
    world.data["screens"]["%3"] = ["  (nothing recognisable)"]
    world.save()
    env3 = build()
    await env3.open(Conn("ide"))
    with pytest.raises(ExternalError) as caught:
        await env3.manager.send("claude", SESSION, "go on", conn=None)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert len(world.keys_sent()) == before
    # The person at a local surface always may.
    await env3.manager.send("claude", SESSION, "go on", conn=Conn("tui"))
    assert len(world.keys_sent()) > before


async def test_guard_failure_goes_back_to_view_only(make: Any) -> None:
    build, world, _probe = make
    env = build()
    conn = Conn("ide")
    await env.open(conn)
    world.pane("%3")["command"] = "bash"  # the CLI is gone from the foreground
    world.save()
    with pytest.raises(ExternalError) as caught:
        await env.manager.send("claude", SESSION, "rm nothing", conn=conn)
    assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
    assert world.keys_sent() == []
    row = (await env.manager.get("claude", SESSION))["session"]
    assert row["host"] == "terminal" and row["terminal"] is None and not row["canSend"]


async def test_mode_switch_in_the_terminal(make: Any) -> None:
    build, world, _probe = make
    world.data["screens"]["%3"] = ["  ? for shortcuts", "  ⏸ plan mode on"]
    world.save()
    env = build()
    await env.open(Conn("ide"))
    for conn in (None, Conn("connector"), _remote()):
        with pytest.raises(ExternalError) as caught:
            await env.manager.set_mode("claude", SESSION, native_mode="plan", conn=conn)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert world.keys_sent() == []

    async def to_plan() -> None:
        world.data["at"] = {"%3": 1}
        world.save()

    # The fake shows plan mode after /plan + Enter.
    row = None
    original = env.manager._bridge_of(env.manager._records[("claude", SESSION)]).bridge.slash

    async def slash(command: str, *, by: str = "x") -> None:
        await original(command, by=by)
        await to_plan()

    env.manager._bridge_of(env.manager._records[("claude", SESSION)]).bridge.slash = slash
    row = (await env.manager.set_mode("claude", SESSION, native_mode="plan",
                                      conn=Conn("ide")))["session"]
    assert (row["mode"], row["nativeMode"]) == ("readonly", "plan")
    assert row["terminal"]["modeState"] == "confirmed"
    assert ["send-keys", "-t", "%3", "-l", "--", "/plan"] in world.keys_sent()
    # A mode the table cannot reach in a terminal is refused, nothing typed.
    count = len(world.keys_sent())
    with pytest.raises(ExternalError):
        await env.manager.set_mode("claude", SESSION, native_mode="dontAsk", conn=Conn("ide"))
    assert len(world.keys_sent()) == count


async def test_recognised_prompt_becomes_a_person_only_approval(make: Any) -> None:
    build, world, _probe = make
    dialog = (RECORDINGS / "codex_05_permission_dialog.txt").read_text()
    env = build("codex")
    conn = Conn("ide")
    await env.open(conn)
    world.data["screens"]["%3"] = [dialog]
    world.save()
    env.adapter.busy = True
    pending = await until(env.hub.pending, 5)
    request = ApprovalRequest.model_validate(pending[0])
    assert request.personOnly is True and request.tool == "terminal.prompt"
    assert request.externalTerminal is not None and request.externalTerminal.pane == "%3"
    assert "Would you like to run" in request.externalTerminal.text
    row = (await env.manager.get("codex", SESSION))["session"]
    assert row["terminal"]["prompt"]["recognised"] is True
    # Jarvis cannot answer it; the person can, and "y" goes to the pane.
    from snowpea_core.server.errors import RpcError

    with pytest.raises(RpcError):
        await env.approvals.respond(request.requestId, "allow", by="jarvis")
    await env.approvals.respond(request.requestId, "allow", conn=conn)
    await until(lambda: ["send-keys", "-t", "%3", "y"] in world.keys_sent(), 5)


async def test_terminal_snapshot(make: Any) -> None:
    build, world, _probe = make
    env = build()
    world.data["screens"]["%3"] = ["\x1b[31mDo something?\x1b[0m\n  ? for shortcuts"]
    world.save()
    shot = await env.manager.terminal_snapshot("claude", SESSION)
    assert shot["pane"] == "%3" and "\x1b" not in shot["text"]
    assert "Do something?" in shot["text"]
    world.data["panes"] = []
    world.save()
    env2 = build()
    with pytest.raises(ExternalError) as caught:
        await env2.manager.terminal_snapshot("claude", SESSION)
    assert caught.value.code == errors.EXTERNAL_UNAVAILABLE
