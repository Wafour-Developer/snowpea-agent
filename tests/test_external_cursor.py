"""M19 WP6d: the Cursor agent adapter, against ``fake_cursor.py`` and spike recordings."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _support import env_vars, pinned_cli

from snowpea_core.config.settings import Settings
from snowpea_core.external import cursor as cur
from snowpea_core.external import proc
from snowpea_core.external.base import LaunchSpec, SessionInfo
from snowpea_core.external.cursor import CursorAdapter, CursorChannel, _StreamState
from snowpea_core.external.manager import ExternalManager
from snowpea_core.external.proc import ExternalError, PinnedExecutable
from snowpea_core.external.registry import AdapterRegistry
from snowpea_core.external.timeline import TimelineBuilder
from snowpea_core.server import errors

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CURSOR = FIXTURES / "fake_clis" / "fake_cursor.py"
RECORDINGS = FIXTURES / "external_recordings" / "cursor"


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


async def until(predicate: Callable[[], Any], timeout: float = 15.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def gone(pid: int) -> bool:
    """Exited (a zombie waiting for init counts as exited)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return not proc.pid_alive(pid)
    return state == "Z"


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.state = tmp_path / "state"
        self.project = (tmp_path / "proj").resolve()
        self.project.mkdir(parents=True)
        self.log = tmp_path / "fake.log"
        self.scenario_file = tmp_path / "scenario.json"
        self.scenario()
        self.settings = Settings.model_validate(
            {"jarvis": {"external": {"clis": {"cursor": {**pinned_cli(FAKE_CURSOR)}}}}}
        )
        self.adapter = CursorAdapter(cursor_home=self.state / ".cursor", stall_sec=60.0)
        self.hub = Hub()
        self.manager = ExternalManager(
            home=self.home,
            settings=lambda: self.settings,
            hub=self.hub,
            registry=AdapterRegistry([self.adapter]),
            roots=self._roots,
            poll_interval=0.1,
            sweep_interval=3600,
        )

    async def _roots(self) -> list[str]:
        return [str(self.project)]

    def scenario(self, scenario: str = "normal", **extra: Any) -> None:
        self.scenario_file.write_text(json.dumps({"scenario": scenario, **extra}))

    def starts(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        records = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [r["data"] | {"pid": r["pid"]} for r in records if r["kind"] == "start"]

    def turns(self) -> list[list[str]]:
        return [s["argv"] for s in self.starts() if "-p" in s["argv"]]

    async def create(self, conn: Conn, **kwargs: Any) -> dict[str, str]:
        row = (await self.manager.create("cursor", str(self.project), conn=conn, **kwargs))
        return row["session"]["ref"]

    async def turn(self, conn: Conn, ref: dict[str, str], text: str = "hello") -> str:
        turn = (await self.manager.send("cursor", ref["id"], text, conn=conn))["turnId"]
        await until(lambda: self.done(conn, turn))
        return turn

    @staticmethod
    def done(conn: Conn, turn: str) -> dict[str, Any] | None:
        return next((e for e in conn.events() if e["kind"] == "turn.done"
                     and e["turnId"] == turn), None)


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    setup = Env(tmp_path)
    with env_vars(FAKE_CLI_LOG=str(setup.log), FAKE_CLI_SCENARIO=None,
                  FAKE_CLI_SCENARIO_FILE=str(setup.scenario_file),
                  FAKE_CLI_STATE_DIR=str(setup.state), FAKE_CURSOR_LOGGED_OUT=None,
                  FAKE_CURSOR_WORKER_PIDFILE=None, FAKE_CLI_CHILD_PIDFILE=None):
        await setup.manager.start()
        try:
            yield setup
        finally:
            await setup.manager.shutdown()


# -- pure helpers --------------------------------------------------------------------------


def test_versions() -> None:
    assert cur.parse_version("2026.11.13-eacade1") == (2026, 11, 13)
    assert cur.version_problem("2026.11.13-eacade1") is None
    assert "too old" in (cur.version_problem("2025.06.01-abc") or "")
    assert "newer than this snowpea" in (cur.version_problem("2031.01.01-abc") or "")
    assert "unrecognised" in (cur.version_problem("cursor 1.2") or "")


def test_parse_models_cleans_the_table() -> None:
    text = ("Available models\n\nauto - Auto (default)\n"
            "\x1b[1mcomposer-2.5\x1b[0m - Composer 2.5 (current)\n"
            "grok-4.7-low-fast - Grok 4.7  Low Fast\u200b\u200b\n"
            "--yolo - not a model\nbroken line\nauto - duplicate\n")
    assert cur.parse_models(text) == ["auto", "composer-2.5", "grok-4.7-low-fast"]


def test_user_wrappers_and_local_time() -> None:
    raw = ("<timestamp>Sunday, Nov 15, 2026, 9:09 PM (UTC+9)</timestamp>\n<user_query>\n"
           "Create a file\n</user_query>")
    text, when = cur.user_text(raw)
    assert text == "Create a file" and when == "2026-11-15T12:09:00.000Z"
    assert cur.parse_local_time("Monday, Jan 5, 2026, 12:30 AM (UTC-5:30)") == (
        "2026-01-05T06:00:00.000Z")
    assert cur.parse_local_time("Mon, Jan 5, 2026, 1:00 PM (UTC)") == "2026-01-05T13:00:00.000Z"
    assert cur.parse_local_time("yesterday") is None
    assert cur.user_text("plain text")[0] == "plain text"


def test_workspace_naming() -> None:
    assert cur.encoded_workspace("/home/a/Wan2.2-x_y") == "home-a-Wan2-2-x-y"
    # Seen with the real CLI: "/tmp/c-1/-mnt/x_y" is stored as "tmp-c-1-mnt-x-y".
    assert cur.encoded_workspace("/tmp/c-1/-mnt/x_y") == "tmp-c-1-mnt-x-y"
    assert cur.workspace_hash("/tmp/a") == hashlib.md5(b"/tmp/a").hexdigest()


def test_registry_has_cursor() -> None:
    registry = AdapterRegistry.builtin()
    assert isinstance(registry.get("cursor"), CursorAdapter)


def test_prompt_size_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    exe = ["/bin/cursor-agent", "-p", "--"]
    cur.check_prompt_size([*exe, "x" * cur.MAX_PROMPT_BYTES], {})
    with pytest.raises(ExternalError) as caught:
        cur.check_prompt_size([*exe, "é" * (cur.MAX_PROMPT_BYTES // 2 + 1)], {})
    assert caught.value.code == errors.INVALID_PARAMS and "too long" in caught.value.message
    monkeypatch.setattr(os, "sysconf", lambda name: 128 * 1024)
    with pytest.raises(ExternalError) as caught:
        cur.check_prompt_size([*exe, "x" * 70_000], {"BIG": "y" * 1000})
    assert "system's limit" in caught.value.message


# -- the stream, from the spike's recordings -----------------------------------------------


def _feed(name: str) -> tuple[list[Any], list[tuple[bool, str | None]]]:
    builder = TimelineBuilder("cursor", "chat-1", salt="live")
    state = _StreamState(builder, "xt-1")
    ends = []
    for line in (RECORDINGS / name).read_text().splitlines():
        result = state.feed(json.loads(line))
        if result is not None:
            ends.append(result)
    state.close()
    return builder.drain(), ends


def test_recorded_partial_output_is_shown_once() -> None:
    items, ends = _feed("oneshot.jsonl")
    done = [i for i in items if i.kind == "message.done"]
    assert [d.payload["text"] for d in done] == ["OK"]
    assert [i.payload["text"] for i in items if i.kind == "message.delta"] == ["OK"]
    assert any(i.kind == "message.reasoning" for i in items)
    assert ends == [(True, None)]
    assert all(i.turn_id == "xt-1" for i in items)


def test_recorded_edit_becomes_call_result_and_diff() -> None:
    items, _ends = _feed("write_without_force.jsonl")
    call = next(i for i in items if i.kind == "tool.call")
    result = next(i for i in items if i.kind == "tool.result")
    diff = next(i for i in items if i.kind == "diff")
    assert call.payload["name"] == "Edit" and "\n" not in call.payload["callId"]
    assert result.payload["callId"] == call.payload["callId"] == diff.payload["callId"]
    assert result.payload["ok"] and diff.payload["path"].endswith("note.txt")
    assert "+hi" in diff.payload["patch"]
    assert len([i for i in items if i.kind == "message.done"]) == 1


def test_recorded_denied_shell_is_a_failed_result() -> None:
    items, _ends = _feed("shell_denied_by_project_cli_json.jsonl")
    results = [i.payload for i in items if i.kind == "tool.result"]
    shells = [r for r in results if r["name"] == "Shell"]
    assert shells and all(not r["ok"] and "permissionDenied" in r["error"] for r in shells)
    edits = [r for r in results if r["name"] == "Edit"]
    assert edits and edits[0]["ok"]
    calls = [i.payload["callId"] for i in items if i.kind == "tool.call"]
    assert len(calls) == len(set(calls)) == 3
    assert {r["callId"] for r in results} == set(calls)


def test_recorded_shell_output() -> None:
    items, _ends = _feed("shell_sandbox_without_force.jsonl")
    result = next(i.payload for i in items if i.kind == "tool.result")
    assert result["name"] == "Shell" and result["ok"]


def test_stream_segments_around_a_tool_call() -> None:
    builder = TimelineBuilder("cursor", "c", salt="s")
    state = _StreamState(builder, "t")
    frames = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let "}]},
         "timestamp_ms": 1},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "me look."}]},
         "timestamp_ms": 2},
        {"type": "tool_call", "subtype": "started", "call_id": "a\nb",
         "tool_call": {"readToolCall": {"args": {"path": "x"}}}},
        {"type": "tool_call", "subtype": "completed", "call_id": "a\nb",
         "tool_call": {"readToolCall": {"args": {"path": "x"},
                                        "result": {"success": {"content": "data"}}}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]},
         "timestamp_ms": 3},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me look."}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "x"},
    ]
    for frame in frames:
        state.feed(frame)
    items = builder.drain()
    assert [i.payload["text"] for i in items if i.kind == "message.done"] == [
        "Let me look.", "Done."]
    result = next(i.payload for i in items if i.kind == "tool.result")
    assert result["output"] == "data" and result["name"] == "Read"


def test_stream_failed_shell_and_error_result() -> None:
    builder = TimelineBuilder("cursor", "c", salt="s")
    state = _StreamState(builder, "t")
    state.feed({"type": "tool_call", "subtype": "completed", "call_id": "z",
                "tool_call": {"shellToolCall": {"args": {"command": "false"}, "result": {
                    "success": {"exitCode": 1, "stdout": "", "stderr": "boom"}}}}})
    assert state.feed({"type": "result", "subtype": "error", "is_error": True,
                       "result": "quota"}) == (False, "quota")
    items = builder.drain()
    assert [i.kind for i in items] == ["tool.call", "tool.result"]
    assert items[1].payload["ok"] is False and items[1].payload["error"] == "boom"


# -- history --------------------------------------------------------------------------------


def _store_chat(env: Env, chat: str, cwd: Path, records: list[dict[str, Any]] | str, *,
                updated: int = 1_794_744_703_933, **meta: Any) -> Path:
    chat_dir = env.state / ".cursor" / "chats" / cur.workspace_hash(str(cwd)) / chat
    chat_dir.mkdir(parents=True)
    (chat_dir / "meta.json").write_text(json.dumps(
        {"schemaVersion": 1, "createdAtMs": updated - 1000, "hasConversation": True,
         "updatedAtMs": updated, "cwd": str(cwd), **meta}))
    folder = (env.state / ".cursor" / "projects" / cur.encoded_workspace(str(cwd))
              / "agent-transcripts" / chat)
    folder.mkdir(parents=True)
    text = records if isinstance(records, str) else "".join(
        json.dumps(r) + "\n" for r in records)
    (folder / f"{chat}.jsonl").write_text(text)
    return chat_dir


async def test_read_recorded_transcript(env: Env) -> None:
    chat = str(uuid.uuid4())
    sample = (RECORDINGS / "transcript_sample.jsonl").read_text()
    junk = json.dumps({"role": "system", "message": {"content": "secret system prompt"}})
    _store_chat(env, chat, env.project, sample + junk + "\nnot json\n")
    info = await env.adapter.get_session(chat)
    assert info is not None and info.title == "Create a file named note.txt containing the word hi"
    items = await env.adapter.read(info)
    again = await env.adapter.read(info)
    assert [i.key for i in items] == [i.key for i in again]
    kinds = [i.kind for i in items]
    users = [i.payload["text"] for i in items if i.kind == "message.user"]
    assert users[0] == "Create a file named note.txt containing the word hi"
    assert all("<user_query>" not in u and "<timestamp>" not in u for u in users)
    assert items[0].ts == "2026-11-15T12:09:00.000Z"
    calls = [i.payload for i in items if i.kind == "tool.call"]
    assert [c["name"] for c in calls] == ["Write", "Shell", "Write", "Shell", "Shell"]
    diffs = [i.payload for i in items if i.kind == "diff"]
    assert len(diffs) == 2 and diffs[0]["path"].endswith("note.txt") and "+hi" in diffs[0]["patch"]
    assert diffs[0]["callId"] == calls[0]["callId"]
    assert kinds.count("tool.result") == 5 and kinds.count("message.done") == 3
    assert "secret system prompt" not in json.dumps([i.payload for i in items])


async def test_history_edits_errors_and_patches(env: Env) -> None:
    chat = str(uuid.uuid4())
    records = [
        {"role": "user", "message": {"content": [{"type": "text", "text": "fix it"}]}},
        {"role": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "StrReplace",
             "input": {"path": "/p/a.py", "old_string": "x = 1", "new_string": "x = 2"}},
            {"type": "tool_use", "name": "ApplyPatch",
             "input": "*** Begin Patch\n*** Update File: b.py\n-a\n+b\n*** End Patch"}]}},
        {"type": "turn_ended", "status": "error", "error": "rate limited"},
    ]
    _store_chat(env, chat, env.project, records)
    items = await env.adapter.read(await env.adapter.get_session(chat))  # type: ignore[arg-type]
    diffs = [i.payload for i in items if i.kind == "diff"]
    assert diffs[0]["path"] == "/p/a.py" and "-x = 1" in diffs[0]["patch"]
    assert "+x = 2" in diffs[0]["patch"]
    assert diffs[1]["path"] == "b.py"
    error = next(i.payload for i in items if i.kind == "error")
    assert error["message"] == "rate limited"


async def test_list_by_cwd_titles_subagents_and_paging(env: Env) -> None:
    sub = env.project / "pkg"
    sub.mkdir()
    other = env.tmp / "other"
    other.mkdir()
    user = {"role": "user", "message": {"content": [{"type": "text", "text": "q"}]}}
    ids = []
    for index, cwd in enumerate([env.project, env.project, sub, other]):
        chat = str(uuid.uuid4())
        ids.append(chat)
        _store_chat(env, chat, cwd, [user], updated=1_794_744_000_000 + index * 1000)
    # A renamed chat (meta.title), a subagent and an empty chat are handled.
    renamed = str(uuid.uuid4())
    _store_chat(env, renamed, env.project, [user], updated=1_794_745_000_000, title="Renamed")
    sub_chat = env.state / ".cursor" / "chats" / "x" / str(uuid.uuid4())
    sub_chat.mkdir(parents=True)
    (sub_chat / "meta.json").write_text(json.dumps({"isSubagent": True, "updatedAtMs": 1}))
    empty = env.state / ".cursor" / "chats" / cur.workspace_hash(str(env.project)) / str(
        uuid.uuid4())
    empty.mkdir(parents=True)
    (empty / "meta.json").write_text(json.dumps(
        {"cwd": str(env.project), "hasConversation": False, "updatedAtMs": 1_794_746_000_000}))
    first = await env.manager.list_sessions(clis=["cursor"], cwd=str(env.project), limit=2)
    rows = first["sessions"]
    assert [r["ref"]["id"] for r in rows] == [renamed, ids[2]] and rows[0]["title"] == "Renamed"
    assert first["nextBefore"]
    rest = await env.manager.list_sessions(clis=["cursor"], cwd=str(env.project), limit=2,
                                           before=first["nextBefore"])
    assert [r["ref"]["id"] for r in rest["sessions"]] == [ids[1], ids[0]]
    assert rest["nextBefore"] is None
    assert all(r["host"] == "none" and r["canSend"] for r in rows)
    assert rows[1]["title"] == "q" and rows[1]["preview"] == "q"


# -- discovery ------------------------------------------------------------------------------


async def test_discovery_and_models(env: Env) -> None:
    rows = {row["id"]: row for row in await env.manager.clis()}
    row = rows["cursor"]
    assert row["installed"] and row["problems"] == [] and row["defaultMode"] == "readonly"
    assert row["capabilities"] == {"streaming": True, "approvals": "degraded", "liveView": True,
                                   "steer": False, "tmux": False, "modes": ["default", "readonly",
                                                            "unrestricted"]}
    found = await env.adapter.discover(str(FAKE_CURSOR))
    assert await env.adapter.list_models(found) == ["auto", "composer-fake"]
    assert await env.adapter.list_models(found) == ["auto", "composer-fake"]
    listed = [s for s in env.starts() if "--list-models" in s["argv"]]
    assert len(listed) == 1  # cached
    assert await env.adapter.list_models(None) == []


@pytest.mark.parametrize("version,needle", [("2025.01.01-x", "too old"),
                                             ("2031.01.01-x", "newer than"),
                                             ("garbage", "unrecognised")])
async def test_discovery_refuses_unsupported_versions(env: Env, version: str,
                                                      needle: str) -> None:
    env.scenario(version=version)
    found = await env.adapter.discover(str(FAKE_CURSOR))
    assert found.installed and not found.usable and needle in found.problems[0]


async def test_discovery_reports_logged_out(env: Env) -> None:
    with env_vars(FAKE_CURSOR_LOGGED_OUT="1"):
        found = await env.adapter.discover(str(FAKE_CURSOR))
    assert found.problems == [CursorAdapter.LOGIN_HINT]
    assert not (await env.adapter.discover("/nonexistent/cursor-agent")).usable


# -- running --------------------------------------------------------------------------------


def _options(argv: list[str]) -> list[str]:
    return argv[: argv.index("--")]


async def test_create_and_send_readonly_by_default(env: Env) -> None:
    conn = Conn()
    created = (await env.manager.create("cursor", str(env.project), conn=conn))["session"]
    ref = created["ref"]
    assert created["mode"] == "readonly" and created["controlled"] is True
    assert created["approvals"] == "none" and created["host"] == "snowpea"
    turn = await env.turn(conn, ref)
    assert env.done(conn, turn)["payload"]["reason"] == "complete"  # type: ignore[index]
    argv = env.turns()[0]
    assert argv[-2:] == ["--", "hello"]
    opts = _options(argv)
    assert ["--mode", "ask"] == opts[opts.index("--mode"):opts.index("--mode") + 2]
    assert opts[opts.index("--resume") + 1] == ref["id"]
    assert opts[opts.index("--workspace") + 1] == str(env.project)
    assert "--trust" in opts and opts[opts.index("--sandbox") + 1] == "enabled"
    assert "--stream-partial-output" in opts
    assert not {"--yolo", "-f", "--force"} & set(argv)
    done = [e for e in conn.events() if e["kind"] == "message.done"]
    assert [d["payload"]["text"] for d in done] == ["OK"]
    assert "message.reasoning" in conn.kinds()
    # The title comes from the first prompt; the second turn resumes the same chat.
    states = [p for m, p in env.hub.notes if m == "external.state"]
    assert any(s.get("title") == "hello" for s in states)
    await env.turn(conn, ref, "again")
    assert env.turns()[1][env.turns()[1].index("--resume") + 1] == ref["id"]
    start = env.starts()[-1]
    assert not [n for n in start["env"] if n.startswith("SNOWPEA_")]
    assert cur.TAG_ENV in start["env"]


async def test_readonly_refuses_edits(env: Env) -> None:
    env.scenario("approval")
    conn = Conn()
    ref = await env.create(conn)
    await env.turn(conn, ref, "write note.txt")
    assert not (env.project / "note.txt").exists()
    call = next(e["payload"] for e in conn.events() if e["kind"] == "tool.call")
    result = next(e["payload"] for e in conn.events() if e["kind"] == "tool.result")
    assert "\n" not in call["callId"] and result["callId"] == call["callId"]
    assert result["ok"] is False and "permissionDenied" in result["error"]


async def test_default_mode_edits_in_the_sandbox(env: Env) -> None:
    env.scenario("approval")
    conn = Conn()
    created = (await env.manager.create("cursor", str(env.project), mode="default",
                                        conn=conn))["session"]
    assert created["approvals"] == "degraded" and created["controlled"] is False
    await env.turn(conn, created["ref"], "write note.txt")
    assert (env.project / "note.txt").read_text() == "hi"
    diff = next(e["payload"] for e in conn.events() if e["kind"] == "diff")
    assert diff["path"].endswith("note.txt") and diff["callId"]
    opts = _options(env.turns()[0])
    assert "--mode" not in opts and not {"--yolo", "-f", "--force"} & set(env.turns()[0])


async def test_unrestricted_only_with_a_person_grant(env: Env) -> None:
    with pytest.raises(ExternalError) as caught:
        await env.manager.create("cursor", str(env.project), mode="unrestricted", conn=None)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    with pytest.raises(ExternalError):
        await env.manager.create("cursor", str(env.project), mode="unrestricted",
                                 conn=Conn("browser"))
    conn = Conn("ide")
    created = (await env.manager.create("cursor", str(env.project), mode="unrestricted",
                                        conn=conn))["session"]
    assert created["approvals"] == "none" and created["controlled"] is False
    ref = created["ref"]
    await env.turn(conn, ref)
    argv = env.turns()[0]
    assert "--yolo" in _options(argv) and "-f" not in argv and "--force" not in argv
    await env.manager.set_mode("cursor", ref["id"], "readonly", conn=conn)
    await env.turn(conn, ref, "again")
    assert "--yolo" not in env.turns()[1] and "--mode" in env.turns()[1]
    ledger = [json.loads(line) for line in
              (env.home / "jarvis" / "ledger.jsonl").read_text().splitlines()]
    assert [(e["kind"], e["decision"]) for e in ledger] == [("unrestricted", "create")]


async def test_channel_never_releases_yolo_without_a_grant(env: Env, tmp_path: Path) -> None:
    exe = PinnedExecutable(str(FAKE_CURSOR))
    spec = LaunchSpec(cwd=str(env.project), project=str(env.project), executable=exe,
                      mode="readonly", session_id=str(uuid.uuid4()), resume=True)
    channel = CursorChannel(spec, object(), env.adapter)  # type: ignore[arg-type]
    for mode in ("readonly", "default"):
        channel.mode = mode
        argv = channel.argv("--yolo -f --force")
        assert argv[-2:] == ["--", "--yolo -f --force"]
        assert not {"--yolo", "-f", "--force"} & set(_options(argv))
    channel.mode = "unrestricted"
    with pytest.raises(ExternalError) as caught:
        channel.argv("x")
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    with pytest.raises(ExternalError):
        await channel.set_mode("unrestricted", None)
    channel.mode = "readonly"
    channel.spec.model = "--yolo"
    with pytest.raises(ExternalError):
        channel.argv("x")
    channel.spec.model = "-f"
    with pytest.raises(ExternalError):
        channel.argv("x")


@pytest.mark.parametrize("prompt", ["-f", "--yolo", "--resume other --mode agent", "--", "-"])
async def test_a_prompt_is_never_a_flag(env: Env, prompt: str) -> None:
    conn = Conn()
    ref = await env.create(conn)
    await env.turn(conn, ref, prompt)
    argv = env.turns()[0]
    assert argv[-2:] == ["--", prompt]
    assert _options(argv).count("--resume") == 1
    assert _options(argv)[_options(argv).index("--resume") + 1] == ref["id"]
    assert not {"--yolo", "-f"} & set(_options(argv))
    states = [p for m, p in env.hub.notes if m == "external.state"]
    assert states  # the turn ran and ended normally
    assert env.done(conn, next(e["turnId"] for e in conn.events()
                               if e["kind"] == "turn.started"))["payload"]["reason"] == (
        "complete")


async def test_long_prompt_is_refused_before_anything_runs(env: Env) -> None:
    conn = Conn()
    ref = await env.create(conn)
    with pytest.raises(ExternalError) as caught:
        await env.manager.send("cursor", ref["id"], "x" * (cur.MAX_PROMPT_BYTES + 1), conn=conn)
    assert caught.value.code == errors.INVALID_PARAMS and "too long" in caught.value.message
    assert env.turns() == []
    done = next(e for e in conn.events() if e["kind"] == "turn.done")
    assert done["payload"]["reason"] == "error"
    await env.turn(conn, ref, "short")  # the session is still usable


# -- interrupt, leftovers, failures -----------------------------------------------------------


async def test_interrupt_kills_the_tree_and_the_worker_but_not_the_owners(
    env: Env, tmp_path: Path
) -> None:
    env.scenario("hang")
    child_file, worker_file = tmp_path / "child.pid", tmp_path / "worker.pid"
    owners = subprocess.Popen(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(60)", "worker-server"],
        start_new_session=True,
    )
    try:
        with env_vars(FAKE_CLI_CHILD_PIDFILE=str(child_file),
                      FAKE_CURSOR_WORKER_PIDFILE=str(worker_file)):
            conn = Conn()
            ref = await env.create(conn)
            turn = (await env.manager.send("cursor", ref["id"], "hi", conn=conn))["turnId"]
        await until(lambda: "message.reasoning" in conn.kinds() and child_file.exists()
                    and worker_file.exists() and worker_file.read_text())
        fake = env.starts()[-1]["pid"]
        child, worker = int(child_file.read_text()), int(worker_file.read_text())
        assert proc.pid_alive(worker) and os.getpgid(worker) != os.getpgid(fake)
        await env.manager.interrupt("cursor", ref["id"])
        done = await until(lambda: env.done(conn, turn))
        assert done["payload"]["reason"] == "interrupted"
        await until(lambda: gone(fake) and gone(child) and gone(worker), timeout=10)
        assert owners.poll() is None  # the owner's own worker-server is untouched
        # The session is free for the next message.
        env.scenario("normal")
        await env.turn(conn, ref, "next")
    finally:
        owners.kill()
        owners.wait()


async def test_a_normal_turn_leaves_no_worker_behind(env: Env, tmp_path: Path) -> None:
    worker_file = tmp_path / "worker.pid"
    with env_vars(FAKE_CURSOR_WORKER_PIDFILE=str(worker_file)):
        conn = Conn()
        ref = await env.create(conn)
        await env.turn(conn, ref)
    worker = int(worker_file.read_text())
    await until(lambda: gone(worker), timeout=10)


async def test_a_cli_that_stays_up_after_its_result_is_stopped(env: Env) -> None:
    env.adapter.exit_grace_sec = 0.2
    env.scenario("linger")
    conn = Conn()
    ref = await env.create(conn)
    turn = await env.turn(conn, ref)
    assert env.done(conn, turn)["payload"]["reason"] == "complete"  # type: ignore[index]
    first = env.starts()[-1]["pid"]
    # The next message waits for the lingering process to be stopped, then runs.
    env.scenario("normal")
    turn = await env.turn(conn, ref, "again")
    assert env.done(conn, turn)["payload"]["reason"] == "complete"  # type: ignore[index]
    assert gone(first)


async def test_close_kills_a_running_turn(env: Env, tmp_path: Path) -> None:
    env.scenario("hang")
    worker_file = tmp_path / "worker.pid"
    with env_vars(FAKE_CURSOR_WORKER_PIDFILE=str(worker_file)):
        conn = Conn()
        ref = await env.create(conn)
        turn = (await env.manager.send("cursor", ref["id"], "hi", conn=conn))["turnId"]
    await until(lambda: "message.reasoning" in conn.kinds() and worker_file.exists()
                and worker_file.read_text())
    fake = env.starts()[-1]["pid"]
    await env.manager.close("cursor", ref["id"], conn=conn)
    done = await until(lambda: env.done(conn, turn))
    assert done["payload"]["reason"] == "interrupted"
    await until(lambda: gone(fake) and gone(int(worker_file.read_text())), timeout=10)


async def test_death_mid_turn_is_an_error_and_the_chat_continues(env: Env) -> None:
    env.scenario("die", reply="a longer reply")
    conn = Conn()
    ref = await env.create(conn)
    turn = await env.turn(conn, ref)
    done = env.done(conn, turn)
    assert done["payload"]["reason"] == "error"  # type: ignore[index]
    error = next(e["payload"] for e in conn.events() if e["kind"] == "error")
    assert "status 3" in error["message"]
    assert "message.delta" in conn.kinds()
    env.scenario("normal")
    turn = await env.turn(conn, ref, "again")
    assert env.done(conn, turn)["payload"]["reason"] == "complete"  # type: ignore[index]


async def test_a_hung_turn_is_stopped(env: Env) -> None:
    env.adapter.stall_sec = 1.0
    env.scenario("hang")
    conn = Conn()
    ref = await env.create(conn)
    turn = await env.turn(conn, ref)
    assert env.done(conn, turn)["payload"]["reason"] == "error"  # type: ignore[index]
    error = next(e["payload"] for e in conn.events() if e["kind"] == "error")
    assert "stopped answering" in error["message"]
    await until(lambda: gone(env.starts()[-1]["pid"]), timeout=10)


async def test_garbage_lines_are_skipped(env: Env) -> None:
    env.scenario("garbage")
    conn = Conn()
    ref = await env.create(conn)
    turn = await env.turn(conn, ref)
    assert env.done(conn, turn)["payload"]["reason"] == "complete"  # type: ignore[index]
    assert [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.done"] == ["OK"]


async def test_a_slow_stream_arrives_whole(env: Env) -> None:
    reply = "The quick brown fox jumps over the lazy dog."
    env.scenario("slow", reply=reply, delay=0.01)
    conn = Conn()
    ref = await env.create(conn)
    await env.turn(conn, ref)
    deltas = [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.delta"]
    assert len(deltas) > 5 and "".join(deltas) == reply
    assert [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.done"] == [reply]


async def test_error_result_ends_the_turn_with_an_error(env: Env) -> None:
    env.scenario("error")
    conn = Conn()
    ref = await env.create(conn)
    turn = await env.turn(conn, ref)
    assert env.done(conn, turn)["payload"]["reason"] == "error"  # type: ignore[index]
    assert any(e["kind"] == "error" and "model unavailable" in e["payload"]["message"]
               for e in conn.events())


async def test_logged_out_turn_says_so(env: Env) -> None:
    await env.manager.clis()  # discovery happens while logged in, and is cached
    conn = Conn()
    with env_vars(FAKE_CURSOR_LOGGED_OUT="1"):
        ref = await env.create(conn)
    turn = await env.turn(conn, ref)
    assert env.done(conn, turn)["payload"]["reason"] == "error"  # type: ignore[index]
    error = next(e["payload"] for e in conn.events() if e["kind"] == "error")
    assert error["message"] == CursorAdapter.LOGIN_HINT


async def test_resume_of_an_unknown_chat(env: Env) -> None:
    with pytest.raises(ExternalError) as caught:
        await env.manager.open("cursor", str(uuid.uuid4()), conn=Conn())
    assert caught.value.code == errors.NOT_FOUND
    spec = LaunchSpec(cwd=str(env.project), project=None,
                      executable=PinnedExecutable(str(FAKE_CURSOR)), mode="readonly",
                      session_id=str(uuid.uuid4()), resume=True)
    with pytest.raises(ExternalError) as caught:
        await CursorChannel(spec, object(), env.adapter).start()  # type: ignore[arg-type]
    assert caught.value.code == errors.NOT_FOUND


async def test_open_continues_a_stored_chat(env: Env) -> None:
    conn = Conn()
    ref = await env.create(conn)
    await env.turn(conn, ref, "first")
    await env.manager.close("cursor", ref["id"], conn=conn)
    viewer = Conn("tui")
    opened = await env.manager.open("cursor", ref["id"], conn=viewer)
    assert opened["session"]["host"] == "snowpea" and not opened["readOnly"]
    users = [e["payload"]["text"] for e in opened["events"] if e["kind"] == "message.user"]
    assert users == ["first"]
    await env.turn(viewer, ref, "second")
    assert env.turns()[-1][-1] == "second"


# -- liveness -------------------------------------------------------------------------------


def _holder(path: Path, *argv: str, env: dict[str, str] | None = None) -> subprocess.Popen:
    code = ("import sys, time\nf = open(sys.argv[1], 'rb')\nprint('ready', flush=True)\n"
            "while True: time.sleep(60)")
    child = subprocess.Popen([sys.executable, "-c", code, str(path), *argv],
                             stdout=subprocess.PIPE, env=env)
    assert child.stdout is not None and child.stdout.readline().strip() == b"ready"
    return child


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc scan")
async def test_open_elsewhere_is_view_only(env: Env) -> None:
    chat = str(uuid.uuid4())
    user = {"role": "user", "message": {"content": [{"type": "text", "text": "q"}]}}
    chat_dir = _store_chat(env, chat, env.project, [user])
    store = chat_dir / "store.db"
    store.write_bytes(b"")
    info = await env.adapter.get_session(chat)
    assert info is not None
    assert (await env.adapter.liveness(info, own_pids=set())).host == "none"
    # A worker-server and one of snowpea's own runs are not "somebody else".
    worker = _holder(store, "worker-server")
    tagged = _holder(store, env={**os.environ, cur.TAG_ENV: "cursor-abc"})
    try:
        await asyncio.sleep(cur._HOLDERS_TTL_SEC + 0.1)
        assert (await env.adapter.liveness(info, own_pids=set())).host == "none"
    finally:
        for child in (worker, tagged):
            child.kill()
            child.wait()
    terminal = _holder(store)
    try:
        await asyncio.sleep(cur._HOLDERS_TTL_SEC + 0.1)
        live = await env.adapter.liveness(info, own_pids=set())
        assert live.host == "terminal" and live.busy  # the transcript was just written
        assert (await env.adapter.liveness(info, own_pids={terminal.pid})).host == "none"
        rows = (await env.manager.list_sessions(clis=["cursor"], cwd=str(env.project)))
        row = next(r for r in rows["sessions"] if r["ref"]["id"] == chat)
        assert row["host"] == "terminal" and not row["canSend"]
        opened = await env.manager.open("cursor", chat, conn=Conn())
        assert opened["readOnly"]
        with pytest.raises(ExternalError) as caught:
            await env.manager.send("cursor", chat, "hi", conn=Conn())
        assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
    finally:
        terminal.kill()
        terminal.wait()
    assert env.turns() == []


async def test_tagged_pids_finds_only_the_tag(tmp_path: Path) -> None:
    sleeper = "import time\nwhile True: time.sleep(60)"
    mine = subprocess.Popen([sys.executable, "-c", sleeper],
                            env={**os.environ, cur.TAG_ENV: "cursor-mine"})
    other = subprocess.Popen([sys.executable, "-c", sleeper],
                             env={**os.environ, cur.TAG_ENV: "cursor-other"})
    try:
        if sys.platform.startswith("linux"):
            assert cur.tagged_pids({"cursor-mine"}) == {mine.pid}
        await cur.kill_pids({mine.pid: proc.process_start_time(mine.pid)}, grace=1.0)
        mine.wait(timeout=5)
        assert other.poll() is None
        # A reused pid (different start time) is never signalled.
        await cur.kill_pids({other.pid: "not-its-start-time"}, grace=0.1)
        assert other.poll() is None
    finally:
        for child in (mine, other):
            child.kill()
            child.wait()


async def test_no_store_means_not_open_anywhere(tmp_path: Path) -> None:
    info = SessionInfo(id="x", cwd=str(tmp_path))
    live = await CursorAdapter(cursor_home=tmp_path).liveness(info, own_pids=set())
    assert live.host == "none" and not live.busy


async def test_a_reread_history_does_not_repeat_snowpea_prompts(env: Env) -> None:
    conn = Conn()
    ref = await env.create(conn)
    await env.turn(conn, ref, "first prompt")
    await env.turn(conn, ref, "second prompt")
    record = env.manager._records[("cursor", ref["id"])]
    fresh = await env.adapter.get_session(ref["id"])
    assert fresh is not None
    await env.manager._publish(record, await env.adapter.read(fresh))
    users = [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.user"]
    assert users == ["first prompt", "second prompt"]
