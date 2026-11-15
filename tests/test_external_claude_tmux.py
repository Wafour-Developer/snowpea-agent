"""M19 WP6a: Claude Code in a shared tmux terminal (spike S5).

Every test runs its own tmux server (``tmux -L sp-test-<random>``) with the
fake Claude's interactive UI in the pane, and kills that server by its socket
at teardown.  The person's own tmux server is never touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _claude_support import (
    OWNER_EMAIL,
    StubHost,
    claude_home,
    fake_log,
    pinned,
    starts,
    until,
    user,
    write_transcript,
)
from _external_adapter import FAKE_CLAUDE
from _support import env_vars
from test_external_claude import Conn, Hub

from snowpea_core.config.settings import Settings
from snowpea_core.external import claude as claude_mod
from snowpea_core.external import proc
from snowpea_core.external.base import ApprovalAnswer, LaunchSpec, SessionInfo
from snowpea_core.external.claude import ClaudeAdapter
from snowpea_core.external.claude_tmux import (
    ClaudeTmuxChannel,
    Tmux,
    refuse_text,
    tmux_socket_of,
    tmux_version_ok,
    validate_pane,
)
from snowpea_core.external.manager import ExternalManager
from snowpea_core.external.proc import ExternalError
from snowpea_core.external.registry import AdapterRegistry
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="no tmux on Windows"),
    pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed"),
]


@pytest.fixture
def socket() -> Iterator[str]:
    name = f"sp-test-{uuid.uuid4().hex[:10]}"
    try:
        yield name
    finally:
        subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True, check=False)
        stale = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}" / name
        if stale.is_socket():
            stale.unlink()


def tmux(socket: str, *args: str) -> str:
    return subprocess.run(["tmux", "-L", socket, *args], capture_output=True, text=True,
                          check=False).stdout


class TmuxEnv:
    def __init__(self, tmp_path: Path, socket: str) -> None:
        self.socket = socket
        self.home = tmp_path / "home"
        self.project = (tmp_path / "proj").resolve()
        self.project.mkdir(parents=True)
        self.settings = Settings.model_validate(
            {"jarvis": {"external": {"clis": {"claude": {"path": str(FAKE_CLAUDE)}}}}}
        )
        self.hub = Hub()
        self.approvals = ApprovalQueue(self.settings, hub=self.hub)  # type: ignore[arg-type]
        self.log = tmp_path / "fake.log"
        self.adapter = ClaudeAdapter(tmux_socket=socket, poll_interval=0.05, ready_timeout=10.0)
        self.manager = ExternalManager(
            home=self.home, settings=lambda: self.settings, hub=self.hub,
            approvals=self.approvals, registry=AdapterRegistry([self.adapter]),
            roots=self._roots, poll_interval=0.1, sweep_interval=3600,
        )

    async def _roots(self) -> list[str]:
        return [str(self.project)]

    async def create(self, conn: Conn, **kwargs: Any) -> dict[str, Any]:
        return (await self.manager.create("claude", str(self.project), transport="tmux",
                                          conn=conn, **kwargs))["session"]

    async def turn(self, conn: Conn, ref: dict[str, str], text: str) -> dict[str, Any]:
        turn = (await self.manager.send("claude", ref["id"], text, conn=conn))["turnId"]
        return await until(lambda: conn.turn_done(turn))

    def transcript(self, session_id: str) -> list[dict[str, Any]]:
        path = claude_mod.transcript_path(str(self.project), session_id)
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest_asyncio.fixture
async def env(tmp_path: Path, socket: str) -> AsyncIterator[TmuxEnv]:
    setup = TmuxEnv(tmp_path, socket)
    with env_vars(FAKE_CLI_LOG=str(setup.log), FAKE_CLI_SCENARIO=None,
                  FAKE_CLI_SCENARIO_FILE=None, FAKE_CLAUDE_TRUST=None):
        await setup.manager.start()
        try:
            yield setup
        finally:
            await setup.manager.shutdown()


# -- small pieces ---------------------------------------------------------------------


async def test_tmux_version_and_ids() -> None:
    assert await tmux_version_ok()
    assert validate_pane("%12") == "%12"
    for bad in ("12", "%", "%1;kill-server", "-t", "%1 %2"):
        with pytest.raises(ExternalError):
            validate_pane(bad)
    with pytest.raises(ExternalError):
        Tmux(socket_name="../evil")
    with pytest.raises(ExternalError):
        Tmux(socket_path="/etc/passwd")


@pytest.mark.parametrize("text", ["/help", "  /model opus", "!ls", "\t!rm -rf x", "a\x1b[Ab",
                                  "ring\x07", "\x03", "", "   "])
def test_prompts_the_terminal_refuses(text: str) -> None:
    with pytest.raises(ExternalError) as caught:
        refuse_text(text)
    assert caught.value.code == errors.INVALID_PARAMS


def test_prompts_the_terminal_takes() -> None:
    for text in ("hello", "two\nlines", "a/b !c", "tab\there", "한국어 고마워요"):
        refuse_text(text)


# -- through the manager -----------------------------------------------------------------


async def test_create_send_and_follow_in_tmux(env: TmuxEnv) -> None:
    conn = Conn()
    row = await env.create(conn)
    ref = row["ref"]
    assert row["host"] == "tmux" and row["approvals"] == "person" and row["canSend"]
    assert row["tmux"]["session"] == f"sp-{ref['id'].replace('-', '')[:12]}"
    assert row["tmux"]["pane"].startswith("%")
    # The CLI really runs in that pane on snowpea's (here: the test's) server.
    panes = tmux(env.socket, "list-panes", "-a", "-F", "#{session_name} #{pane_id} #{pane_pid}")
    session, pane, pid = panes.split()
    assert (session, pane) == (row["tmux"]["session"], row["tmux"]["pane"])
    status = claude_mod.read_status(int(pid))
    assert status is not None and status["sessionId"] == ref["id"]
    assert status["tmux"].endswith(pane)
    done = await env.turn(conn, ref, "hello")
    assert done["payload"]["reason"] == "complete"
    kinds = conn.kinds()
    assert kinds[:2] == ["turn.started", "message.user"] and "message.done" in kinds
    assert [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.done"] == ["OK"]
    # The prompt itself is not shown twice (snowpea already published it).
    assert [e["kind"] for e in conn.events()].count("message.user") == 1
    # Multi-line and Korean go in as one bracketed paste, exactly.
    text = "첫 줄입니다\n둘째 줄: 고마워요 — tab\there"
    done = await env.turn(conn, ref, text)
    assert done["payload"]["reason"] == "complete"
    prompts = [r["message"]["content"] for r in env.transcript(ref["id"])
               if r.get("type") == "user" and isinstance(r["message"]["content"], str)]
    assert prompts == ["hello", text]
    argv = starts(env.log)[0]["argv"]
    assert "-p" not in argv and argv[argv.index("--session-id") + 1] == ref["id"]
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert starts(env.log)[0]["bypass"] == []
    assert OWNER_EMAIL not in json.dumps(conn.frames)
    await env.manager.close("claude", ref["id"], conn=conn)
    assert tmux(env.socket, "list-sessions") == ""
    assert not proc.pid_alive(int(pid))


async def test_tmux_refuses_mode_switching_prompts(env: TmuxEnv) -> None:
    conn = Conn()
    ref = (await env.create(conn))["ref"]
    for text in ("/clear", "!ls"):
        with pytest.raises(ExternalError) as caught:
            await env.manager.send("claude", ref["id"], text, conn=conn)
        assert caught.value.code == errors.INVALID_PARAMS
    prompts = [entry for entry in fake_log(env.log) if entry["kind"] == "prompt"]
    assert prompts == []
    assert (await env.turn(conn, ref, "fine"))["payload"]["reason"] == "complete"


async def test_never_types_while_an_attached_person_is_typing(env: TmuxEnv) -> None:
    import time

    conn = Conn()
    ref = (await env.create(conn))["ref"]
    channel = env.manager._records[("claude", ref["id"])].channel
    assert isinstance(channel, ClaudeTmuxChannel) and channel.server is not None
    assert channel.session_name is not None
    # Nobody attached: tmux reports no client activity at all.
    assert await channel.server.last_client_activity(channel.session_name) is None
    real = channel.server.last_client_activity

    async def typing_now(_name: str) -> float:
        return time.time()

    channel.server.last_client_activity = typing_now  # type: ignore[method-assign]
    with pytest.raises(ExternalError) as caught:
        await env.manager.send("claude", ref["id"], "not now", conn=conn)
    assert caught.value.code == errors.SESSION_BUSY
    assert [e for e in fake_log(env.log) if e["kind"] == "prompt"] == []
    channel.server.last_client_activity = real  # type: ignore[method-assign]
    assert (await env.turn(conn, ref, "now"))["payload"]["reason"] == "complete"


async def test_approvals_are_answered_by_the_person(env: TmuxEnv) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="approval"):
        row = await env.create(conn)
    ref, pane = row["ref"], row["tmux"]["pane"]
    turn = (await env.manager.send("claude", ref["id"], "write a note", conn=conn))["turnId"]
    note = await until(lambda: [e for e in conn.events() if e["kind"] == "message.done"
                                and "asking for permission to use Write" in
                                e["payload"]["text"]])
    assert f"tmux -L {env.socket} attach -t {row['tmux']['session']}" in note[0]["payload"]["text"]
    # snowpea never answers and never types into the open dialog.
    with pytest.raises(ExternalError) as caught:
        await env.manager.interrupt("claude", ref["id"])
    assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
    assert env.hub.pending() == []
    assert [e for e in fake_log(env.log) if e["kind"] == "key"] == []
    # The person answers in the terminal.
    tmux(env.socket, "send-keys", "-t", pane, "1")
    done = await until(lambda: conn.turn_done(turn))
    assert done["payload"]["reason"] == "complete"
    assert (env.project / "note.txt").read_text() == "hi\n"
    events = conn.events()
    assert any(e["kind"] == "tool.call" and e["payload"]["name"] == "Write" for e in events)
    assert any(e["kind"] == "diff" for e in events)
    assert [e["payload"]["text"] for e in events if e["kind"] == "message.done"][-1] == \
        "Allowed: wrote note.txt"


async def test_interrupt_sends_escape(env: TmuxEnv) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="slow", FAKE_CLI_DELAY="0.3",
                  FAKE_CLI_REPLY="a slow answer that keeps going for quite a while"):
        ref = (await env.create(conn))["ref"]
    turn = (await env.manager.send("claude", ref["id"], "go", conn=conn))["turnId"]
    await until(lambda: any(claude_mod.read_status(pid) and claude_mod.read_status(pid)
                            .get("status") == "busy"
                            for pid in _pane_pids(env.socket)))
    await env.manager.interrupt("claude", ref["id"])
    done = await until(lambda: conn.turn_done(turn))
    assert done["payload"]["reason"] == "interrupted"
    assert [e["data"] for e in fake_log(env.log) if e["kind"] == "key"] == ["Escape"]
    assert any(e["payload"].get("text") == "Interrupted." for e in conn.events())


def _pane_pids(socket: str) -> list[int]:
    return [int(line) for line in tmux(socket, "list-panes", "-a", "-F", "#{pane_pid}").split()]


async def test_the_pane_dying_ends_the_turn(env: TmuxEnv) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="die"):
        ref = (await env.create(conn))["ref"]
    turn = (await env.manager.send("claude", ref["id"], "go", conn=conn))["turnId"]
    done = await until(lambda: conn.turn_done(turn))
    assert done["payload"]["reason"] == "error"
    assert any("pane closed" in e["payload"].get("message", "") for e in conn.events()
               if e["kind"] == "error")
    await until(lambda: env.hub.states()[-1]["host"] == "none")


async def test_trust_dialog_is_shown_to_the_person(env: TmuxEnv) -> None:
    with env_vars(FAKE_CLAUDE_TRUST="ask"):
        env.adapter.ready_timeout = 6.0
        with pytest.raises(ExternalError) as caught:
            await env.create(Conn())
    assert caught.value.code == errors.EXTERNAL_UNAVAILABLE
    assert "trust this folder" in caught.value.message
    assert tmux(env.socket, "list-sessions") == ""


async def test_mode_change_restarts_the_pane_with_resume(env: TmuxEnv) -> None:
    conn = Conn()
    row = await env.create(conn)
    ref = row["ref"]
    await env.turn(conn, ref, "first")
    old = _pane_pids(env.socket)
    await env.manager.set_mode("claude", ref["id"], "readonly", conn=conn)
    await env.turn(conn, ref, "second")
    new = _pane_pids(env.socket)
    assert len(new) == 1 and new != old and not proc.pid_alive(old[0])
    argv = starts(env.log)[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == ref["id"]
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    await env.manager.set_mode("claude", ref["id"], "unrestricted", conn=conn)
    await env.turn(conn, ref, "third")
    assert starts(env.log)[-1]["bypass"] == ["--dangerously-skip-permissions"]


async def test_unrestricted_tmux_needs_a_person(env: TmuxEnv) -> None:
    with pytest.raises(ExternalError) as caught:
        await env.manager.create("claude", str(env.project), transport="tmux",
                                 mode="unrestricted", conn=None)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert tmux(env.socket, "list-sessions") == ""


# -- adopting the person's own pane -----------------------------------------------------


def _person_pane(socket: str, project: Path, session_id: str) -> tuple[str, int]:
    """The person runs claude in their own tmux session (on the test's server)."""
    out = tmux(socket, "new-session", "-d", "-P", "-F", "#{pane_id} #{pane_pid}", "-s", "mine",
               "-x", "120", "-y", "40", "-c", str(project), "--", str(FAKE_CLAUDE),
               "--session-id", session_id)
    pane, pid = out.split()
    return pane, int(pid)


async def test_adopting_a_persons_pane(tmp_path: Path, socket: str) -> None:
    project = (tmp_path / "proj").resolve()
    project.mkdir()
    sid = str(uuid.uuid4())
    log = tmp_path / "fake.log"
    with env_vars(FAKE_CLI_LOG=str(log), FAKE_CLI_SCENARIO=None):
        pane, pid = _person_pane(socket, project, sid)
        await until(lambda: (claude_mod.read_status(pid) or {}).get("status") == "idle")
        assert tmux_socket_of(pid) is not None
        adapter = ClaudeAdapter(tmux_socket="unused-" + uuid.uuid4().hex[:6], poll_interval=0.05)
        live = await adapter.liveness(SessionInfo(id=sid, cwd=str(project)), own_pids=set())
        assert live.host == "tmux" and live.tmux == {"session": "mine", "pane": pane}
        spec = LaunchSpec(cwd=str(project), project=str(project), executable=pinned(),
                          mode="default", session_id=sid, resume=True, transport="tmux")
        # The structured transport refuses a session open in a pane.
        with pytest.raises(ExternalError) as caught:
            await adapter.channel(LaunchSpec(cwd=str(project), project=None,
                                             executable=pinned(), mode="default",
                                             session_id=sid, resume=True),
                                  StubHost(tmp_path / "home", sid)).start()
        assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
        # Not allowed by the person: view only.
        refused = StubHost(tmp_path / "home", sid, answer=ApprovalAnswer(allow=False))
        with pytest.raises(ExternalError) as caught:
            await adapter.channel(spec, refused).start()
        assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
        assert refused.asks[0].tool == "tmux.adopt" and refused.asks[0].risk == "high"
        # Allowed: snowpea types into the same process.
        host = StubHost(tmp_path / "home", sid)
        channel = adapter.channel(spec, host)
        assert isinstance(channel, ClaudeTmuxChannel)
        await channel.start()
        try:
            assert not channel.owned and channel.tmux == {"session": "mine", "pane": pane}
            assert channel.pids() == {pid}
            await channel.send("hello from snowpea", "t1")
            await until(lambda: host.finished)
            assert host.finished == [("complete", None)] and "OK" in host.texts()
            # A mode change cannot restart someone else's process.
            with pytest.raises(ExternalError):
                await channel.set_mode("readonly", None)
        finally:
            await channel.close()
        # Closing snowpea's side leaves the person's pane running.
        assert proc.pid_alive(pid)
        assert tmux(socket, "list-panes", "-a", "-F", "#{pane_id}").split() == [pane]
        assert [e for e in fake_log(log) if e["kind"] == "prompt"][0]["data"] == \
            "hello from snowpea"


async def test_the_channel_never_types_into_an_open_dialog(tmp_path: Path, socket: str) -> None:
    project = tmp_path
    with env_vars(FAKE_CLI_SCENARIO="approval", FAKE_CLI_LOG=str(tmp_path / "log")):
        host = StubHost(tmp_path / "home")
        adapter = ClaudeAdapter(tmux_socket=socket, poll_interval=0.05, ready_timeout=10.0)
        spec = LaunchSpec(cwd=str(project), project=None, executable=pinned(),
                          mode="default", transport="tmux")
        channel = adapter.channel(spec, host)
        await channel.start()
        try:
            assert isinstance(channel, ClaudeTmuxChannel) and channel.owned
            await channel.send("write", "t1")
            await until(lambda: (channel.status() or {}).get("status") == "waiting")
            channel.turn_id = None  # as if the manager let a second prompt through
            with pytest.raises(ExternalError) as caught:
                await channel.send("type this into the dialog", "t2")
            assert caught.value.code == errors.SESSION_BUSY
            channel.turn_id = "t1"
            with pytest.raises(ExternalError) as caught:
                await channel.interrupt()
            assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
            assert channel.pane is not None
            tmux(socket, "send-keys", "-t", channel.pane, "Escape")  # the person declines
            await until(lambda: host.finished)
            assert host.finished == [("complete", None)]
            assert not (project / "note.txt").exists()
        finally:
            await channel.close()
    prompts = [e["data"] for e in fake_log(tmp_path / "log") if e["kind"] == "prompt"]
    assert prompts == ["write"]
    assert claude_home().is_dir()


async def test_adopting_over_external_open_is_the_persons_call(tmp_path: Path,
                                                               socket: str) -> None:
    """external.open {transport: 'tmux'} adopts the person's pane after a person-only ask."""
    project = (tmp_path / "proj").resolve()
    project.mkdir()
    sid = str(uuid.uuid4())
    settings = Settings.model_validate(
        {"jarvis": {"external": {"clis": {"claude": {"path": str(FAKE_CLAUDE)}}}}}
    )
    hub = Hub()
    approvals = ApprovalQueue(settings, hub=hub)  # type: ignore[arg-type]

    async def roots() -> list[str]:
        return [str(project)]

    manager = ExternalManager(
        home=tmp_path / "home", settings=lambda: settings, hub=hub, approvals=approvals,
        registry=AdapterRegistry([ClaudeAdapter(tmux_socket="unused-" + uuid.uuid4().hex[:6],
                                                poll_interval=0.05)]),
        roots=roots, poll_interval=0.1, sweep_interval=3600,
    )
    with env_vars(FAKE_CLI_LOG=str(tmp_path / "fake.log"), FAKE_CLI_SCENARIO=None):
        write_transcript(str(project), [user("earlier", str(project))], session_id=sid)
        pane, pid = _person_pane(socket, project, sid)
        await until(lambda: (claude_mod.read_status(pid) or {}).get("status") == "idle")
        await manager.start()
        try:
            viewer = Conn("tui")
            opened = await manager.open("claude", sid, conn=viewer)
            assert opened["session"]["host"] == "terminal" and opened["readOnly"]
            adopting = asyncio.ensure_future(
                manager.open("claude", sid, conn=viewer, transport="tmux")
            )
            pending = await until(lambda: [p["request"] for m, p in hub.notes
                                           if m == "approval.pending"])
            request = pending[-1]
            assert request["tool"] == "tmux.adopt" and request["personOnly"] is True
            # Neither Jarvis nor the engine may allow it.
            for by in ("jarvis", "jarvis:auto"):
                with pytest.raises(RpcError) as caught:
                    await approvals.respond(request["requestId"], "allow", by=by)
                assert caught.value.code == errors.JARVIS_PERSON_ONLY
            await approvals.respond(request["requestId"], "allow", conn=viewer)
            adopted = (await adopting)["session"]
            assert adopted["host"] == "tmux" and adopted["canSend"]
            assert adopted["approvals"] == "person" and adopted["tmux"]["pane"] == pane
            # Idle: an interrupt types nothing into the person's pane.
            await manager.interrupt("claude", sid)
        finally:
            await manager.shutdown()
        # The person's pane outlives snowpea's side.
        assert proc.pid_alive(pid)
