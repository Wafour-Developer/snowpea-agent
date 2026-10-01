"""The lead stays reachable while it delegates.

A user message wakes ``subagent_wait``; a background run nobody collected is
announced to its lead; ``ask_user`` answers survive tool-output pruning.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from snowpea_core.agent import loop as agent_loop
from snowpea_core.agent.subagent import SubagentResult, get_manager
from snowpea_core.providers.base import ChatMessage, ToolCall
from snowpea_core.session.compaction import prune_old_tool_outputs
from snowpea_core.session.session import Session
from snowpea_core.tools import delegate
from snowpea_core.tools.registry import ToolContext


class FakeHub:
    async def emit_event(self, session_id: str, event: Any) -> None:
        pass


def _ctx(tmp_path: Path) -> ToolContext:
    sessions = SimpleNamespace(get=lambda _i: None)
    core = SimpleNamespace(hub=FakeHub(), settings=None, sessions=sessions)
    session = Session(id="s-lead", workdir=tmp_path)
    return ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]


async def test_subagent_wait_wakes_when_the_user_speaks(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    manager = get_manager(ctx.core)
    release = asyncio.Event()

    async def fake_run(parent: Session, task: str, **kwargs: Any) -> SubagentResult:
        await release.wait()
        return SubagentResult(agent_id="a-1", ok=True, summary="done")

    manager.run = fake_run  # type: ignore[method-assign]
    started = await delegate.delegate_task(ctx, {"task": "long job", "run_in_background": True})
    task_id = started.meta["task_id"]  # type: ignore[index]

    async def speak() -> None:
        await asyncio.sleep(0.05)
        ctx.session.user_waiting.set()

    asyncio.ensure_future(speak())
    woke = await asyncio.wait_for(
        delegate.subagent_wait(ctx, {"task_ids": [task_id], "timeout": 30}), timeout=5
    )
    assert "The user sent a message" in woke.output and "still running" in woke.output
    ctx.session.user_waiting.clear()
    release.set()
    done = await delegate.subagent_wait(ctx, {"task_ids": [task_id], "timeout": 5})
    assert "done" in done.output


async def test_foreground_delegation_detaches_when_the_user_speaks(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    manager = get_manager(ctx.core)
    release = asyncio.Event()

    async def fake_run(parent: Session, task: str, **kwargs: Any) -> SubagentResult:
        await release.wait()
        return SubagentResult(agent_id="a-1", ok=True, summary="report")

    manager.run = fake_run  # type: ignore[method-assign]
    ctx.session.user_waiting.set()
    result = await asyncio.wait_for(delegate.delegate_task(ctx, {"task": "long job"}), timeout=5)
    assert result.ok and result.meta and result.meta["detached"] is True
    release.set()
    collected = await delegate.subagent_wait(
        ctx, {"task_ids": [result.meta["task_id"]], "timeout": 5}
    )
    assert "report" in collected.output


async def test_an_unclaimed_background_run_is_announced(tmp_path: Path, monkeypatch: Any) -> None:
    ctx = _ctx(tmp_path)
    manager = get_manager(ctx.core)
    started: list[str] = []
    monkeypatch.setattr(
        agent_loop, "start_turn", lambda core, session, text, **kw: started.append(kw["model_text"])
    )

    async def fake_run(parent: Session, task: str, **kwargs: Any) -> SubagentResult:
        return SubagentResult(agent_id="a-1", ok=True, summary="report")

    manager.run = fake_run  # type: ignore[method-assign]
    result = await delegate.delegate_task(
        ctx, {"task": "job", "run_in_background": True, "title": "scan"}
    )
    await asyncio.sleep(delegate.ANNOUNCE_GRACE + 0.2)
    assert started and result.meta["task_id"] in started[0]  # type: ignore[index]


def test_ask_user_answers_are_never_pruned() -> None:
    history: list[ChatMessage] = [ChatMessage(role="user", content="build it")]
    for n in range(12):
        name = "ask_user" if n == 0 else "shell"
        history.append(
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id=f"c{n}", name=name, arguments={})],
            )
        )
        history.append(
            ChatMessage(role="tool", content=f"answer {n} " * 50, tool_call_id=f"c{n}", name=name)
        )
    pruned = prune_old_tool_outputs(history, keep_rounds=2)
    assert pruned[2].content == history[2].content
    assert pruned[4].content != history[4].content


def test_compaction_keeps_the_users_words_verbatim_across_two_compactions() -> None:
    from snowpea_core.session.compaction import VERBATIM_HEADING, summary_message, verbatim_section

    first = [
        ChatMessage(role="user", content="make it a real 3D game"),
        ChatMessage(role="tool", content="Q: accounts? A: parent + child", name="ask_user"),
        ChatMessage(role="user", content="[system] Stop hook: keep going"),
        ChatMessage(role="user", content="[from the user, mid-task] no cards UI"),
    ]
    block = verbatim_section(first)
    assert block.startswith(VERBATIM_HEADING)
    assert "no cards UI" in block and "Stop hook" not in block
    assert block.index("no cards UI") < block.index("make it a real 3D game")
    second = [summary_message("notes\n\n" + block), ChatMessage(role="user", content="use Flux")]
    carried = verbatim_section(second)
    for words in ("use Flux", "no cards UI", "parent + child", "make it a real 3D game"):
        assert words in carried
    assert carried.count("make it a real 3D game") == 1
