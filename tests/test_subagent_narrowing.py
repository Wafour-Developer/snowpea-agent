"""A delegation may narrow the session's root agent, never widen it.

host-security-review: a session whose root agent is the narrow ``browser``
agent must not spawn ``browser-code`` (or any agent with tools the root
lacks) unless ``agents.allowBroaderChildren`` is on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from test_subagents import TIMEOUT, daemon, open_session, workdir  # noqa: F401

from snowpea_core.agent.definition import AgentDefinition, write_definition
from snowpea_core.agent.session_agent import apply_agent
from snowpea_core.agent.subagent import SubagentRecord, get_manager
from snowpea_core.server.app_server import Daemon


def _definitions(workdir: Path) -> None:  # noqa: F811
    for name, tools in (
        ("browser", ["read_file", "repl"]),
        ("browser-code", "*"),
        ("reader", ["read_file"]),
        ("shell-reader", ["read_file", "shell"]),
    ):
        write_definition(
            AgentDefinition(name=name, description=name, tools=tools, prompt=name), workdir
        )


async def _browser_root(daemon: Daemon, workdir: Path):  # noqa: F811
    _definitions(workdir)
    session = await open_session(daemon.core, workdir)
    apply_agent(daemon.core, session, "browser")
    return session


async def test_a_broader_named_agent_is_refused_under_a_browser_root(
    daemon: Daemon, workdir: Path  # noqa: F811
) -> None:
    root = await _browser_root(daemon, workdir)
    manager = get_manager(daemon.core)
    for agent, reach in (("browser-code", "every tool"), ("shell-reader", "shell")):
        result = await asyncio.wait_for(
            manager.run(root, "quick child", agent=agent), timeout=TIMEOUT
        )
        assert result.ok is False, agent
        assert f"agent '{agent}' refused" in (result.error or "")
        assert reach in (result.error or "")
        assert "agents.allowBroaderChildren" in (result.error or "")


async def test_a_narrower_agent_passes_and_the_setting_lifts_the_check(
    daemon: Daemon, workdir: Path  # noqa: F811
) -> None:
    root = await _browser_root(daemon, workdir)
    manager = get_manager(daemon.core)
    reader = manager.definition(root, "reader")
    wide = manager.definition(root, "browser-code")
    record = SubagentRecord(agent_id="a", name="reader", task="t", parent_session_id=root.id)
    assert manager._narrowing(root, record, reader, None, explicit=True) is None
    assert record.tool_ceiling is None

    record = SubagentRecord(agent_id="b", name="browser-code", task="t", parent_session_id=root.id)
    assert manager._narrowing(root, record, wide, None, explicit=True)
    daemon.core.settings.agents.allowBroaderChildren = True
    try:
        assert manager._narrowing(root, record, wide, None, explicit=True) is None
        assert record.tool_ceiling is None
    finally:
        daemon.core.settings.agents.allowBroaderChildren = False


async def test_an_unnamed_or_picked_child_is_capped_at_the_roots_tools(
    daemon: Daemon, workdir: Path  # noqa: F811
) -> None:
    root = await _browser_root(daemon, workdir)
    manager = get_manager(daemon.core)
    record = SubagentRecord(agent_id="c", name="", task="t", parent_session_id=root.id)
    assert manager._narrowing(root, record, None, None, explicit=False) is None
    assert record.tool_ceiling == {"read_file", "repl"}
    wide = manager.definition(root, "browser-code")
    picked = SubagentRecord(
        agent_id="d", name="browser-code", task="t", parent_session_id=root.id
    )
    assert manager._narrowing(root, picked, wide, None, explicit=False) is None
    assert picked.tool_ceiling == {"read_file", "repl"}


async def test_a_grandchild_is_measured_against_the_root_not_its_parent(
    daemon: Daemon, workdir: Path  # noqa: F811
) -> None:
    root = await _browser_root(daemon, workdir)
    child = await daemon.core.sessions.create(
        workdir, mode="auto", parent_session_id=root.id, kind="subagent"
    )
    manager = get_manager(daemon.core)
    assert manager._root_of(child) is root
    wide = manager.definition(root, "browser-code")
    record = SubagentRecord(
        agent_id="e", name="browser-code", task="t", parent_session_id=child.id
    )
    assert "refused" in (manager._narrowing(child, record, wide, None, explicit=True) or "")


async def test_a_root_without_an_agent_may_spawn_anything(
    daemon: Daemon, workdir: Path  # noqa: F811
) -> None:
    _definitions(workdir)
    root = await open_session(daemon.core, workdir)
    manager = get_manager(daemon.core)
    wide = manager.definition(root, "browser-code")
    record = SubagentRecord(agent_id="f", name="browser-code", task="t", parent_session_id=root.id)
    assert manager._narrowing(root, record, wide, None, explicit=True) is None
    assert record.tool_ceiling is None


async def test_a_running_unnamed_child_gets_only_the_roots_tools(
    daemon: Daemon, workdir: Path  # noqa: F811
) -> None:
    root = await _browser_root(daemon, workdir)
    manager = get_manager(daemon.core)
    runner = asyncio.ensure_future(manager.run(root, "slow child under a definition"))
    seen: set[str] | None = None
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while seen is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
        for record in manager.records():
            child = daemon.core.sessions.get(record.session_id or "")
            if child is not None and child.parent_session_id == root.id:
                seen = set(child.allowed_tools or ())
    await asyncio.wait_for(runner, timeout=TIMEOUT)
    assert seen is not None and seen and seen <= {"read_file", "repl"}
