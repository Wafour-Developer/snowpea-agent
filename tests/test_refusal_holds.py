"""After a person refuses a change, the model does not route around it (feedback report).

Told "Choose a different action", a model whose change was declined made the
same change through another tool.  Now a person's refusal tells it to ask or
stop, and a later change to the same file in the same turn is put to the person
again — even where it would otherwise run silently (accept mode's writes, auto
mode, a cached answer).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
from _support import RpcClient, connect, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage

TIMEOUT = 30.0


class _Script:
    """Makes ``calls`` one per round, then answers; ``on_second`` runs before round 2."""

    vendor = "fake"

    def __init__(self, calls: list[ToolCall], on_second: Any = None) -> None:
        self.calls = calls
        self.on_second = on_second
        self.requests: list[list[ChatMessage]] = []

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.requests.append(list(messages))
        n = len(self.requests)
        if n == 2 and self.on_second is not None:
            self.on_second()
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=10, output_tokens=2))
        if n <= len(self.calls):
            yield StreamEvent(kind="tool_call", tool_call=self.calls[n - 1])
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        yield StreamEvent(kind="text_delta", text="stopped")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _turn(client: RpcClient, session_id: str, text: str) -> None:
    turn = await client.ok("session.prompt", {"sessionId": session_id, "text": text})
    assert await client.wait_turn(turn["turnId"], timeout=TIMEOUT) == "complete"


def _write(call_id: str, path: str, content: str) -> ToolCall:
    return ToolCall(id=call_id, name="write_file", arguments={"path": path, "content": content})


async def test_a_refused_command_does_not_come_back_as_a_silent_write(tmp_path: Path) -> None:
    """accept mode: the declined ``sed -i`` on a.py is not redone with write_file."""
    daemon = await make_daemon(tmp_path / "home")
    core = daemon.core
    assert core is not None
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "a.py").write_text("x = 1\n")
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon, timeout=TIMEOUT, approval_mode="deny")
            try:
                created = await client.ok(
                    "session.create", {"workdir": str(workdir), "mode": "accept"}
                )
                session_id = str(created["sessionId"])
                provider = _Script([
                    ToolCall(id="c1", name="shell", arguments={"command": "sed -i s/1/2/ a.py"}),
                    _write("c2", "a.py", "x = 2\n"),
                    _write("c3", "b.py", "y = 1\n"),
                ])
                core.providers.get = lambda _p, _m: provider
                await _turn(client, session_id, "set x to 2")

                asked = [(r["tool"], r.get("note") or "") for r in client.approval_requests]
                # accept mode writes without asking — but not the file just refused.
                assert [tool for tool, _ in asked] == ["shell", "write_file"]
                assert "declined a change to" in asked[1][1]
                assert (workdir / "a.py").read_text() == "x = 1\n"
                assert (workdir / "b.py").read_text() == "y = 1\n"

                first = client.of_kind("tool.result")[0]["payload"]["error"]
                assert "do not reach the same result another way" in first
                assert "ask the user" in first and "Choose a different action" not in first

                # Same turn only: the next turn writes a.py as accept mode always does.
                provider.calls = [_write("c4", "a.py", "x = 3\n")]
                provider.requests.clear()
                await _turn(client, session_id, "now set it to 3")
                assert len(client.approval_requests) == 2
                assert (workdir / "a.py").read_text() == "x = 3\n"
            finally:
                await client.stop()
    finally:
        await daemon.stop()


async def test_a_refused_write_is_asked_again_after_a_switch_to_auto(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    core = daemon.core
    assert core is not None
    workdir = tmp_path / "w"
    workdir.mkdir()
    outside = tmp_path / "notes.txt"
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon, timeout=TIMEOUT, approval_mode="deny")
            try:
                created = await client.ok(
                    "session.create", {"workdir": str(workdir), "mode": "accept"}
                )
                session = core.sessions.get(str(created["sessionId"]))

                def to_auto() -> None:
                    session.mode = "auto"  # "allow everything" pressed mid-turn

                provider = _Script(
                    [_write("c1", str(outside), "one\n"), _write("c2", str(outside), "two\n")],
                    on_second=to_auto,
                )
                core.providers.get = lambda _p, _m: provider
                await _turn(client, session.id, "note it outside")
                assert [r["tool"] for r in client.approval_requests] == [
                    "write_file", "write_file"]
                assert not outside.exists()
            finally:
                await client.stop()
    finally:
        await daemon.stop()


def test_touches_refused_is_narrow(tmp_path: Path) -> None:
    core = SimpleNamespace()
    (tmp_path / "a.py").write_text("x = 1\n")
    session = SimpleNamespace(workdir=tmp_path, refused_write_paths=None)
    agent_loop._note_refused_write(core, session, "t1", "write", {"path": "a.py"})
    refused = str((tmp_path / "a.py").resolve())

    def hit(name: str, tag: str, args: dict[str, Any], turn: str = "t1") -> str | None:
        return agent_loop._touches_refused(core, session, turn, name, tag, args)

    assert hit("patch", "write", {"path": "a.py"}) == refused
    assert hit("write_file", "write", {"path": str(tmp_path / "a.py")}) == refused
    assert hit("shell", "exec", {"command": "python fix.py a.py"}) == refused
    assert hit("execute_code", "exec", {"code": "open('a.py','w').write('x')"}) == refused
    # Another file, a plain look, another turn: not touched.
    assert hit("write_file", "write", {"path": "b.py"}) is None
    assert hit("shell", "exec", {"command": "cat a.py"}) is None
    assert hit("read_file", "read", {"path": "a.py"}) is None
    assert hit("patch", "write", {"path": "a.py"}, turn="t2") is None
    # A refused look keeps nothing; a refused change keeps the files it names.
    other = SimpleNamespace(workdir=tmp_path, refused_write_paths=None)
    agent_loop._note_refused_write(core, other, "t1", "exec", {"command": "cat a.py"})
    assert other.refused_write_paths is None
    agent_loop._note_refused_write(core, other, "t1", "exec", {"command": "sed -i s/1/2/ a.py"})
    assert other.refused_write_paths == ("t1", {refused})
