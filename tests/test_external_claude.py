"""M19 WP6a: the Claude Code adapter (structured transport, store, liveness).

Everything runs against ``tests/fixtures/fake_clis/fake_claude.py`` with
``CLAUDE_CONFIG_DIR`` pointing into the test home (conftest), so the real
``~/.claude`` is never read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _claude_support import (
    OWNER_EMAIL,
    StubHost,
    assistant,
    claude_home,
    pinned,
    private,
    start_terminal,
    starts,
    stop,
    until,
    user,
    write_transcript,
)
from _external_adapter import FAKE_CLAUDE
from _support import env_vars, pinned_cli

from snowpea_core.config.settings import Settings
from snowpea_core.external import claude as claude_mod
from snowpea_core.external import proc
from snowpea_core.external.base import ApprovalAnswer, LaunchSpec, SessionInfo
from snowpea_core.external.claude import (
    ClaudeAdapter,
    TranscriptTail,
    encode_cwd,
    iter_lines,
    read_transcript,
    scan_meta,
)
from snowpea_core.external.manager import ExternalManager
from snowpea_core.external.proc import ExternalError
from snowpea_core.external.registry import BUILTIN_ADAPTERS, AdapterRegistry
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.server import errors

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")

RECORDINGS = Path(__file__).parent / "fixtures" / "external_recordings" / "claude"


# -- manager harness ----------------------------------------------------------------


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

    def turn_done(self, turn: str) -> dict[str, Any] | None:
        return next((e for e in self.events()
                     if e["kind"] == "turn.done" and e["turnId"] == turn), None)


class Hub:
    def __init__(self) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any], *, exclude: Any = None) -> None:
        self.notes.append((method, params))

    async def tell_observers(self, method: str, params: dict[str, Any]) -> None:
        return None

    def pending(self) -> list[dict[str, Any]]:
        return [params["request"] for method, params in self.notes
                if method == "approval.pending"]

    def states(self) -> list[dict[str, Any]]:
        return [params for method, params in self.notes if method == "external.state"]


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.project = (tmp_path / "proj").resolve()
        self.project.mkdir(parents=True)
        self.settings = Settings.model_validate(
            {"jarvis": {"external": {"clis": {"claude": {**pinned_cli(FAKE_CLAUDE)}}}}}
        )
        self.hub = Hub()
        self.approvals = ApprovalQueue(self.settings, hub=self.hub)  # type: ignore[arg-type]
        self.log = tmp_path / "fake.log"
        self.adapter = ClaudeAdapter(interrupt_grace=1.0, init_timeout=10.0)
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

    async def create(self, conn: Conn, **kwargs: Any) -> dict[str, str]:
        row = (await self.manager.create("claude", str(self.project), conn=conn,
                                         **kwargs))["session"]
        return row["ref"]

    async def turn(self, conn: Conn, ref: dict[str, str], text: str = "hello") -> dict[str, Any]:
        turn = (await self.manager.send("claude", ref["id"], text, conn=conn))["turnId"]
        return await until(lambda: conn.turn_done(turn))


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    setup = Env(tmp_path)
    with env_vars(FAKE_CLI_LOG=str(setup.log), FAKE_CLI_SCENARIO=None,
                  FAKE_CLI_SCENARIO_FILE=None, FAKE_CLAUDE_LOGGED_IN=None):
        await setup.manager.start()
        try:
            yield setup
        finally:
            await setup.manager.shutdown()


# -- registry and discovery ---------------------------------------------------------------


def test_the_claude_adapter_is_builtin() -> None:
    adapters = [factory() for factory in BUILTIN_ADAPTERS]
    assert any(isinstance(adapter, ClaudeAdapter) for adapter in adapters)
    assert AdapterRegistry.builtin().get("claude") is not None


async def test_discovery_reports_version_login_and_capabilities(
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    found = await ClaudeAdapter().discover(str(FAKE_CLAUDE))
    assert found.usable and found.problems == []
    assert found.executable is not None
    assert found.executable.version == "2.1.325 (Claude Code, fake)"
    assert found.capabilities is not None
    wire = found.capabilities.to_wire()
    assert wire["approvals"] == "bridged" and wire["streaming"] and wire["liveView"]
    assert wire["modes"] == ["default", "readonly", "unrestricted"]
    assert wire["tmux"] is False
    # The auth answer's account fields never leave the probe.
    assert OWNER_EMAIL not in repr(found) and OWNER_EMAIL not in caplog.text


@pytest.mark.parametrize(
    ("version", "problem"),
    [("1.0.88 (Claude Code)", "too old"), ("3.0.1 (Claude Code)", "newer than"),
     ("Claude Code nightly", "could not read")],
)
async def test_discovery_rejects_unsupported_versions(tmp_path: Path, version: str,
                                                      problem: str) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"version": version}))
    with env_vars(FAKE_CLI_SCENARIO_FILE=str(scenario)):
        found = await ClaudeAdapter().discover(str(FAKE_CLAUDE))
    assert not found.usable
    assert any(problem in text for text in found.problems), found.problems


async def test_discovery_reports_a_logged_out_cli() -> None:
    with env_vars(FAKE_CLAUDE_LOGGED_IN="0"):
        found = await ClaudeAdapter().discover(str(FAKE_CLAUDE))
    assert not found.usable
    assert found.problems == ["Claude Code is not logged in: run `claude auth login`"]
    assert OWNER_EMAIL not in repr(found)


async def test_discovery_without_the_binary(tmp_path: Path) -> None:
    found = await ClaudeAdapter().discover(str(tmp_path / "missing" / "claude"))
    assert not found.installed


async def test_models_are_the_cli_aliases() -> None:
    models = await ClaudeAdapter().list_models(None)
    assert models == ["fable", "opus", "sonnet", "haiku"]
    for model in models:
        assert proc.validate_model(model) == model


# -- listing by working directory -------------------------------------------------------


async def test_list_sessions_by_cwd_newest_first_with_paging(tmp_path: Path) -> None:
    project = str((tmp_path / "proj").resolve())
    now = time.time()
    a = write_transcript(project, [*private(project), user("first prompt", project),
                                   assistant([{"type": "text", "text": "hi"}], project)],
                         mtime=now - 30)
    b = write_transcript(f"{project}/sub", [user("in a subfolder", f"{project}/sub")],
                         mtime=now - 20)
    c = write_transcript(project, [user("named one", project),
                                   {"type": "ai-title", "aiTitle": "A titled session"},
                                   {"type": "custom-title", "customTitle": "Renamed"}],
                         mtime=now - 10)
    # Lossy folder names: "/proj-x" encodes like "/proj/x" but is not inside it.
    write_transcript(f"{project}-x", [user("sibling", f"{project}-x")], mtime=now)
    write_transcript("/somewhere/else", [user("outside", "/somewhere/else")], mtime=now)
    # Not a session: an old sidechain file.
    (a.parent / "agent-1234.jsonl").write_text(a.read_text())
    adapter = ClaudeAdapter()
    rows = await adapter.list_sessions([project], limit=10)
    assert [row.id for row in rows] == [c.stem, b.stem, a.stem]
    assert rows[0].title == "Renamed" and rows[0].preview == "named one"
    assert rows[1].cwd == f"{project}/sub"
    assert rows[2].title == "first prompt" and rows[2].model == "claude-haiku-5-5"
    first = await adapter.list_sessions([project], limit=2)
    assert [row.id for row in first] == [c.stem, b.stem]
    rest = await adapter.list_sessions([project], limit=2, before=rows[2].updated_at)
    assert [row.id for row in rest] == [a.stem]
    for row in rows:
        assert OWNER_EMAIL not in repr(row) and OWNER_EMAIL not in row.preview
    got = await adapter.get_session(a.stem)
    assert got is not None and got.cwd == project
    assert await adapter.get_session("not-a-uuid") is None
    assert await adapter.get_session(str(uuid.uuid4())) is None


async def test_list_titles_from_commands_and_skips_meta(tmp_path: Path) -> None:
    project = str(tmp_path)
    path = write_transcript(project, [
        user("<local-command-caveat>Caveat: ...</local-command-caveat>", project, isMeta=True),
        user("<command-name>/model</command-name>\n<command-args>opus</command-args>", project),
        user("then a real question", project),
    ])
    meta = scan_meta(path)
    assert meta.title == "/model opus" and meta.preview == "then a real question"


async def test_claude_config_dir_is_honoured(tmp_path: Path) -> None:
    other = tmp_path / "other-config"
    with env_vars(CLAUDE_CONFIG_DIR=str(other)):
        assert claude_mod.config_dir() == other
        write_transcript(str(tmp_path), [user("here", str(tmp_path))])
        rows = await ClaudeAdapter().list_sessions([str(tmp_path)], limit=5)
    assert len(rows) == 1 and rows[0].extra["path"].startswith(str(other))
    assert await ClaudeAdapter().list_sessions([str(tmp_path)], limit=5) == []


def test_bounded_reads_of_a_huge_transcript(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    project = str(tmp_path)
    records = [user(f"prompt {index}", project) for index in range(400)]
    path = write_transcript(project, records)
    with open(path, "a", encoding="utf-8") as handle:
        giant = user("x" * 5000, project)
        handle.write(json.dumps(giant) + "\n")
        handle.write(json.dumps(user("after the giant line", project)) + "\n")
    monkeypatch.setattr(claude_mod, "MAX_LINE_BYTES", 2000)
    monkeypatch.setattr(claude_mod, "MAX_READ_BYTES", 8000)
    items, offset, _calls = read_transcript(path, path.stem, project)
    texts = [item.payload.get("text") for item in items]
    assert offset == path.stat().st_size
    assert texts[0].startswith("Older parts")  # the note about what was cut
    assert "prompt 399" in texts and "prompt 0" not in texts
    assert texts[-1] == "after the giant line" and not any("xxxx" in str(t) for t in texts)
    # The line reader never yields an over-long line.
    assert all(len(line) <= 2000 for _offset, line in iter_lines(path))


# -- history -> timeline ------------------------------------------------------------------


def test_history_renders_only_conversation(tmp_path: Path,
                                           caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    project = str(tmp_path)
    file = tmp_path / "note.txt"
    path = write_transcript(project, [
        *private(project),
        user("write a note", project),
        assistant([{"type": "thinking", "thinking": "", "signature": "sig"}], project),
        assistant([{"type": "tool_use", "id": "toolu_1", "name": "Write",
                    "input": {"file_path": str(file), "content": "hi\n"}}], project),
        user([{"type": "tool_result", "tool_use_id": "toolu_1",
               "content": "File created successfully"}], project,
             toolUseResult={"type": "create", "filePath": str(file), "content": "hi\n",
                            "structuredPatch": [], "originalFile": None}),
        assistant([{"type": "tool_use", "id": "toolu_2", "name": "Edit",
                    "input": {"file_path": str(file), "old_string": "hi",
                              "new_string": "ho"}}], project),
        user([{"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"}], project,
             toolUseResult={"filePath": str(file), "structuredPatch": [
                 {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1,
                  "lines": ["-hi", "+ho"]}]}),
        assistant([{"type": "tool_use", "id": "toolu_3", "name": "Bash",
                    "input": {"command": "rm -rf /"}}], project),
        user([{"type": "tool_result", "tool_use_id": "toolu_3", "is_error": True,
               "content": "The user doesn't want to proceed"}], project),
        user("[Request interrupted by user for tool use]", project),
        assistant([{"type": "text", "text": "Done."}], project),
        user("sidechain", project, isSidechain=True),
        user("summary of before", project, isCompactSummary=True),
        {"type": "system", "subtype": "turn_duration", "content": OWNER_EMAIL},
        {"type": "api-request", "params": {"metadata": OWNER_EMAIL}},
    ])
    items, _offset, calls = read_transcript(path, path.stem, project)
    kinds = [item.kind for item in items]
    assert kinds == ["message.user", "message.reasoning", "tool.call", "tool.result", "diff",
                     "tool.call", "tool.result", "diff", "tool.call", "tool.result",
                     "message.done", "message.done", "message.done"]
    assert items[0].payload["text"] == "write a note"
    diffs = [item.payload for item in items if item.kind == "diff"]
    assert diffs[0]["path"] == str(file) and "+hi" in diffs[0]["patch"]
    assert "--- a/note.txt" in diffs[1]["patch"] and "+ho" in diffs[1]["patch"]
    assert diffs[1]["callId"] == "toolu_2"
    results = [item.payload for item in items if item.kind == "tool.result"]
    assert results[0]["name"] == "Write" and results[2]["ok"] is False
    notes = [item.payload["text"] for item in items if item.kind == "message.done"]
    assert notes == ["Interrupted.", "Done.", "Earlier conversation was compacted by Claude Code."]
    assert calls == {}
    dumped = json.dumps([item.payload for item in items])
    assert OWNER_EMAIL not in dumped and "org-1234" not in dumped
    assert OWNER_EMAIL not in caplog.text
    again, _o, _c = read_transcript(path, path.stem, project)
    assert [item.key for item in again] == [item.key for item in items]


def test_the_recorded_transcript_converts() -> None:
    path = RECORDINGS / "transcript_sample.jsonl"
    items, _offset, _calls = read_transcript(path, "72b3e6e6-81b3-49e4-9824-9c99d396912e",
                                             "<PROJ>")
    kinds = {item.kind for item in items}
    assert {"message.user", "message.done", "tool.call", "tool.result", "diff"} <= kinds
    assert "<EMAIL>" not in json.dumps([item.payload for item in items])


def test_tail_follows_appends_and_survives_rewrites(tmp_path: Path) -> None:
    project = str(tmp_path)
    path = write_transcript(project, [user("one", project)])
    tail = TranscriptTail(path, path.stem, project, start=0)
    assert [i.payload["text"] for i in tail.poll()] == ["one"]
    assert tail.poll() == []
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(user("two", project)) + "\n")
        handle.write('{"type": "user", "partial')  # still being written
    assert [i.payload["text"] for i in tail.poll()] == ["two"]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('": 1}\n')
    assert tail.poll() == []
    # Rewritten shorter (compaction, rotation): read again, nothing doubled.
    first = json.loads(path.read_text().splitlines()[0])
    path.write_text(json.dumps(first) + "\n" + json.dumps(user("three", project)) + "\n")
    assert [i.payload["text"] for i in tail.poll()] == ["three"]
    assert tail.resets == 1
    # Replaced by a new file (new inode).
    replacement = path.with_suffix(".tmp")
    replacement.write_text(path.read_text() + json.dumps(user("four", project)) + "\n")
    os.replace(replacement, path)
    assert [i.payload["text"] for i in tail.poll()] == ["four"]


# -- liveness ------------------------------------------------------------------------------


def _status_file(pid: int, session_id: str, cwd: str, **extra: Any) -> Path:
    path = claude_home() / "sessions" / f"{pid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "sessionId": session_id, "cwd": cwd,
                                "procStart": proc.process_start_time(pid), "status": "idle",
                                "updatedAt": int(time.time() * 1000), **extra}))
    return path


async def test_liveness_kinds(tmp_path: Path) -> None:
    sid = str(uuid.uuid4())
    info = SessionInfo(id=sid, cwd=str(tmp_path))
    adapter = ClaudeAdapter()
    assert (await adapter.liveness(info, own_pids=set())).host == "none"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        status = _status_file(sleeper.pid, sid, str(tmp_path), status="busy")
        live = await adapter.liveness(info, own_pids=set())
        assert live.host == "terminal" and live.busy and live.tmux is None
        assert (await adapter.liveness(info, own_pids={sleeper.pid})).host == "none"
        _status_file(sleeper.pid, sid, str(tmp_path), status="waiting", tmux="work:@1.%7")
        live = await adapter.liveness(info, own_pids=set())
        assert live.host == "tmux" and live.busy and live.tmux == {"session": "work",
                                                                     "pane": "%7"}
        if sys.platform.startswith("linux"):
            # A reused pid (start time differs) is not the session's process.
            _status_file(sleeper.pid, sid, str(tmp_path), procStart="1")
            assert (await adapter.liveness(info, own_pids=set())).host == "none"
        status.write_text("not json")
        assert (await adapter.liveness(info, own_pids=set())).host == "none"
    finally:
        sleeper.kill()
        sleeper.wait()
    _status_file(sleeper.pid, sid, str(tmp_path))
    assert (await adapter.liveness(info, own_pids=set())).host == "none"  # pid is gone


async def test_a_session_open_elsewhere_is_never_resumed(tmp_path: Path) -> None:
    sid = str(uuid.uuid4())
    holder = start_terminal(sid, tmp_path)
    try:
        await until(lambda: any(claude_mod.live_holders(sid, set())))
        host = StubHost(tmp_path / "home", sid)
        spec = LaunchSpec(cwd=str(tmp_path), project=str(tmp_path), executable=pinned(),
                          mode="default", session_id=sid, resume=True)
        channel = ClaudeAdapter().channel(spec, host)
        with pytest.raises(ExternalError) as caught:
            await channel.start()
        assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
        assert not channel.alive and host.supervisor.pids() == set()
    finally:
        stop(holder)


# -- structured channel through the manager ---------------------------------------------


async def test_create_send_stream_and_read_back(env: Env) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_REPLY="Hello there, streaming"):
        ref = await env.create(conn)
        done = await env.turn(conn, ref)
    assert done["payload"]["reason"] == "complete"
    kinds = conn.kinds()
    assert kinds[:2] == ["turn.started", "message.user"]
    assert "message.delta" in kinds and "message.reasoning" in kinds
    final = [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.done"]
    assert final == ["Hello there, streaming"]
    start = starts(env.log)[0]["argv"]
    for flag in ("-p", "--input-format", "--output-format", "--verbose",
                 "--include-partial-messages", "--replay-user-messages",
                 "--permission-prompts", "--permission-prompt-tool"):
        assert flag in start
    assert start[start.index("--permission-mode") + 1] == "default"
    assert start[start.index("--session-id") + 1] == ref["id"]
    assert starts(env.log)[0]["bypass"] == []
    # The stored transcript reads back as the same conversation, without the owner.
    await env.manager.close("claude", ref["id"], conn=conn)
    listed = await env.manager.list_sessions(cwd=str(env.project))
    assert [row["ref"] for row in listed["sessions"]] == [ref]
    assert listed["sessions"][0]["title"] == "hello"
    page = await env.manager.read("claude", ref["id"])
    history = [(e["kind"], e["payload"].get("text")) for e in page["events"]]
    assert ("message.user", "hello") in history
    assert ("message.done", "Hello there, streaming") in history
    assert OWNER_EMAIL not in json.dumps(page) + json.dumps(listed) + json.dumps(conn.frames)


@pytest.mark.parametrize("answer", ["allow", "deny"])
async def test_approval_round_trip(env: Env, answer: str) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="approval"):
        ref = await env.create(conn)
        turn = (await env.manager.send("claude", ref["id"], "write", conn=conn))["turnId"]
    request = (await until(env.hub.pending))[0]
    assert request["tool"] == "Write" and request["external"] == ref
    assert request["paths"] == [str(env.project / "note.txt")]
    assert "+hi" in (request.get("diff") or "")
    await env.approvals.respond(request["requestId"], answer, by="client", conn=conn,
                                reason="not now" if answer == "deny" else "")
    await until(lambda: conn.turn_done(turn))
    final = [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.done"]
    results = [e["payload"] for e in conn.events() if e["kind"] == "tool.result"]
    if answer == "allow":
        assert (env.project / "note.txt").read_text() == "hi\n"
        assert final[-1] == "Allowed: wrote note.txt" and results[0]["ok"]
        assert any(e["kind"] == "diff" for e in conn.events())
    else:
        assert not (env.project / "note.txt").exists()
        assert final[-1] == "Denied: did not write" and not results[0]["ok"]
        frames = [entry["data"] for entry in claude_log(env.log) if entry["kind"] == "in"
                  and entry["data"].get("type") == "control_response"]
        assert frames[-1]["response"]["response"] == {
            "behavior": "deny", "message": "not now", "interrupt": False}


def claude_log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def test_interrupt_ends_the_turn(env: Env) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="slow", FAKE_CLI_DELAY="0.2",
                  FAKE_CLI_REPLY="a long and slow answer that takes a while"):
        ref = await env.create(conn)
        turn = (await env.manager.send("claude", ref["id"], "go", conn=conn))["turnId"]
        await until(lambda: "message.delta" in conn.kinds())
        await env.manager.interrupt("claude", ref["id"])
        done = await until(lambda: conn.turn_done(turn))
    assert done["payload"]["reason"] == "interrupted"
    sent = [entry["data"]["request"]["subtype"] for entry in claude_log(env.log)
            if entry["kind"] == "in" and entry["data"].get("type") == "control_request"]
    assert sent == ["initialize", "interrupt"]
    # The process is still there for the next turn.
    with env_vars(FAKE_CLI_SCENARIO="normal"):
        assert (await env.turn(conn, ref))["payload"]["reason"] == "complete"
    assert len(starts(env.log)) == 1


async def test_unrestricted_only_with_a_persons_grant(env: Env) -> None:
    with pytest.raises(ExternalError) as caught:
        await env.manager.create("claude", str(env.project), mode="unrestricted", conn=None)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert starts(env.log) == []
    conn = Conn("ide")
    ref = await env.create(conn, mode="unrestricted")
    await env.turn(conn, ref)
    argv = starts(env.log)[-1]["argv"]
    assert "--dangerously-skip-permissions" in argv
    assert "--permission-prompts" not in argv and "--permission-mode" not in argv
    # Built by hand, unrestricted without a grant is refused; other modes never carry it.
    adapter = ClaudeAdapter()
    spec = LaunchSpec(cwd=str(env.project), project=None, executable=pinned(),
                      mode="unrestricted")
    with pytest.raises(ExternalError) as caught:
        adapter.argv(spec, "unrestricted", None, ["--session-id", "x"], structured=True)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    for mode in ("default", "readonly"):
        argv = adapter.argv(spec, mode, None, ["--session-id", "x"], structured=True)
        assert not any(proc.is_dangerous(arg) for arg in argv)
    with pytest.raises(ExternalError):
        adapter.argv(LaunchSpec(cwd="/", project=None, executable=pinned(), mode="default",
                                model="--dangerously-skip-permissions"),
                     "default", None, [], structured=True)


async def test_mode_change_restarts_with_resume(env: Env) -> None:
    conn = Conn()
    ref = await env.create(conn)
    await env.turn(conn, ref)
    await env.manager.set_mode("claude", ref["id"], "readonly", conn=conn)
    assert len(starts(env.log)) == 1  # applied when the next turn starts
    await env.turn(conn, ref, "again")
    first, second = starts(env.log)
    assert first["argv"][first["argv"].index("--permission-mode") + 1] == "default"
    assert second["argv"][second["argv"].index("--permission-mode") + 1] == "plan"
    assert second["argv"][second["argv"].index("--resume") + 1] == ref["id"]
    assert "--session-id" not in second["argv"]
    # Back to default, then unrestricted (a person's grant) and back: each restarts.
    await env.manager.set_mode("claude", ref["id"], "unrestricted", conn=conn)
    await env.turn(conn, ref, "third")
    assert starts(env.log)[-1]["bypass"] == ["--dangerously-skip-permissions"]
    await env.manager.set_mode("claude", ref["id"], "default", conn=conn)
    await env.turn(conn, ref, "fourth")
    assert starts(env.log)[-1]["bypass"] == []


async def test_plan_exit_is_the_persons_to_answer(tmp_path: Path) -> None:
    """1.56.0: plan mode's ExitPlanMode prompt is bridged as a person-only approval.

    Denied, Claude Code keeps planning; allowed, it leaves plan mode for the
    default mode (said with setMode) and the session row follows.
    """
    for allow in (False, True):
        host = StubHost(tmp_path / "home", answer=ApprovalAnswer(allow=allow, reason="no"))
        spec = LaunchSpec(cwd=str(tmp_path), project=None, executable=pinned(), mode="readonly",
                          native_mode="plan")
        channel = ClaudeAdapter().channel(spec, host)
        assert isinstance(channel, claude_mod.ClaudeChannel)
        channel.started_mode, channel.started_native = "readonly", "plan"

        class Sink:
            def __init__(self) -> None:
                self.lines: list[dict[str, Any]] = []

            async def write_line(self, value: dict[str, Any]) -> None:
                self.lines.append(value)

        sink = Sink()
        await channel._approve(sink, "r1", {  # type: ignore[arg-type]
            "tool_name": "ExitPlanMode", "input": {"plan": "x"}})
        assert [a.tool for a in host.asks] == ["ExitPlanMode"]
        assert host.asks[0].person_only is True
        body = sink.lines[0]["response"]["response"]
        if allow:
            assert body["behavior"] == "allow"
            assert body["updatedPermissions"] == [
                {"type": "setMode", "mode": "default", "destination": "session"}]
            assert (channel.mode, channel.native_mode) == ("default", "default")
            # The process already runs in the default mode: no restart for it.
            assert (channel.started_mode, channel.started_native) == ("default", "default")
            assert host.modes == ["default"]
        else:
            assert body["behavior"] == "deny" and body["message"] == "no"
            assert (channel.mode, channel.native_mode) == ("readonly", "plan")
            assert host.modes == []


async def test_cli_dies_mid_turn(env: Env) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="die"):
        ref = await env.create(conn)
        turn = (await env.manager.send("claude", ref["id"], "go", conn=conn))["turnId"]
        done = await until(lambda: conn.turn_done(turn))
    assert done["payload"]["reason"] == "error"
    errors_seen = [e["payload"]["message"] for e in conn.events() if e["kind"] == "error"]
    assert errors_seen and "exited (status 3)" in errors_seen[0]
    state = env.hub.states()[-1]
    assert state["status"] == "error" and state["host"] == "none"
    # The next send starts a fresh process on the same session.
    with env_vars(FAKE_CLI_SCENARIO="normal"):
        again = await env.turn(conn, ref)
    assert again["payload"]["reason"] == "complete"
    assert "--resume" in starts(env.log)[-1]["argv"]


async def test_a_hung_cli_is_killed_after_interrupt(env: Env, tmp_path: Path) -> None:
    conn = Conn()
    child_file = tmp_path / "child.pid"
    with env_vars(FAKE_CLI_SCENARIO="hang", FAKE_CLI_CHILD_PIDFILE=str(child_file)):
        ref = await env.create(conn)
        turn = (await env.manager.send("claude", ref["id"], "go", conn=conn))["turnId"]
        await until(lambda: "message.reasoning" in conn.kinds())
        pids = set(env.manager.supervisor.pids())
        await env.manager.interrupt("claude", ref["id"])
        done = await until(lambda: conn.turn_done(turn), timeout=15)
    assert done["payload"]["reason"] == "interrupted"
    grandchild = int(child_file.read_text())
    await until(lambda: not any(proc.pid_alive(pid) for pid in pids | {grandchild}))
    assert env.manager.supervisor.pids() == set()


async def test_close_kills_a_cli_that_ignores_sigterm(env: Env) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="hang"):
        ref = await env.create(conn)
        await env.manager.send("claude", ref["id"], "go", conn=conn)
        await until(lambda: "message.reasoning" in conn.kinds())
        pids = set(env.manager.supervisor.pids())
        started = time.monotonic()
        await env.manager.close("claude", ref["id"], conn=conn)
    assert time.monotonic() - started < 10
    assert not any(proc.pid_alive(pid) for pid in pids)
    assert conn.kinds()[-1] == "turn.done"


async def test_garbage_lines_do_not_break_a_turn(env: Env) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="garbage"):
        ref = await env.create(conn)
        done = await env.turn(conn, ref)
    assert done["payload"]["reason"] == "complete"
    assert [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.done"] == ["OK"]


async def test_not_logged_in_at_runtime(env: Env) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="autherror"):
        ref = await env.create(conn)
        done = await env.turn(conn, ref)
    assert done["payload"]["reason"] == "error"
    message = next(e["payload"]["message"] for e in conn.events() if e["kind"] == "error")
    assert "not logged in" in message and "claude auth login" in message


async def test_takeover_after_the_other_terminal_closes(env: Env) -> None:
    conn = Conn()
    ref = await env.create(conn)
    await env.turn(conn, ref, "from snowpea")
    await env.manager.close("claude", ref["id"], conn=conn)
    holder = start_terminal(ref["id"], env.project)
    try:
        await until(lambda: any(claude_mod.live_holders(ref["id"], set())))
        viewer = Conn()
        opened = await env.manager.open("claude", ref["id"], conn=viewer)
        assert opened["session"]["host"] == "terminal" and opened["readOnly"]
        with pytest.raises(ExternalError) as caught:
            await env.manager.send("claude", ref["id"], "hi", conn=viewer)
        assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
        # The person types in their terminal: the view follows the transcript live.
        transcript = claude_mod.transcript_path(str(env.project), ref["id"])
        with open(transcript, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(user("typed in the terminal", str(env.project))) + "\n")
            handle.write(json.dumps(assistant([{"type": "text", "text": "answered there"}],
                                              str(env.project))) + "\n")
        await until(lambda: any(e["payload"].get("text") == "answered there"
                                for e in viewer.events()))
    finally:
        stop(holder)
    await until(lambda: any(s["host"] == "none" and s.get("previousHost") == "terminal"
                            for s in env.hub.states()))
    done = await env.turn(viewer, ref, "taken over")
    assert done["payload"]["reason"] == "complete"
    texts = [e["payload"].get("text") for e in viewer.events()]
    assert texts.count("answered there") == 1  # the tail and the catch-up read agree
    assert "--resume" in starts(env.log)[-1]["argv"]


async def test_title_is_reported_after_a_turn(tmp_path: Path) -> None:
    host = StubHost(tmp_path / "home")
    spec = LaunchSpec(cwd=str(tmp_path), project=None, executable=pinned(), mode="default")
    channel = ClaudeAdapter().channel(spec, host)
    try:
        await channel.start()
        await channel.send("name me please", "t1")
        await until(lambda: host.finished)
        await until(lambda: host.titles)
    finally:
        await channel.close()
    assert host.titles == ["name me please"] and host.ids == [channel.session_id]
    assert host.finished == [("complete", None)] and host.died == []


async def test_answer_by_jarvis_or_cache_still_answers(tmp_path: Path) -> None:
    """A denied answer without a reason still sends a message (the CLI needs one)."""
    with env_vars(FAKE_CLI_SCENARIO="approval", FAKE_CLI_LOG=str(tmp_path / "log")):
        host = StubHost(tmp_path / "home", answer=ApprovalAnswer(allow=False))
        spec = LaunchSpec(cwd=str(tmp_path), project=None, executable=pinned(), mode="default")
        channel = ClaudeAdapter().channel(spec, host)
        try:
            await channel.start()
            await channel.send("write", "t1")
            await until(lambda: host.finished)
        finally:
            await channel.close()
    assert host.asks and host.asks[0].tool == "Write" and host.asks[0].risk == "medium"
    answers = [entry["data"]["response"]["response"] for entry in claude_log(tmp_path / "log")
               if entry["kind"] == "in" and entry["data"].get("type") == "control_response"]
    assert answers == [{"behavior": "deny", "message": "Denied", "interrupt": False}]


async def test_idle_close_and_shutdown_leave_no_process(env: Env) -> None:
    conn = Conn()
    ref = await env.create(conn)
    await env.turn(conn, ref)
    pids = env.manager.supervisor.pids()
    assert pids
    env.manager._idle_sec = lambda: 0.5  # type: ignore[method-assign]
    await asyncio.sleep(0.6)
    assert await env.manager.sweep_idle() == 1
    assert not any(proc.pid_alive(pid) for pid in pids)
    assert encode_cwd(str(env.project)) in str(claude_mod.transcript_path(str(env.project),
                                                                          ref["id"]))


async def test_a_reread_history_does_not_repeat_snowpea_prompts(env: Env) -> None:
    conn = Conn()
    ref = await env.create(conn)
    await env.turn(conn, ref, "first prompt")
    await env.turn(conn, ref, "second prompt")
    record = env.manager._records[("claude", ref["id"])]
    fresh = await env.adapter.get_session(ref["id"])
    assert fresh is not None
    await env.manager._publish(record, await env.adapter.read(fresh))
    users = [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.user"]
    assert users == ["first prompt", "second prompt"]


# -- external.setModel (1.67.0) -------------------------------------------------------------


def _model_of(argv: list[str]) -> str | None:
    return argv[argv.index("--model") + 1] if "--model" in argv else None


async def test_set_model_restarts_on_the_next_turn(env: Env) -> None:
    conn = Conn()
    ref = await env.create(conn)
    await env.turn(conn, ref)
    result = await env.manager.set_model("claude", ref["id"], "opus")
    assert result == {"ref": ref, "model": "opus"}  # nothing ran: no appliesNextTurn
    assert len(starts(env.log)) == 1  # applied when the next turn starts
    assert env.hub.states()[-1]["model"] == "opus"
    assert (await env.manager.get("claude", ref["id"]))["session"]["model"] == "opus"
    await env.turn(conn, ref, "again")
    second = starts(env.log)[-1]["argv"]
    assert _model_of(second) == "opus" and second[second.index("--resume") + 1] == ref["id"]
    # A full model id is fine for Claude Code; the same model again restarts nothing.
    await env.manager.set_model("claude", ref["id"], "claude-opus-4-5-20251101")
    await env.turn(conn, ref, "third")
    assert _model_of(starts(env.log)[-1]["argv"]) == "claude-opus-4-5-20251101"
    count = len(starts(env.log))
    await env.turn(conn, ref, "fourth")
    assert len(starts(env.log)) == count
    # null: the CLI's default, no --model at all.
    assert (await env.manager.set_model("claude", ref["id"], None))["model"] is None
    await env.turn(conn, ref, "fifth")
    assert _model_of(starts(env.log)[-1]["argv"]) is None
    assert env.hub.states()[-1]["model"] is None


async def test_set_model_refuses_bad_names(env: Env) -> None:
    conn = Conn()
    ref = await env.create(conn)
    for bad in ("--dangerously-skip-permissions", "-m", "a b", "../x", ""):
        with pytest.raises(ExternalError) as caught:
            await env.manager.set_model("claude", ref["id"], bad)
        assert caught.value.code == errors.INVALID_PARAMS
    await env.turn(conn, ref)
    assert _model_of(starts(env.log)[-1]["argv"]) is None


async def test_set_model_during_a_turn_applies_next_turn(env: Env) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="hang"):
        ref = await env.create(conn)
        turn = (await env.manager.send("claude", ref["id"], "go", conn=conn))["turnId"]
        await until(lambda: "message.reasoning" in conn.kinds())
        result = await env.manager.set_model("claude", ref["id"], "sonnet")
        assert result == {"ref": ref, "model": "sonnet", "appliesNextTurn": True}
        assert len(starts(env.log)) == 1  # the running turn is left alone
        await env.manager.interrupt("claude", ref["id"])
        await until(lambda: conn.turn_done(turn), timeout=15)
    await env.turn(conn, ref, "again")
    assert _model_of(starts(env.log)[0]["argv"]) is None
    assert _model_of(starts(env.log)[-1]["argv"]) == "sonnet"


async def test_set_model_survives_a_daemon_restart(env: Env) -> None:
    conn = Conn()
    ref = await env.create(conn, model="haiku")
    await env.turn(conn, ref)  # a transcript the next daemon finds
    await env.manager.set_model("claude", ref["id"], "sonnet")
    await env.manager.close("claude", ref["id"], conn=conn)
    saved = json.loads((env.home / "external" / "sessions" / "models.json").read_text())
    assert saved == {f"claude:{ref['id']}": "sonnet"}
    # A new daemon (manager) on the same home: the row and the next start use it.
    again = ExternalManager(home=env.home, settings=lambda: env.settings, hub=env.hub,
                            approvals=env.approvals,
                            registry=AdapterRegistry([ClaudeAdapter(init_timeout=10.0)]),
                            roots=env._roots, poll_interval=0.1, sweep_interval=3600)
    await again.start()
    try:
        assert (await again.get("claude", ref["id"]))["session"]["model"] == "sonnet"
        listed = await again.list_sessions(cwd=str(env.project))
        assert [row["model"] for row in listed["sessions"]] == ["sonnet"]
        turn = (await again.send("claude", ref["id"], "after restart", conn=conn))["turnId"]
        await until(lambda: conn.turn_done(turn))
        assert _model_of(starts(env.log)[-1]["argv"]) == "sonnet"
        # null is remembered as a choice too (the CLI's default, not the recorded model).
        await again.set_model("claude", ref["id"], None)
        assert (await again.get("claude", ref["id"]))["session"]["model"] is None
    finally:
        await again.shutdown()


async def test_set_model_refuses_other_terminals(env: Env) -> None:
    conn = Conn()
    ref = await env.create(conn)
    record = env.manager._records[("claude", ref["id"])]
    record.host = "terminal"
    with pytest.raises(ExternalError) as caught:
        await env.manager.set_model("claude", ref["id"], "opus")
    assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
    record.host = "snowpea"
    assert not (env.home / "external" / "sessions" / "models.json").exists()
