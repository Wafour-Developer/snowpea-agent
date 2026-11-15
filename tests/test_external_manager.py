"""M19 WP5: the external session manager, end to end with the generic test adapter.

The adapter (``tests/_external_adapter.py``) drives ``fake_claude.py`` through
the base layer exactly like a real adapter would.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _external_adapter import FAKE_CLAUDE, FakeClaudeAdapter
from _support import env_vars

from snowpea_core.config.settings import Settings
from snowpea_core.external import proc
from snowpea_core.external.manager import ExternalManager
from snowpea_core.external.proc import ExternalError
from snowpea_core.external.registry import AdapterRegistry
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.server import errors

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")


class Conn:
    """A connection as the manager sees one."""

    def __init__(self, kind: str | None = "ide") -> None:
        self.client_kind = kind
        self.client_id = f"{kind}-1"
        self.authenticated = True
        self.closed = False
        self.on_close: list[Any] = []
        self.frames: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.frames.append((method, params))

    def events(self) -> list[dict[str, Any]]:
        return [params for method, params in self.frames if method == "external.event"]

    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.events()]

    async def close(self) -> None:
        self.closed = True
        for callback in self.on_close:
            await callback(self)


class Hub:
    def __init__(self) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any], *, exclude: Any = None) -> None:
        self.notes.append((method, params))

    async def tell_observers(self, method: str, params: dict[str, Any]) -> None:
        return None

    def states(self) -> list[dict[str, Any]]:
        return [params for method, params in self.notes if method == "external.state"]

    def pending(self) -> list[dict[str, Any]]:
        return [params["request"] for method, params in self.notes
                if method == "approval.pending"]


async def until(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def _settings(**cli: Any) -> Settings:
    return Settings.model_validate(
        {"jarvis": {"external": {"clis": {"claude": {"path": str(FAKE_CLAUDE), **cli}}}}}
    )


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.project = (tmp_path / "proj").resolve()
        self.project.mkdir(parents=True)
        self.outside = (tmp_path / "elsewhere").resolve()
        self.outside.mkdir()
        self.settings = _settings()
        self.hub = Hub()
        self.approvals = ApprovalQueue(self.settings, hub=self.hub)  # type: ignore[arg-type]
        self.log = tmp_path / "fake.log"
        self.manager = ExternalManager(
            home=self.home,
            settings=lambda: self.settings,
            hub=self.hub,
            approvals=self.approvals,
            registry=AdapterRegistry([FakeClaudeAdapter()]),
            roots=self._roots,
            poll_interval=0.1,
            sweep_interval=3600,
        )

    async def _roots(self) -> list[str]:
        return [str(self.project)]

    def starts(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["data"] for line in self.log.read_text().splitlines()
                if json.loads(line)["kind"] == "start"]


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    setup = Env(tmp_path)
    with env_vars(FAKE_CLI_LOG=str(setup.log), FAKE_CLI_SCENARIO=None,
                  FAKE_CLI_SCENARIO_FILE=None):
        await setup.manager.start()
        try:
            yield setup
        finally:
            await setup.manager.shutdown()


async def _turn(env: Env, conn: Conn, ref: dict[str, str], text: str = "hello") -> str:
    turn = (await env.manager.send(ref["cli"], ref["id"], text, conn=conn))["turnId"]
    await until(lambda: any(e["kind"] == "turn.done" and e["turnId"] == turn
                            for e in conn.events()))
    return turn


# -- zero adapters ----------------------------------------------------------------


async def test_no_adapters_means_nothing_is_available(tmp_path: Path) -> None:
    manager = ExternalManager(home=tmp_path, settings=Settings, registry=AdapterRegistry())
    rows = await manager.clis()
    assert [row["id"] for row in rows] == ["claude", "codex", "agy", "cursor"]
    assert all(not row["installed"] and row["problems"] for row in rows)
    assert rows[3]["defaultMode"] == "readonly"
    calls = [
        manager.list_sessions(all_projects=True),
        manager.open("claude", "abc"),
        manager.read("codex", "abc"),
        manager.send("agy", "abc", "hi"),
        manager.interrupt("cursor", "abc"),
        manager.create("claude", str(tmp_path)),
        manager.set_mode("claude", "abc", "readonly"),
        manager.close("claude", "abc"),
    ]
    for call in calls:
        with pytest.raises(ExternalError) as caught:
            await call
        assert caught.value.code == errors.EXTERNAL_UNAVAILABLE


async def test_clis_reports_the_installed_adapter(env: Env) -> None:
    rows = {row["id"]: row for row in await env.manager.clis()}
    claude = rows["claude"]
    assert claude["installed"] and claude["path"] == str(FAKE_CLAUDE.resolve())
    assert claude["version"] == "2.1.325 (Claude Code, fake)" and claude["problems"] == []
    assert claude["capabilities"]["approvals"] == "bridged"
    assert not rows["codex"]["installed"]
    env.settings = Settings.model_validate(
        {"jarvis": {"external": {"clis": {"claude": {"path": str(FAKE_CLAUDE),
                                                     "enabled": False}}}}}
    )
    with pytest.raises(ExternalError) as caught:
        await env.manager.create("claude", str(env.project))
    assert caught.value.code == errors.EXTERNAL_UNAVAILABLE


# -- create, send, fan-out -----------------------------------------------------------


async def test_create_send_and_events_fan_out(env: Env) -> None:
    watcher, bystander = Conn("ide"), Conn("tui")
    created = (await env.manager.create("claude", str(env.project), conn=watcher))["session"]
    ref = created["ref"]
    assert created["host"] == "snowpea" and created["canSend"]
    assert created["approvals"] == "bridged" and created["mode"] == "default"
    assert created["controlled"] and created["project"] == str(env.project)
    turn = await _turn(env, watcher, ref)
    kinds = watcher.kinds()
    assert kinds[:2] == ["turn.started", "message.user"]
    assert "message.done" in kinds and kinds[-1] == "turn.done"
    seqs = [event["seq"] for event in watcher.events()]
    assert seqs == list(range(1, len(seqs) + 1))
    done = next(e for e in watcher.events() if e["kind"] == "message.done")
    assert done["payload"]["text"] == "OK" and done["turnId"] == turn
    user = next(e for e in watcher.events() if e["kind"] == "message.user")
    assert user["payload"]["origin"] == {"kind": "human", "surface": "ide"}
    assert bystander.events() == []
    states = env.hub.states()
    assert [s["status"] for s in states if s["ref"] == ref][-1] == "idle"
    assert any(s["status"] == "running" for s in states)
    # The bystander subscribes by opening: it gets the page, then live events.
    opened = await env.manager.open(ref["cli"], ref["id"], conn=bystander, limit=3)
    assert opened["hasMore"] and len(opened["events"]) == 3 and not opened["readOnly"]
    older = await env.manager.read(ref["cli"], ref["id"],
                                   before_seq=opened["events"][0]["seq"], limit=100)
    assert [e["seq"] for e in older["events"]] == list(range(1, opened["events"][0]["seq"]))
    await _turn(env, watcher, ref, "again")
    assert bystander.kinds()[-1] == "turn.done"
    # A connection that goes away stops getting events.
    await bystander.close()
    count = len(bystander.events())
    await _turn(env, watcher, ref, "third")
    assert len(bystander.events()) == count
    # The prompt went over stdin, never into argv; the env was scrubbed.
    start = env.starts()[0]
    assert "hello" not in json.dumps(start["argv"]) and start["bypass"] == []
    assert not [n for n in start["env"] if n.startswith("SNOWPEA_")]


async def test_list_by_working_directory_and_paging(env: Env) -> None:
    conn = Conn()
    refs = []
    for text in ("one", "two", "three"):
        ref = (await env.manager.create("claude", str(env.project), conn=conn))["session"]["ref"]
        await _turn(env, conn, ref, text)
        await env.manager.close(ref["cli"], ref["id"], conn=conn)
        refs.append(ref)
        await asyncio.sleep(0.02)
    first = await env.manager.list_sessions(cwd=str(env.project), limit=2)
    assert len(first["sessions"]) == 2 and first["nextBefore"]
    rest = await env.manager.list_sessions(cwd=str(env.project), limit=2,
                                           before=first["nextBefore"])
    ids = [row["ref"]["id"] for row in first["sessions"] + rest["sessions"]]
    assert sorted(ids) == sorted(r["id"] for r in refs) and len(set(ids)) == 3
    assert rest["nextBefore"] is None
    row = first["sessions"][0]
    assert row["host"] == "none" and row["canSend"] and row["project"] == str(env.project)
    everything = await env.manager.list_sessions(all_projects=True, limit=10)
    assert len(everything["sessions"]) == 3
    with pytest.raises(ExternalError) as caught:
        await env.manager.list_sessions(cwd=str(env.outside))
    assert caught.value.code == errors.OUTSIDE_ROOT
    with pytest.raises(ExternalError):
        await env.manager.list_sessions()


async def test_history_never_carries_owner_records(env: Env) -> None:
    conn = Conn()
    ref = (await env.manager.create("claude", str(env.project), conn=conn))["session"]["ref"]
    await _turn(env, conn, ref)
    await env.manager.close(ref["cli"], ref["id"], conn=conn)
    transcript = next((Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects").glob(
        f"*/{ref['id']}.jsonl"))
    assert "owner@example.com" in transcript.read_text()  # the fake wrote it ...
    viewer = Conn()
    opened = await env.manager.open(ref["cli"], ref["id"], conn=viewer)
    assert "owner@example.com" not in json.dumps(opened)  # ... nobody sees it
    assert [e["kind"] for e in opened["events"]][:1] == ["message.user"]


# -- approvals ---------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["allow", "deny"])
async def test_approval_bridge_round_trip(env: Env, answer: str) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="approval"):
        ref = (await env.manager.create("claude", str(env.project), conn=conn))["session"]["ref"]
        turn = (await env.manager.send(ref["cli"], ref["id"], "write it", conn=conn))["turnId"]
    request = (await until(env.hub.pending))[0]
    assert request["sessionId"] == f"ext:claude:{ref['id']}"
    assert request["external"] == {"cli": "claude", "id": ref["id"]}
    assert request["tool"] == "Write" and request["args"]["content"] == "hi\n"
    await env.approvals.respond(request["requestId"], answer, by="client", conn=conn,
                                reason="not now" if answer == "deny" else "")
    await until(lambda: any(e["kind"] == "turn.done" and e["turnId"] == turn
                            for e in conn.events()))
    result = next(e for e in conn.events() if e["kind"] == "tool.result")
    note = env.project / "note.txt"
    if answer == "allow":
        assert result["payload"]["ok"] and note.read_text() == "hi\n"
    else:
        assert not result["payload"]["ok"] and result["payload"]["error"] == "not now"
        assert not note.exists()
    resolved = [p for m, p in env.hub.notes if m == "approval.resolved"]
    assert resolved and resolved[0]["decision"] == answer


async def test_session_answer_is_remembered_for_the_session(env: Env) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="approval"):
        ref = (await env.manager.create("claude", str(env.project), conn=conn))["session"]["ref"]
        turn = (await env.manager.send(ref["cli"], ref["id"], "write", conn=conn))["turnId"]
        request = (await until(env.hub.pending))[0]
        await env.approvals.respond(request["requestId"], "allow", scope="session", conn=conn)
        await until(lambda: any(e["kind"] == "turn.done" and e["turnId"] == turn
                                for e in conn.events()))
        await _turn(env, conn, ref, "write again")
    assert len(env.hub.pending()) == 1  # the second Write was not asked again


async def test_interrupt_denies_a_pending_approval(env: Env) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="approval"):
        ref = (await env.manager.create("claude", str(env.project), conn=conn))["session"]["ref"]
        turn = (await env.manager.send(ref["cli"], ref["id"], "write", conn=conn))["turnId"]
    await until(env.hub.pending)
    await env.manager.interrupt(ref["cli"], ref["id"])
    done = await until(lambda: [e for e in conn.events()
                                if e["kind"] == "turn.done" and e["turnId"] == turn])
    assert done[0]["payload"]["reason"] == "interrupted"
    assert not env.approvals.unattended()
    assert not (env.project / "note.txt").exists()


# -- dying, hanging, idle --------------------------------------------------------------


async def test_cli_dying_mid_turn(env: Env, tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"turns": ["die", "normal"]}))
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO_FILE=str(scenario)):
        ref = (await env.manager.create("claude", str(env.project), conn=conn))["session"]["ref"]
        turn = (await env.manager.send(ref["cli"], ref["id"], "hi", conn=conn))["turnId"]
        done = await until(lambda: [e for e in conn.events()
                                    if e["kind"] == "turn.done" and e["turnId"] == turn])
        assert done[0]["payload"]["reason"] == "error"
        error = next(e for e in conn.events() if e["kind"] == "error")
        assert "status 3" in error["payload"]["message"]
        state = await until(lambda: [s for s in env.hub.states() if s["status"] == "error"])
        assert state[-1]["host"] == "none" and state[-1]["error"]
        assert not env.manager.supervisor.live()
        # The next prompt continues the session in a fresh process (--resume).
        # The fake counts turns per process, so its first turn here dies again;
        # a turns file of ["normal"] is what a restarted process should see.
        scenario.write_text(json.dumps({"turns": ["normal"]}))
        await _turn(env, conn, ref, "again")
    assert env.starts()[-1]["argv"].count("--resume") == 1
    row = env.manager.opened()[0]
    assert row["status"] == "idle" and row["host"] == "snowpea"


async def test_close_kills_a_hung_cli(env: Env) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="hang"):
        ref = (await env.manager.create("claude", str(env.project), conn=conn))["session"]["ref"]
        turn = (await env.manager.send(ref["cli"], ref["id"], "hi", conn=conn))["turnId"]
    pid = env.manager.supervisor.live()[0].pid
    await asyncio.sleep(0.3)
    await env.manager.close(ref["cli"], ref["id"], conn=conn)
    assert not proc.pid_alive(pid) or not env.manager.supervisor.live()
    done = [e for e in conn.events() if e["kind"] == "turn.done" and e["turnId"] == turn]
    assert done and done[0]["payload"]["reason"] == "interrupted"
    assert env.hub.states()[-1]["host"] == "none"


async def test_idle_channels_are_stopped(env: Env) -> None:
    conn = Conn()
    ref = (await env.manager.create("claude", str(env.project), conn=conn))["session"]["ref"]
    await _turn(env, conn, ref)
    env.manager._idle_sec = lambda: 0.0  # type: ignore[method-assign]
    assert await env.manager.sweep_idle() == 1
    assert not env.manager.supervisor.live()
    assert env.hub.states()[-1]["host"] == "none"
    # Still subscribed; the next prompt starts it again.
    await _turn(env, conn, ref, "back")
    assert env.manager.opened()[0]["host"] == "snowpea"


# -- view only, takeover -----------------------------------------------------------------


async def test_view_only_while_open_elsewhere_then_takeover(env: Env) -> None:
    owner = Conn()
    ref = (await env.manager.create("claude", str(env.project), conn=owner))["session"]["ref"]
    await _turn(env, owner, ref)
    await env.manager.close(ref["cli"], ref["id"], conn=owner)
    # The person resumes it in their own terminal.
    terminal = subprocess.Popen(
        [str(FAKE_CLAUDE), "--fake-terminal", "--resume", ref["id"]],
        cwd=env.project, stdin=subprocess.PIPE, env=proc.scrubbed_env(),
    )
    try:
        sessions = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions"
        await until(lambda: (sessions / f"{terminal.pid}.json").exists())
        viewer = Conn("tui")
        opened = await env.manager.open(ref["cli"], ref["id"], conn=viewer)
        assert opened["readOnly"] and opened["session"]["host"] == "terminal"
        assert not opened["session"]["canSend"] and opened["session"]["approvals"] == "none"
        assert opened["events"]  # history is readable
        listed = await env.manager.list_sessions(cwd=str(env.project))
        assert listed["sessions"][0]["host"] == "terminal"
        for call in (env.manager.send(ref["cli"], ref["id"], "hi", conn=viewer),
                     env.manager.set_mode(ref["cli"], ref["id"], "readonly", conn=viewer),
                     env.manager.interrupt(ref["cli"], ref["id"])):
            with pytest.raises(ExternalError) as caught:
                await call
            assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
        assert not env.manager.supervisor.live()  # nothing started behind the person's back
        # The person closes their terminal.
        assert terminal.stdin is not None
        terminal.stdin.close()
        terminal.wait(timeout=10)
        state = await until(lambda: [s for s in env.hub.states()
                                     if s.get("previousHost") == "terminal"])
        assert state[-1]["host"] == "none" and state[-1]["canSend"]
        # Takeover: the next prompt continues it under snowpea.
        await _turn(env, viewer, ref, "taking over")
        assert env.manager.opened()[0]["host"] == "snowpea"
    finally:
        if terminal.poll() is None:
            terminal.kill()
            terminal.wait()


# -- unrestricted ------------------------------------------------------------------------


def _remote() -> Conn:
    """A web login on the remote listener: a person, but not for unrestricted mode."""
    conn = Conn("web")
    conn.remote_login = True  # type: ignore[attr-defined]
    return conn


async def test_unrestricted_only_from_a_person_with_a_ledger_entry(env: Env) -> None:
    for conn in (None, Conn("browser"), Conn("connector"), _remote()):
        with pytest.raises(ExternalError) as caught:
            await env.manager.create("claude", str(env.project), mode="unrestricted", conn=conn)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert not env.manager.supervisor.live() and env.starts() == []
    ledger = env.home / "jarvis" / "ledger.jsonl"
    assert not ledger.exists()
    person = Conn("tui")
    row = (await env.manager.create("claude", str(env.project), mode="unrestricted",
                                    conn=person))["session"]
    assert row["mode"] == "unrestricted" and row["controlled"] is False
    assert row["approvals"] == "none"
    assert env.starts()[-1]["bypass"] == ["--dangerously-skip-permissions"]
    entries = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert entries[-1]["kind"] == "unrestricted" and entries[-1]["decision"] == "create"
    assert entries[-1]["project"] == str(env.project)


async def test_set_mode_and_default_mode_setting(env: Env) -> None:
    env.settings = _settings(defaultMode="readonly")
    conn = Conn("web")
    ref = (await env.manager.create("claude", str(env.project), conn=conn))["session"]["ref"]
    assert env.starts()[-1]["argv"][env.starts()[-1]["argv"].index("--permission-mode") + 1] \
        == "plan"
    with pytest.raises(ExternalError) as caught:
        await env.manager.set_mode(ref["cli"], ref["id"], "unrestricted", conn=None)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    row = (await env.manager.set_mode(ref["cli"], ref["id"], "unrestricted", conn=conn))["session"]
    assert row["mode"] == "unrestricted" and not row["controlled"]
    state = env.hub.states()[-1]
    assert state["mode"] == "unrestricted" and state["controlled"] is False
    await _turn(env, conn, ref, "from the next turn")
    assert env.starts()[-1]["bypass"] == ["--dangerously-skip-permissions"]
    entries = (env.home / "jarvis" / "ledger.jsonl").read_text().splitlines()
    assert json.loads(entries[-1])["decision"] == "setMode"
    with pytest.raises(ExternalError):
        await env.manager.set_mode(ref["cli"], ref["id"], "yolo", conn=conn)


async def test_refusals(env: Env) -> None:
    conn = Conn()
    with pytest.raises(ExternalError) as caught:
        await env.manager.create("claude", str(env.outside), conn=conn)
    assert caught.value.code == errors.OUTSIDE_ROOT
    for model in ("--dangerously-skip-permissions", "-x", "a b"):
        with pytest.raises(ExternalError) as caught:
            await env.manager.create("claude", str(env.project), model=model, conn=conn)
        assert caught.value.code == errors.INVALID_PARAMS
    for bad_id in ("--resume", "../x"):
        with pytest.raises(ExternalError) as caught:
            await env.manager.open("claude", bad_id, conn=conn)
        assert caught.value.code == errors.INVALID_PARAMS
    with pytest.raises(ExternalError) as caught:
        await env.manager.open("claude", "0000-not-there", conn=conn)
    assert caught.value.code == errors.NOT_FOUND
    with pytest.raises(ExternalError) as caught:
        await env.manager.create("claude", str(env.project), transport="tmux", conn=conn)
    assert caught.value.code == errors.EXTERNAL_UNAVAILABLE
    assert env.starts() == []


async def test_a_session_recorded_outside_every_project_is_not_opened(env: Env) -> None:
    conn = Conn()
    ref = (await env.manager.create("claude", str(env.project), conn=conn))["session"]["ref"]
    await _turn(env, conn, ref)
    await env.manager.close(ref["cli"], ref["id"], conn=conn)

    async def no_roots() -> list[str]:
        return [str(env.outside)]

    env.manager._roots = no_roots
    with pytest.raises(ExternalError) as caught:
        await env.manager.open(ref["cli"], ref["id"], conn=conn)
    assert caught.value.code == errors.OUTSIDE_ROOT


async def test_shutdown_stops_every_child(env: Env) -> None:
    conn = Conn()
    await env.manager.create("claude", str(env.project), conn=conn)
    child = env.manager.supervisor.live()[0]
    await env.manager.shutdown()
    assert not child.alive
    assert not env.manager.supervisor.live()
    assert not list((env.home / "external").glob("*.json"))
