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
    get_manager,
)
from snowpea_core.server.app_server import Core, Daemon

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


async def test_an_unknown_agent_name_still_runs(daemon: Daemon, workdir: Path) -> None:
    """A missing definition is a note in the log, not a failed delegation."""
    core = daemon.core
    assert core is not None
    session = await open_session(core, workdir)
    result = await asyncio.wait_for(
        get_manager(core).run(session, "quick child", agent="nobody-defined-this"),
        timeout=TIMEOUT,
    )
    assert result.ok


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
    assert tool.permission == "exec"

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
    assert "src/AGENTS.md" in deepinit.dir_task(root / "src", root)
    assert "- src/AGENTS.md" in deepinit.root_task(root, [root / "src"])
