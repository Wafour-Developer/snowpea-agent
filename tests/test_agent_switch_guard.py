"""Addendum 25: a turn the agent was switched in asks before every change.

``session.setAgent {guardTurn}`` marks the running turn: from then on every
call that is not a read, and every ``mcp__`` call, is put to the person in
any mode, past saved rules and cached answers; plan still denies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from snowpea_core.agent.loop import AGENT_SWITCH_GUARD, _run_one_call
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.permissions.approval_queue import Decision
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.base import ToolCall
from snowpea_core.server.app_server import Core
from snowpea_core.server.protocol import CAPABILITIES
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import Tool, ToolResult


async def _ok(_ctx: Any, _args: dict[str, Any]) -> ToolResult:
    return ToolResult(ok=True, output="done")


class AlwaysAllow(PermissionPolicy):
    """A saved "always" rule for everything."""

    def promote(self, verdict: Any, *_args: Any, **_kwargs: Any) -> Any:
        return "allow"


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Core, Session, list[dict]]:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    for name, permission in (
        ("run_cmd", "exec"),
        ("put_file", "write"),
        ("look", "read"),
        ("mcp__x__y", "read"),
    ):
        core.tools.register(
            Tool(
                name=name,
                category="test",
                description=name,
                input_schema={},
                permission=permission,
                run=_ok,
            )
        )
    asked: list[dict] = []

    async def request(session: Any, tool: str, args: dict, **kwargs: Any) -> Decision:
        asked.append({"tool": tool, **kwargs})
        return Decision("allow", "once", "test")

    monkeypatch.setattr(core.approvals, "request", request)
    session = Session(id="s-guard", workdir=tmp_path, mode="auto")
    return core, session, asked


async def _call(core: Core, session: Session, name: str, turn: str, policy: Any = None) -> Any:
    return await _run_one_call(
        core,
        session,
        LocalBackend(session.workdir),
        policy or AlwaysAllow(),
        ToolCall(id=f"c-{name}-{turn}", name=name, arguments={}),
        turn,
        False,
    )


async def test_a_guarded_turn_asks_for_every_change_even_in_auto(setup: Any) -> None:
    core, session, asked = setup
    session.agent_guard = True
    for name in ("run_cmd", "put_file", "look", "mcp__x__y"):
        await _call(core, session, name, "t-1")
    # Reads run; exec, write and any MCP call are asked, past the "always" rule.
    assert [a["tool"] for a in asked] == ["run_cmd", "put_file", "mcp__x__y"]
    assert all(a["guard"] == AGENT_SWITCH_GUARD and a["cacheable"] is False for a in asked)


async def test_plan_still_denies_inside_a_guarded_turn(setup: Any) -> None:
    core, session, asked = setup
    session.mode = "plan"
    session.agent_guard = True
    # Plan denies writes; a guard never turns that into a question.
    assert await _call(core, session, "put_file", "t-1", PermissionPolicy()) == "denied"
    assert asked == []


async def test_a_child_started_later_is_guarded_and_released_with_its_parent(
    setup: Any, tmp_path: Path
) -> None:
    """A background delegation or team worker created after the guarded turn
    moved on is still covered: the guard is read along the parent chain."""
    core, session, asked = setup
    core.sessions._sessions[session.id] = session  # type: ignore[attr-defined]
    session.agent_guard = True
    child = Session(id="s-child", workdir=tmp_path, mode="auto", parent_session_id=session.id)
    await _call(core, child, "run_cmd", "c-1")
    assert [a["tool"] for a in asked] == ["run_cmd"]
    session.agent_guard = False
    asked.clear()
    await _call(core, child, "run_cmd", "c-2")
    assert asked == []
    # A delegation accepted while guarded keeps the guard after the parent's
    # is lifted: its brief was written under it.
    from snowpea_core.agent.subagent import inherit_host

    session.agent_guard = True
    stamped = Session(id="s-stamped", workdir=tmp_path, mode="auto")
    inherit_host(stamped, session)
    session.agent_guard = False
    await _call(core, stamped, "run_cmd", "c-3")
    assert [a["tool"] for a in asked] == ["run_cmd"]


async def test_only_the_persons_next_turn_lifts_the_guard(setup: Any, tmp_path: Path) -> None:
    from snowpea_core.agent import loop as agent_loop
    from snowpea_core.providers.fake import FakeProvider

    core, session, _ = setup
    core.providers.register("local", FakeProvider({"steps": [], "default": {"text": "ok"}}))
    session.provider = "local"
    session.agent_guard = True
    await agent_loop.run_turn(core, session, "queued by the agent", origin="agent")
    assert session.agent_guard
    # A turn whose origin nobody recorded (a command's nested run) keeps it.
    await agent_loop.run_turn(core, session, "a nested run")
    assert session.agent_guard
    await agent_loop.run_turn(core, session, "the person again", origin="human")
    assert not session.agent_guard


def test_the_capability_is_advertised() -> None:
    assert "setAgentGuardTurn" in CAPABILITIES
