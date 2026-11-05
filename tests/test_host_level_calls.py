"""Protocol 1.40.0 host-level calls: ``llm.sample`` without a session, ``approval.evaluate``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import connect
from test_host_tools import Daemon, daemon, open_client, spec  # noqa: F401
from test_llm_sample import code_of, stub

from snowpea_core.permissions.allowlist import tool_target
from snowpea_core.server.protocol import CAPABILITIES


def test_the_capabilities() -> None:
    assert {"llmSampleHost", "approvalEvaluate"} <= set(CAPABILITIES)


# -- llm.sample without a session ---------------------------------------------------------


async def test_a_host_can_sample_without_a_session(
    http: aiohttp.ClientSession,
    daemon: Daemon,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    provider, asked = stub(monkeypatch, daemon, '{"price": 3, "title": "Cup"}')
    host = await open_client(http, daemon, client_id="profile-1")
    try:
        await host.ok("tool.register", {"tools": [spec("repl", "read")]})
        schema = {
            "type": "object",
            "properties": {"price": {"type": "number"}, "title": {"type": "string"}},
            "required": ["price", "title"],
        }
        result = await host.ok(
            "llm.sample", {"input": "Cup 3 dollars", "schema": schema, "modelCategory": "fast"}
        )
        assert result["json"] == {"price": 3, "title": "Cup"}
        assert result["usage"] == {"inputTokens": 11, "outputTokens": 7}
        assert provider.calls and provider.calls[0]["tools"] == []
        assert asked  # the daemon's own default model was looked up
        # nothing was written to any session
        assert daemon.core.sessions.all() == []
    finally:
        await host.stop()


async def test_a_plain_client_cannot_sample_without_a_session(
    http: aiohttp.ClientSession,
    daemon: Daemon,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    stub(monkeypatch, daemon, "x")
    plain = await connect(http, daemon)  # said hello, registered no host tools
    try:
        frame = await plain.call("llm.sample", {"input": "hello"})
        assert code_of(frame) == "unauthorized"
    finally:
        await plain.stop()


async def test_a_session_call_still_needs_its_own_host(
    http: aiohttp.ClientSession,
    daemon: Daemon,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    stub(monkeypatch, daemon, "x")
    other = await open_client(http, daemon)
    try:
        await other.ok("tool.register", {"tools": [spec("repl", "read")]})
        frame = await other.call("llm.sample", {"sessionId": "nope", "input": "hello"})
        assert code_of(frame) == "not_found"
    finally:
        await other.stop()


# -- approval.evaluate ---------------------------------------------------------------------


async def evaluate(host: Any, **params: Any) -> dict[str, Any]:
    return await host.ok("approval.evaluate", params)


async def test_the_mode_matrix_decides_before_the_allowlist(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id="profile-1")
    try:
        await host.ok("tool.register", {"tools": [spec("repl", "read")]})
        read = await evaluate(host, tool="repl", permission="read", mode="accept")
        assert read == {"decision": "allow", "by": "mode"}
        denied = await evaluate(host, tool="repl", permission="write", mode="plan")
        assert denied == {"decision": "deny", "by": "mode"}
        asks = await evaluate(host, tool="repl", permission="network", mode="accept")
        assert asks == {"decision": "ask", "by": "none"}
    finally:
        await host.stop()


async def test_a_saved_rule_of_this_profile_allows_and_another_profiles_does_not(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
) -> None:
    daemon.core.allowlist.add(
        "^.*$", "global", tool_target("repl"), origin="https://shop.example", host="profile-1"
    )
    mine = await open_client(http, daemon, client_id="profile-1")
    theirs = await open_client(http, daemon, client_id="profile-2")
    try:
        for host in (mine, theirs):
            await host.ok("tool.register", {"tools": [spec("repl", "network")]})
        args = {"tool": "repl", "permission": "network", "mode": "accept"}
        hit = await evaluate(mine, **args, site="https://shop.example")
        assert hit == {"decision": "allow", "by": "allowlist"}
        other_site = await evaluate(mine, **args, site="https://evil.example")
        assert other_site == {"decision": "ask", "by": "none"}
        other_profile = await evaluate(theirs, **args, site="https://shop.example")
        assert other_profile == {"decision": "ask", "by": "none"}
        # a payment, or an action the host cannot attribute to a site, is never remembered
        paid = await evaluate(mine, **args, site="https://shop.example", risk="payment")
        assert paid["decision"] == "ask"
        once = await evaluate(mine, **args, site="https://shop.example", args={"onceOnly": True})
        assert once["decision"] == "ask"
    finally:
        await mine.stop()
        await theirs.stop()


async def test_evaluate_changes_nothing_and_needs_a_host(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
) -> None:
    before = daemon.core.allowlist.list("global", any_host=True)
    plain = await connect(http, daemon)
    try:
        frame = await plain.call(
            "approval.evaluate", {"tool": "repl", "permission": "read", "mode": "auto"}
        )
        assert code_of(frame) == "unauthorized"
    finally:
        await plain.stop()
    assert daemon.core.allowlist.list("global", any_host=True) == before
