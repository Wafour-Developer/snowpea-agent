"""Addendum 27, C1: memory and site memory can be turned off for one session.

The browser's eval runner compares off / cold / warm runs; flipping the user's
own ``memory.enabled`` or ``browser.siteMemory`` for that would change every
other session too.
"""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest
from _support import connect
from test_host_tools import Daemon, daemon, open_client  # noqa: F401
from test_site_memory import ORIGIN, code_of, entry

import snowpea_core.memory as memory_mod
from snowpea_core.agent.agent import memory_enabled
from snowpea_core.memory.tools import INACTIVE, save_memory, search_memory
from snowpea_core.server import errors
from snowpea_core.tools.registry import ToolContext

pytestmark = pytest.mark.asyncio

A = "snowpea-browser-A"


def _ctx(core: object, session: object) -> ToolContext:
    return ToolContext(core=core, session=session, backend=None)  # type: ignore[arg-type]


async def _create(client: object, workdir: Path, **extra: object) -> str:
    workdir.mkdir(parents=True, exist_ok=True)
    created = await client.ok("session.create", {"workdir": str(workdir), **extra})  # type: ignore[attr-defined]
    return str(created["sessionId"])


async def test_memory_override_for_one_session_only(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    tmp_path: Path,
) -> None:
    """This daemon runs with memory.enabled off: memory: true turns it on for one
    session, memory: false keeps it off even for its tools, and a session with
    no override follows the setting."""
    core = daemon.core
    client = await connect(http, daemon)
    try:
        await client.ok("memory.write", {"text": "배포 대상은 duho 서버다"})
        on = core.sessions.get(await _create(client, tmp_path / "on", memory=True))
        off = core.sessions.get(await _create(client, tmp_path / "off", memory=False))
        plain = core.sessions.get(await _create(client, tmp_path / "plain"))
        assert (on.memory_override, off.memory_override, plain.memory_override) == (
            True,
            False,
            None,
        )

        assert "duho" in await memory_mod.context_for_turn(core, on, "배포 대상")
        assert await memory_mod.context_for_turn(core, off, "배포 대상") == ""
        assert await memory_mod.context_for_turn(core, plain, "배포 대상") == ""
        assert [memory_enabled(core, s) for s in (on, off, plain)] == [True, False, False]

        assert await memory_mod.nudge_after_turn(core, off, "기억해: 나는 로켓배송만 써") is None
        assert await memory_mod.nudge_after_turn(core, on, "기억해: 나는 로켓배송만 써")

        off_ctx = _ctx(core, off)
        saved = await save_memory(off_ctx, {"text": "다크 모드", "scope": "global"})
        found = await search_memory(off_ctx, {"query": "duho"})
        assert saved.ok is False and saved.error == INACTIVE
        assert found.ok is False and found.error == INACTIVE
        assert (await search_memory(_ctx(core, on), {"query": "duho"})).ok
    finally:
        await client.stop()


async def test_site_memory_off_for_one_session_only(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    tmp_path: Path,
) -> None:
    host = await open_client(http, daemon, client_id=A)
    try:
        off_id = await _create(host, tmp_path / "off", siteMemory=False)
        on_id = await _create(host, tmp_path / "on")
        put = await host.ok("site.put", {"origin": ORIGIN, "entry": entry(), "sessionId": on_id})
        for method, params in (
            ("site.get", {"origin": ORIGIN}),
            ("site.put", {"origin": ORIGIN, "entry": entry("login", "/login")}),
            ("site.mark", {"origin": ORIGIN, "entryId": put["entryId"], "outcome": "ok"}),
            (
                "site.markPart",
                {
                    "origin": ORIGIN,
                    "entryId": put["entryId"],
                    "part": "flow",
                    "name": "search",
                    "outcome": "ok",
                },
            ),
        ):
            refused = await host.call(method, {**params, "sessionId": off_id})
            assert code_of(refused) == errors.TOOL_INACTIVE, method
            assert "for this session" in refused["error"]["message"]
        got = await host.ok("site.get", {"origin": ORIGIN, "sessionId": on_id})
        assert len(got["entries"]) == 1

        # siteMemory: true wins over the setting being off.
        daemon.core.settings.browser.siteMemory = False
        forced_id = await _create(host, tmp_path / "forced", siteMemory=True)
        assert (await host.call("site.get", {"origin": ORIGIN}))["error"]
        assert await host.ok("site.get", {"origin": ORIGIN, "sessionId": forced_id})
    finally:
        await host.stop()
