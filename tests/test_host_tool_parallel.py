"""Host tools that may run side by side (protocol 1.18.0, snowpea-browser).

``tool.register`` takes ``parallel: "safe" | "never"`` (default never).  Two
calls of a safe host tool in one model response run at once, like the
built-in readers; an unmarked one keeps its order and closes the parallel run.
Only read and network tools may be marked safe.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from snowpea_core.agent.tool_batch import plan_tool_batch_segments
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers.base import ToolCall
from snowpea_core.server.app_server import Core
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION, HostToolSpec
from snowpea_core.session.session import Session
from snowpea_core.tools.host_tools import HOST_TOOLS, HostToolError
from snowpea_core.tools.registry import register_builtin_tools


def _call(name: str, index: int = 0) -> ToolCall:
    return ToolCall(id=f"{name}-{index}", name=name, arguments={"query": f"q{index}"})


@pytest.fixture
def host(tmp_path: Path):  # type: ignore[no-untyped-def]
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    register_builtin_tools(core.tools.bind(core.settings))
    conn = SimpleNamespace(
        surface_id="surf-par",
        client_id="browser-par",
        client_kind="browser",
        closed=False,
        on_close=[],
    )
    HOST_TOOLS.register(
        core,
        conn,
        [
            HostToolSpec(
                name="google_search", description="d", permission="network", parallel="safe"
            ).model_dump(),
            HostToolSpec(
                name="read_tab", description="d", permission="read", parallel="safe"
            ).model_dump(),
            HostToolSpec(name="repl", description="d", permission="read").model_dump(),
        ],
    )
    session = Session(id="s-par", workdir=tmp_path)
    session.host_tools_from = "browser-par"
    try:
        yield core, conn, session
    finally:
        HOST_TOOLS.drop_connection(conn)


def _lookup(core: Core, session: Session):  # type: ignore[no-untyped-def]
    return lambda name: bool(getattr(core.tools.get(name, session), "parallel_safe", False))


def test_the_capability_and_version() -> None:
    assert "hostToolParallel" in CAPABILITIES
    assert tuple(map(int, PROTOCOL_VERSION.split("."))) >= (1, 18, 0)
    assert HostToolSpec(name="x", description="d", permission="read").parallel == "never"


def test_two_safe_host_calls_share_a_parallel_run(host) -> None:  # type: ignore[no-untyped-def]
    core, _conn, session = host
    calls = [_call("google_search", 0), _call("google_search", 1), _call("read_tab", 2)]
    segments = plan_tool_batch_segments(
        calls, workdir=session.workdir, parallel_safe=_lookup(core, session)
    )
    assert segments == [("parallel", calls)]


def test_an_unmarked_host_tool_is_a_barrier(host) -> None:  # type: ignore[no-untyped-def]
    core, _conn, session = host
    calls = [_call("google_search", 0), _call("repl", 1), _call("google_search", 2)]
    segments = plan_tool_batch_segments(
        calls, workdir=session.workdir, parallel_safe=_lookup(core, session)
    )
    assert [kind for kind, _batch in segments] == ["sequential"]
    assert [call.id for _kind, batch in segments for call in batch] == [c.id for c in calls]


def test_without_the_lookup_host_tools_stay_sequential(host) -> None:  # type: ignore[no-untyped-def]
    _core, _conn, session = host
    calls = [_call("google_search", 0), _call("google_search", 1)]
    assert plan_tool_batch_segments(calls, workdir=session.workdir) == [("sequential", calls)]


def test_only_read_or_network_tools_may_be_marked_safe(host) -> None:  # type: ignore[no-untyped-def]
    core, conn, _session = host
    with pytest.raises(HostToolError, match="read or network"):
        HOST_TOOLS.register(
            core,
            conn,
            [
                HostToolSpec(
                    name="click", description="d", permission="write", parallel="safe"
                ).model_dump()
            ],
        )
    with pytest.raises(HostToolError, match="'safe' or 'never'"):
        HOST_TOOLS.register(
            core,
            conn,
            [{"name": "odd", "description": "d", "permission": "read", "parallel": "maybe"}],
        )


async def test_safe_host_calls_really_overlap(host) -> None:  # type: ignore[no-untyped-def]
    """Run as the planner says (gather for a parallel run): both calls are in flight at once."""
    core, _conn, session = host
    tool = core.tools.get("google_search", session)
    assert tool is not None and tool.parallel_safe is True
    running = 0
    peak = 0

    async def slow(*_args: object) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.3)
        running -= 1

    calls = [_call("google_search", 0), _call("google_search", 1)]
    for kind, batch in plan_tool_batch_segments(
        calls, workdir=session.workdir, parallel_safe=_lookup(core, session)
    ):
        if kind == "parallel":
            await asyncio.gather(*[slow(call) for call in batch])
        else:
            for call in batch:
                await slow(call)
    assert peak == 2
