"""M7 US-019: subagents, the concurrency limit and the ``subagent.*`` events.

Everything here runs against a real in-process daemon with the scripted fake
provider, so the assertions describe what a TUI or the SDK would actually see:
the events on the parent session, the child session while it is alive, and the
``agent.list`` snapshot that ``snowpea agents --json`` prints.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _support import OriginConn, Recorder

from snowpea_core.agent.definition import AgentDefinition, write_definition
from snowpea_core.agent.subagent import (
    DONE,
    ERROR,
    QUEUED,
    RUNNING,
    SUBAGENT_KIND,
    SubagentManager,
    SubagentRecord,
    _ChildWatcher,
    get_manager,
)
from snowpea_core.config.project import ModelProfile
from snowpea_core.permissions.approval_queue import Decision
from snowpea_core.providers.base import StreamEvent, ToolCall
from snowpea_core.server import errors
from snowpea_core.server.app_server import Core, Daemon
from snowpea_core.tools.delegate import render_report

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "subagents.json"
TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    instance = Daemon(port=0, home=home)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project


async def open_session(core: Core, workdir: Path, **kwargs: Any) -> Any:
    session = await core.sessions.create(workdir, mode="auto", **kwargs)
    return session


# ---------------------------------------------------------------------------
# the concurrency limit
# ---------------------------------------------------------------------------


async def test_max_concurrent_is_never_exceeded(daemon: Daemon, workdir: Path) -> None:
    """Four delegations, a limit of two: never three running at once."""
    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir, max_concurrent=2)
    manager = get_manager(core)
    assert manager.limit_for(session) == 2

    peak = 0
    samples: list[int] = []
    stop = asyncio.Event()

    async def sample() -> None:
        nonlocal peak
        while not stop.is_set():
            running = sum(1 for record in manager.records() if record.status == RUNNING)
            samples.append(running)
            peak = max(peak, running)
            await asyncio.sleep(0.01)

    watcher = asyncio.ensure_future(sample())
    results = await asyncio.wait_for(
        asyncio.gather(*(manager.run(session, f"slow child {index}") for index in range(4))),
        timeout=TIMEOUT,
    )
    stop.set()
    await watcher

    assert all(result.ok for result in results), [r.error for r in results]
    assert peak == 2, f"expected at most 2 running, peaked at {peak}; samples={samples}"
    assert max(samples) >= 2, "the two slots should both have been busy at some point"
    assert all(record.status == DONE for record in manager.records())


async def test_the_limit_comes_from_the_session(daemon: Daemon, workdir: Path) -> None:
    """``session.create(maxConcurrent)`` beats project and global settings."""
    core = daemon.core
    assert core is not None
    default_session = await open_session(core, workdir)
    override = await open_session(core, workdir, max_concurrent=1)
    manager = get_manager(core)

    assert manager.limit_for(default_session) == core.settings.agents.max_concurrent
    assert manager.limit_for(override) == 1

    peak = 0
    stop = asyncio.Event()

    async def sample() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, sum(1 for r in manager.records() if r.status == RUNNING))
            await asyncio.sleep(0.01)

    watcher = asyncio.ensure_future(sample())
    await asyncio.wait_for(
        asyncio.gather(*(manager.run(override, f"slow child {i}") for i in range(3))),
        timeout=TIMEOUT,
    )
    stop.set()
    await watcher
    assert peak == 1


# ---------------------------------------------------------------------------
# the events on the parent
# ---------------------------------------------------------------------------


async def test_parent_sees_spawn_update_done_in_order(daemon: Daemon, workdir: Path) -> None:
    """One delegation: spawn, at least one update, then done — seq increasing."""
    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    result = await asyncio.wait_for(
        get_manager(core).run(session, "quick child for the event test"), timeout=TIMEOUT
    )
    assert result.ok

    kinds = recorder.kinds()
    assert kinds[0] == "subagent.spawn", kinds
    assert kinds[-1] == "subagent.done", kinds
    assert "subagent.update" in kinds

    seqs = [event["seq"] for event in recorder.events]
    assert seqs == sorted(seqs), seqs
    assert len(set(seqs)) == len(seqs), "every event on the parent needs its own seq"
    assert {event["sessionId"] for event in recorder.events} == {session.id}

    spawn = recorder.of_kind("subagent.spawn")[0]["payload"]
    assert spawn["agentId"] == result.agent_id
    assert spawn["status"] == QUEUED
    assert spawn["task"] == "quick child for the event test"

    updates = [event["payload"] for event in recorder.of_kind("subagent.update")]
    assert updates[0]["status"] == RUNNING
    assert any(update["lastText"] for update in updates)

    done = recorder.of_kind("subagent.done")[0]["payload"]
    assert done["agentId"] == result.agent_id
    assert done["status"] == DONE
    assert done["ok"] is True
    assert "child finished" in done["summary"]
    assert done["usage"]["outputTokens"] >= 0
    assert done["sessionId"] == result.session_id
    assert "rounds" in done and isinstance(done["rounds"], int)
    assert "budget" in done and isinstance(done["budget"], int)


async def test_child_events_flow_on_the_child_session(daemon: Daemon, workdir: Path) -> None:
    """The child's own turn is visible to anybody watching every session."""
    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    everything = Recorder()
    core.hub.subscribe(everything, None)

    result = await asyncio.wait_for(
        get_manager(core).run(session, "quick child watched from outside"), timeout=TIMEOUT
    )

    child_events = [e for e in everything.events if e["sessionId"] == result.session_id]
    assert [e["kind"] for e in child_events][-1] == "turn.done"
    assert "message.done" in [e["kind"] for e in child_events]
    parent_kinds = [e["kind"] for e in everything.events if e["sessionId"] == session.id]
    assert parent_kinds[0] == "subagent.spawn"


async def test_a_childs_token_stream_does_not_become_parent_events() -> None:
    """The parent's panel is refreshed per message, never per token.

    ``subagent.update`` carries the child's last text into the parent's agent
    panel, and every one of them is a repaint in whatever surface is watching.
    A child streams a ``message.delta`` per token, so republishing those would
    put a hundred events a second on the parent session — which is what made
    the TUI's agent panel flicker.  The watcher therefore reacts to whole
    messages and to tool calls; a ``usage`` event is one per model round, so
    it may republish the meter (that is what shows a quiet child is alive).
    """
    record = SubagentRecord(
        agent_id="a1", name="executor", task="stream a lot", parent_session_id="parent"
    )

    class _CountingManager:
        def __init__(self) -> None:
            self.updates: list[str] = []

        async def emit_update(self, rec: SubagentRecord, *, last_text: str = "") -> None:
            self.updates.append(last_text)

    manager = _CountingManager()
    watcher = _ChildWatcher(manager, record)  # type: ignore[arg-type]

    for index in range(200):
        await watcher.notify(
            "session.event", {"kind": "message.delta", "payload": {"text": f"tok{index} "}}
        )
    assert manager.updates == [], "a delta must not reach the parent"

    await watcher.notify(
        "session.event", {"kind": "usage", "payload": {"inputTokens": 7, "outputTokens": 9}}
    )
    assert manager.updates == [""], "usage moves the meter without a new last line"
    assert record.usage() == {"inputTokens": 7, "outputTokens": 9}
    manager.updates.clear()

    await watcher.notify(
        "session.event", {"kind": "tool.call", "payload": {"name": "read_file"}}
    )
    await watcher.notify(
        "session.event", {"kind": "message.done", "payload": {"text": "all 200 tokens"}}
    )
    assert manager.updates == ["calling read_file", "all 200 tokens"]
    assert record.summary == "all 200 tokens"


# ---------------------------------------------------------------------------
# agent definitions
# ---------------------------------------------------------------------------


async def test_definition_sets_the_prompt_and_narrows_the_tools(
    daemon: Daemon, workdir: Path
) -> None:
    """``agent="reader"`` applies the definition to the child session."""
    core = daemon.core
    assert core is not None
    write_definition(
        AgentDefinition(
            name="reader",
            description="Reads, never writes.",
            tools=["read_file", "list_dir"],
            permission="plan",
            prompt="You are the reader. You only ever read files.",
        ),
        workdir,
    )
    session = await open_session(core, workdir)
    manager = get_manager(core)
    assert manager.definition(session, "reader") is not None

    seen: dict[str, Any] = {}
    runner = asyncio.ensure_future(
        manager.run(session, "slow child under a definition", agent="reader")
    )

    # While the child is alive, look at the session the definition built.
    child = None
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while child is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
        for record in manager.records():
            if record.session_id:
                child = core.sessions.get(record.session_id)
        if child is not None:
            seen = {
                "mode": child.mode,
                "allowed_tools": set(child.allowed_tools or ()),
                "system_prompt": child.system_prompt,
                "parent": child.parent_session_id,
                "agent": child.agent,
                "workdir": child.workdir,
            }
    result = await asyncio.wait_for(runner, timeout=TIMEOUT)

    assert result.ok
    assert seen, "the child session should have been observable while it ran"
    assert seen["allowed_tools"] == {"read_file", "list_dir"}
    assert seen["system_prompt"] == "You are the reader. You only ever read files."
    assert seen["mode"] == "plan", "permission: plan in the definition wins over the parent"
    assert seen["parent"] == session.id
    assert seen["agent"] == "reader"
    assert seen["workdir"] == session.workdir


async def test_builtin_agent_name_resolves_and_applies_role_prompt(
    daemon: Daemon, workdir: Path
) -> None:
    """``agent="executor"`` works without a project agent file and adds its role."""
    from snowpea_core.agent import agent as agent_prompt

    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    manager = get_manager(core)
    defn = manager.definition(session, "executor")
    assert defn is not None
    assert defn.source == "builtin"
    assert defn.prompt == ""

    seen: dict[str, Any] = {}
    runner = asyncio.ensure_future(
        manager.run(session, "slow child under the builtin executor", agent="executor")
    )

    child = None
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while child is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
        for record in manager.records():
            if record.session_id:
                child = core.sessions.get(record.session_id)
        if child is not None:
            prompt = agent_prompt.build_system_prompt(child, [], core=core)
            seen = {
                "agent": child.agent,
                "allowed_tools": child.allowed_tools,
                "prompt_role": child.prompt_role,
                "system_prompt": child.system_prompt,
                "prompt": prompt,
            }
    result = await asyncio.wait_for(runner, timeout=TIMEOUT)

    assert result.ok
    assert seen, "the built-in executor child should have been observable"
    assert seen["agent"] == "executor"
    assert seen["allowed_tools"] is None
    assert seen["prompt_role"] == "executor"
    assert seen["system_prompt"] is None
    assert "Role: executor." in seen["prompt"]


async def test_explorer_defaults_to_read_only_tools_unless_declared(
    daemon: Daemon, workdir: Path
) -> None:
    core = daemon.core
    assert core is not None
    parent = await open_session(core, workdir)
    manager = get_manager(core)

    builtin = manager.definition(parent, "explorer")
    assert builtin is not None and builtin.source == "builtin"
    child = await open_session(core, workdir)
    manager._apply_definition(child, builtin, None)
    assert child.allowed_tools == {"read_file", "glob", "grep"}
    assert "shell" not in (child.allowed_tools or set())

    write_definition(
        AgentDefinition(
            name="explorer",
            description="project explorer with shell",
            tools=["read_file", "glob", "grep", "shell"],
            prompt="",
        ),
        workdir,
    )
    project = manager.definition(parent, "explorer")
    assert project is not None and project.source == "project"
    child = await open_session(core, workdir)
    manager._apply_definition(child, project, None)
    assert child.allowed_tools is not None
    assert "shell" in child.allowed_tools


async def test_a_narrowed_child_cannot_reach_other_tools(daemon: Daemon, workdir: Path) -> None:
    """``tools=[...]`` really refuses the tools it leaves out."""
    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    everything = Recorder()
    core.hub.subscribe(everything, None)

    result = await asyncio.wait_for(
        get_manager(core).run(session, "restricted child", tools=["read_file"]),
        timeout=TIMEOUT,
    )

    refusals = [
        event["payload"]
        for event in everything.of_kind("tool.result")
        if event["payload"]["name"] == "shell"
    ]
    assert refusals, f"the child should have tried the shell; saw {everything.kinds()}"
    assert refusals[0]["ok"] is False
    assert "allowed-tools" in refusals[0]["error"]
    assert result.ok


async def test_a_denied_tool_call_with_a_final_answer_stays_ok(
    daemon: Daemon, workdir: Path
) -> None:
    class DeniedThenFinalProvider:
        vendor = "test-denied"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(
            self, messages: list, tools: list, **kwargs: Any
        ) -> AsyncIterator[StreamEvent]:
            del messages, tools, kwargs
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(
                    kind="tool_call",
                    tool_call=ToolCall(
                        id="call_1", name="shell", arguments={"command": "rm -rf build"}
                    ),
                )
                yield StreamEvent(kind="done", stop_reason="tool_use")
                return
            yield StreamEvent(kind="text_delta", text="final answer from read-only fallback")
            yield StreamEvent(kind="done", stop_reason="end_turn")

    core = daemon.core
    assert core is not None
    provider = DeniedThenFinalProvider()
    core.providers.get = lambda _provider, _model: provider  # type: ignore[assignment]
    async def deny(*args: Any, **kwargs: Any) -> Decision:
        del args, kwargs
        return Decision("deny", "once", "test", errors.APPROVAL_DENIED)
    core.approvals.request = deny  # type: ignore[method-assign]
    session = await core.sessions.create(workdir, mode="accept")

    result = await asyncio.wait_for(
        get_manager(core).run(session, "denied once then finish"), TIMEOUT
    )
    assert result.ok is True
    assert result.reason == "complete"
    assert result.denied_tools == ["shell"]
    assert "1 tool call was denied: shell" in render_report(result)


async def test_a_child_that_ends_on_denials_still_fails(daemon: Daemon, workdir: Path) -> None:
    class AlwaysDeniedProvider:
        vendor = "test-denied"

        async def stream(
            self, messages: list, tools: list, **kwargs: Any
        ) -> AsyncIterator[StreamEvent]:
            del messages, tools, kwargs
            yield StreamEvent(
                kind="tool_call",
                tool_call=ToolCall(
                    id="call_1",
                    name="shell",
                    arguments={"command": "rm -rf build"},
                ),
            )
            yield StreamEvent(kind="done", stop_reason="tool_use")

    core = daemon.core
    assert core is not None
    core.providers.get = lambda _provider, _model: AlwaysDeniedProvider()  # type: ignore[assignment]
    async def deny(*args: Any, **kwargs: Any) -> Decision:
        del args, kwargs
        return Decision("deny", "once", "test", errors.APPROVAL_DENIED)
    core.approvals.request = deny  # type: ignore[method-assign]
    session = await core.sessions.create(workdir, mode="accept")

    result = await asyncio.wait_for(get_manager(core).run(session, "deny until stopped"), TIMEOUT)
    assert result.ok is False
    assert result.reason == "denied"
    assert "stopped after a denied call" in (result.error or "")


async def test_an_unknown_agent_name_is_refused(daemon: Daemon, workdir: Path) -> None:
    """An explicit typo must not silently become an untyped delegation."""
    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    result = await asyncio.wait_for(
        get_manager(core).run(session, "quick child", agent="nobody-defined-this"),
        timeout=TIMEOUT,
    )
    assert result.ok is False
    assert result.error == "unknown agent 'nobody-defined-this'"


async def test_active_team_restricts_and_defaults_delegation(
    daemon: Daemon, workdir: Path
) -> None:
    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    session.team = "delivery"
    session.team_agents = ("executor", "verifier")
    refused = await get_manager(core).run(session, "inspect", agent="architect")
    assert refused.ok is False
    assert "not in active team 'delivery'" in str(refused.error)
    automatic = await get_manager(core).run(session, "implement")
    assert automatic.ok
    assert get_manager(core).records()[-1].name == "executor"


# ---------------------------------------------------------------------------
# the tool, the RPC and the listing
# ---------------------------------------------------------------------------


async def test_delegate_task_is_active_and_returns_the_child_report(
    daemon: Daemon, workdir: Path
) -> None:
    """The M1 stub is gone: the model can delegate and read the answer back."""
    from snowpea_core.agent import loop as agent_loop

    core = daemon.core
    assert core is not None
    tool = core.tools.get("delegate_task")
    assert tool is not None and tool.state == "active"
    assert tool.permission == "delegate"

    session = await open_session(core, workdir)
    recorder = Recorder()
    core.hub.subscribe(recorder, None)
    await asyncio.wait_for(agent_loop.run_turn(core, session, "delegate please"), timeout=TIMEOUT)

    results = [
        event["payload"]
        for event in recorder.of_kind("tool.result")
        if event["payload"]["name"] == "delegate_task"
    ]
    assert results, f"delegate_task was never called; saw {recorder.kinds()}"
    assert results[0]["ok"] is True
    assert "child finished its quick task" in results[0]["output"]
    assert recorder.of_kind("subagent.done")


async def test_empty_task_is_refused(daemon: Daemon, workdir: Path) -> None:
    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    result = await get_manager(core).run(session, "   ")
    assert not result.ok
    assert result.status == ERROR
    assert "non-empty" in (result.error or "")


async def test_agent_list_reports_running_subagents(daemon: Daemon, workdir: Path) -> None:
    """What ``snowpea agents --json`` polls during a ralph run (AC-04)."""
    from snowpea_core.server.agent_handlers import agent_list_handler

    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir, max_concurrent=2)
    manager = get_manager(core)

    runner = asyncio.ensure_future(
        asyncio.gather(*(manager.run(session, f"slow child {i}") for i in range(2)))
    )
    best = 0
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while not runner.done() and asyncio.get_running_loop().time() < deadline:
        listing = await agent_list_handler(OriginConn(session), None, core)  # type: ignore[arg-type]
        rows = [row for row in listing.agents if row.kind == SUBAGENT_KIND]
        best = max(best, sum(1 for row in rows if row.status == RUNNING))
        for row in rows:
            assert row.parentSessionId == session.id
            assert row.agentId
        await asyncio.sleep(0.01)
    await asyncio.wait_for(runner, timeout=TIMEOUT)

    assert best >= 2, f"expected two subagents running at once, saw {best}"
    # Finished children drop out of the listing; the records survive for the CLI.
    listing = await agent_list_handler(OriginConn(session), None, core)  # type: ignore[arg-type]
    assert [row for row in listing.agents if row.kind == SUBAGENT_KIND] == []
    assert len(manager.records()) == 2


async def test_agent_spawn_rpc_answers_with_an_id(daemon: Daemon, workdir: Path) -> None:
    """``agent.spawn`` returns immediately and reports through the events."""
    from snowpea_core.server.agent_handlers import agent_spawn_handler
    from snowpea_core.server.protocol import AgentSpawnParams

    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    result = await asyncio.wait_for(
        agent_spawn_handler(
            OriginConn(session),  # type: ignore[arg-type]
            AgentSpawnParams(name="", task="quick child from the rpc", sessionId=session.id),
            core,
        ),
        timeout=TIMEOUT,
    )
    assert result.agentId.startswith("a-")

    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while not recorder.of_kind("subagent.done") and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    done = recorder.of_kind("subagent.done")
    assert done, f"the spawned agent never reported; saw {recorder.kinds()}"
    assert done[0]["payload"]["agentId"] == result.agentId


async def test_a_manager_is_created_once_per_core(daemon: Daemon) -> None:
    core = daemon.core
    assert core is not None
    first = get_manager(core)
    assert isinstance(first, SubagentManager)
    assert get_manager(core) is first


# ---------------------------------------------------------------------------
# /ultrawork and /deepinit
# ---------------------------------------------------------------------------


async def test_ultrawork_splits_fans_out_and_merges(daemon: Daemon, workdir: Path) -> None:
    """The split comes from the model; the parallelism comes from the daemon."""
    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    await asyncio.wait_for(
        core.commands.run(core, session, "ultrawork", '"do two separable things"'),
        timeout=TIMEOUT,
    )

    assert len(recorder.of_kind("subagent.spawn")) == 2
    assert len(recorder.of_kind("subagent.done")) == 2
    merged = "\n".join(str(event["payload"]["text"]) for event in recorder.of_kind("message.done"))
    assert "T1 first half" in merged
    assert "T2 second half" in merged
    assert merged.count("child finished its quick task") == 2


async def test_ultrawork_without_a_task_explains_itself(daemon: Daemon, workdir: Path) -> None:
    from snowpea_core.commands import ultrawork

    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    await asyncio.wait_for(core.commands.run(core, session, "ultrawork", ""), timeout=TIMEOUT)
    assert ultrawork.USAGE in str(recorder.of_kind("message.done")[-1]["payload"]["text"])


def test_deepinit_picks_the_directories_worth_documenting(tmp_path: Path) -> None:
    from snowpea_core.commands import deepinit

    root = tmp_path / "tree"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("a", encoding="utf-8")
    (root / "src" / "b.py").write_text("b", encoding="utf-8")
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "index.js").write_text("x", encoding="utf-8")
    (root / "node_modules" / "dep" / "other.js").write_text("y", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / "thin").mkdir()
    (root / "thin" / "only.txt").write_text("one file", encoding="utf-8")
    (root / "README.md").write_text("readme", encoding="utf-8")

    assert [path.name for path in deepinit.interesting_dirs(root)] == ["src"]
    assert "Map the code under src" in deepinit.map_dir_task(root / "src", root)
    assert "Do NOT write" in deepinit.map_dir_task(root / "src", root)
    assert "src/AGENTS.md" in deepinit.write_dir_task(root / "src", root, "notes here")
    assert "Explore notes" in deepinit.write_dir_task(root / "src", root, "notes here")
    assert "write_file now" in deepinit.write_dir_task(root / "src", root, "", retry=True)
    assert "- src/AGENTS.md" in deepinit.root_task(root, [root / "src"])


def test_deepinit_root_task_with_summaries(tmp_path: Path) -> None:
    from snowpea_core.commands import deepinit

    root = tmp_path / "tree"
    documented = [(root / "src", "Core source code containing modules.")]
    task = deepinit.root_task(root, documented)
    assert "- src/AGENTS.md: Core source code containing modules." in task
    assert "- src/AGENTS.md" in task
    retry = deepinit.root_task(root, documented, retry=True)
    assert "Previous attempt did not create AGENTS.md" in retry


async def test_deepinit_falls_back_when_model_skips_write(
    daemon: Daemon, workdir: Path
) -> None:
    """Model attempts (plus manager incomplete retry) still leave no file → stub."""
    from snowpea_core.agent.subagent import SubagentResult, get_manager
    from snowpea_core.commands.deepinit import FALLBACK_MARKER

    core = daemon.core
    assert core is not None
    root = workdir / "tree"
    src = root / "src"
    src.mkdir(parents=True)
    (src / "a.py").write_text("a\n", encoding="utf-8")
    (src / "b.py").write_text("b\n", encoding="utf-8")

    async def fake_run(_parent: object, brief: str, **kwargs: object) -> SubagentResult:
        title = str(kwargs.get("title") or "")
        if title.startswith("explore"):
            return SubagentResult(
                agent_id="a-map",
                ok=True,
                summary="src holds the core package.",
                reason="complete",
                name="explorer",
            )
        return SubagentResult(
            agent_id="a-1",
            ok=True,
            summary="Stopped after budget without writing.",
            reason="budget",
            name="executor",
        )

    manager = get_manager(core)
    manager.run = fake_run  # type: ignore[method-assign]

    session = await open_session(core, root)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    await asyncio.wait_for(core.commands.run(core, session, "deepinit", ""), timeout=TIMEOUT)

    assert (src / "AGENTS.md").is_file()
    assert (root / "AGENTS.md").is_file()
    assert FALLBACK_MARKER in (src / "AGENTS.md").read_text(encoding="utf-8")
    assert FALLBACK_MARKER in (root / "AGENTS.md").read_text(encoding="utf-8")
    said = "\n".join(
        str(event["payload"]["text"]) for event in recorder.of_kind("message.done")
    )
    assert "fallback" in said
    assert "wrote 2 file(s)" in said
    assert "did not get written" not in said


async def test_deepinit_explore_then_write_passes_notes(
    daemon: Daemon, workdir: Path
) -> None:
    """Explorer maps; executor receives those notes in the write brief."""
    from snowpea_core.agent.subagent import SubagentResult, get_manager

    core = daemon.core
    assert core is not None
    root = workdir / "tree"
    src = root / "src"
    src.mkdir(parents=True)
    (src / "a.py").write_text("a\n", encoding="utf-8")
    (src / "b.py").write_text("b\n", encoding="utf-8")

    briefs: list[str] = []

    async def fake_run(_parent: object, brief: str, **kwargs: object) -> SubagentResult:
        briefs.append(brief)
        title = str(kwargs.get("title") or "")
        agent = kwargs.get("agent") or kwargs.get("prefer")
        if title.startswith("explore"):
            return SubagentResult(
                agent_id="a-map",
                ok=True,
                summary="MAP-NOTE: src is the Python core.",
                name="explorer",
            )
        if title.startswith("write src"):
            (src / "AGENTS.md").write_text("# src\n\nfrom map\n", encoding="utf-8")
            return SubagentResult(
                agent_id="a-write", ok=True, summary="Wrote src.", name="executor"
            )
        if "outline" in title:
            return SubagentResult(
                agent_id="a-outline", ok=True, summary="ROOT-OUTLINE", name="architect"
            )
        if title.startswith("write AGENTS"):
            (root / "AGENTS.md").write_text("# root\n", encoding="utf-8")
            return SubagentResult(
                agent_id="a-root", ok=True, summary="Wrote root.", name="executor"
            )
        return SubagentResult(agent_id="a-x", ok=False, summary="", error=f"unexpected {title} {agent}")

    manager = get_manager(core)
    manager.run = fake_run  # type: ignore[method-assign]

    session = await open_session(core, root)
    # Give the session a default-team-like roster so picks resolve.
    session.team_agents = ("architect", "executor", "explorer", "critic")
    await asyncio.wait_for(core.commands.run(core, session, "deepinit", ""), timeout=TIMEOUT)

    assert (src / "AGENTS.md").is_file()
    assert (root / "AGENTS.md").is_file()
    write_briefs = [b for b in briefs if "Write src/AGENTS.md" in b or "write src/AGENTS.md" in b.lower() or "Explore notes:" in b]
    assert any("MAP-NOTE: src is the Python core." in b for b in write_briefs)


def test_deepinit_fallback_helpers_are_deterministic(tmp_path: Path) -> None:
    from snowpea_core.commands import deepinit

    root = tmp_path / "proj"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "main.py").write_text("x\n", encoding="utf-8")
    (pkg / "util.py").write_text("y\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo\n\nA demo project.\n", encoding="utf-8")

    path, summary = deepinit.write_dir_fallback(pkg, root)
    assert path == pkg / "AGENTS.md"
    assert path.is_file()
    assert "main.py" in path.read_text(encoding="utf-8")
    assert "Fallback stub" in summary

    documented = [(pkg, summary)]
    root_path = deepinit.write_root_fallback(root, documented, [pkg])
    assert root_path.is_file()
    text = root_path.read_text(encoding="utf-8")
    assert deepinit.FALLBACK_MARKER in text
    assert "Demo" in text or "demo project" in text.lower()
    assert "pkg/AGENTS.md" in text


def test_deepinit_picks_team_writing_roles() -> None:
    """Map prefers explorer; write prefers executor — not the other way around."""
    from snowpea_core.commands import deepinit

    class _Defn:
        def __init__(self, name: str) -> None:
            self.name = name

    class _Manager:
        def __init__(self, names: set[str]) -> None:
            self.names = names

        def definition(self, _session: object, name: str | None) -> object | None:
            return _Defn(name) if name in self.names else None

    class _Session:
        def __init__(self, team: tuple[str, ...] = ()) -> None:
            self.team_agents = team

    manager = _Manager({"architect", "executor", "explorer", "critic"})
    team = _Session(("architect", "executor", "explorer", "critic"))
    assert deepinit.pick_doc_agent(team, manager, deepinit.MAP_AGENTS) == "explorer"
    assert deepinit.pick_doc_agent(team, manager, deepinit.WRITE_AGENTS) == "executor"
    assert deepinit.pick_doc_agent(team, manager, deepinit.ROOT_MAP_AGENTS) == "architect"
    # Team without a writing role → None for write (anonymous + WRITE_TOOLS).
    explore_only = _Session(("explorer", "critic"))
    assert deepinit.pick_doc_agent(explore_only, manager, deepinit.WRITE_AGENTS) is None
    assert deepinit.pick_doc_agent(explore_only, manager, deepinit.MAP_AGENTS) == "explorer"
    # No team: matching builtins.
    bare = _Session()
    assert deepinit.pick_doc_agent(bare, manager, deepinit.MAP_AGENTS) == "explorer"
    assert deepinit.pick_doc_agent(bare, manager, deepinit.WRITE_AGENTS) == "executor"



# ---------------------------------------------------------------------------
# per-agent model assignment (CORE-model-assignment)
# ---------------------------------------------------------------------------


async def _observe_child(manager: SubagentManager, core: Core, runner: Any) -> dict[str, Any]:
    """Snapshot the child session's route while the delegation is alive."""
    seen: dict[str, Any] = {}
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while not seen and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
        for record in manager.records():
            child = core.sessions.get(record.session_id) if record.session_id else None
            if child is not None:
                seen = {"provider": child.provider, "model": child.model, "agent": child.agent}
    await asyncio.wait_for(runner, timeout=TIMEOUT)
    return seen


async def test_a_profile_id_in_an_agent_definition_is_not_read_as_a_vendor(
    daemon: Daemon, workdir: Path
) -> None:
    """B-P1-1: the live repro from the review.

    Profiles configured, **no** ``models.default`` and no assignment: the old
    bypass split ``model: fast`` as a vendor name and the child died at its
    first turn with ``unknown provider vendor: fast``.
    """
    core = daemon.core
    assert core is not None
    core.settings.models.profiles = {
        "fast": ModelProfile(provider="openai", model="gpt-fast")
    }
    core.settings.models.default = None
    write_definition(
        AgentDefinition(name="scribe", description="Writes.", model="fast"), workdir
    )
    session = await open_session(core, workdir)
    manager = get_manager(core)
    runner = asyncio.ensure_future(manager.run(session, "do a thing", agent="scribe"))
    seen = await _observe_child(manager, core, runner)
    assert (seen["provider"], seen["model"]) == ("openai", "gpt-fast")


async def test_a_delegation_model_override_outranks_the_assignment(
    daemon: Daemon, workdir: Path
) -> None:
    core = daemon.core
    assert core is not None
    core.settings.models.profiles = {
        "fast": ModelProfile(provider="openai", model="gpt-fast"),
        "deep": ModelProfile(provider="anthropic", model="claude-deep"),
    }
    core.settings.agents.models = {"executor": "fast"}
    session = await open_session(core, workdir)
    manager = get_manager(core)
    runner = asyncio.ensure_future(
        manager.run(session, "do a thing", agent="executor", model="deep")
    )
    seen = await _observe_child(manager, core, runner)
    assert (seen["provider"], seen["model"]) == ("anthropic", "claude-deep")


async def test_an_unresolvable_delegation_model_is_refused(
    daemon: Daemon, workdir: Path
) -> None:
    """The caller asked for a specific model; falling back would be a lie."""
    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    result = await get_manager(core).run(
        session, "do a thing", agent="executor", model="no-such-profile"
    )
    assert not result.ok
    assert result.error is not None and "unknown model" in result.error


async def test_a_child_inherits_the_parents_pin_when_nothing_else_applies(
    daemon: Daemon, workdir: Path
) -> None:
    """The pin is rung 3: it applies when no assignment or definition does."""
    core = daemon.core
    assert core is not None
    core.settings.models.profiles = {"deep": ModelProfile(provider="anthropic", model="deep-1")}
    core.settings.models.default = "deep"
    session = await open_session(core, workdir)
    await core.sessions.set_model(session, "openai:pinned-by-the-user")

    manager = get_manager(core)
    runner = asyncio.ensure_future(manager.run(session, "do a thing"))
    seen = await _observe_child(manager, core, runner)
    # It used to be dropped for every child the moment any models.default existed.
    assert (seen["provider"], seen["model"]) == ("openai", "pinned-by-the-user")


async def test_parent_interrupt_cascades_to_slow_child(
    daemon: Daemon, workdir: Path, tmp_path: Path
) -> None:
    """Parent interrupt cascades to running child and subagent.done is interrupted."""
    from snowpea_core.agent import loop as agent_loop
    from snowpea_core.server.session_handlers import interrupt_session
    from snowpea_core.tools.registry import Tool, ToolResult

    core = daemon.core
    assert core is not None

    async def _sleeping_run(ctx: Any, args: dict[str, Any]) -> ToolResult:
        del ctx, args
        await asyncio.sleep(30.0)
        return ToolResult(ok=True, output="slept")

    sleeping_tool = Tool(
        name="sleep_tool",
        category="test",
        description="sleeps for 30s",
        input_schema={"type": "object"},
        permission="read",
        run=_sleeping_run,
    )
    core.tools.register(sleeping_tool)

    class CascadeProvider:
        vendor = "test-cascade"

        async def stream(
            self, messages: list[Any], tools: list[Any], **kwargs: Any
        ) -> AsyncIterator[StreamEvent]:
            del tools, kwargs
            users = "\n".join(
                str(getattr(m, "content", "")) for m in messages if getattr(m, "role", "") == "user"
            )
            if "slow child task" in users:
                yield StreamEvent(
                    kind="tool_call",
                    tool_call=ToolCall(
                        id="call_child_sleep",
                        name="sleep_tool",
                        arguments={},
                    ),
                )
                yield StreamEvent(kind="done", stop_reason="tool_use")
                return
            yield StreamEvent(
                kind="tool_call",
                tool_call=ToolCall(
                    id="call_delegate",
                    name="delegate_task",
                    arguments={"task": "slow child task", "agent": "executor"},
                ),
            )
            yield StreamEvent(kind="done", stop_reason="tool_use")

    core.providers.get = lambda _p, _m: CascadeProvider()  # type: ignore[assignment]

    parent_session = await open_session(core, workdir)
    unrelated_dir = tmp_path / "unrelated"
    unrelated_dir.mkdir()
    unrelated_session = await open_session(core, unrelated_dir)

    recorder = Recorder()
    core.hub.subscribe(recorder, None)

    parent_runner = asyncio.create_task(
        agent_loop.run_turn(core, parent_session, "delegate to slow child", turn_id="t-parent")
    )

    deadline = asyncio.get_running_loop().time() + TIMEOUT
    child_session_id = None
    while asyncio.get_running_loop().time() < deadline:
        calls = [
            e
            for e in recorder.of_kind("tool.call")
            if e["sessionId"] != parent_session.id and e["payload"].get("name") == "sleep_tool"
        ]
        if calls:
            child_session_id = calls[0]["sessionId"]
            break
        await asyncio.sleep(0.01)
    assert child_session_id is not None

    await interrupt_session(core, parent_session)
    await asyncio.wait_for(parent_runner, timeout=TIMEOUT)

    parent_turn_done = [
        e["payload"]
        for e in recorder.of_kind("turn.done")
        if e["sessionId"] == parent_session.id
    ]
    assert any(
        p.get("turnId") == "t-parent" and p.get("reason") == "interrupted" for p in parent_turn_done
    )

    child_turn_done = [
        e["payload"]
        for e in recorder.of_kind("turn.done")
        if e["sessionId"] == child_session_id
    ]
    assert any(p.get("reason") == "interrupted" for p in child_turn_done)

    subagent_done = [
        e["payload"]
        for e in recorder.of_kind("subagent.done")
        if e["sessionId"] == parent_session.id
    ]
    assert any(p.get("status") == "interrupted" for p in subagent_done)

    assert not unrelated_session.interrupt.is_set()
