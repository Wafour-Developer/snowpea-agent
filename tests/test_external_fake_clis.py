"""M19 WP5: the fake CLIs speak their real protocols closely enough for WP6.

Each fake is driven through the base layer (``ProcessSupervisor``,
``ExternalProcess``) the way an adapter will drive it.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _external_adapter import FAKE_CLIS
from _support import env_vars

from snowpea_core.external import proc
from snowpea_core.external.proc import ExternalProcess, PinnedExecutable, ProcessSupervisor

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc")
fcntl = pytest.importorskip("fcntl")


class Wire:
    """Frames from one fake, with a waiter."""

    def __init__(self, child: ExternalProcess) -> None:
        self.child = child
        self.frames: list[dict[str, Any]] = []
        self.task = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        async for frame in self.child.json_lines():
            self.frames.append(frame)

    async def send(self, frame: dict[str, Any]) -> None:
        await self.child.write_line(frame)

    async def wait(self, predicate: Callable[[dict[str, Any]], bool],
                   timeout: float = 10.0) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for frame in self.frames:
                if predicate(frame):
                    return frame
            await asyncio.sleep(0.02)
        raise AssertionError(f"no matching frame; saw {self.frames[-5:]}")

    async def stop(self) -> None:
        await self.child.terminate(grace=1)
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@pytest_asyncio.fixture
async def spawn(tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    supervisor = ProcessSupervisor(tmp_path / "home", max_processes=8)
    wires: list[Wire] = []

    async def start(name: str, *args: str, cwd: Path | None = None) -> Wire:
        argv = proc.build_argv(PinnedExecutable(str(FAKE_CLIS / name)), list(args))
        child = await supervisor.spawn(argv, cwd=str(cwd or tmp_path), env=proc.scrubbed_env(),
                                       cli=name)
        wire = Wire(child)
        wires.append(wire)
        return wire

    yield start
    for wire in wires:
        await wire.stop()
    await supervisor.shutdown(grace=1)


# -- claude --------------------------------------------------------------------


async def test_claude_interrupt_mid_stream(spawn: Any) -> None:
    with env_vars(FAKE_CLI_SCENARIO="slow", FAKE_CLI_REPLY="a long answer that streams slowly",
                  FAKE_CLI_DELAY="0.05"):
        wire = await spawn("fake_claude.py", "-p", "--input-format", "stream-json",
                           "--output-format", "stream-json", "--include-partial-messages",
                           "--session-id", "11111111-2222-3333-4444-555555555555")
    await wire.send({"type": "user", "message": {"role": "user", "content": "go"}})
    await wire.wait(lambda f: f.get("type") == "stream_event")
    await wire.send({"type": "control_request", "request_id": "int_1",
                     "request": {"subtype": "interrupt"}})
    ack = await wire.wait(lambda f: f.get("type") == "control_response")
    assert ack["response"]["request_id"] == "int_1"
    result = await wire.wait(lambda f: f.get("type") == "result")
    assert result["terminal_reason"] == "interrupted"


async def test_claude_without_host_prompts_denies(spawn: Any) -> None:
    with env_vars(FAKE_CLI_SCENARIO="approval"):
        wire = await spawn("fake_claude.py", "-p", "--input-format", "stream-json",
                           "--output-format", "stream-json", "--permission-prompts", "host",
                           "--session-id", "11111111-2222-3333-4444-555555555556")
    await wire.send({"type": "user", "message": {"role": "user", "content": "write"}})
    result = await wire.wait(lambda f: f.get("type") == "result")
    assert result["permission_denials"][0]["tool_name"] == "Write"
    assert not any(f.get("type") == "control_request" for f in wire.frames)


# -- codex -----------------------------------------------------------------------


async def _codex(spawn: Any, project: Path) -> tuple[Any, str]:
    wire = await spawn("fake_codex.py", "app-server", cwd=project)
    await wire.send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "t"}}})
    await wire.wait(lambda f: f.get("id") == 1 and "result" in f)
    await wire.send({"method": "initialized"})
    await wire.send({"id": 2, "method": "thread/start",
                     "params": {"cwd": str(project), "approvalPolicy": "untrusted",
                                "sandbox": "read-only"}})
    started = await wire.wait(lambda f: f.get("id") == 2 and "result" in f)
    return wire, started["result"]["thread"]["id"]


async def test_codex_turn_approval_and_writer_lock(spawn: Any, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    with env_vars(FAKE_CLI_SCENARIO="approval"):
        wire, thread = await _codex(spawn, project)
    await wire.send({"id": 3, "method": "turn/start", "params": {
        "threadId": thread, "input": [{"type": "text", "text": "write", "text_elements": []}]}})
    ask = await wire.wait(lambda f: f.get("method") == "item/commandExecution/requestApproval")
    assert ask["id"] == 0 and ask["params"]["threadId"] == thread  # server ids start at 0
    await wire.send({"id": ask["id"], "result": {"decision": "accept"}})
    done = await wire.wait(lambda f: f.get("method") == "turn/completed")
    assert done["params"]["turn"]["status"] == "completed"
    assert (project / "note.txt").read_text() == "hi"
    # A second app-server cannot load the same thread while this one holds it.
    other = await spawn("fake_codex.py", "app-server", cwd=project)
    await other.send({"id": 1, "method": "thread/resume", "params": {"threadId": thread}})
    refused = await other.wait(lambda f: f.get("id") == 1)
    assert refused["error"]["code"] == -32600 and "active writer" in refused["error"]["message"]
    listed_wire = other
    await listed_wire.send({"id": 2, "method": "thread/list", "params": {"cwd": str(project)}})
    listed = await listed_wire.wait(lambda f: f.get("id") == 2)
    assert [t["id"] for t in listed["result"]["data"]] == [thread]
    # Released on unsubscribe: now the other process may resume it.
    await wire.send({"id": 4, "method": "thread/unsubscribe", "params": {"threadId": thread}})
    await wire.wait(lambda f: f.get("id") == 4)
    await other.send({"id": 3, "method": "thread/resume", "params": {"threadId": thread}})
    resumed = await other.wait(lambda f: f.get("id") == 3)
    assert resumed["result"]["thread"]["turns"][0]["status"] == "completed"


async def test_codex_interrupt_steer_and_unknown(spawn: Any, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    with env_vars(FAKE_CLI_SCENARIO="slow", FAKE_CLI_DELAY="0.05",
                  FAKE_CLI_REPLY="a long answer that streams slowly enough"):
        wire, thread = await _codex(spawn, project)
    await wire.send({"id": 3, "method": "turn/start", "params": {
        "threadId": thread, "input": [{"type": "text", "text": "go", "text_elements": []}]}})
    turn = (await wire.wait(lambda f: f.get("id") == 3))["result"]["turn"]["id"]
    await wire.send({"id": 4, "method": "turn/steer", "params": {
        "threadId": thread, "expectedTurnId": "wrong", "input": []}})
    assert "expected active turn id" in (await wire.wait(lambda f: f.get("id") == 4))[
        "error"]["message"]
    await wire.send({"id": 5, "method": "turn/interrupt",
                     "params": {"threadId": thread, "turnId": turn}})
    done = await wire.wait(lambda f: f.get("method") == "turn/completed")
    assert done["params"]["turn"]["status"] == "interrupted"
    await wire.send({"id": 6, "method": "turn/interrupt", "params": {"threadId": thread}})
    assert "no active turn" in (await wire.wait(lambda f: f.get("id") == 6))["error"]["message"]
    await wire.send({"id": 7, "method": "nope/nothing", "params": {}})
    assert (await wire.wait(lambda f: f.get("id") == 7))["error"]["code"] == -32601


async def test_codex_not_logged_in(spawn: Any, tmp_path: Path) -> None:
    with env_vars(FAKE_CLI_SCENARIO="noauth"):
        wire, thread = await _codex(spawn, tmp_path)
    await wire.send({"id": 3, "method": "account/read", "params": {}})
    assert (await wire.wait(lambda f: f.get("id") == 3))["result"]["account"] is None
    await wire.send({"id": 4, "method": "turn/start", "params": {
        "threadId": thread, "input": [{"type": "text", "text": "hi"}]}})
    error = await wire.wait(lambda f: f.get("method") == "error")
    assert error["params"]["error"]["codexErrorInfo"]["responseStreamDisconnected"][
        "httpStatusCode"] == 401


# -- agy ------------------------------------------------------------------------------


async def _agy(spawn: Any, project: Path) -> tuple[Any, str]:
    wire = await spawn("fake_agy.py", "acp", cwd=project)
    await wire.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": 1}})
    await wire.wait(lambda f: f.get("id") == 1)
    await wire.send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
                     "params": {"cwd": str(project), "mcpServers": []}})
    new = await wire.wait(lambda f: f.get("id") == 2)
    return wire, new["result"]["sessionId"]


@pytest.mark.parametrize("option", ["allow", "deny"])
async def test_agy_permission_and_load(spawn: Any, tmp_path: Path, option: str) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    state = tmp_path / "state"
    with env_vars(FAKE_CLI_SCENARIO="approval", FAKE_CLI_STATE_DIR=str(state)):
        wire, session = await _agy(spawn, project)
        await wire.send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {
            "sessionId": session, "prompt": [{"type": "text", "text": "run it"}]}})
        ask = await wire.wait(lambda f: f.get("method") == "session/request_permission")
        assert ask["id"] == "1"  # a string id, echoed back verbatim
        assert [o["kind"] for o in ask["params"]["options"]] == ["allow_once", "reject_once"]
        await wire.send({"jsonrpc": "2.0", "id": ask["id"], "result": {
            "outcome": {"outcome": "selected", "optionId": option}}})
        done = await wire.wait(lambda f: f.get("id") == 3)
        assert done["result"]["stopReason"] == "end_turn"
        update = await wire.wait(lambda f: (f.get("params") or {}).get("update", {}).get(
            "sessionUpdate") == "tool_call_update")
        assert update["params"]["update"]["status"] == ("completed" if option == "allow"
                                                        else "failed")
        texts = [f for f in wire.frames if (f.get("params") or {}).get("update", {}).get(
            "sessionUpdate") == "agent_message_chunk"]
        assert bool(texts) == (option == "allow")  # a deny ends with no assistant text
        # Another process loads it: history replays before the result.
        other = await spawn("fake_agy.py", "acp", cwd=project)
        await other.send({"jsonrpc": "2.0", "id": 1, "method": "session/load",
                          "params": {"sessionId": session, "cwd": str(project)}})
        await other.wait(lambda f: f.get("id") == 1)
        result_index = next(i for i, f in enumerate(other.frames) if f.get("id") == 1)
        replayed = [f["params"]["update"]["sessionUpdate"] for f in other.frames[:result_index]]
        assert replayed[0] == "user_message_chunk" and "tool_call" in replayed
        await other.send({"jsonrpc": "2.0", "id": 2, "method": "session/list", "params": {}})
        listed = await other.wait(lambda f: f.get("id") == 2)
        assert listed["result"]["sessions"][0]["sessionId"] == session


async def test_agy_cancel_and_auto_policy(spawn: Any, tmp_path: Path) -> None:
    with env_vars(FAKE_CLI_SCENARIO="slow", FAKE_CLI_DELAY="0.05",
                  FAKE_CLI_REPLY="a long reply that keeps streaming for a while"):
        wire, session = await _agy(spawn, tmp_path)
    await wire.send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {
        "sessionId": session, "prompt": [{"type": "text", "text": "go"}]}})
    await wire.wait(lambda f: (f.get("params") or {}).get("update", {}).get(
        "sessionUpdate") == "agent_message_chunk")
    await wire.send({"jsonrpc": "2.0", "method": "session/cancel",
                     "params": {"sessionId": session}})
    assert (await wire.wait(lambda f: f.get("id") == 3))["result"]["stopReason"] == "cancelled"
    with env_vars(FAKE_CLI_SCENARIO="approval", FAKE_AGY_POLICY="auto"):
        auto, auto_session = await _agy(spawn, tmp_path)
    await auto.send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {
        "sessionId": auto_session, "prompt": [{"type": "text", "text": "run it"}]}})
    await auto.wait(lambda f: f.get("id") == 3)
    assert not any(f.get("method") == "session/request_permission" for f in auto.frames)
    assert (tmp_path / "shell_allow.txt").exists()


def test_agy_terminal_holds_the_presence_lock(tmp_path: Path) -> None:
    state = tmp_path / "state"
    terminal = subprocess.Popen(
        [str(FAKE_CLIS / "fake_agy.py"), "--fake-terminal", "--conversation", "conv-1"],
        stdin=subprocess.PIPE, env={**proc.scrubbed_env(), "FAKE_CLI_STATE_DIR": str(state)},
    )
    lock = state / "presence" / "conv-1.lock"

    def held() -> bool:
        if not lock.exists():
            return False
        # Read /proc/locks for the inode, as the adapter must: never take the lock.
        return f":{lock.stat().st_ino} " in Path("/proc/locks").read_text()

    try:
        deadline = time.monotonic() + 10
        while not held() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert held()
        assert terminal.stdin is not None
        terminal.stdin.close()
        terminal.wait(timeout=10)
        assert not held()
        with open(lock, "a+") as handle:  # free again
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if terminal.poll() is None:
            terminal.kill()
            terminal.wait()


# -- cursor -------------------------------------------------------------------------------


async def _cursor(tmp_path: Path, *args: str, scenario: str = "normal",
                  prompt: str = "hello") -> tuple[int, list[dict[str, Any]]]:
    supervisor = ProcessSupervisor(tmp_path / "home")
    argv = proc.build_argv(PinnedExecutable(str(FAKE_CLIS / "fake_cursor.py")),
                           ["-p", "--output-format", "stream-json", "--stream-partial-output",
                            "--trust", "--sandbox", "enabled", "--workspace", str(tmp_path),
                            *args], prompt_arg=prompt)
    with env_vars(FAKE_CLI_SCENARIO=scenario, FAKE_CLI_STATE_DIR=str(tmp_path / "state")):
        child = await supervisor.spawn(argv, cwd=str(tmp_path), env=proc.scrubbed_env(),
                                       cli="cursor", stdin=False)
    frames = [frame async for frame in child.json_lines()]
    code = await child.wait()
    return code, frames


async def test_cursor_one_shot_modes(tmp_path: Path) -> None:
    code, frames = await _cursor(tmp_path, prompt="--yolo is just text here")
    assert code == 0 and frames[0]["subtype"] == "init"
    assert frames[1]["message"]["content"][0]["text"] == "--yolo is just text here"
    partial = [f for f in frames if f["type"] == "assistant" and "timestamp_ms" in f]
    final = [f for f in frames if f["type"] == "assistant" and "timestamp_ms" not in f]
    assert partial and len(final) == 1 and frames[-1]["type"] == "result"
    chat = frames[0]["session_id"]
    assert list((tmp_path / "state" / ".cursor" / "chats").glob(f"*/{chat}/meta.json"))
    # ask mode: the edit is refused; outside it, it simply runs (no permission mechanism).
    _code, frames = await _cursor(tmp_path, "--mode", "ask", scenario="approval")
    done = next(f for f in frames if f.get("subtype") == "completed" and f["type"] == "tool_call")
    assert "permissionDenied" in done["tool_call"]["editToolCall"]["result"]
    assert "\n" in done["call_id"]
    assert not (tmp_path / "note.txt").exists()
    _code, frames = await _cursor(tmp_path, "--resume", chat, scenario="approval")
    assert (tmp_path / "note.txt").read_text() == "hi"
    code, frames = await _cursor(tmp_path, scenario="die")
    assert code == 3 and frames[-1]["type"] != "result"


async def test_cursor_create_chat(tmp_path: Path) -> None:
    out = await asyncio.create_subprocess_exec(
        str(FAKE_CLIS / "fake_cursor.py"), "create-chat", stdout=asyncio.subprocess.PIPE,
        env={**proc.scrubbed_env(), "FAKE_CLI_STATE_DIR": str(tmp_path)}, cwd=str(tmp_path))
    stdout, _ = await out.communicate()
    chat = stdout.decode().strip()
    assert proc.validate_session_id(chat) == chat
    assert json.loads(next((tmp_path / ".cursor" / "chats").glob(f"*/{chat}/meta.json"))
                      .read_text())["cwd"] == str(tmp_path)
