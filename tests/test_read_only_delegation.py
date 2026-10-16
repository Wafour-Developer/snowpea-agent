"""Read-only delegations and the "fast" model (snowpea-browser addendum 23).

``delegate_task {readOnly: true}`` runs the child in plan mode with every
mutating call refused — even the markdown plan mode itself allows — and a call
that would ask a person refused instead of asked.  ``model: "fast"`` resolves to
``browser.fastModel``, or to the parent's own model when that is unset.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from _support import Recorder, make_daemon

from snowpea_core.agent.subagent import get_manager
from snowpea_core.server.app_server import Daemon
from snowpea_core.tools import delegate

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "read_only.json"


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    instance = await make_daemon(
        tmp_path / "home",
        settings={"agents": {"incompleteRetries": 0}, "approvals": {"timeout_sec": 5}},
    )
    try:
        yield instance
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


async def test_a_read_only_child_changes_nothing_and_asks_nobody(
    daemon: Daemon, tmp_path: Path
) -> None:
    core = daemon.core
    assert core is not None
    workdir = tmp_path / "project"
    workdir.mkdir()
    parent = await core.sessions.create(workdir, mode="auto")
    everything = Recorder()
    core.hub.subscribe(everything, None)
    asked: list[Any] = []
    real_request = core.approvals.request

    async def record_request(*args: Any, **kwargs: Any) -> Any:
        asked.append(args[1] if len(args) > 1 else kwargs.get("tool"))
        return await real_request(*args, **kwargs)

    core.approvals.request = record_request  # type: ignore[method-assign]

    result = await get_manager(core).run(parent, "write the notes file", read_only=True)

    assert "nothing could be changed" in result.summary
    assert not (workdir / "notes.md").exists()
    assert not (workdir / "made-by-child").exists()
    # The shell call plan mode would ask about was refused without asking.
    assert asked == []
    errors_seen = " ".join(
        str(event["payload"].get("error") or "") for event in everything.of_kind("tool.result")
    )
    assert errors_seen.count("read-only delegation") == 2
    assert "set_mode" not in errors_seen
    # The parent itself is untouched.
    assert parent.mode == "auto" and parent.read_only is False


async def test_a_read_only_child_runs_in_plan_mode_and_passes_it_on(
    daemon: Daemon, tmp_path: Path
) -> None:
    core = daemon.core
    assert core is not None
    manager = get_manager(core)
    parent = await core.sessions.create(tmp_path, mode="auto")
    record = manager.new_record(parent, "look", None)
    record.read_only = True
    child = await manager._child_session(parent, record, None)
    assert child.mode == "plan" and child.read_only is True
    grandchild = await manager._child_session(child, manager.new_record(child, "x", None), None)
    assert grandchild.mode == "plan" and grandchild.read_only is True


async def test_approval_ask_from_a_read_only_child_is_refused_not_asked(
    daemon: Daemon, tmp_path: Path
) -> None:
    from snowpea_core.server.host_handlers import approval_ask_handler
    from snowpea_core.server.protocol import ApprovalAskParams
    from snowpea_core.tools.host_tools import HOST_TOOLS

    core = daemon.core
    assert core is not None
    conn = SimpleNamespace(
        surface_id="surf-ro", client_id="browser-ro", client_kind="browser", closed=False,
        on_close=[],
    )
    HOST_TOOLS.register(core, conn, [{"name": "repl", "permission": "read"}])
    try:
        child = await core.sessions.create(tmp_path, mode="plan")
        child.host_tools_from = "browser-ro"
        child.read_only = True
        asked: list[Any] = []

        async def never(*args: Any, **kwargs: Any) -> Any:
            asked.append(args)
            raise AssertionError("a read-only child must not ask")

        core.approvals.request = never  # type: ignore[method-assign]
        cases = (("write", False), ("send", False), ("read", True), ("exec", False))
        for permission, force in cases:
            answer = await approval_ask_handler(
                conn,  # type: ignore[arg-type]
                ApprovalAskParams(
                    sessionId=child.id, tool="repl.click", permission=permission,
                    reason="r", forceAsk=force,
                ),
                core,
            )
            assert answer.decision == "deny" and answer.by == "readOnly", permission
        assert asked == []
    finally:
        HOST_TOOLS.drop_connection(conn)


async def test_fast_resolves_to_the_setting_or_the_parent_model(
    daemon: Daemon, tmp_path: Path
) -> None:
    core = daemon.core
    assert core is not None
    parent = await core.sessions.create(tmp_path, mode="auto")
    parent.provider, parent.model = "mainv", "big-1"
    ctx = SimpleNamespace(core=core, session=parent)
    assert delegate.fast_model_reference(ctx) == "mainv:big-1"  # type: ignore[arg-type]
    core.settings.models.categories["fast"] = "catv:mid-1"
    assert delegate.fast_model_reference(ctx) == "catv:mid-1"  # type: ignore[arg-type]
    core.settings.browser.fastModel = "otherv:small-1"
    assert delegate.fast_model_reference(ctx) == "otherv:small-1"  # type: ignore[arg-type]


async def test_delegate_task_passes_read_only_and_fast_through(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.agent.subagent import SubagentResult

    core = daemon.core
    assert core is not None
    parent = await core.sessions.create(tmp_path, mode="auto")
    parent.provider, parent.model = "mainv", "big-1"
    core.settings.browser.fastModel = "otherv:small-1"
    seen: dict[str, Any] = {}

    async def run(self: Any, parent: Any, task: str, **kwargs: Any) -> SubagentResult:
        seen.update(kwargs)
        return SubagentResult(agent_id="a", ok=True, summary="ok")

    monkeypatch.setattr(type(get_manager(core)), "run", run)
    ctx = SimpleNamespace(core=core, session=parent, progress=None)
    await delegate.delegate_task(
        ctx,  # type: ignore[arg-type]
        {"task": "read the page", "model": "fast", "readOnly": True},
    )
    assert seen["read_only"] is True and seen["model"] == "otherv:small-1"



async def test_a_read_only_child_may_not_leave_plan_mode(daemon: Daemon, tmp_path: Path) -> None:
    core = daemon.core
    assert core is not None
    parent = await core.sessions.create(tmp_path, mode="auto")
    everything = Recorder()
    core.hub.subscribe(everything, None)

    await get_manager(core).run(parent, "switch to accept", read_only=True)

    [refused] = [
        event["payload"]
        for event in everything.of_kind("tool.result")
        if event["payload"].get("name") == "set_mode"
    ]
    assert refused["ok"] is False and "read-only delegation" in str(refused["error"])
    assert not everything.of_kind("question.request")
