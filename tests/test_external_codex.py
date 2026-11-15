"""M19 WP6b: the Codex adapter against ``fake_codex.py`` (one shared app-server).

Every test drives the real :class:`CodexAdapter` through the
:class:`ExternalManager`, with the fake's path as the configured codex path.
The fake app-server is started by discovery and shared by every thread.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _external_adapter import FAKE_CLIS
from _support import env_vars

from snowpea_core.config.paths import resolve_home
from snowpea_core.config.settings import Settings
from snowpea_core.external import codex, proc
from snowpea_core.external.base import ApprovalAnswer
from snowpea_core.external.codex import CodexAdapter
from snowpea_core.external.manager import ExternalManager
from snowpea_core.external.proc import ExternalError
from snowpea_core.external.registry import AdapterRegistry
from snowpea_core.external.timeline import TimelineBuilder
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.server import errors

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="POSIX fake CLIs, /proc/locks")
fcntl = pytest.importorskip("fcntl")

FAKE_CODEX = FAKE_CLIS / "fake_codex.py"
RECORDINGS = Path(__file__).parent / "fixtures" / "external_recordings" / "codex"


class Conn:
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


async def until(predicate: Callable[[], Any], timeout: float = 15.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def _settings(path: Path = FAKE_CODEX, **cli: Any) -> Settings:
    return Settings.model_validate(
        {"jarvis": {"external": {"clis": {"codex": {"path": str(path), **cli}}}}}
    )


class Env:
    def __init__(self, tmp_path: Path) -> None:
        # The daemon's home: the shared server's pid file lives here too.
        self.home = resolve_home()
        self.project = (tmp_path / "proj").resolve()
        self.project.mkdir(parents=True)
        self.outside = (tmp_path / "elsewhere").resolve()
        self.outside.mkdir()
        self.codex_home = Path(os.environ["CODEX_HOME"])
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self.settings = _settings()
        self.hub = Hub()
        self.approvals = ApprovalQueue(self.settings, hub=self.hub)  # type: ignore[arg-type]
        self.log = tmp_path / "fake.log"
        self.scenario = tmp_path / "scenario.json"
        self.adapter = CodexAdapter()
        self.adapter.interrupt_grace_sec = 0.5
        self.adapter.backoff_sec = 0.1
        self.manager = ExternalManager(
            home=self.home,
            settings=lambda: self.settings,
            hub=self.hub,
            approvals=self.approvals,
            registry=AdapterRegistry([self.adapter]),
            roots=self._roots,
            poll_interval=0.1,
            sweep_interval=3600,
        )

    async def _roots(self) -> list[str]:
        return [str(self.project)]

    def records(self, kind: str) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        out = []
        for line in self.log.read_text().splitlines():
            record = json.loads(line)
            if record["kind"] == kind:
                out.append(record)
        return out

    def sent(self, method: str) -> list[dict[str, Any]]:
        """Frames the fake received with ``method`` (requests and notifications)."""
        return [r["data"] for r in self.records("in") if r["data"].get("method") == method]

    def write_scenario(self, **values: Any) -> None:
        self.scenario.write_text(json.dumps(values))

    def seed(self, threads: list[dict[str, Any]]) -> None:
        """Threads the fake app-server loads at start (``fake-threads.json``)."""
        state = {thread["id"]: thread for thread in threads}
        (self.codex_home / "fake-threads.json").write_text(json.dumps(state))

    def server_pid(self) -> int:
        pid = self.adapter.server.pid
        assert pid is not None
        return pid


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    setup = Env(tmp_path)
    with env_vars(FAKE_CLI_LOG=str(setup.log), FAKE_CLI_SCENARIO=None,
                  FAKE_CLI_SCENARIO_FILE=str(setup.scenario), FAKE_CLI_REPLY=None,
                  FAKE_CLI_DELAY=None):
        setup.write_scenario(scenario="normal")
        await setup.manager.start()
        try:
            yield setup
        finally:
            await setup.manager.shutdown()
            await setup.adapter.stop_server()


def _thread(cwd: Path, *, updated: int, turns: list[dict[str, Any]] | None = None,
            name: str | None = None, preview: str = "hello") -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "name": name, "preview": preview, "cwd": str(cwd),
            "createdAt": updated - 10, "updatedAt": updated, "source": "cli",
            "model": "gpt-fake", "cliVersion": "0.167.1", "approvalPolicy": "untrusted",
            "sandbox": "workspace-write", "turns": turns or []}


async def _turn(env: Env, conn: Conn, ref: dict[str, str], text: str = "hello") -> str:
    turn = (await env.manager.send(ref["cli"], ref["id"], text, conn=conn))["turnId"]
    await until(lambda: any(e["kind"] == "turn.done" and e["turnId"] == turn
                            for e in conn.events()))
    return turn


def _done(conn: Conn, turn: str) -> dict[str, Any]:
    return next(e for e in conn.events() if e["kind"] == "turn.done" and e["turnId"] == turn)


async def _create(env: Env, conn: Conn, **kwargs: Any) -> dict[str, str]:
    created = await env.manager.create("codex", str(env.project), conn=conn, **kwargs)
    return created["session"]["ref"]


# -- discovery -----------------------------------------------------------------------


async def test_discovery_version_login_and_capabilities(env: Env) -> None:
    rows = {row["id"]: row for row in await env.manager.clis()}
    row = rows["codex"]
    assert row["installed"] and row["version"].startswith("codex-cli 0.167.1")
    assert row["problems"] == []
    assert row["capabilities"] == {"streaming": True, "approvals": "bridged", "liveView": True,
                                   "steer": True, "tmux": False,
                                   "modes": ["default", "readonly", "unrestricted"]}
    assert row["defaultMode"] == "default"
    # Discovery started the one shared app-server and asked account/read.
    starts = env.records("start")
    assert len(starts) == 1 and starts[0]["data"]["argv"] == ["app-server"]
    assert not [n for n in starts[0]["data"]["env"] if n.startswith("SNOWPEA_")]
    initialize = env.sent("initialize")[0]
    assert "jsonrpc" not in initialize and initialize["params"]["clientInfo"]["name"] == "snowpea"
    assert env.sent("initialized") and env.sent("account/read")
    assert env.adapter.approvals("readonly", None) == "none"
    assert env.adapter.approvals("default", None) == "bridged"


async def test_not_logged_in_is_a_problem(env: Env) -> None:
    env.write_scenario(scenario="noauth")
    row = {r["id"]: r for r in await env.manager.clis()}["codex"]
    assert row["installed"]
    assert any("not logged in" in problem for problem in row["problems"])
    with pytest.raises(ExternalError) as caught:
        await env.manager.list_sessions(cwd=str(env.project))
    assert caught.value.code == errors.EXTERNAL_UNAVAILABLE
    assert any("codex login" in problem for problem in caught.value.details["problems"])


async def test_too_old_version_is_a_problem(env: Env) -> None:
    env.write_scenario(scenario="normal", version="codex-cli 0.98.0")
    row = {r["id"]: r for r in await env.manager.clis()}["codex"]
    assert any("too old" in problem and "0.167.0" in problem for problem in row["problems"])
    assert env.records("start") == []  # no app-server was started


async def test_app_server_that_cannot_start_is_unavailable(env: Env, tmp_path: Path) -> None:
    broken = tmp_path / "codex-broken"
    broken.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'codex-cli 0.167.1'; "
                      "exit 0; fi\necho 'error: unknown subcommand' >&2\nexit 2\n")
    broken.chmod(broken.stat().st_mode | stat.S_IXUSR)
    env.settings = _settings(broken)
    row = {r["id"]: r for r in await env.manager.clis()}["codex"]
    assert row["installed"]
    assert any("app-server did not start" in p and "unknown subcommand" in p
               for p in row["problems"])
    with pytest.raises(ExternalError) as caught:
        await env.manager.create("codex", str(env.project), conn=Conn())
    assert caught.value.code == errors.EXTERNAL_UNAVAILABLE


async def test_list_models(env: Env) -> None:
    found = await env.manager.discover("codex")
    assert await env.adapter.list_models(found) == ["gpt-fake", "gpt-fake-mini"]


# -- list and read ---------------------------------------------------------------------


async def test_list_by_working_directory_newest_first_and_paging(env: Env) -> None:
    sub = env.project / "pkg"
    sub.mkdir()
    threads = [
        _thread(env.project, updated=1_794_700_000, name="oldest"),
        _thread(env.project, updated=1_794_700_100, preview="second prompt\nmore"),
        _thread(sub, updated=1_794_700_200, name="in a sub-folder"),
        _thread(env.project, updated=1_794_700_300, name="newest"),
        _thread(env.outside, updated=1_794_700_400, name="elsewhere"),
    ]
    env.seed(threads)
    first = await env.manager.list_sessions(cwd=str(env.project), limit=2)
    assert [row["title"] for row in first["sessions"]] == ["newest", "in a sub-folder"]
    assert first["nextBefore"]
    rest = await env.manager.list_sessions(cwd=str(env.project), limit=2,
                                           before=first["nextBefore"])
    assert [row["title"] for row in rest["sessions"]] == ["second prompt", "oldest"]
    assert rest["nextBefore"] is None
    row = first["sessions"][0]
    assert row["ref"] == {"cli": "codex", "id": threads[3]["id"]}
    assert row["host"] == "none" and row["canSend"] and row["project"] == str(env.project)
    assert row["updatedAt"] == "2026-11-14T23:51:40.000Z"
    listed = env.sent("thread/list")[0]["params"]
    assert listed["sourceKinds"] == ["cli", "vscode", "exec", "appServer"]
    assert listed["cwd"] == [str(env.project)] and listed["sortKey"] == "updated_at"


def _items_turn(turn_id: str) -> dict[str, Any]:
    return {"id": turn_id, "status": "completed", "error": None, "items": [
        {"type": "userMessage", "id": "u1",
         "content": [{"type": "text", "text": "fix the bug", "text_elements": []},
                     {"type": "localImage", "path": "/tmp/x.png"}]},
        {"type": "reasoning", "id": "r1", "summary": ["Looking at the code"], "content": []},
        {"type": "hookPrompt", "id": "h1", "fragments": [{"text": "owner hook text"}]},
        {"type": "commandExecution", "id": "exec-1", "command": "/bin/bash -lc 'ls'",
         "cwd": "/w", "status": "completed", "aggregatedOutput": "a.py\n", "exitCode": 0},
        {"type": "commandExecution", "id": "exec-2", "command": "rm -rf /", "cwd": "/w",
         "status": "declined", "aggregatedOutput": None, "exitCode": None},
        {"type": "fileChange", "id": "patch-1", "status": "completed", "changes": [
            {"path": "a.py", "kind": {"type": "update", "move_path": None},
             "diff": "@@ -1 +1 @@\n-x\n+y\n"}]},
        {"type": "mcpToolCall", "id": "mcp-1", "server": "docs", "tool": "search",
         "status": "completed", "arguments": {"q": "x"},
         "result": {"content": [{"type": "text", "text": "found"}]}, "error": None},
        {"type": "agentMessage", "id": "m1", "text": "Fixed it.", "phase": "final_answer"},
    ]}


async def test_read_converts_history_to_the_timeline(env: Env) -> None:
    failed = {"id": "t2", "status": "failed", "items": [
        {"type": "userMessage", "id": "u2", "content": [{"type": "text", "text": "again"}]}],
        "error": {"message": "Reconnecting... 5/5",
                  "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 401}}}}
    thread = _thread(env.project, updated=1_794_700_000, turns=[_items_turn("t1"), failed])
    env.seed([thread])
    opened = await env.manager.open("codex", thread["id"], conn=Conn())
    assert not opened["readOnly"] and opened["session"]["host"] == "snowpea"
    events = opened["events"]
    kinds = [event["kind"] for event in events]
    assert kinds == ["message.user", "message.reasoning", "tool.call", "tool.result",
                     "tool.call", "tool.result", "tool.call", "diff", "tool.result",
                     "tool.call", "tool.result", "message.done", "message.user", "error"]
    payloads = [event["payload"] for event in events]
    assert payloads[0]["text"] == "fix the bug\n[image]"
    assert payloads[0]["origin"] == {"kind": "human", "surface": "codex"}
    assert payloads[1]["text"] == "Looking at the code"
    assert payloads[2]["name"] == "shell" and payloads[2]["args"]["command"].endswith("'ls'")
    assert payloads[3]["ok"] and payloads[3]["output"] == "a.py\n"
    assert not payloads[5]["ok"] and payloads[5]["error"] == "declined"
    assert payloads[6]["name"] == "apply_patch" and payloads[7]["path"] == "a.py"
    assert payloads[7]["callId"] == payloads[6]["callId"]
    assert payloads[9]["name"] == "mcp:docs.search" and payloads[10]["output"] == "found"
    assert payloads[11]["text"] == "Fixed it."
    assert "codex login" in payloads[13]["message"]
    assert "owner hook text" not in json.dumps(events)
    assert [event["turnId"] for event in events[:2]] == ["t1", "t1"]


def test_real_recording_converts() -> None:
    recorded = json.loads((RECORDINGS / "thread_turns_list.json").read_text())
    turns = sorted(recorded["result"]["data"], key=lambda turn: turn["startedAt"])
    items = codex.convert_turns(turns, TimelineBuilder("codex", "x"))
    assert [(i.kind, i.payload.get("text")) for i in items] == [
        ("message.user", "Create a file named note.txt containing the word hi"),
        ("message.user", "Create a file named note.txt containing the word hi"),
        ("message.done", "Created [note.txt](/work/proj/note.txt) containing `hi`."),
    ]
    again = codex.convert_turns(turns, TimelineBuilder("codex", "x"))
    assert [i.key for i in items] == [i.key for i in again]  # stable keys


# -- create, send, stream ----------------------------------------------------------------


async def test_create_send_stream_and_read_back(env: Env) -> None:
    conn = Conn()
    ref = await _create(env, conn)
    turn = await _turn(env, conn, ref, "reply with ok")
    kinds = conn.kinds()
    assert kinds[:2] == ["turn.started", "message.user"] and kinds[-1] == "turn.done"
    assert "message.delta" in kinds
    done = next(e for e in conn.events() if e["kind"] == "message.done")
    assert done["payload"]["text"] == "OK" and done["turnId"] == turn
    assert _done(conn, turn)["payload"]["reason"] == "complete"
    assert kinds.count("message.user") == 1  # codex's echo of the prompt is not repeated
    started = env.sent("thread/start")[0]["params"]
    assert started == {"cwd": str(env.project), "approvalPolicy": "untrusted",
                       "sandbox": "workspace-write"}
    turn_start = env.sent("turn/start")[0]["params"]
    assert turn_start["input"][0]["text"] == "reply with ok"
    assert "approvalPolicy" not in turn_start  # same mode: no override
    # Read back from codex's store.
    info = await env.adapter.get_session(ref["id"])
    assert info is not None and info.cwd == str(env.project) and info.preview == "reply with ok"
    history = await env.adapter.read(info)
    assert [(i.kind, i.payload.get("text")) for i in history] == [
        ("message.user", "reply with ok"), ("message.done", "OK")]
    # The streamed reply and the history read share their key: no duplicate.
    live_keys = {e["seq"]: e for e in conn.events()}
    assert len(live_keys) == len(conn.events())


async def test_model_selection(env: Env) -> None:
    ref = await _create(env, Conn(), model="gpt-fake-mini")
    assert env.sent("thread/start")[0]["params"]["model"] == "gpt-fake-mini"
    info = await env.adapter.get_session(ref["id"])
    assert info is not None and info.model == "gpt-fake-mini"


async def test_several_threads_share_one_app_server(env: Env) -> None:
    env.write_scenario(scenario="slow", delay=0.02,
                       reply="each thread streams its own answer")
    conns = [Conn(), Conn(), Conn()]
    refs = [await _create(env, conn) for conn in conns]
    turns = await asyncio.gather(*(env.manager.send(ref["cli"], ref["id"], f"go {i}", conn=c)
                                   for i, (ref, c) in enumerate(zip(refs, conns, strict=True))))
    for conn, sent in zip(conns, turns, strict=True):
        await until(lambda c=conn, t=sent["turnId"]: any(
            e["kind"] == "turn.done" and e["turnId"] == t for e in c.events()))
        done = next(e for e in conn.events() if e["kind"] == "message.done")
        assert done["payload"]["text"] == "each thread streams its own answer"
        # Nothing from another thread leaked in.
        assert {e["turnId"] for e in conn.events()} == {sent["turnId"]}
    assert len([r for r in env.records("start") if r["data"]["argv"] == ["app-server"]]) == 1
    assert len({ref["id"] for ref in refs}) == 3


# -- approvals ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["allow", "deny"])
async def test_approval_accept_and_decline(env: Env, answer: str) -> None:
    env.write_scenario(scenario="approval")
    conn = Conn()
    ref = await _create(env, conn)
    turn = (await env.manager.send(ref["cli"], ref["id"], "write it", conn=conn))["turnId"]
    request = (await until(env.hub.pending))[0]
    assert request["external"] == {"cli": "codex", "id": ref["id"]}
    assert request["tool"] == "shell" and request["args"]["command"] == "printf hi > note.txt"
    await env.approvals.respond(request["requestId"], answer, by="client", conn=conn)
    await until(lambda: any(e["kind"] == "turn.done" and e["turnId"] == turn
                            for e in conn.events()))
    decision = [f for f in env.records("in") if "result" in f["data"]
                and "decision" in (f["data"]["result"] or {})][0]["data"]
    assert decision["id"] == 0  # the server's own id space
    result = next(e for e in conn.events() if e["kind"] == "tool.result")
    note = env.project / "note.txt"
    if answer == "allow":
        assert decision["result"] == {"decision": "accept"}
        assert result["payload"]["ok"] and note.read_text() == "hi"
        text = "Created note.txt"
    else:
        assert decision["result"] == {"decision": "decline"}
        assert not result["payload"]["ok"] and not note.exists()
        text = "I could not create the file"
    assert next(e for e in conn.events() if e["kind"] == "message.done")["payload"][
        "text"] == text
    assert _done(conn, turn)["payload"]["reason"] == "complete"


async def test_session_answer_maps_to_accept_for_session(env: Env) -> None:
    env.write_scenario(scenario="approval")
    conn = Conn()
    ref = await _create(env, conn)
    channel = env.manager._records[("codex", ref["id"])].channel
    assert isinstance(channel, codex.CodexChannel)
    asks: list[Any] = []

    async def ask(request: Any) -> ApprovalAnswer:
        asks.append(request)
        return ApprovalAnswer(allow=True, scope="session")

    channel.host_api.ask_approval = ask  # type: ignore[method-assign]
    await _turn(env, conn, ref, "write")
    # The person judged the command codex runs, not its bash wrapper.
    assert asks[0].args["command"] == "printf hi > note.txt"
    decisions = [f["data"]["result"]["decision"] for f in env.records("in")
                 if "decision" in (f["data"].get("result") or {})]
    assert decisions == ["acceptForSession"]


async def test_session_scope_for_an_unmatchable_command_stays_once(env: Env) -> None:
    env.write_scenario(scenario="approval")
    conn = Conn()
    ref = await _create(env, conn)
    turn = (await env.manager.send(ref["cli"], ref["id"], "write", conn=conn))["turnId"]
    request = (await until(env.hub.pending))[0]
    assert request["args"]["command"] == "printf hi > note.txt"
    # snowpea keeps "for the session" to once for a redirecting command ...
    await env.approvals.respond(request["requestId"], "allow", scope="session", conn=conn)
    await until(lambda: any(e["kind"] == "turn.done" and e["turnId"] == turn
                            for e in conn.events()))
    decisions = [f["data"]["result"]["decision"] for f in env.records("in")
                 if "decision" in (f["data"].get("result") or {})]
    assert decisions == ["accept"]  # ... so codex is not told otherwise


async def test_approval_decisions_never_write_codex_rules() -> None:
    params = {"availableDecisions": ["accept", {"acceptWithExecpolicyAmendment": {
        "execpolicy_amendment": ["ls"]}}, "cancel"]}
    always = ApprovalAnswer(allow=True, scope="always")
    decide = codex.CodexChannel._decision
    assert decide(codex.COMMAND_APPROVAL, params, always) == "accept"
    assert decide(codex.COMMAND_APPROVAL, {"availableDecisions": ["acceptForSession"]},
                  always) == "acceptForSession"
    assert decide(codex.FILE_APPROVAL, {}, always) == "acceptForSession"
    assert decide(codex.FILE_APPROVAL, {}, ApprovalAnswer(allow=True)) == "accept"
    assert decide(codex.FILE_APPROVAL, {}, ApprovalAnswer(allow=False)) == "decline"


async def test_file_change_approval_carries_the_diff(env: Env) -> None:
    conn = Conn()
    ref = await _create(env, conn)
    channel = env.manager._records[("codex", ref["id"])].channel
    assert isinstance(channel, codex.CodexChannel)
    asks: list[Any] = []

    async def ask(request: Any) -> ApprovalAnswer:
        asks.append(request)
        return ApprovalAnswer(allow=True, scope="session")

    channel.host_api.ask_approval = ask  # type: ignore[method-assign]
    channel.turn_open = True
    await channel.on_notification("item/started", {"threadId": ref["id"], "turnId": "t", "item": {
        "type": "fileChange", "id": "p1", "status": "inProgress",
        "changes": [{"path": "a.py", "kind": {"type": "add"}, "diff": "+print(1)\n"}]}})
    channel.on_approval_request(41, codex.FILE_APPROVAL, {
        "threadId": ref["id"], "turnId": "t", "itemId": "p1", "reason": "new file"})
    await until(lambda: [f for f in env.records("in") if f["data"].get("id") == 41])
    assert asks[0].tool == "apply_patch" and asks[0].paths == ["a.py"]
    assert asks[0].diff == "+print(1)\n" and asks[0].note == "new file"
    reply = [f["data"] for f in env.records("in") if f["data"].get("id") == 41][0]
    assert reply["result"] == {"decision": "acceptForSession"}
    assert "diff" in conn.kinds()


async def test_unknown_server_requests_and_notifications(env: Env) -> None:
    conn = Conn()
    ref = await _create(env, conn)
    server = env.adapter.server
    await server._dispatch({"method": "item/tool/requestUserInput", "id": 7,
                            "params": {"threadId": ref["id"], "questions": []}})
    await server._dispatch({"method": "thread/brandNew/thing", "params": {"threadId": ref["id"]}})
    await server._dispatch({"method": "item/started", "params": {
        "threadId": ref["id"], "item": {"type": "somethingNew", "id": "x"}}})
    await server._dispatch({"id": 999, "result": {}})  # nobody asked
    refusal = await until(lambda: [f["data"] for f in env.records("in")
                                   if f["data"].get("id") == 7 and "error" in f["data"]])
    assert refusal[0]["error"]["code"] == -32601
    await _turn(env, conn, ref, "still fine")


# -- interrupt, steer ---------------------------------------------------------------------


async def test_interrupt(env: Env) -> None:
    env.write_scenario(scenario="slow", delay=0.05, reply="x" * 30)
    conn = Conn()
    ref = await _create(env, conn)
    turn = (await env.manager.send(ref["cli"], ref["id"], "go", conn=conn))["turnId"]
    await until(lambda: "message.delta" in conn.kinds())
    await env.manager.interrupt(ref["cli"], ref["id"])
    await until(lambda: any(e["kind"] == "turn.done" for e in conn.events()))
    assert _done(conn, turn)["payload"]["reason"] == "interrupted"
    interrupt = env.sent("turn/interrupt")[0]["params"]
    assert interrupt["threadId"] == ref["id"] and interrupt["turnId"]
    await _turn(env, conn, ref, "next")


async def test_steer(env: Env) -> None:
    env.write_scenario(scenario="slow", delay=0.05, reply="y" * 30)
    conn = Conn()
    ref = await _create(env, conn)
    turn = (await env.manager.send(ref["cli"], ref["id"], "go", conn=conn))["turnId"]
    await until(lambda: "message.delta" in conn.kinds())
    channel = env.manager._records[("codex", ref["id"])].channel
    assert isinstance(channel, codex.CodexChannel)
    assert channel.can_steer
    # external.send mid-turn steers (capabilities.steer) instead of session_busy.
    steered = await env.manager.send(ref["cli"], ref["id"], "also add tests", conn=conn)
    assert steered["turnId"] == turn
    steer = env.sent("turn/steer")[0]["params"]
    assert steer["expectedTurnId"] == channel.codex_turn
    assert steer["input"][0]["text"] == "also add tests"
    users = [e for e in conn.events() if e["kind"] == "message.user"]
    assert users[-1]["payload"]["text"] == "also add tests" and users[-1]["turnId"] == turn
    assert users[-1]["payload"]["origin"]["kind"] == "human"
    # A stale turn id is refused by codex: session_busy.
    current, channel.codex_turn = channel.codex_turn, "stale"
    with pytest.raises(ExternalError) as caught:
        await channel.steer("late")
    assert caught.value.code == errors.SESSION_BUSY
    channel.codex_turn = current
    await until(lambda: any(e["kind"] == "turn.done" for e in conn.events()))
    assert not channel.can_steer
    with pytest.raises(ExternalError) as caught:
        await channel.steer("nothing runs")
    assert caught.value.code == errors.SESSION_BUSY


# -- modes and the bypass ---------------------------------------------------------------


def _bypass_frames(env: Env) -> list[dict[str, Any]]:
    out = []
    for record in env.records("in"):
        text = json.dumps(record["data"])
        if "danger-full-access" in text or "dangerFullAccess" in text or (
            '"approvalPolicy": "never"' in text and '"read-only"' not in text
            and '"readOnly"' not in text
        ):
            out.append(record["data"])
    return out


async def test_unrestricted_only_with_a_grant(env: Env) -> None:
    remote = Conn("web")
    remote.remote_login = True  # type: ignore[attr-defined]
    for conn in (None, Conn("browser"), remote):
        with pytest.raises(ExternalError) as caught:
            await env.manager.create("codex", str(env.project), mode="unrestricted", conn=conn)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    conn = Conn()
    ref = await _create(env, conn)
    await _turn(env, conn, ref)
    assert _bypass_frames(env) == []  # never present without a grant
    person = Conn("tui")
    row = (await env.manager.create("codex", str(env.project), mode="unrestricted",
                                    conn=person))["session"]
    assert row["mode"] == "unrestricted" and row["controlled"] is False
    assert row["approvals"] == "none"
    started = env.sent("thread/start")[-1]["params"]
    assert started["approvalPolicy"] == "never" and started["sandbox"] == "danger-full-access"
    ledger = env.home / "jarvis" / "ledger.jsonl"
    assert json.loads(ledger.read_text().splitlines()[-1])["decision"] == "create"


async def test_mode_switch_applies_from_the_next_turn(env: Env) -> None:
    conn = Conn("web")
    ref = await _create(env, conn, mode="readonly")
    started = env.sent("thread/start")[0]["params"]
    assert started["approvalPolicy"] == "never" and started["sandbox"] == "read-only"
    await _turn(env, conn, ref, "look")
    assert "approvalPolicy" not in env.sent("turn/start")[-1]["params"]
    await env.manager.set_mode(ref["cli"], ref["id"], "default", conn=conn)
    await _turn(env, conn, ref, "now edit")
    params = env.sent("turn/start")[-1]["params"]
    assert params["approvalPolicy"] == "untrusted"
    assert params["sandboxPolicy"]["type"] == "workspaceWrite"
    assert _bypass_frames(env) == []
    with pytest.raises(ExternalError) as caught:
        await env.manager.set_mode(ref["cli"], ref["id"], "unrestricted", conn=None)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    await env.manager.set_mode(ref["cli"], ref["id"], "unrestricted", conn=conn)
    await _turn(env, conn, ref, "do anything")
    params = env.sent("turn/start")[-1]["params"]
    assert params["approvalPolicy"] == "never"
    assert params["sandboxPolicy"] == {"type": "dangerFullAccess"}


async def test_bypass_values_never_reach_a_refusing_codex(env: Env) -> None:
    env.write_scenario(scenario="bypass_refused")
    conn = Conn()
    ref = await _create(env, conn)
    await _turn(env, conn, ref)
    await env.manager.close(ref["cli"], ref["id"], conn=conn)
    await env.manager.open(ref["cli"], ref["id"], conn=conn)  # thread/resume, default mode
    await _turn(env, conn, ref, "again")
    assert env.adapter.server.alive  # it never saw a bypass value and never exited
    assert not [r for r in env.records("bypass")]


def test_payload_checks() -> None:
    codex.check_policy({"approvalPolicy": "never", "sandbox": "read-only"})
    codex.check_policy({"approvalPolicy": "never", "sandboxPolicy": {"type": "readOnly"}})
    codex.check_policy({"approvalPolicy": "untrusted", "sandbox": "workspace-write"})
    for bad in ({"approvalPolicy": "never", "sandbox": "workspace-write"},
                {"approvalPolicy": "never"},
                {"approvalPolicy": "never", "sandboxPolicy": {"type": "workspaceWrite"}}):
        with pytest.raises(ExternalError):
            codex.check_policy(bad)
    with pytest.raises(ExternalError):
        codex._scan({"sandbox": "danger-full-access"})
    with pytest.raises(ExternalError):
        codex._scan({"sandboxPolicy": {"type": "dangerFullAccess"}})
    # The prompt and the project folder are free text, not protocol values.
    codex._scan({"threadId": "a", "input": [{"type": "text", "text": "is yolo mode dangerous?"}],
                 "cwd": "/work/bypass-tool"})


async def test_scan_refuses_before_anything_is_written(env: Env) -> None:
    await env.manager.discover("codex")
    before = len(env.records("in"))
    with pytest.raises(ExternalError):
        await env.adapter.server.request("thread/start", {"cwd": str(env.project),
                                                          "approvalPolicy": "never",
                                                          "sandbox": "workspace-write"})
    assert len(env.records("in")) == before


# -- open elsewhere ---------------------------------------------------------------------------


async def test_writer_lock_held_elsewhere_is_view_only_then_taken_over(env: Env) -> None:
    thread = _thread(env.project, updated=1_794_700_000,
                     turns=[{"id": "t1", "status": "completed", "error": None, "items": [
                         {"type": "userMessage", "id": "u1",
                          "content": [{"type": "text", "text": "earlier"}]},
                         {"type": "agentMessage", "id": "m1", "text": "earlier answer"}]}])
    env.seed([thread])
    locks = env.codex_home / "thread-writer-locks"
    locks.mkdir(parents=True, exist_ok=True)
    handle = open(locks / f"{thread['id']}.lock", "a+")  # noqa: SIM115 - held across awaits
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert os.getpid() in (codex.lock_holders(locks / f"{thread['id']}.lock") or set())
        listed = await env.manager.list_sessions(cwd=str(env.project))
        assert listed["sessions"][0]["host"] == "terminal"
        assert not listed["sessions"][0]["canSend"]
        viewer = Conn("tui")
        opened = await env.manager.open("codex", thread["id"], conn=viewer)
        assert opened["readOnly"] and opened["session"]["host"] == "terminal"
        assert [e["payload"].get("text") for e in opened["events"]] == [
            "earlier", "earlier answer"]
        with pytest.raises(ExternalError) as caught:
            await env.manager.send("codex", thread["id"], "hi", conn=viewer)
        assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
        assert env.sent("thread/resume") == []  # nothing was taken from the person
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    state = await until(lambda: [s for s in env.hub.states()
                                 if s.get("previousHost") == "terminal"])
    assert state[-1]["host"] == "none" and state[-1]["canSend"]
    await _turn(env, viewer, {"cli": "codex", "id": thread["id"]}, "taking over")
    assert env.manager.opened()[0]["host"] == "snowpea"
    # Our own app-server holds the lock now: that is not "elsewhere".
    holders = codex.lock_holders(locks / f"{thread['id']}.lock")
    assert holders == {env.server_pid()}
    info = await env.adapter.get_session(thread["id"])
    assert info is not None
    assert (await env.adapter.liveness(info, own_pids=set())).host == "none"


async def test_active_writer_answer_reads_as_view_only(env: Env) -> None:
    thread = _thread(env.project, updated=1_794_700_000)
    env.seed([thread])
    env.write_scenario(scenario="second_writer")
    viewer = Conn("tui")
    opened = await env.manager.open("codex", thread["id"], conn=viewer)
    # codex said "active writer": shown view only at once, not after a refresh.
    assert opened["session"]["host"] == "terminal" and opened["readOnly"] is True
    assert opened["session"]["canSend"] is False
    assert env.sent("thread/resume")
    with pytest.raises(ExternalError) as caught:
        await env.manager.send("codex", thread["id"], "hi", conn=viewer)
    assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
    listed = await env.manager.list_sessions(cwd=str(env.project))
    assert listed["sessions"][0]["host"] == "terminal"


def test_lock_holders(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    assert codex.lock_holders(path) == set()
    path.write_text("")
    assert codex.lock_holders(path) == set()
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        assert codex.lock_holders(path) == {os.getpid()}
    assert codex.lock_holders(path) == set()


# -- robustness ------------------------------------------------------------------------------


async def test_app_server_crash_mid_turn_and_restart(env: Env) -> None:
    env.write_scenario(scenario="normal", turns=["die"])
    conn = Conn()
    ref = await _create(env, conn)
    first_pid = env.server_pid()
    turn = (await env.manager.send(ref["cli"], ref["id"], "hi", conn=conn))["turnId"]
    await until(lambda: any(e["kind"] == "turn.done" and e["turnId"] == turn
                            for e in conn.events()))
    assert _done(conn, turn)["payload"]["reason"] == "error"
    error = next(e for e in conn.events() if e["kind"] == "error")
    assert "status 3" in error["payload"]["message"]
    state = await until(lambda: [s for s in env.hub.states() if s["status"] == "error"])
    assert state[-1]["host"] == "none"
    # The thread is resumable: the next prompt restarts the server and resumes it.
    env.write_scenario(scenario="normal")
    await _turn(env, conn, ref, "again")
    assert env.server_pid() != first_pid and env.adapter.server.starts == 2
    assert env.sent("thread/resume")[-1]["params"]["threadId"] == ref["id"]
    assert env.manager.opened()[0]["host"] == "snowpea"


async def test_thread_closed_by_codex_is_resumed_on_the_next_prompt(env: Env) -> None:
    conn = Conn()
    ref = await _create(env, conn)
    await env.adapter.server._dispatch({"method": "thread/closed",
                                        "params": {"threadId": ref["id"]}})
    state = await until(lambda: [s for s in env.hub.states() if s.get("previousHost")
                                 == "snowpea"])
    assert state[-1]["host"] == "none"
    await _turn(env, conn, ref, "back again")
    assert env.sent("thread/resume")[-1]["params"]["threadId"] == ref["id"]


async def test_hung_turn_is_ended_by_interrupt_and_close(env: Env) -> None:
    env.write_scenario(scenario="hang")
    conn = Conn()
    ref = await _create(env, conn)
    turn = (await env.manager.send(ref["cli"], ref["id"], "hi", conn=conn))["turnId"]
    await asyncio.sleep(0.3)
    await env.manager.interrupt(ref["cli"], ref["id"])
    await until(lambda: any(e["kind"] == "turn.done" and e["turnId"] == turn
                            for e in conn.events()))
    assert _done(conn, turn)["payload"]["reason"] == "interrupted"
    await env.manager.close(ref["cli"], ref["id"], conn=conn)
    assert env.sent("thread/unsubscribe")
    pid = env.server_pid()
    await env.adapter.stop_server()  # the fake ignores SIGTERM: it is killed
    assert not proc.pid_alive(pid)


async def test_unresponsive_app_server_is_restarted(env: Env) -> None:
    env.adapter.request_timeout_sec = 1.0
    env.adapter.list_timeout_sec = 1.0
    env.seed([_thread(env.project, updated=1_794_700_000, name="kept")])
    await env.manager.discover("codex")
    pid = env.server_pid()
    os.kill(pid, signal.SIGSTOP)
    try:
        with pytest.raises(ExternalError) as caught:
            await env.adapter.list_sessions([str(env.project)], limit=5)
        assert "did not answer" in caught.value.message
        await until(lambda: not proc.pid_alive(pid))
    finally:
        if proc.pid_alive(pid):
            os.kill(pid, signal.SIGCONT)
    rows = await env.adapter.list_sessions([str(env.project)], limit=5)
    assert [row.title for row in rows] == ["kept"] and env.server_pid() != pid


async def test_garbage_lines_are_ignored(env: Env) -> None:
    env.write_scenario(scenario="garbage")
    conn = Conn()
    ref = await _create(env, conn)
    turn = await _turn(env, conn, ref)
    assert _done(conn, turn)["payload"]["reason"] == "complete"
    process = env.adapter.server.process
    assert process is not None and process.garbage_lines >= 4


async def test_slow_stream(env: Env) -> None:
    reply = "a slow answer, one character at a time"
    env.write_scenario(scenario="slow", delay=0.01, reply=reply)
    conn = Conn()
    ref = await _create(env, conn)
    await _turn(env, conn, ref)
    deltas = [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.delta"]
    assert len(deltas) == len(reply) and "".join(deltas) == reply
    done = next(e for e in conn.events() if e["kind"] == "message.done")
    assert done["payload"]["text"] == reply


async def test_lost_login_ends_the_turn_with_a_clear_error(env: Env) -> None:
    env.write_scenario(scenario="normal", turns=["noauth"])
    conn = Conn()
    ref = await _create(env, conn)
    turn = await _turn(env, conn, ref)
    assert _done(conn, turn)["payload"]["reason"] == "error"
    errors_seen = [e["payload"]["message"] for e in conn.events() if e["kind"] == "error"]
    assert len(errors_seen) == 1 and "codex login" in errors_seen[0]


async def test_idle_app_server_stops_and_comes_back(env: Env) -> None:
    env.adapter.server_idle_sec = 0.3
    conn = Conn()
    ref = await _create(env, conn)
    await _turn(env, conn, ref)
    pid = env.server_pid()
    await asyncio.sleep(0.8)
    assert env.adapter.server.alive  # a loaded thread keeps it
    await env.manager.close(ref["cli"], ref["id"], conn=conn)
    await until(lambda: env.adapter.server.process is None)
    assert not proc.pid_alive(pid)
    assert env.records("exit")  # it left on stdin EOF, releasing its locks
    rows = await env.adapter.list_sessions([str(env.project)], limit=5)
    assert [row.id for row in rows] == [ref["id"]]


async def test_shutdown_stops_the_shared_server(env: Env) -> None:
    conn = Conn()
    await _create(env, conn)
    pid = env.server_pid()
    await env.manager.shutdown()  # the daemon's supervisor stops it (pid file)
    await until(lambda: not proc.pid_alive(pid))
    await until(lambda: env.adapter.server.process is None)
    assert not list((env.home / "external").glob("*.json"))


# -- review follow-ups ------------------------------------------------------------------------


def _channel(env: Env, ref: dict[str, str]) -> codex.CodexChannel:
    channel = env.manager._records[("codex", ref["id"])].channel
    assert isinstance(channel, codex.CodexChannel)
    return channel


async def test_login_lost_while_codex_retries_never_blocks_the_reader(env: Env) -> None:
    env.write_scenario(scenario="slow", delay=0.2, reply="z" * 20)
    conn = Conn()
    ref = await _create(env, conn)
    turn = (await env.manager.send(ref["cli"], ref["id"], "go", conn=conn))["turnId"]
    channel = _channel(env, ref)
    await until(lambda: channel.codex_turn is not None)
    codex_turn = channel.codex_turn
    loop = asyncio.get_running_loop()
    started = loop.time()
    await env.adapter.server._dispatch({"method": "error", "params": {
        "threadId": ref["id"], "turnId": codex_turn, "willRetry": True,
        "error": {"message": "Reconnecting... 1/5",
                  "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 401}}}}})
    assert loop.time() - started < 1.0  # the reader did not wait for an answer
    done = _done(conn, turn)
    assert done["payload"]["reason"] == "error"
    await until(lambda: env.sent("turn/interrupt"))
    assert env.sent("turn/interrupt")[0]["params"]["turnId"] == codex_turn
    # The interrupted turn's late completion does not touch the next turn.
    await asyncio.sleep(0.5)
    await _turn(env, conn, ref, "after")
    assert env.adapter.server.alive and env.adapter.server.starts == 1


async def test_late_completion_of_an_ended_turn_is_ignored(env: Env) -> None:
    conn = Conn()
    ref = await _create(env, conn)
    channel = _channel(env, ref)
    channel._ended.add("old-turn")
    channel.turn_open, channel.codex_turn = True, None
    await channel.on_notification("item/agentMessage/delta", {
        "threadId": ref["id"], "turnId": "old-turn", "itemId": "m", "delta": "stale"})
    await channel.on_notification("turn/started", {"threadId": ref["id"],
                                                   "turn": {"id": "old-turn"}})
    await channel.on_notification("turn/completed", {"threadId": ref["id"], "turn": {
        "id": "old-turn", "status": "interrupted"}})
    assert channel.turn_open and channel.codex_turn is None
    assert "message.delta" not in conn.kinds()
    channel.turn_open = False


async def test_root_grants_are_never_widened(env: Env) -> None:
    conn = Conn()
    ref = await _create(env, conn)
    channel = _channel(env, ref)
    asks: list[Any] = []

    async def ask(request: Any) -> ApprovalAnswer:
        asks.append(request)
        return ApprovalAnswer(allow=True, scope="session")

    channel.host_api.ask_approval = ask  # type: ignore[method-assign]
    channel.on_approval_request(51, codex.FILE_APPROVAL, {
        "threadId": ref["id"], "turnId": "t", "itemId": "p", "grantRoot": "/"})
    channel.on_approval_request(52, codex.FILE_APPROVAL, {
        "threadId": ref["id"], "turnId": "t", "itemId": "p",
        "grantRoot": str(env.project / "src")})
    def answered() -> dict[int, str] | None:
        found = {f["data"]["id"]: f["data"]["result"]["decision"] for f in env.records("in")
                 if f["data"].get("id") in (51, 52)}
        return found if len(found) == 2 else None

    replies = await until(answered)
    assert replies == {51: "decline", 52: "accept"}
    assert len(asks) == 1 and "src" in asks[0].note  # "/" was refused without asking


async def test_best_effort_timeouts_do_not_restart_the_server(env: Env) -> None:
    await env.manager.discover("codex")
    pid = env.server_pid()
    os.kill(pid, signal.SIGSTOP)
    try:
        with pytest.raises(ExternalError):
            await env.adapter.server.request("thread/loaded/list", {}, timeout=0.3,
                                             restart_on_timeout=False)
        await asyncio.sleep(0.3)
        assert proc.pid_alive(pid) and env.adapter.server.process is not None
    finally:
        os.kill(pid, signal.SIGCONT)
    assert await env.adapter.server.request("thread/loaded/list", {}) is not None


async def test_new_codex_binary_waits_for_running_threads(env: Env) -> None:
    conn = Conn()
    ref = await _create(env, conn)
    pid = env.server_pid()
    upgraded = proc.PinnedExecutable(path=str(FAKE_CODEX), version="codex-cli 0.168.0")
    await env.adapter.ensure_server(upgraded)
    assert env.server_pid() == pid  # the open thread keeps its server
    await env.manager.close(ref["cli"], ref["id"], conn=conn)
    await env.adapter.ensure_server(upgraded)
    assert env.server_pid() != pid


async def test_idle_stop_and_a_new_request_do_not_race(env: Env) -> None:
    env.adapter.server_idle_sec = 0.2
    await env.manager.discover("codex")
    for _ in range(10):
        rows = await env.adapter.list_sessions([str(env.project)], limit=5)
        assert rows == []
        await asyncio.sleep(0.12)
    await until(lambda: env.adapter.server.process is None)
    rows = await env.adapter.list_sessions([str(env.project)], limit=5)
    assert rows == [] and env.adapter.server.alive
    idle = env.adapter.server._idle
    assert idle is not None and not idle.done()  # the new server still stops when idle


async def test_a_reread_history_does_not_repeat_snowpea_prompts(env: Env) -> None:
    conn = Conn()
    ref = await _create(env, conn)
    await _turn(env, conn, ref, "first prompt")
    await _turn(env, conn, ref, "second prompt")
    record = env.manager._records[("codex", ref["id"])]
    # A later re-read (a live tail, the catch-up when a terminal closes) brings the
    # thread's userMessage items back under codex's own item ids.
    await env.manager._publish(record, await env.adapter.read(record.info))
    users = [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.user"]
    assert users == ["first prompt", "second prompt"]
