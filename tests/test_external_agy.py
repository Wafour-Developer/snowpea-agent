"""M19 WP6c: the Antigravity CLI (``agy``) adapter against ``fake_agy.py``.

The manager drives the real :class:`AgyAdapter` exactly as the daemon does;
the fake speaks ACP after spike S3.  ``FAKE_CLI_STATE_DIR`` doubles as agy's
data folder (settings, summaries database, presence locks).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _external_adapter import FAKE_CLIS
from _support import env_vars, pinned_cli
from test_external_manager import Conn, Hub, until

from snowpea_core.config.settings import Settings
from snowpea_core.external import agy as agy_mod
from snowpea_core.external import proc
from snowpea_core.external.agy import (
    AgyAdapter,
    AgyChannel,
    history_items,
    indexed_items,
    read_indexed,
    read_summaries,
    version_problem,
)
from snowpea_core.external.base import SessionInfo
from snowpea_core.external.manager import ExternalManager
from snowpea_core.external.proc import ExternalError
from snowpea_core.external.registry import BUILTIN_ADAPTERS, AdapterRegistry
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.server import errors

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")

FAKE_AGY = FAKE_CLIS / "fake_agy.py"
RECORDINGS = Path(__file__).parent / "fixtures" / "external_recordings" / "agy"


def _settings(**cli: Any) -> Settings:
    return Settings.model_validate(
        {"jarvis": {"external": {"clis": {"agy": {**pinned_cli(FAKE_AGY), **cli}}}}}
    )


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.state = tmp_path / "agy-data"
        self.state.mkdir()
        self.project = (tmp_path / "proj").resolve()
        self.project.mkdir()
        self.other = (tmp_path / "other").resolve()
        self.other.mkdir()
        self.settings = _settings()
        self.hub = Hub()
        self.approvals = ApprovalQueue(self.settings, hub=self.hub)  # type: ignore[arg-type]
        self.log = tmp_path / "fake.log"
        self.adapter = AgyAdapter(data_dir=self.state)
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
        return [str(self.project), str(self.other)]

    def policy(self, value: str) -> None:
        (self.state / "settings.json").write_text(
            json.dumps({"toolPermission": value, "permissions": {"secret": "never shown"}})
        )

    def records(self, kind: str) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row["data"] for row in rows if row["kind"] == kind]

    def starts(self) -> list[dict[str, Any]]:
        """``agy acp`` starts (not the fake interactive terminals)."""
        return [start for start in self.records("start") if start["argv"][:1] == ["acp"]]

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [frame for frame in self.records("in") if frame.get("method") == method]


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    setup = Env(tmp_path)
    with env_vars(FAKE_CLI_LOG=str(setup.log), FAKE_CLI_SCENARIO=None,
                  FAKE_CLI_SCENARIO_FILE=None, FAKE_CLI_STATE_DIR=str(setup.state),
                  FAKE_AGY_POLICY=None, FAKE_CLI_REPLY=None):
        await setup.manager.start()
        try:
            yield setup
        finally:
            await setup.manager.shutdown()


async def _turn(env: Env, conn: Conn, ref: dict[str, str], text: str = "hello",
                reason: str = "complete") -> str:
    turn = (await env.manager.send(ref["cli"], ref["id"], text, conn=conn))["turnId"]
    done = await until(lambda: [e for e in conn.events()
                                if e["kind"] == "turn.done" and e["turnId"] == turn])
    assert done[0]["payload"]["reason"] == reason
    return turn


async def _create(env: Env, conn: Conn, **kwargs: Any) -> dict[str, Any]:
    return (await env.manager.create("agy", str(env.project), conn=conn, **kwargs))["session"]


def _of(conn: Conn, turn: str, kind: str) -> list[dict[str, Any]]:
    return [e for e in conn.events() if e["turnId"] == turn and e["kind"] == kind]


# -- registry, discovery, versions, models -------------------------------------------


def test_agy_is_a_builtin_adapter() -> None:
    assert [factory().id for factory in BUILTIN_ADAPTERS].count("agy") == 1
    adapter = AgyAdapter()
    assert adapter.capabilities.modes == ("default", "unrestricted")
    assert not adapter.capabilities.tmux


async def test_discovery_reads_the_owner_policy(env: Env) -> None:
    rows = {row["id"]: row for row in await env.manager.clis()}
    row = rows["agy"]
    assert row["installed"] and row["version"] == "1.5.7 (fake agy)" and row["problems"] == []
    assert row["approvalPolicy"] == "asks" and row["capabilities"]["approvals"] == "bridged"
    assert row["capabilities"]["modes"] == ["default", "unrestricted"]
    assert row["capabilities"]["tmux"] is False and row["defaultMode"] == "default"
    env.policy("always-proceed")
    row = {r["id"]: r for r in await env.manager.clis(refresh=True)}["agy"]
    assert row["approvalPolicy"] == "auto" and row["capabilities"]["approvals"] == "degraded"
    assert "never shown" not in json.dumps(row)
    found = await env.adapter.discover(str(FAKE_AGY))
    assert getattr(found, "tool_permission", None) == "always-proceed"
    env.policy("request-review")
    assert (await env.adapter.discover(str(FAKE_AGY))).approval_policy == "asks"
    (env.state / "settings.json").write_text("{not json")
    assert (await env.adapter.discover(str(FAKE_AGY))).approval_policy == "asks"


@pytest.mark.parametrize(("version", "problem"), [
    ("1.4.2", "too old"), ("2.0.1", "newer than this snowpea"), ("1.9.0", None),
])
async def test_versions_outside_the_supported_range(env: Env, tmp_path: Path, version: str,
                                                    problem: str | None) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"version": version}))
    with env_vars(FAKE_CLI_SCENARIO_FILE=str(scenario)):
        found = await env.adapter.discover(str(FAKE_AGY))
    if problem is None:
        assert found.problems == [] and found.usable
    else:
        assert problem in found.problems[0] and not found.usable


def test_version_problem_wording() -> None:
    assert version_problem("1.5.9") is None
    assert "agy update" in (version_problem("1.2.0") or "")
    assert version_problem(None) is None
    assert "could not read" in (version_problem("unknown") or "")


async def test_models(env: Env) -> None:
    found = await env.adapter.discover(str(FAKE_AGY))
    assert await env.adapter.list_models(found) == ["gemini-fake", "gemini-fake-low"]
    conn = Conn()
    row = await _create(env, conn, model="gemini-fake-low")
    assert row["model"] == "gemini-fake-low"
    assert env.sent("session/set_model")[-1]["params"]["modelId"] == "gemini-fake-low"
    with pytest.raises(ExternalError) as caught:
        await _create(env, conn, model="gpt-unknown")
    assert caught.value.code == errors.INVALID_PARAMS
    with pytest.raises(ExternalError):
        await _create(env, conn, model="--dangerously-skip-permissions")


async def test_set_model_switches_before_the_next_prompt(env: Env) -> None:
    conn = Conn()
    ref = (await _create(env, conn))["ref"]
    await _turn(env, conn, ref)
    assert env.sent("session/set_model") == []
    result = await env.manager.set_model("agy", ref["id"], "gemini-fake-low")
    assert result == {"ref": ref, "model": "gemini-fake-low"}
    assert env.sent("session/set_model") == []  # from the next turn
    assert env.hub.states()[-1]["model"] == "gemini-fake-low"
    await _turn(env, conn, ref, "again")
    switched = [f["params"]["modelId"] for f in env.sent("session/set_model")]
    assert switched == ["gemini-fake-low"]
    await _turn(env, conn, ref, "third")
    assert len(env.sent("session/set_model")) == 1  # switched once
    # null: back to the model the conversation had when snowpea attached.
    await env.manager.set_model("agy", ref["id"], None)
    await _turn(env, conn, ref, "fourth")
    assert env.sent("session/set_model")[-1]["params"]["modelId"] == "gemini-fake"
    with pytest.raises(ExternalError) as caught:
        await env.manager.set_model("agy", ref["id"], "gpt-unknown")
    assert caught.value.code == errors.INVALID_PARAMS


# -- create, stream, history -------------------------------------------------------


async def test_create_turn_and_history_replay(env: Env) -> None:
    conn = Conn("ide")
    row = await _create(env, conn)
    ref = row["ref"]
    assert row["host"] == "snowpea" and row["approvals"] == "bridged" and row["controlled"]
    assert row["mode"] == "default"
    turn = await _turn(env, conn, ref, "first")
    assert _of(conn, turn, "message.done")[0]["payload"]["text"] == "OK"
    assert _of(conn, turn, "message.reasoning")[0]["payload"]["text"] == "Thinking about it."
    assert len(_of(conn, turn, "message.user")) == 1  # the prompt echo is not repeated
    assert env.manager.opened()[0]["title"] == "first"
    await env.manager.close(ref["cli"], ref["id"], conn=conn)
    start = env.starts()[0]
    assert start["argv"] == ["acp"] and start["bypass"] == []
    assert not [name for name in start["env"] if name.startswith("SNOWPEA_")]
    assert "first" not in json.dumps(start["argv"])
    # A new viewer reads the history from session/load (no tokens), then continues it.
    viewer = Conn("tui")
    opened = await env.manager.open(ref["cli"], ref["id"], conn=viewer)
    kinds = [e["kind"] for e in opened["events"]]
    assert kinds == ["message.user", "message.reasoning", "message.done"]
    assert opened["events"][0]["payload"]["text"] == "first"
    assert opened["session"]["host"] == "snowpea" and not opened["readOnly"]
    await _turn(env, viewer, ref, "second")
    users = [e for e in opened["events"] + viewer.events() if e["kind"] == "message.user"]
    assert [u["payload"]["text"] for u in users] == ["first", "second"]  # no replay duplicates
    assert len([e for e in viewer.events() if e["kind"] == "message.done"]) == 1
    assert len(env.sent("session/load")) >= 2  # history helper + the channel's own load


def test_history_from_the_recorded_load() -> None:
    frames = [json.loads(line)["msg"] for line in
              (RECORDINGS / "acp_load.jsonl").read_text().splitlines()]
    updates = [f["params"]["update"] for f in frames if f.get("method") == "session/update"]
    updates.append({"sessionUpdate": "plan", "entries": []})  # unknown kinds are skipped
    items = history_items("336920a3-1e6f-4884-8b77-3f383e9e76a6", updates)
    kinds = [item.kind for item in items]
    assert kinds == ["message.user", "message.reasoning", "tool.call", "diff", "tool.result",
                     "message.done"]
    call = items[2].payload
    assert call["name"] == "write_file" and call["args"]["Path"].endswith("note.txt")
    assert items[3].payload["callId"] == call["callId"]
    assert "+hi" in items[3].payload["patch"]
    assert items[4].payload["ok"] is True
    again = history_items("336920a3-1e6f-4884-8b77-3f383e9e76a6", updates)
    assert [i.key for i in again] == [i.key for i in items]  # stable across reads


# -- permissions ----------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["allow", "deny"])
async def test_permission_allow_and_deny(env: Env, answer: str) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="approval"):
        ref = (await _create(env, conn))["ref"]
        turn = (await env.manager.send(ref["cli"], ref["id"], "run it", conn=conn))["turnId"]
        request = (await until(env.hub.pending))[0]
        assert request["external"] == {"cli": "agy", "id": ref["id"]}
        assert request["tool"] == "run_command"
        assert request["args"]["command"] == "echo hi > shell_allow.txt"
        await env.approvals.respond(request["requestId"], answer, conn=conn,
                                    reason="not now" if answer == "deny" else "")
        done = await until(lambda: _of(conn, turn, "turn.done"))
    result = _of(conn, turn, "tool.result")[0]["payload"]
    replies = [f for f in env.records("in") if "result" in f and isinstance(f.get("id"), str)]
    assert replies[0]["id"] == "1"  # the string id echoed verbatim
    if answer == "allow":
        assert done[0]["payload"]["reason"] == "complete" and result["ok"]
        assert (env.project / "shell_allow.txt").exists()
        assert _of(conn, turn, "message.done")[-1]["payload"]["text"] == "Ran the command."
    else:
        assert done[0]["payload"]["reason"] == "denied"
        assert not result["ok"] and "not now" in result["error"]
        assert not _of(conn, turn, "error")  # a clean end, not an error
        assert not (env.project / "shell_allow.txt").exists()
        assert env.manager.opened()[0]["status"] == "idle"


async def test_auto_policy_is_reported_honestly(env: Env) -> None:
    env.policy("always-proceed")
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="approval", FAKE_AGY_POLICY="auto"):
        row = await _create(env, conn)
        assert row["approvals"] == "none" and row["controlled"] is False
        turn = await _turn(env, conn, row["ref"], "run it")
    assert env.hub.pending() == []  # no bridge: agy never asked
    assert _of(conn, turn, "tool.result")[0]["payload"]["ok"]
    state = [s for s in env.hub.states() if s["ref"] == row["ref"]][-1]
    assert state["approvals"] == "none" and state["controlled"] is False


async def test_native_modes_are_agys_own_session_modes(env: Env) -> None:
    """1.56.0: agy's modes come from its --help; a mode is set with ACP session/set_mode
    (no restart), and what agy still asks in a mode is bridged as in default."""
    found = await env.manager.discover("agy", refresh=True)
    assert [m.id for m in env.adapter.native_modes(found)] == [
        "default", "accept-edits", "auto", "plan", "dangerously-skip-permissions"]
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="approval"):
        row = await _create(env, conn, native_mode="plan")
        assert (row["mode"], row["nativeMode"]) == ("default", "plan")
        assert [f["params"]["modeId"] for f in env.sent("session/set_mode")] == ["plan"]
        await _turn(env, conn, row["ref"], "plan it")
        assert env.hub.pending() == []  # the fake plans instead of running the command
        ref = row["ref"]
        switched = (await env.manager.set_mode("agy", ref["id"], native_mode="default",
                                               conn=conn))["session"]
        assert (switched["mode"], switched["nativeMode"]) == ("default", "default")
        turn = (await env.manager.send("agy", ref["id"], "run it", conn=conn))["turnId"]
        request = (await until(env.hub.pending))[0]
        assert request["tool"] == "run_command"
        await env.approvals.respond(request["requestId"], "deny", conn=conn, reason="no")
        await until(lambda: _of(conn, turn, "turn.done"))
    assert [f["params"]["modeId"] for f in env.sent("session/set_mode")] == ["plan", "default"]
    assert len(env.starts()) == 1  # switched in place
    # accept-edits and auto are the person's: never Jarvis or another in-process caller.
    for native in ("accept-edits", "auto", "dangerously-skip-permissions"):
        with pytest.raises(ExternalError) as caught:
            await _create(env, None, native_mode=native)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    with env_vars(FAKE_AGY_MODES="plan"):
        older = await env.manager.discover("agy", refresh=True)
    assert [m.id for m in env.adapter.native_modes(older)] == [
        "default", "plan", "dangerously-skip-permissions"]


# -- listing ----------------------------------------------------------------------------


async def test_list_by_working_directory_and_paging_over_acp(env: Env) -> None:
    conn = Conn()
    refs = []
    for text in ("one", "two", "three"):
        ref = (await _create(env, conn))["ref"]
        await _turn(env, conn, ref, text)
        await env.manager.close(ref["cli"], ref["id"], conn=conn)
        refs.append(ref)
        await asyncio.sleep(1.05)  # the fake's updatedAt has one-second resolution
    elsewhere = (await env.manager.create("agy", str(env.other), conn=conn))["session"]["ref"]
    await env.manager.close(elsewhere["cli"], elsewhere["id"], conn=conn)
    first = await env.manager.list_sessions(cwd=str(env.project), limit=2)
    assert [r["ref"]["id"] for r in first["sessions"]] == [refs[2]["id"], refs[1]["id"]]
    rest = await env.manager.list_sessions(cwd=str(env.project), limit=2,
                                           before=first["nextBefore"])
    assert [r["ref"]["id"] for r in rest["sessions"]] == [refs[0]["id"]]
    assert rest["nextBefore"] is None
    row = first["sessions"][0]
    assert row["title"] == "three" and row["host"] == "none" and row["canSend"]
    assert row["project"] == str(env.project)
    everything = await env.manager.list_sessions(all_projects=True, limit=10)
    assert len(everything["sessions"]) == 4
    assert await env.adapter.get_session("not-a-session") is None


def _summaries_db(path: Path, rows: list[dict[str, Any]], *, fts: list[tuple] | None = None,
                  extra_column: bool = False) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE conversation_summaries (conversation_id text, title text NOT NULL "
        "DEFAULT '', preview text NOT NULL DEFAULT '', step_count integer NOT NULL DEFAULT 0, "
        "last_modified_time datetime NOT NULL, workspace_uris text NOT NULL, status text "
        "NOT NULL DEFAULT '', nesting_depth integer NOT NULL DEFAULT 0, raw_summary blob"
        + (", future_column text" if extra_column else "") + ", PRIMARY KEY (conversation_id))"
    )
    for row in rows:
        db.execute(
            "INSERT INTO conversation_summaries (conversation_id, title, preview, step_count, "
            "last_modified_time, workspace_uris, nesting_depth) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["id"], row.get("title", ""), row.get("preview", ""), 4, row["time"],
             json.dumps([Path(p).as_uri() for p in row["folders"]]), row.get("depth", 0)),
        )
    db.execute(
        "CREATE TABLE conversations_fts (conversation_id, chunk_index, chunk_first_step, "
        "chunk_last_step, last_modified_time, step_count, last_step_created_at, archived, "
        "user_step_byte_offsets, agent_step_byte_offsets, title, workspace, project, "
        "user_body, agent_body)"
    )
    for entry in fts or []:
        db.execute(
            "INSERT INTO conversations_fts (conversation_id, chunk_index, "
            "user_step_byte_offsets, agent_step_byte_offsets, user_body, agent_body) "
            "VALUES (?, ?, ?, ?, ?, ?)", entry,
        )
    db.commit()
    db.close()


async def test_list_from_the_summaries_database(env: Env) -> None:
    project, sub = str(env.project), str(env.project / "pkg")
    (env.project / "pkg").mkdir()
    _summaries_db(env.state / "conversation_summaries.db", [
        {"id": "a1", "title": "Newest", "time": "2026-11-15 12:05:58.610161143+00:00",
         "folders": [project]},
        {"id": "a2", "title": "", "preview": "first prompt",
         "time": "2026-11-15 12:04:00.000000001+00:00", "folders": [sub]},
        {"id": "a3", "title": "Subagent", "time": "2026-11-15 12:06:00+00:00",
         "folders": [project], "depth": 1},
        {"id": "a4", "title": "Elsewhere", "time": "2026-11-15 12:07:00+00:00",
         "folders": [str(env.other)]},
        {"id": "a5", "title": "Second workspace", "time": "2026-11-15 12:01:00+00:00",
         "folders": ["/somewhere/else", project]},
        {"id": "bad/id", "title": "x", "time": "2026-11-15 12:00:00+00:00",
         "folders": [project]},
    ], extra_column=True)
    first = await env.manager.list_sessions(cwd=project, limit=2)
    assert [r["ref"]["id"] for r in first["sessions"]] == ["a1", "a2"]
    assert first["sessions"][0]["updatedAt"] == "2026-11-15T12:05:58.610Z"
    assert first["sessions"][1]["title"] is None
    assert first["sessions"][1]["preview"] == "first prompt"
    assert first["sessions"][1]["cwd"] == sub
    rest = await env.manager.list_sessions(cwd=project, limit=2, before=first["nextBefore"])
    assert [r["ref"]["id"] for r in rest["sessions"]] == ["a5"]
    assert rest["sessions"][0]["cwd"] == project
    assert not env.starts()  # no agy process for a database listing
    info = await env.adapter.get_session("a4")
    assert info is not None and info.cwd == str(env.other)


async def test_database_locked_or_unknown_shape(env: Env) -> None:
    path = env.state / "conversation_summaries.db"
    _summaries_db(path, [{"id": "a1", "title": "T", "time": "2026-11-15 12:05:58+00:00",
                          "folders": [str(env.project)]}])
    locker = sqlite3.connect(path, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        rows = await asyncio.to_thread(read_summaries, path)
        assert [r.id for r in rows] == ["a1"]  # immutable read while locked
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE conversation_summaries RENAME COLUMN workspace_uris TO uris")
    db.commit()
    db.close()
    # An unknown shape falls back to ACP session/list.
    conn = Conn()
    ref = (await _create(env, conn))["ref"]
    await _turn(env, conn, ref)
    listed = await env.manager.list_sessions(cwd=str(env.project))
    assert [r["ref"]["id"] for r in listed["sessions"]] == [ref["id"]]


# -- open elsewhere ------------------------------------------------------------------


async def test_presence_lock_view_only_then_takeover(env: Env) -> None:
    owner = Conn()
    ref = (await _create(env, owner))["ref"]
    await _turn(env, owner, ref, "before")
    await env.manager.close(ref["cli"], ref["id"], conn=owner)
    await owner.close()
    terminal = subprocess.Popen(
        [sys.executable, str(FAKE_AGY), "--fake-terminal", "--conversation", ref["id"]],
        stdin=subprocess.PIPE, env=proc.scrubbed_env({"FAKE_CLI_STATE_DIR": str(env.state)}),
    )
    try:
        await until(lambda: env.adapter.holders(ref["id"]))
        loads = len(env.sent("session/load"))
        listed = await env.manager.list_sessions(cwd=str(env.project))
        assert listed["sessions"][0]["host"] == "terminal"
        assert not listed["sessions"][0]["canSend"]
        viewer = Conn("tui")
        opened = await env.manager.open(ref["cli"], ref["id"], conn=viewer)
        assert opened["readOnly"] and opened["session"]["host"] == "terminal"
        assert opened["session"]["approvals"] == "none"
        assert [e["payload"]["role"] for e in opened["events"]] == ["system"]
        for call in (env.manager.send(ref["cli"], ref["id"], "hi", conn=viewer),
                     env.manager.set_mode(ref["cli"], ref["id"], "unrestricted", conn=viewer),
                     env.manager.interrupt(ref["cli"], ref["id"])):
            with pytest.raises(ExternalError) as caught:
                await call
            assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
        assert len(env.sent("session/load")) == loads  # not loaded behind the person's back
        assert not env.manager.supervisor.live()
        assert terminal.stdin is not None
        terminal.stdin.close()
        terminal.wait(timeout=10)
        state = await until(lambda: [s for s in env.hub.states()
                                     if s.get("previousHost") == "terminal"])
        assert state[-1]["host"] == "none" and state[-1]["canSend"]
        await until(lambda: any(e["kind"] == "message.user" for e in viewer.events()))
        await _turn(env, viewer, ref, "taking over")
        assert env.manager.opened()[0]["host"] == "snowpea"
    finally:
        if terminal.poll() is None:
            terminal.kill()
            terminal.wait()


async def test_channel_refuses_to_load_a_held_conversation(env: Env) -> None:
    conn = Conn()
    ref = (await _create(env, conn))["ref"]
    await _turn(env, conn, ref)
    await env.manager.close(ref["cli"], ref["id"], conn=conn)
    lock = env.state / "presence" / f"{ref['id']}.lock"
    terminal = subprocess.Popen(
        [sys.executable, str(FAKE_AGY), "--fake-terminal", "--conversation", ref["id"]],
        stdin=subprocess.PIPE, env=proc.scrubbed_env({"FAKE_CLI_STATE_DIR": str(env.state)}),
    )
    try:
        await until(lambda: env.adapter.holders(ref["id"]))
        assert lock.exists()
        found = await env.adapter.discover(str(FAKE_AGY))
        assert found.executable is not None
        from snowpea_core.external.base import LaunchSpec

        supervisor = env.manager.supervisor

        class _Host:
            env = proc.scrubbed_env()

        _Host.supervisor = supervisor  # type: ignore[attr-defined]

        channel = AgyChannel(env.adapter, LaunchSpec(
            cwd=str(env.project), project=str(env.project), executable=found.executable,
            mode="default", session_id=ref["id"], resume=True), _Host())  # type: ignore[arg-type]
        with pytest.raises(ExternalError) as caught:
            await channel.start()
        assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
        assert not env.manager.supervisor.live()
    finally:
        assert terminal.stdin is not None
        terminal.stdin.close()
        terminal.wait(timeout=10)


async def test_view_from_the_search_index_matches_the_full_history(env: Env) -> None:
    sid = "c0ffee00-1111-2222-3333-444455556666"
    user, agent = "Create note.txt\n", "\nI created note.txt.\n"
    _summaries_db(
        env.state / "conversation_summaries.db",
        [{"id": sid, "title": "Note", "time": "2026-11-15 12:00:00+00:00",
          "folders": [str(env.project)]}],
        fts=[(sid, -1, "", "", "", ""),
             (sid, 0, b"[[0,0]]", b"[[1,0],[3,1]]", user, agent)],
    )
    steps = read_indexed(env.state / "conversation_summaries.db", sid)
    assert steps == [(0, "user", user), (1, "agent", "\n"), (3, "agent", agent[1:])]
    indexed = indexed_items(sid, steps or [])
    assert [(i.kind, i.payload["text"]) for i in indexed] == [
        ("message.user", "Create note.txt"), ("message.done", "I created note.txt.")]
    replay = [
        {"sessionUpdate": "user_message_chunk", "content": {"type": "text",
                                                            "text": "Create note.txt"}},
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}},
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text",
                                                             "text": "I created note.txt."}},
    ]
    full = history_items(sid, replay)
    shared = {i.key for i in indexed} & {i.key for i in full}
    assert len(shared) == 2  # the takeover re-read does not repeat the view
    # While held, read() and follow() use the index and never start agy.
    terminal = subprocess.Popen(
        [sys.executable, str(FAKE_AGY), "--fake-terminal", "--conversation", sid],
        stdin=subprocess.PIPE, env=proc.scrubbed_env({"FAKE_CLI_STATE_DIR": str(env.state)}),
    )
    try:
        await until(lambda: env.adapter.holders(sid))
        info = SessionInfo(id=sid, cwd=str(env.project))
        assert [i.key for i in await env.adapter.read(info)] == [i.key for i in indexed]
        follow = env.adapter.follow(info, interval=0.01)
        assert [i.key for i in await follow.__anext__()] == [i.key for i in indexed]
        await follow.aclose()
        assert not env.starts()
    finally:
        assert terminal.stdin is not None
        terminal.stdin.close()
        terminal.wait(timeout=10)


# -- interrupt, modes ----------------------------------------------------------------------


async def test_cancel_mid_stream(env: Env) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="slow", FAKE_CLI_DELAY="0.05",
                  FAKE_CLI_REPLY="counting " * 40):
        ref = (await _create(env, conn))["ref"]
        turn = (await env.manager.send(ref["cli"], ref["id"], "count", conn=conn))["turnId"]
        await until(lambda: _of(conn, turn, "message.delta"))
        await env.manager.interrupt(ref["cli"], ref["id"])
        done = await until(lambda: _of(conn, turn, "turn.done"), timeout=5)
    assert done[0]["payload"]["reason"] == "interrupted"
    assert env.sent("session/cancel")[0]["params"]["sessionId"] == ref["id"]
    assert env.manager.opened()[0]["host"] == "snowpea"  # the process keeps serving
    await _turn(env, conn, ref, "again")


async def test_unrestricted_only_with_a_grant(env: Env) -> None:
    for conn in (None, Conn("browser")):
        with pytest.raises(ExternalError) as caught:
            await env.manager.create("agy", str(env.project), mode="unrestricted", conn=conn)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    with pytest.raises(ExternalError) as caught:
        await env.manager.create("agy", str(env.project), mode="readonly", conn=Conn())
    assert caught.value.code == errors.INVALID_PARAMS  # agy has no read-only mode
    person = Conn("web")
    row = await _create(env, person)
    ref = row["ref"]
    await _turn(env, person, ref)
    assert all(start["bypass"] == [] for start in env.starts())
    with pytest.raises(ExternalError) as caught:
        await env.manager.set_mode(ref["cli"], ref["id"], "unrestricted", conn=None)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    switched = (await env.manager.set_mode(ref["cli"], ref["id"], "unrestricted",
                                           conn=person))["session"]
    assert switched["controlled"] is False and switched["approvals"] == "none"
    await _turn(env, person, ref, "with the flag")
    assert env.starts()[-1]["bypass"] == ["--dangerously-skip-permissions"]
    assert env.sent("session/load")  # the restart continued the conversation
    await env.manager.set_mode(ref["cli"], ref["id"], "default", conn=person)
    await _turn(env, person, ref, "without it")
    assert env.starts()[-1]["bypass"] == []
    created = await _create(env, Conn("tui"), mode="unrestricted")
    assert created["mode"] == "unrestricted" and env.starts()[-1]["bypass"] == [
        "--dangerously-skip-permissions"]


# -- robustness ------------------------------------------------------------------------


async def test_death_mid_turn_then_continue(env: Env, tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"turns": ["die"]}))
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO_FILE=str(scenario)):
        ref = (await _create(env, conn))["ref"]
        turn = (await env.manager.send(ref["cli"], ref["id"], "hi", conn=conn))["turnId"]
        done = await until(lambda: _of(conn, turn, "turn.done"))
        assert done[0]["payload"]["reason"] == "error"
        assert "status 3" in _of(conn, turn, "error")[0]["payload"]["message"]
        state = await until(lambda: [s for s in env.hub.states() if s["status"] == "error"])
        assert state[-1]["host"] == "none"
        assert not env.manager.supervisor.live()
        scenario.write_text(json.dumps({"turns": ["normal"]}))
        await _turn(env, conn, ref, "again")
    assert env.sent("session/load")[-1]["params"]["sessionId"] == ref["id"]
    assert env.manager.opened()[0]["host"] == "snowpea"


async def test_hang_is_stopped_on_interrupt_and_close(env: Env,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AgyChannel, "cancel_grace", 0.3)
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO="hang"):
        ref = (await _create(env, conn))["ref"]
        turn = (await env.manager.send(ref["cli"], ref["id"], "hi", conn=conn))["turnId"]
        await until(lambda: _of(conn, turn, "message.reasoning"))
        pid = env.manager.supervisor.live()[0].pid
        await env.manager.interrupt(ref["cli"], ref["id"])
        done = await until(lambda: _of(conn, turn, "turn.done"), timeout=10)
        assert done[0]["payload"]["reason"] == "interrupted"
        await until(lambda: not proc.pid_alive(pid), timeout=10)
        assert env.hub.states()[-1]["host"] == "none"
        # close() on a hung process kills it too.
        turn = (await env.manager.send(ref["cli"], ref["id"], "again", conn=conn))["turnId"]
        await until(lambda: _of(conn, turn, "message.reasoning"))
        pid = env.manager.supervisor.live()[0].pid
        await env.manager.close(ref["cli"], ref["id"], conn=conn)
    assert not proc.pid_alive(pid) and not env.manager.supervisor.live()


@pytest.mark.parametrize("scenario", ["garbage", "slow"])
async def test_garbage_and_slow_streams(env: Env, scenario: str) -> None:
    conn = Conn()
    with env_vars(FAKE_CLI_SCENARIO=scenario, FAKE_CLI_DELAY="0.01",
                  FAKE_CLI_REPLY="a slow and steady reply"):
        ref = (await _create(env, conn))["ref"]
        turn = await _turn(env, conn, ref)
    text = _of(conn, turn, "message.done")[0]["payload"]["text"]
    assert text == "a slow and steady reply"
    if scenario == "slow":
        assert len(_of(conn, turn, "message.delta")) > 10


async def test_not_signed_in(env: Env) -> None:
    with env_vars(FAKE_CLI_SCENARIO="noauth"):
        with pytest.raises(ExternalError) as caught:
            await _create(env, Conn())
    assert caught.value.code == errors.EXTERNAL_UNAVAILABLE
    assert "agy acp --login" in caught.value.message
    assert caught.value.details == {"needsLogin": True, "hint": "agy acp --login"}
    await until(lambda: not env.manager.supervisor.live())


def test_session_errors_that_mean_not_signed_in() -> None:
    assert agy_mod._session_error(agy_mod.AcpError(-32603, "Failed to start Antigravity: x"),
                                  "start").details == {"needsLogin": True,
                                                       "hint": "agy acp --login"}
    other = agy_mod._session_error(agy_mod.AcpError(-32602, "session not found"), "load it")
    assert other.details is None and "session not found" in other.message


async def test_a_reread_history_does_not_repeat_snowpea_prompts(env: Env) -> None:
    conn = Conn()
    ref = (await _create(env, conn))["ref"]
    await _turn(env, conn, ref, "first prompt")
    await _turn(env, conn, ref, "second prompt")
    record = env.manager._records[("agy", ref["id"])]
    await env.manager.close("agy", ref["id"])  # agy loads (replays) only a free session
    env.manager._records[("agy", ref["id"])] = record
    await env.manager._publish(record, await env.adapter.read(record.info))
    users = [e["payload"]["text"] for e in conn.events() if e["kind"] == "message.user"]
    assert users == ["first prompt", "second prompt"]


async def test_agent_requests_are_refused_and_permissions_bounded(env: Env) -> None:
    """Security review 2 (agy ACP): fs/terminal refused, own session only, bounded asks."""
    conn = Conn()
    ref = (await _create(env, conn))["ref"]
    channel = env.manager._records[("agy", ref["id"])].channel
    assert isinstance(channel, AgyChannel)
    acp = channel.conn
    assert acp is not None
    replies: list[tuple[Any, Any]] = []
    errors_: list[tuple[Any, int]] = []

    async def reply(rid: Any, result: Any) -> None:
        replies.append((rid, result))

    async def reply_error(rid: Any, code: int, message: str) -> None:
        errors_.append((rid, code))

    acp.reply = reply  # type: ignore[method-assign]
    acp.reply_error = reply_error  # type: ignore[method-assign]
    for method in ("fs/read_text_file", "fs/write_text_file", "terminal/create",
                   "terminal/output", "session/something_new"):
        await channel._on_request({"jsonrpc": "2.0", "id": method, "method": method,
                                   "params": {"sessionId": channel.session_id, "path": "/x"}})
    assert [code for _rid, code in errors_] == [-32601] * 5
    started: list[Any] = []
    gate = asyncio.Event()

    async def slow(conn_: Any, rid: Any, params: Any) -> None:
        started.append(rid)
        await gate.wait()

    channel._permission = slow  # type: ignore[method-assign]
    # Another session's permission: cancelled, never asked.
    await channel._on_request({"id": "x1", "method": "session/request_permission",
                               "params": {"sessionId": "someone-else"}})
    assert replies == [("x1", {"outcome": {"outcome": "cancelled"}})]
    for index in range(agy_mod.MAX_OPEN_PERMISSIONS + 2):
        await channel._on_request({"id": index, "method": "session/request_permission",
                                   "params": {"sessionId": channel.session_id}})
    await channel._on_request({"id": 0, "method": "session/request_permission",
                               "params": {"sessionId": channel.session_id}})  # reused
    await asyncio.sleep(0.05)
    assert started == list(range(agy_mod.MAX_OPEN_PERMISSIONS))
    assert [rid for rid, _ in replies[1:]] == [agy_mod.MAX_OPEN_PERMISSIONS,
                                               agy_mod.MAX_OPEN_PERMISSIONS + 1]
    gate.set()
    await asyncio.sleep(0.05)
