"""A call to a host tool after its host went away says so (snowpea-browser).

When the browser crashed its connection closed, ``drop_connection`` forgot
its tools, and the next ``repl`` call came back "unknown tool: repl; did you
mean grep?" — session s-c5651255ae10 then spent twelve rounds on tool_search
and delegate_task.  Now the names are remembered for ``GONE_GRACE_SEC`` (or
until the client registers again) and the call gets ``host_unavailable``
with an instruction to stop; tool_search says the browser tools are offline;
and of two live connections of one clientId the newest serves the session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
from test_host_tools import (  # noqa: F401
    Daemon,
    daemon,
    new_session,
    open_client,
    results_for,
    run_turn,
    spec,
)

from snowpea_core.tools import tool_search
from snowpea_core.tools.host_tools import GONE_GRACE_SEC, HOST_TOOLS, HostTools
from snowpea_core.tools.registry import ToolContext


def _conn(surface: str, client_id: str | None = None, kind: str | None = "browser") -> Any:
    return SimpleNamespace(
        surface_id=surface, client_id=client_id, client_kind=kind, closed=False, on_close=[]
    )


def _core() -> Any:
    return SimpleNamespace(tools=SimpleNamespace(get=lambda name: None))


def _registered(tools: HostTools, conn: Any, *names: str) -> None:
    tools.register(_core(), conn, [spec(name) for name in names])


def test_a_closed_browser_s_tools_are_remembered_for_the_grace_period() -> None:
    tools = HostTools()
    now = [1000.0]
    tools._clock = lambda: now[0]
    conn = _conn("sock-1", "browser-install-1")
    _registered(tools, conn, "repl", "browser_click")
    session = SimpleNamespace(host_tools_from="browser-install-1", origin_conn=None)
    assert tools.gone_for(session, "repl") is None, "a live host is not gone"

    tools.drop_connection(conn)
    gone = tools.gone_for(session, "repl")
    assert gone is not None
    message = gone.message("repl")
    assert message.startswith("host_unavailable: Snowpea Browser closed or crashed, so repl")
    assert "Don't search for other tools or delegate" in message
    assert tools.gone_for(session, "grep") is None, "only its own tools"

    now[0] += GONE_GRACE_SEC + 1
    assert tools.gone_for(session, "repl") is None


def test_registering_again_ends_the_absence() -> None:
    tools = HostTools()
    first = _conn("sock-1", "browser-install-1")
    _registered(tools, first, "repl")
    tools.drop_connection(first)
    session = SimpleNamespace(host_tools_from="browser-install-1", origin_conn=None)
    assert tools.gone_for(session) is not None
    second = _conn("sock-2", "browser-install-1")
    _registered(tools, second, "repl")
    assert tools.gone_for(session) is None
    assert tools.gone_for(session, "repl") is None


def test_a_session_on_its_origin_connection_and_another_host_kind() -> None:
    tools = HostTools()
    ide = _conn("sock-ide", None, "ide")
    _registered(tools, ide, "ide_open")
    session = SimpleNamespace(host_tools_from=None, origin_conn=ide)
    tools.drop_connection(ide)
    gone = tools.gone_for(session, "ide_open")
    assert gone is not None and "the ide that provided ide_open disconnected" in gone.message(
        "ide_open"
    )


def test_the_newest_live_connection_of_a_client_serves_the_session() -> None:
    tools = HostTools()
    old = _conn("sock-old", "browser-install-1")
    new = _conn("sock-new", "browser-install-1")
    _registered(tools, old, "repl")
    _registered(tools, new, "repl")
    session = SimpleNamespace(host_tools_from="browser-install-1", origin_conn=None)
    assert tools.owner_of(session) == "sock-new"
    new.closed = True
    assert tools.owner_of(session) == "sock-old"


async def test_tool_search_says_the_browser_tools_are_offline() -> None:
    tools = HostTools()
    conn = _conn("sock-1", "browser-install-1")
    _registered(tools, conn, "repl")
    tools.drop_connection(conn)
    session = SimpleNamespace(
        host_tools_from="browser-install-1", origin_conn=None, allowed_tools=None
    )
    original = HOST_TOOLS._gone
    HOST_TOOLS._gone = tools._gone
    try:
        assert "Browser tools are offline (repl)" in tool_search.offline_note(session)
    finally:
        HOST_TOOLS._gone = original
    assert tool_search.offline_note(SimpleNamespace(host_tools_from=None, origin_conn=None)) == ""


async def test_a_call_after_the_browser_closed_is_told_to_stop(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id="browser-install-1")
    driver = await open_client(http, daemon)
    try:
        await host.ok("tool.register", {"tools": [spec("host_echo")]})
        session_id = await new_session(driver, tmp_path / "d", hostToolsFrom="browser-install-1")
        session = daemon.core.sessions.get(session_id)
        await host.stop()
        for _ in range(100):
            if not HOST_TOOLS.names_for(session):
                break
            await asyncio.sleep(0.02)
        assert await run_turn(driver, session_id, "use host echo") == "complete"
        [result] = results_for(driver, "host_echo")
        assert result["ok"] is False
        assert str(result["error"]).startswith("host_unavailable: Snowpea Browser closed")
        assert "unknown tool" not in str(result["error"])

        search = await daemon.core.tools.get("tool_search").run(
            ToolContext(session=session, core=daemon.core, backend=None),  # type: ignore[arg-type]
            {"query": "host"},
        )
        assert "Browser tools are offline" in search.output
    finally:
        await driver.stop()
