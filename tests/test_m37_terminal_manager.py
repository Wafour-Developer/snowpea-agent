"""M37 §2.1–§2.5 end to end: TerminalManager driving real holders, PTYs and programs.

POSIX only (a real PTY); every terminal a test starts is closed (kill sequence)
by the fixture, and any holder still running is ended by the pid it reported.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")

from snowpea_core.terminal.link import HolderError, HolderLink  # noqa: E402
from snowpea_core.terminal.manager import (  # noqa: E402
    TERMINAL_LIMIT,
    TerminalError,
    TerminalManager,
    TerminalSpec,
)
from snowpea_core.terminal.status import HookReport  # noqa: E402

FAKE_CLI = r'''
import base64, sys, time
def report(state, **extra):
    fields = ["state=" + state] + [k + "=" + v for k, v in extra.items()]
    sys.stdout.write("\x1b]7501;" + ":".join(fields) + "\x1b\\")
    sys.stdout.flush()
report("idle", app="fake")
sys.stdout.write("fake> ")
sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    report("working")
    time.sleep(0.3)
    if line == "ask":
        report("blocked", kind="question")
        continue
    sys.stdout.write("did:" + line + "\r\nfake> ")
    sys.stdout.flush()
    report("done")
'''

QUERY_PROBE = r'''
import os, select, sys, termios, tty
tty.setraw(0)
os.write(1, b"\x1b[c")
ready, _, _ = select.select([0], [], [], 2.0)
reply = os.read(0, 64) if ready else b""
termios.tcsetattr(0, termios.TCSANOW, termios.tcgetattr(0))
os.write(1, b"\r\nREPLY:" + reply.replace(b"\x1b", b"^") + b":END\r\n")
import time; time.sleep(30)
'''


class Conn:
    """A viewer connection that records what it is sent."""

    def __init__(self) -> None:
        self.closed = False
        self.data = bytearray()
        self.notes: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notes.append((method, params))
        if method == "terminal.data":
            import base64

            self.data += base64.b64decode(params["data"])


def _env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}


def _spec(tmp_path: Path, argv: list[str], **kwargs: Any) -> TerminalSpec:
    return TerminalSpec(argv=argv, env=_env(), cwd=str(tmp_path), **kwargs)


def _make(home: Path, events: list, **kwargs: Any) -> TerminalManager:
    def emit(method: str, params: dict[str, Any]) -> None:
        events.append((method, params))

    async def send(conn: Conn, method: str, params: dict[str, Any]) -> None:
        await conn.notify(method, params)

    return TerminalManager(home, emit=emit, send=send, **kwargs)


@pytest.fixture
async def env(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    events: list = []
    managers: list[TerminalManager] = []
    holders: set[int] = set()

    def new(**kwargs: Any) -> TerminalManager:
        manager = _make(home, events, **kwargs)
        managers.append(manager)
        return manager

    box = {"new": new, "home": home, "events": events, "holders": holders, "tmp": tmp_path}
    try:
        yield box
    finally:
        for manager in managers:
            for term in manager.terminals():
                if term.holder_pid:
                    holders.add(term.holder_pid)
            await manager.close_all()
            await manager.shutdown()
        for pid in holders:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            os.kill(pid, signal.SIGKILL)


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_create_attach_write_and_read(env) -> None:
    manager = env["new"]()
    await manager.start()
    info = await manager.create(_spec(env["tmp"], ["sh", "-c", "echo hello-from-pty; cat"]))
    tid = info["terminalId"]
    assert info["alive"] and info["pid"] and info["profile"] == "shell"
    conn = Conn()
    await _until(lambda: "hello-from-pty" in manager.read(tid)["text"])
    attached = await manager.attach(tid, conn, "interactive", 100, 30)
    assert (attached["cols"], attached["rows"]) == (100, 30)
    import base64

    assert b"hello-from-pty" in base64.b64decode(attached["repaint"])
    await manager.write(tid, conn, b"typed line\r")
    await _until(lambda: b"typed line" in conn.data)
    assert all(m == "terminal.data" for m, _ in conn.notes)
    assert ("terminal.created", {"terminal": info}) in env["events"]
    manager.detach_all(conn)
    assert manager.get(tid).screen.cols == 160  # headless again


async def test_exit_code_arrives_after_the_last_output(env) -> None:
    manager = env["new"]()
    await manager.start()
    conn = Conn()
    info = await manager.create(
        _spec(env["tmp"], ["sh", "-c", "sleep 0.3; printf 'last words'; exit 7"])
    )
    tid = info["terminalId"]
    await manager.attach(tid, conn, "observe")
    result = await manager.wait(tid, timeout=5)
    assert result["reason"] == "exit"
    term = manager.get(tid)
    assert term.exit_code == 7 and not term.alive
    assert "last words" in manager.read(tid)["text"]
    assert b"last words" in conn.data
    assert ("terminal.exit", {"terminalId": tid, "exitCode": 7}) in env["events"]


async def test_holder_survives_a_daemon_restart(env) -> None:
    first = env["new"]()
    await first.start()
    info = await first.create(
        _spec(env["tmp"], ["sh", "-c", "echo before-restart; read x; echo after:$x; sleep 30"])
    )
    tid = info["terminalId"]
    await _until(lambda: "before-restart" in first.read(tid)["text"])
    holder = first.get(tid).holder_pid
    env["holders"].add(holder)
    await first.shutdown()
    os.kill(holder, 0)  # still running without a daemon

    second = env["new"]()
    await second.start()
    rows = second.list()
    assert [r["terminalId"] for r in rows] == [tid] and rows[0]["alive"]
    assert "before-restart" in second.read(tid)["text"]
    conn = Conn()
    await second.attach(tid, conn, "interactive", 80, 24)
    await second.write(tid, conn, b"zz\r")
    await _until(lambda: "after:zz" in second.read(tid)["text"])


async def test_lost_entry_when_the_holder_died_without_an_exit(env) -> None:
    first = env["new"]()
    await first.start()
    info = await first.create(_spec(env["tmp"], ["sleep", "30"]))
    tid = info["terminalId"]
    holder = first.get(tid).holder_pid
    await first.shutdown()
    os.kill(holder, signal.SIGKILL)
    await _until(lambda: not _alive(holder))
    second = env["new"]()
    await second.start()
    row = second.list()[0]
    assert row["terminalId"] == tid and not row["alive"]
    assert row["labels"].get("snowpea.lost") == "1"
    await second.close(tid)
    assert not list((env["home"] / "terminals").glob(f"{tid}*"))


def _alive(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_close_runs_the_kill_sequence(env) -> None:
    manager = env["new"]()
    await manager.start()
    stubborn = await manager.create(_spec(
        env["tmp"],
        ["sh", "-c", "trap '' HUP TERM; echo stubborn; while :; do sleep 0.05; done"],
    ))
    tid = stubborn["terminalId"]
    await _until(lambda: "stubborn" in manager.read(tid)["text"])
    holder = manager.get(tid).holder_pid
    started = time.monotonic()
    result = await manager.close(tid)
    took = time.monotonic() - started
    assert result["exitCode"] == 128 + signal.SIGKILL
    assert 0.9 <= took < 4.0  # HUP, 500 ms, TERM, 500 ms, KILL
    assert manager.list() == []
    await _until(lambda: not _alive(holder))
    assert ("terminal.closed", {"terminalId": tid}) in env["events"]

    easy = await manager.create(_spec(env["tmp"], ["sleep", "30"]))
    started = time.monotonic()
    result = await manager.close(easy["terminalId"])
    assert time.monotonic() - started < 0.9  # HUP was enough
    assert result["exitCode"] == 128 + signal.SIGHUP


async def test_wrong_token_is_refused(env) -> None:
    manager = env["new"]()
    await manager.start()
    info = await manager.create(_spec(env["tmp"], ["sleep", "30"]))
    term = manager.get(info["terminalId"])
    link = HolderLink(term.socket, "00" * 32, on_data=lambda d, t: None, on_exit=lambda c: None)
    with pytest.raises(HolderError):
        await link.connect()
    socket = Path(term.socket)
    assert oct(socket.stat().st_mode & 0o777) == "0o600"
    assert oct(socket.parent.stat().st_mode & 0o777) == "0o700"
    meta = (env["home"] / "terminals" / f"{info['terminalId']}.json")
    assert oct(meta.stat().st_mode & 0o777) == "0o600"
    assert "PATH" not in meta.read_text()  # the environment is never written down


async def test_core_answers_queries_only_without_a_human_viewer(env) -> None:
    manager = env["new"]()
    await manager.start()
    probe = env["tmp"] / "probe.py"
    probe.write_text(QUERY_PROBE)
    alone = await manager.create(_spec(env["tmp"], [sys.executable, str(probe)]))
    await _until(lambda: ":END" in manager.read(alone["terminalId"])["text"])
    assert "REPLY:^[?1;2c:END" in manager.read(alone["terminalId"])["text"]


async def test_fake_cli_status_send_wait_and_seen(env) -> None:
    manager = env["new"]()
    await manager.start()
    cli = env["tmp"] / "fake_cli.py"
    cli.write_text(FAKE_CLI)
    info = await manager.create(_spec(env["tmp"], [sys.executable, str(cli)], profile="claude"))
    tid = info["terminalId"]
    await _until(lambda: "fake>" in manager.read(tid)["text"])
    term = manager.get(tid)
    term.gate.launched_at -= 60  # skip the start-up wait
    sent = await manager.send(tid, "build it")
    assert sent == {"accepted": True, "sent": True}
    result = await manager.wait(tid, timeout=5)
    assert result["reason"] == "left_working"
    assert result["status"]["state"] == "done" and result["status"]["source"] == "osc"
    assert result["status"]["turn"] == 1 and not result["status"]["seen"]
    assert "did:build it" in manager.read(tid)["text"]
    statuses = [p["status"]["state"] for m, p in env["events"] if m == "terminal.status"]
    assert "working" in statuses and statuses[-1] == "done"
    assert manager.seen(tid)["seen"]

    await manager.send(tid, "ask")
    await _until(lambda: term.status.state == "blocked")
    refused = await manager.send(tid, "more")
    assert refused == {"accepted": False, "sent": False, "reason": "blocked"}
    assert (await manager.wait(tid, timeout=5))["reason"] == "blocked"
    await manager.keys(tid, ["x", "Enter"])  # the person (or Jarvis) answers
    await _until(lambda: term.status.state == "done")
    match = asyncio.ensure_future(manager.wait(tid, timeout=5, output=r"did:\w+"))
    await asyncio.sleep(0.1)
    await manager.keys(tid, ["n", "e", "x", "t", "Enter"])
    done = await match
    assert done["reason"] == "match" and done["text"] == "did:next", (done, manager.read(tid))


async def test_hook_report_needs_the_terminal_token(env) -> None:
    manager = env["new"]()
    await manager.start()
    info = await manager.create(_spec(env["tmp"], ["sh", "-c", "echo $SNOWPEA_TERMINAL_ID; cat"]))
    tid = info["terminalId"]
    await _until(lambda: tid in manager.read(tid)["text"])
    term = manager.get(tid)
    with pytest.raises(TerminalError):
        manager.report(tid, "wrong", HookReport("Stop", state="done"))
    status = manager.report(tid, term.report_token,
                            HookReport("SessionStart", session_id="sess-1",
                                       transcript_path="/tmp/t.jsonl"))
    assert status["state"] == "idle"
    done = manager.report(tid, term.report_token, HookReport("Stop", state="done"))
    assert done["source"] == "hook"
    assert term.session_id == "sess-1"


async def test_terminal_cap(env) -> None:
    manager = env["new"](max_terminals=1)
    await manager.start()
    await manager.create(_spec(env["tmp"], ["sleep", "30"]))
    with pytest.raises(TerminalError) as refused:
        await manager.create(_spec(env["tmp"], ["sleep", "30"]))
    assert refused.value.code == TERMINAL_LIMIT


async def test_observer_cannot_type_or_resize(env) -> None:
    manager = env["new"]()
    await manager.start()
    info = await manager.create(_spec(env["tmp"], ["sleep", "30"]))
    conn = Conn()
    await manager.attach(info["terminalId"], conn, "observe", 50, 20)
    assert manager.get(info["terminalId"]).screen.cols == 160
    with pytest.raises(TerminalError):
        await manager.write(info["terminalId"], conn, b"x")
    with pytest.raises(TerminalError):
        await manager.resize(info["terminalId"], conn, 50, 20)
