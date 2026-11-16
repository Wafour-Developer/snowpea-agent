"""M19 core integration: external CLI sessions under Jarvis.

Every daemon runs on a throwaway home with a scripted provider and the fake
Claude Code CLI (``tests/fixtures/fake_clis/fake_claude.py``) behind the real
Claude adapter; no model and no real CLI is called.  The real adapters for the
other CLIs are unregistered so nothing installed on this machine runs.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _external_adapter import FAKE_CLAUDE
from _support import connect, env_vars, make_daemon, pinned_cli

from snowpea_core.external.claude import ClaudeAdapter
from snowpea_core.jarvis import tools as jarvis_tools
from snowpea_core.jarvis.delegation import Caller
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage
from snowpea_core.session.history import message_text
from snowpea_core.tools.registry import ToolContext

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")

PERSON = Caller(kind="connection", surface="ide")


def _settings(tmp_path: Path, **jarvis: Any) -> dict[str, Any]:
    return {
        "jarvis": {
            "folder": str(tmp_path / "JarvisFolder"),
            "external": {"clis": {"claude": {**pinned_cli(FAKE_CLAUDE)}}},
            **jarvis,
        }
    }


def _hide(project: Path) -> None:
    folder = project / ".snowpea"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(json.dumps({"jarvis": {"expose": False}}))


class _Quiet:
    vendor = "fake"
    model = "fake-1"

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=5, output_tokens=1))
        yield StreamEvent(kind="text_delta", text="ok")
        yield StreamEvent(kind="done", stop_reason="end_turn")


class Person:
    """A human connection as the external manager sees one."""

    client_kind = "ide"
    client_id = "ide-1"
    authenticated = True
    closed = False

    def __init__(self) -> None:
        self.on_close: list[Any] = []
        self.frames: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.frames.append((method, params))


def _only_the_fake(core: Any) -> ClaudeAdapter:
    """No real CLI runs in these tests: the fake Claude behind the real adapter only."""
    from snowpea_core.server.external_handlers import ensure_manager

    registry = ensure_manager(core).registry
    for cli in registry.ids():
        registry.unregister(cli)
    adapter = ClaudeAdapter(interrupt_grace=1.0, init_timeout=10.0)
    registry.register(adapter)
    return adapter


async def _daemon(tmp_path: Path, provider: Any = None, **jarvis: Any) -> Any:
    daemon = await make_daemon(tmp_path / "home", _settings(tmp_path, enabled=True, **jarvis))
    script = provider or _Quiet()
    daemon.core.providers.get = lambda _p, _m: script
    _only_the_fake(daemon.core)
    return daemon


def _ctx(core: Any, *, human: bool = False) -> ToolContext:
    jarvis = core.jarvis.session()
    assert jarvis is not None
    turn = "t-human" if human else "t-wake"
    jarvis.current_turn = turn
    jarvis.turn_origins[turn] = "human" if human else "system"
    return ToolContext(session=jarvis, core=core, backend=None)  # type: ignore[arg-type]


async def _call(core: Any, name: str, args: dict[str, Any], *, human: bool = False) -> Any:
    tool = core.jarvis.session().session_tools[name]
    return await tool.run(_ctx(core, human=human), args)


async def _wait(predicate: Any, timeout: float = 10.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def _project(core: Any, path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    await core.sessions.create(path, mode="accept")  # registers the project
    return str(path.resolve())


def _scenario(tmp_path: Path, **config: Any) -> dict[str, str]:
    path = tmp_path / f"scenario-{len(list(tmp_path.glob('scenario-*')))}.json"
    path.write_text(json.dumps(config))
    return {"FAKE_CLI_SCENARIO_FILE": str(path), "FAKE_CLI_SCENARIO": None,
            "FAKE_CLI_LOG": str(tmp_path / "fake.log")}


def _key(row: dict[str, Any]) -> str:
    return f"ext:{row['ref']['cli']}:{row['ref']['id']}"


async def _turn_done(core: Any, ref: dict[str, str], turn: str) -> None:
    record = core.external._records[(ref["cli"], ref["id"])]
    await _wait(lambda: any(e["kind"] == "turn.done" and e["turnId"] == turn
                            for e in record.events), 20)


# -- routing, overview, tools ----------------------------------------------------------


async def _step(what: str, awaitable: Any, timeout: float = 60.0) -> Any:
    """``awaitable`` with a bound: a stuck step fails here by name instead of
    hanging until the suite's 120 s timeout (which names no line)."""
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except TimeoutError as exc:
        raise AssertionError(f"{what} did not finish within {timeout:.0f}s") from exc


async def test_external_approvals_are_routed_by_the_engine(tmp_path: Path) -> None:
    # Load-sensitive before review 9: the second send could reach the session
    # while the first (denied) turn was still ending — the approval is gone
    # from the list before the CLI's result arrives — and was answered
    # session_busy (or steered into that turn).  Each turn is now awaited to
    # its end, the waits are generous, and every daemon call is bounded.
    with env_vars(**_scenario(tmp_path, scenario="approval")):
        daemon = await _daemon(tmp_path)
        try:
            core = daemon.core
            root = await _project(core, tmp_path / "proj")
            manager = core.external
            row = (await _step("external.create", manager.create("claude", root)))["session"]
            ref, key = row["ref"], _key(row)
            # Staged (the default): a write in the project is the person's.
            first = (await _step("the first send", manager.send(
                "claude", ref["id"], "write the note",
                origin={"kind": "jarvis", "surface": "jarvis"})))["turnId"]
            request = (await _wait(lambda: core.approvals.list(key), 30))[0]
            assert request.route == "person" and request.external.id == ref["id"]
            refused = await _step("approval_decide (refused)", _call(
                core, "approval_decide", {"requestId": request.requestId, "decision": "allow"}))
            assert not refused.ok and "not yours to decide" in refused.error
            overview = [r for r in core.jarvis.overview_rows() if r.external is not None]
            assert overview[0].pendingApprovals == 1 and overview[0].status == "awaiting_approval"
            assert overview[0].waitingOnPerson == 1 and overview[0].host == "snowpea"
            await _step("the person's deny",
                        core.approvals.respond(request.requestId, "deny", conn=Person()))
            await _wait(lambda: not core.approvals.list(key), 30)
            await _turn_done(core, ref, first)  # the denied turn has ended
            # A full grant on the project: the next one is Jarvis's, once.
            core.jarvis.engine.set_grant("project", root, "full", [], caller=PERSON)
            turn = (await _step("the second send", manager.send(
                "claude", ref["id"], "write it now",
                origin={"kind": "jarvis", "surface": "jarvis"})))["turnId"]
            assert turn != first
            request = (await _wait(lambda: core.approvals.list(key), 30))[0]
            assert request.route == "jarvis"
            allowed = await _step("approval_decide (allowed)", _call(
                core, "approval_decide", {"requestId": request.requestId, "decision": "allow"}))
            assert allowed.ok, allowed.error
            await _turn_done(core, ref, turn)
            assert (Path(root) / "note.txt").read_text() == "hi\n"
            decisions, _ = core.jarvis.engine.ledger.decisions(decided_by="jarvis")
            approval = next(d for d in decisions if d.kind == "approval")
            assert approval.external is not None and approval.external.id == ref["id"]
            assert approval.sessionId is None and approval.decision == "allow"
        finally:
            await daemon.stop()


async def test_overview_and_list_report_external_sessions_honestly(tmp_path: Path) -> None:
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        root = await _project(core, tmp_path / "proj")
        hidden = await _project(core, tmp_path / "hidden")
        _hide(Path(hidden))
        manager = core.external
        person = Person()
        free = (await manager.create("claude", root))["session"]
        wild = (await manager.create("claude", root, mode="unrestricted", conn=person))["session"]
        secret = (await manager.create("claude", hidden))["session"]
        rows = {r.external.id: r for r in core.jarvis.overview_rows() if r.external}
        assert set(rows) == {free["ref"]["id"], wild["ref"]["id"]}  # the hidden one is not
        assert rows[free["ref"]["id"]].controlled is True
        assert rows[free["ref"]["id"]].approvals == "bridged"
        assert rows[wild["ref"]["id"]].controlled is False
        assert rows[wild["ref"]["id"]].mode == "unrestricted"
        listed = (await _call(core, "session_list", {})).output
        assert _key(wild) in listed and "UNCONTROLLED" in listed
        assert _key(secret) not in listed
        # Jarvis never sends into an unrestricted session, even when asked.
        refused = await _call(core, "external_send",
                              {"sessionId": _key(wild), "text": "go"}, human=True)
        assert not refused.ok and "unrestricted" in refused.error
        refused = await _call(core, "external_read", {"sessionId": _key(secret)})
        assert not refused.ok and "no external session" in refused.error
        table = core.jarvis.overview_rows()
        from snowpea_core.jarvis import overview

        rendered = overview.render_table(core.jarvis, table, len(table))
        assert f"ext:claude:{wild['ref']['id']}" in rendered and "UNCONTROLLED" in rendered
    finally:
        await daemon.stop()


async def test_jarvis_tools_for_external_sessions(tmp_path: Path) -> None:
    with env_vars(FAKE_CLI_SCENARIO="normal", FAKE_CLI_SCENARIO_FILE=None,
                  FAKE_CLI_LOG=str(tmp_path / "fake.log")):
        daemon = await _daemon(tmp_path)
        try:
            core = daemon.core
            root = await _project(core, tmp_path / "proj")
            created = await _call(core, "session_create",
                                  {"project": root, "cli": "claude", "mode": "unrestricted"},
                                  human=True)
            assert not created.ok and "unrestricted" in created.error
            created = await _call(core, "session_create",
                                  {"project": root, "cli": "claude", "mode": "readonly",
                                   "prompt": "look around"}, human=True)
            assert created.ok, created.error
            key = created.meta["sessionId"]
            cli, ext_id = key.split(":")[1:]
            row = (await core.external.get(cli, ext_id))["session"]
            assert row["mode"] == "readonly" and row["host"] == "snowpea"
            record = core.external._records[(cli, ext_id)]
            await _wait(lambda: any(e["kind"] == "turn.done" for e in record.events), 20)
            # The prompt went in with Jarvis's origin and prefix.
            user = next(e for e in record.events if e["kind"] == "message.user")
            assert user["payload"]["origin"]["kind"] == "jarvis"
            assert user["payload"]["text"].startswith(jarvis_tools.PROMPT_PREFIX)
            wrong = await _call(core, "session_prompt", {"sessionId": key, "text": "x"},
                                human=True)
            assert not wrong.ok and "external_send" in wrong.error
            listed = await _call(core, "external_list", {})
            assert key in listed.output and "<untrusted" in listed.output
            read = await _call(core, "external_read", {"sessionId": key})
            assert read.ok and "<untrusted" in read.output and "look around" in read.output
            # Outside the person's request and without delegated work: refused.
            refused = await _call(core, "external_send", {"sessionId": key, "text": "more"})
            assert not refused.ok and "delegated" in refused.error
            refused = await _call(core, "external_send",
                                  {"sessionId": key, "text": "/model opus"}, human=True)
            assert not refused.ok and "slash" in refused.error
            sent = await _call(core, "external_send", {"sessionId": key, "text": "and more"},
                               human=True)
            assert sent.ok, sent.error
            await _wait(lambda: sum(e["kind"] == "turn.done" for e in record.events) == 2, 20)
            stopped = await _call(core, "external_interrupt", {"sessionId": key}, human=True)
            assert stopped.ok
            ledger = [json.loads(line) for line in
                      (Path(daemon.paths.home) / "jarvis" / "ledger.jsonl").read_text()
                      .splitlines()]
            access = [line for line in ledger if line.get("type") == "access"]
            assert {line["action"] for line in access} >= {"create", "read", "prompt",
                                                           "interrupt"}
            assert all(line["external"] == {"cli": cli, "id": ext_id} for line in access)
        finally:
            await daemon.stop()


async def test_a_persons_send_pauses_jarvis_there(tmp_path: Path) -> None:
    with env_vars(FAKE_CLI_SCENARIO="normal", FAKE_CLI_SCENARIO_FILE=None,
                  FAKE_CLI_LOG=str(tmp_path / "fake.log")):
        daemon = await _daemon(tmp_path)
        try:
            core = daemon.core
            root = await _project(core, tmp_path / "proj")
            row = (await core.external.create("claude", root))["session"]
            ref, key = row["ref"], _key(row)
            core.jarvis.engine.set_grant("project", root, "full", [], caller=PERSON)
            turn = (await core.external.send("claude", ref["id"], "from jarvis",
                                             origin={"kind": "jarvis",
                                                     "surface": "jarvis"}))["turnId"]
            await _turn_done(core, ref, turn)
            assert not core.jarvis.engine.pause_state(external=ref).paused
            turn = (await core.external.send("claude", ref["id"], "my own words",
                                             conn=Person()))["turnId"]
            await _turn_done(core, ref, turn)
            await _wait(lambda: core.jarvis.engine.pause_state(external=ref).paused)
            status = await core.jarvis.status()
            assert any(p.external is not None and p.external.id == ref["id"]
                       for p in status.paused)
            refused = await _call(core, "external_send", {"sessionId": key, "text": "go on"},
                                  human=True)
            assert not refused.ok and "working in that session" in refused.error
        finally:
            await daemon.stop()


# -- the Jarvis session's own limits -------------------------------------------------------


async def test_jarvis_reads_only_its_folder_and_exposed_projects(tmp_path: Path) -> None:
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        root = Path(await _project(core, tmp_path / "proj"))
        (root / "main.py").write_text("print('hi')\n")
        (root / ".env").write_text("TOKEN=x\n")
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "notes.txt").write_text("private\n")
        await core.jarvis.projects.roots()
        jarvis = core.jarvis.session()
        guard = jarvis_tools.guard_call
        assert guard(core, jarvis, "read_file", "read", {"path": str(root / "main.py")}) is None
        assert guard(core, jarvis, "read_file", "read", {"path": "reports/x.md"}) is None
        assert guard(core, jarvis, "grep", "read", {"pattern": "x"}) is None  # its own folder
        assert "outside" in guard(core, jarvis, "read_file", "read",
                                  {"path": str(outside / "notes.txt")})
        assert "outside" in guard(core, jarvis, "glob", "read",
                                  {"pattern": "*", "path": "/etc"})
        assert "secrets" in guard(core, jarvis, "read_file", "read", {"path": str(root / ".env")})
        assert guard(core, jarvis, "read_file", "read",
                     {"path": str(Path(daemon.paths.home) / "settings.json")}) is not None
    finally:
        await daemon.stop()


async def test_the_jarvis_session_mode_is_pinned(http: aiohttp.ClientSession,
                                                 tmp_path: Path) -> None:
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        jarvis = core.jarvis.session()
        mode = jarvis.mode
        person = await connect(http, daemon)
        refused = await person.call("session.setMode",
                                    {"sessionId": jarvis.id, "mode": "auto"})
        assert refused["error"]["data"]["code"] == "invalid_params"
        assert jarvis.mode == mode
        from snowpea_core.session.manager import ModePinned

        with pytest.raises(ModePinned):
            await core.sessions.set_mode(jarvis, "plan")
        await core.sessions.set_mode(jarvis, mode)  # the same mode is fine
    finally:
        await daemon.stop()


# -- end to end ---------------------------------------------------------------------------


class _Scripted:
    """Jarvis on a scripted provider: what it does is driven by what it is told.

    * the person asks "start claude": ``session_create {cli: claude, prompt}``;
    * a wake lists an approval: ``approval_decide`` allow;
    * the person asks "push it": ``external_send`` into the external session.
    """

    vendor = "fake"
    model = "fake-1"

    def __init__(self, project: str) -> None:
        self.project = project
        self.key = ""
        self.calls = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.calls += 1
        last = messages[-1]
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=5, output_tokens=1))
        if last.role == "tool":
            yield StreamEvent(kind="text_delta", text="done")
            yield StreamEvent(kind="done", stop_reason="end_turn")
            return
        said = message_text(last)
        call: ToolCall | None = None
        wanted = re.findall(r"approval (ap-[0-9a-f]+)", said)
        if wanted:
            call = ToolCall(id=f"c{self.calls}", name="approval_decide",
                            arguments={"requestId": wanted[0], "decision": "allow"})
        elif "start claude" in said:
            call = ToolCall(id=f"c{self.calls}", name="session_create",
                            arguments={"project": self.project, "cli": "claude",
                                       "prompt": "write the note"})
        elif "push it" in said:
            call = ToolCall(id=f"c{self.calls}", name="external_send",
                            arguments={"sessionId": self.key, "text": "push it"})
        if call is not None:
            yield StreamEvent(kind="tool_call", tool_call=call)
            yield StreamEvent(kind="done", stop_reason="tool_use")
        else:
            yield StreamEvent(kind="text_delta", text="noted")
            yield StreamEvent(kind="done", stop_reason="end_turn")


async def test_end_to_end_jarvis_drives_an_external_claude_session(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    root = str(project.resolve())
    script = _Scripted(root)
    scenario = _scenario(tmp_path, turns=["approval", "approval", "normal"],
                         tools=["write", "bash"], command="git push origin main")
    with env_vars(**scenario):
        daemon = await make_daemon(tmp_path / "home", _settings(tmp_path))
        try:
            core = daemon.core
            core.providers.get = lambda _p, _m: script
            _only_the_fake(core)
            person = await connect(http, daemon, approval_mode="deny")
            assert (await person.ok("jarvis.enable", {}))["running"]
            await person.ok("workspace.add", {"path": root})
            # The person hands the project over to Jarvis (full; no guarded class).
            await person.ok("jarvis.delegation.set",
                            {"scope": "project", "target": root, "level": "full", "allow": []})
            jarvis_id = core.jarvis.session_id

            # 1. Jarvis creates an external Claude session through its tool and sends a
            #    prompt; the CLI asks to write a file; the engine routes it to Jarvis,
            #    which allows it once; the turn completes.
            await person.ok("session.prompt", {"sessionId": jarvis_id, "text": "start claude"})
            opened = await _wait(lambda: core.external.opened(), 20)
            ref = opened[0]["ref"]
            script.key = f"ext:claude:{ref['id']}"
            record = core.external._records[("claude", ref["id"])]
            await _wait(lambda: any(e["kind"] == "turn.done" for e in record.events), 40)
            assert (project / "note.txt").read_text() == "hi\n"
            resolved = [f for f in person.of_method("approval.resolved") if f["by"] == "jarvis"]
            assert resolved and resolved[0]["decision"] == "allow"
            decisions, _ = core.jarvis.engine.ledger.decisions(decided_by="jarvis")
            approval = next(d for d in decisions if d.kind == "approval")
            assert approval.external is not None and approval.external.id == ref["id"]
            assert any(f.get("external") == ref for f in person.of_method("jarvis.access"))

            # 2. A guarded command from the CLI goes to the person, whatever the grant.
            jarvis = core.jarvis.session()
            await _wait(lambda: jarvis.turn_task is None or jarvis.turn_task.done(), 20)
            await person.ok("session.prompt", {"sessionId": jarvis_id, "text": "push it"})
            key = script.key
            request = (await _wait(lambda: core.approvals.list(key), 20))[0]
            assert request.route == "person"
            assert request.args["command"] == "git push origin main"
            escalation = await person.wait_notification("jarvis.escalation")
            assert escalation["requestId"] == request.requestId
            await asyncio.sleep(3.0)  # a wake batch would have run by now
            assert core.approvals.get(request.requestId) is not None  # Jarvis did not answer
            await person.ok("approval.respond", {"requestId": request.requestId,
                                                 "decision": "deny", "scope": "once"})
            await _wait(lambda: sum(e["kind"] == "turn.done" for e in record.events) == 2, 20)

            # 3. The person writes into the external session themselves: Jarvis pauses there.
            await person.ok("external.send", {"ref": ref, "text": "I'll take it from here"})
            await _wait(lambda: core.jarvis.engine.pause_state(external=ref).paused, 20)
            paused = (await person.ok("jarvis.status"))["paused"]
            assert [p["external"] for p in paused] == [ref]
            await _wait(lambda: [f for f in person.of_method("jarvis.state")
                                 if f["reason"] == "paused"], 10)
        finally:
            await daemon.stop()


async def test_a_person_only_ask_is_routed_to_the_person(tmp_path: Path) -> None:
    from snowpea_core.external.manager import _ProxySession
    from snowpea_core.server.protocol import ApprovalRequest

    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        root = await _project(core, tmp_path / "proj")
        core.jarvis.engine.set_grant("project", root, "full", [], caller=PERSON)
        proxy = _ProxySession("claude", "abc", root, root, "t")
        base = {"requestId": "ap-1", "sessionId": proxy.id, "tool": "Write",
                "args": {"file_path": f"{root}/x.txt", "content": "x"}, "risk": "medium",
                "timeoutSec": 0, "external": {"cli": "claude", "id": "abc"}}
        assert core.jarvis.route_approval(ApprovalRequest.model_validate(base), proxy) == "jarvis"
        adopt = {**base, "requestId": "ap-2", "tool": "tmux.adopt", "personOnly": True,
                 "args": {"session": "mine", "pane": "%1"}}
        assert core.jarvis.route_approval(ApprovalRequest.model_validate(adopt), proxy) == "person"
        assert core.jarvis.engine.routed("ap-2").reason == "person-only"
    finally:
        await daemon.stop()
