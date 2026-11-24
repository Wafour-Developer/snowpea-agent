"""M36: an MCP server's tool list changes while snowpea stays connected.

hwp.snowpea.ai went from 16 to 33 tools in a redeploy; a core connected before
it kept the 16 for its whole life and could not fill empty table cells
(feedback 5fd55671).  Now ``notifications/tools/list_changed`` makes the core
list the server again, and a miss — a tool search naming the server, or a call
to an ``mcp__<server>__<tool>`` the cache lacks — re-lists it once a minute at
most.

The fixture is a real stdio server (``fixtures/mcp/changing_server.py``) whose
``grow`` tool adds ``write_cell``/``get_table`` and drops ``old_tool``, with
the notification; ``grow_quietly`` makes the same change without it.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from snowpea_core.agent.loop import _run_one_call
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.base import ToolCall
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools import mcp_client
from snowpea_core.tools.registry import ToolContext, register_builtin_tools

FIXTURE = Path(__file__).parent / "fixtures" / "mcp" / "changing_server.py"
NAME = "hwp"


@pytest.fixture
def core(tmp_path: Path) -> Core:
    built = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    register_builtin_tools(built.tools.bind(built.settings))
    return built


@pytest.fixture
def session(tmp_path: Path) -> Session:
    workdir = tmp_path / "work"
    workdir.mkdir()
    return Session(id="s-relist", workdir=workdir)


@pytest_asyncio.fixture(autouse=True)
async def _stop_servers() -> AsyncIterator[None]:
    mcp_client.MANAGER.listeners = []
    yield
    await mcp_client.MANAGER.close_all()
    mcp_client.MANAGER.listeners = []


@pytest.fixture(autouse=True)
def _fast_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_client, "RELIST_DEBOUNCE", 0.05)


def ctx_for(core: Core, session: Session) -> ToolContext:
    return ToolContext(
        session=session, core=core, backend=LocalBackend(session.workdir), call_id="c1"
    )


async def connect(core: Core) -> list[str]:
    config = mcp_client.McpServerConfig(
        name=NAME, command=sys.executable, args=[str(FIXTURE)], source="home"
    )
    return await mcp_client.register_config(core, config)


def mcp_names(core: Core) -> set[str]:
    return {info.name for info in core.tools.list() if info.source == f"mcp:{NAME}"}


async def wait_for(predicate, timeout: float = 10.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def test_list_changed_registers_new_tools_and_drops_removed_ones(core: Core) -> None:
    registered = await connect(core)
    assert "mcp__hwp__old_tool" in registered
    assert "mcp__hwp__write_cell" not in registered
    seen: list[int] = []

    async def listen(server: mcp_client.McpServer) -> None:
        seen.append(len(server.visible_tools()))

    mcp_client.MANAGER.listeners = [listen]
    server = mcp_client.MANAGER.get(NAME)
    assert server is not None
    await server.call("grow", {})

    assert await wait_for(lambda: "mcp__hwp__write_cell" in mcp_names(core))
    names = mcp_names(core)
    assert "mcp__hwp__get_table" in names
    assert "mcp__hwp__old_tool" not in names
    assert {tool["name"] for tool in server.visible_tools()} >= {"write_cell", "get_table"}
    # mcp.changed went out with the new count (the same event shape as before).
    assert await wait_for(lambda: bool(seen))
    assert seen[-1] == len(server.visible_tools())
    # The new tool is callable through the registry.
    tool = core.tools.get("mcp__hwp__write_cell")
    assert tool is not None


async def test_a_burst_of_notifications_costs_one_listing(
    core: Core, monkeypatch: pytest.MonkeyPatch
) -> None:
    await connect(core)
    server = mcp_client.MANAGER.get(NAME)
    assert server is not None
    calls = 0
    real = server.list_tools_now

    async def counted() -> bool:
        nonlocal calls
        calls += 1
        return await real()

    monkeypatch.setattr(server, "list_tools_now", counted)
    note = mcp_client.TOOLS_CHANGED
    message = type("N", (), {"method": note})()
    for _ in range(5):
        await server._on_message(message)
    assert server._relist_task is not None
    await server._relist_task
    assert calls == 1


async def test_a_quiet_change_is_found_by_a_call_that_misses(
    core: Core, session: Session
) -> None:
    await connect(core)
    server = mcp_client.MANAGER.get(NAME)
    assert server is not None
    await server.call("grow_quietly", {})
    await asyncio.sleep(0.2)
    assert "mcp__hwp__write_cell" not in mcp_names(core)

    session.mode = "auto"
    call = ToolCall(
        id="call-1",
        name="mcp__hwp__write_cell",
        arguments={"table": 0, "row": 0, "col": 1, "text": "x"},
    )
    await _run_one_call(
        core, session, LocalBackend(session.workdir), PermissionPolicy(), call, "turn-1", True
    )
    last = session.history.messages[-1]
    assert last.name == "mcp__hwp__write_cell"
    assert "unknown tool" not in last.content
    assert "wrote 'x'" in last.content
    assert "mcp__hwp__old_tool" not in mcp_names(core)


async def test_a_quiet_change_is_found_by_a_search_that_misses(
    core: Core, session: Session
) -> None:
    await connect(core)
    server = mcp_client.MANAGER.get(NAME)
    assert server is not None
    await server.call("grow_quietly", {})
    search = core.tools.get("tool_search")
    assert search is not None

    result = await search.run(ctx_for(core, session), {"query": "select:mcp__hwp__get_table"})
    assert result.ok, result.error
    assert result.meta == {"loaded": ["mcp__hwp__get_table"]}


async def test_a_keyword_search_naming_the_server_relists_it(
    core: Core, session: Session
) -> None:
    await connect(core)
    server = mcp_client.MANAGER.get(NAME)
    assert server is not None
    await server.call("grow_quietly", {})
    search = core.tools.get("tool_search")
    assert search is not None

    result = await search.run(ctx_for(core, session), {"query": "hwp write_cell"})
    assert result.ok, result.error
    assert "mcp__hwp__write_cell" in (result.meta or {}).get("loaded", [])


async def test_the_miss_cooldown_holds(core: Core, session: Session) -> None:
    await connect(core)
    server = mcp_client.MANAGER.get(NAME)
    assert server is not None
    # A miss before anything changed spends the minute.
    assert await mcp_client.relist_for_tool("mcp__hwp__write_cell") is False
    await server.call("grow_quietly", {})
    # Within the cooldown a second miss does not list again.
    assert await mcp_client.relist_for_tool("mcp__hwp__write_cell") is False
    assert "mcp__hwp__write_cell" not in mcp_names(core)
    # Once it has passed, it does.
    server._miss_relist_at = (server._miss_relist_at or 0.0) - mcp_client.MISS_RELIST_COOLDOWN
    assert await mcp_client.relist_for_tool("mcp__hwp__write_cell") is True
    assert "mcp__hwp__write_cell" in mcp_names(core)


async def test_misses_outside_mcp_never_relist(core: Core) -> None:
    await connect(core)
    assert mcp_client.server_of_tool("read_file") is None
    assert mcp_client.server_of_tool("mcp__other__x") is None
    assert mcp_client.server_of_tool("mcp__hwp__write_cell") == NAME
    assert mcp_client.search_misses("select:read_file", []) == []
    assert mcp_client.search_misses("browser click", []) == []
    assert mcp_client.search_misses("hwp", []) == [NAME]
    assert mcp_client.search_misses("select:mcp__hwp__get_table", []) == [NAME]


def test_server_names_with_underscores_resolve_to_the_longest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_client.MANAGER, "names", lambda: ["a", "a__b"])
    assert mcp_client.server_of_tool("mcp__a__b__tool") == "a__b"
    assert mcp_client.server_of_tool("mcp__a__tool") == "a"
