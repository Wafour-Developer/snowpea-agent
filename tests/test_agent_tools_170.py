"""Agent tools added in protocol 1.7.0 for snowpea-browser.

``write_todos`` and its ``todos.updated`` event, background delegation with
``subagent_wait``, ``fork_self``, ``model_category``, ``get_time``, and skills
brought in by their ``autoInject`` keywords.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent import loop as agent_loop
from snowpea_core.agent.subagent import SubagentResult, fork_history, get_manager
from snowpea_core.providers.base import ChatMessage, ToolCall
from snowpea_core.session.session import Session
from snowpea_core.skills.skill_md import parse_skill_md
from snowpea_core.tools import delegate, todos
from snowpea_core.tools.registry import ToolContext


class FakeHub:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def emit_event(self, session_id: str, event: Any) -> None:
        self.events.append((session_id, event))


def _ctx(tmp_path: Path, **core_attrs: Any) -> ToolContext:
    core = SimpleNamespace(hub=FakeHub(), **core_attrs)
    session = Session(id="s-170", workdir=tmp_path)
    return ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# write_todos
# ---------------------------------------------------------------------------


async def test_write_todos_replaces_merges_and_publishes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    first = await todos.write_todos(
        ctx,
        {
            "todos": [
                {"id": "1", "content": "open the inbox", "status": "in_progress"},
                {"id": "2", "content": "find the invoice", "status": "pending"},
            ]
        },
    )
    assert first.ok and first.output.startswith("0/2 done")
    merged = await todos.write_todos(
        ctx,
        {
            "todos": [
                {"id": "1", "status": "completed"},
                {"id": "3", "content": "reply", "status": "pending"},
            ],
            "merge": True,
        },
    )
    assert merged.ok
    assert ctx.session.todos == [
        {"id": "1", "content": "open the inbox", "status": "completed"},
        {"id": "2", "content": "find the invoice", "status": "pending"},
        {"id": "3", "content": "reply", "status": "pending"},
    ]
    kinds = [event[0] for _, event in ctx.core.hub.events]
    assert kinds == ["todos.updated", "todos.updated"]
    assert ctx.core.hub.events[-1][1][1]["todos"][-1]["id"] == "3"


async def test_only_one_item_may_be_in_progress(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await todos.write_todos(
        ctx,
        {
            "todos": [
                {"id": "1", "content": "a", "status": "in_progress"},
                {"id": "2", "content": "b", "status": "in_progress"},
            ]
        },
    )
    assert not result.ok and "in_progress" in (result.error or "")


# ---------------------------------------------------------------------------
# get_time / model_category
# ---------------------------------------------------------------------------


def test_get_time_reports_local_and_utc(tmp_path: Path) -> None:
    result = delegate.get_time(_ctx(tmp_path), {})
    assert result.ok and "local:" in result.output and "utc:" in result.output


def test_model_category_resolves_through_settings(tmp_path: Path) -> None:
    settings = SimpleNamespace(models=SimpleNamespace(categories={"fast": "local:qwen-small"}))
    ctx = _ctx(tmp_path, settings=settings)
    assert delegate._category_model(ctx, "fast") == "local:qwen-small"
    assert delegate._category_model(ctx, "standard") is None


# ---------------------------------------------------------------------------
# fork_self
# ---------------------------------------------------------------------------


def test_fork_history_drops_the_unanswered_delegate_call(tmp_path: Path) -> None:
    parent = Session(id="s-parent", workdir=tmp_path)
    parent.history.messages.extend(
        [
            ChatMessage(role="user", content="plan the trip"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a"})],
            ),
            ChatMessage(role="tool", content="secret", tool_call_id="c1", sensitive=True),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c2", name="delegate_task", arguments={"task": "x"})],
            ),
        ]
    )
    copied = fork_history(parent)
    assert [m.role for m in copied] == ["user", "assistant", "tool"]
    assert copied[2].content == "[redacted]"
    assert parent.history.messages[2].content == "secret"


# ---------------------------------------------------------------------------
# background delegation
# ---------------------------------------------------------------------------


async def test_background_delegation_and_subagent_wait(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, settings=None)
    manager = get_manager(ctx.core)
    release = asyncio.Event()
    seen: dict[str, Any] = {}

    async def fake_run(parent: Session, task: str, **kwargs: Any) -> SubagentResult:
        seen.update(kwargs)
        await release.wait()
        return SubagentResult(agent_id="a-1", ok=True, summary="found 3 invoices")

    manager.run = fake_run  # type: ignore[method-assign]
    started = await delegate.delegate_task(
        ctx, {"task": "find invoices", "run_in_background": True, "profile": "fork_self"}
    )
    assert started.ok and started.meta and started.meta["task_id"].startswith("bg-")
    task_id = started.meta["task_id"]
    await asyncio.sleep(0)
    assert seen["fork"] is True

    early = await delegate.subagent_wait(ctx, {"task_ids": [task_id], "timeout": 0.05})
    assert "still running" in early.output

    release.set()
    done = await delegate.subagent_wait(ctx, {"task_ids": [task_id], "timeout": 5})
    assert done.ok and "found 3 invoices" in done.output

    other = Session(id="s-other", workdir=tmp_path)
    stranger = ToolContext(session=other, core=ctx.core, backend=None)  # type: ignore[arg-type]
    refused = await delegate.subagent_wait(stranger, {"task_ids": [task_id]})
    assert not refused.ok


async def test_foreground_delegation_stop_has_bounded_child_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, settings=None)
    manager = get_manager(ctx.core)
    entered_child = asyncio.Event()
    entered_cleanup = asyncio.Event()
    release_cleanup = asyncio.Event()
    child_task: asyncio.Task[Any] | None = None

    async def fake_run(parent: Session, task: str, **kwargs: Any) -> SubagentResult:
        nonlocal child_task
        child_task = asyncio.current_task()
        entered_child.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            entered_cleanup.set()
            await release_cleanup.wait()
            raise

    manager.run = fake_run  # type: ignore[method-assign]
    monkeypatch.setattr(delegate, "DELEGATE_CANCEL_CLEANUP_TIMEOUT", 0.01)

    call = asyncio.ensure_future(delegate.delegate_task(ctx, {"task": "wait forever"}))
    await entered_child.wait()
    call.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, timeout=1)
    assert entered_cleanup.is_set()

    release_cleanup.set()
    assert child_task is not None
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(child_task, timeout=1)


async def test_foreground_delegation_stop_preserves_cancelled_error_after_cleanup_failure(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path, settings=None)
    manager = get_manager(ctx.core)
    entered_child = asyncio.Event()

    async def fake_run(parent: Session, task: str, **kwargs: Any) -> SubagentResult:
        entered_child.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as exc:
            raise RuntimeError("cleanup failed") from exc

    manager.run = fake_run  # type: ignore[method-assign]

    call = asyncio.ensure_future(delegate.delegate_task(ctx, {"task": "fail cleanup"}))
    await entered_child.wait()
    call.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, timeout=1)


async def test_foreground_delegation_stop_drains_late_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, settings=None)
    manager = get_manager(ctx.core)
    entered_child = asyncio.Event()
    entered_cleanup = asyncio.Event()
    release_cleanup = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def record_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        loop_errors.append(context)

    async def fake_run(parent: Session, task: str, **kwargs: Any) -> SubagentResult:
        entered_child.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as exc:
            entered_cleanup.set()
            await release_cleanup.wait()
            raise RuntimeError("late cleanup failed") from exc

    manager.run = fake_run  # type: ignore[method-assign]
    monkeypatch.setattr(delegate, "DELEGATE_CANCEL_CLEANUP_TIMEOUT", 0.01)
    loop.set_exception_handler(record_exception)
    try:
        call = asyncio.ensure_future(delegate.delegate_task(ctx, {"task": "fail late"}))
        await entered_child.wait()
        call.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, timeout=1)
        assert entered_cleanup.is_set()

        release_cleanup.set()
        await asyncio.sleep(0)
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_handler)


# ---------------------------------------------------------------------------
# autoInject
# ---------------------------------------------------------------------------


async def test_auto_inject_brings_a_skill_in_once_per_session(tmp_path: Path) -> None:
    doc = parse_skill_md(
        "---\nname: gmail\nautoInject: {keywords: [gmail, 지메일]}\n---\n"
        "Open mail.google.com first.",
        default_name="gmail",
    )
    core = SimpleNamespace(skills=SimpleNamespace(skills={"gmail": SimpleNamespace(doc=doc)}))
    session = Session(id="s-skill", workdir=tmp_path)
    injected = await agent_loop._auto_inject_skills(core, session, "지메일에서 청구서 찾아줘")
    assert injected == ["gmail"]
    assert "Open mail.google.com first." in session.history.messages[-1].content
    again = await agent_loop._auto_inject_skills(core, session, "Gmail again")
    assert again == []
    assert await agent_loop._auto_inject_skills(core, session, "unrelated") == []
