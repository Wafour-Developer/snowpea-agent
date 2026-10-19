"""Page actions answered "always, on every site", and who serves a hooked call.

snowpea-browser -8 (protocol 1.19.0):

* ``repl.send`` / ``upload`` / ``script`` / ``navigate`` / ``fetch`` /
  ``download`` answered "always" are stored as the origin-less global rule the
  card describes, and a later session is not asked again (capability
  ``pageActionAlways``).  "project" stays ``once`` for them, and a host's
  ``onceOnly`` (payments) is never remembered.
* PreToolUse payloads say who serves the call (``tool_provider``) and who
  serves the session's browser (``browser_provider``), so the browser guard
  need not read state.db for either.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from test_host_tools import Daemon, daemon, new_session, open_client, spec  # noqa: F401

from snowpea_core.permissions.approval_queue import REPL_ALWAYS_OK, Decision, guard_scope
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION, ApprovalRequest
from snowpea_core.skills import hooks

KINDS = sorted(REPL_ALWAYS_OK)


def _request(tool: str, **args: Any) -> ApprovalRequest:
    return ApprovalRequest(
        requestId="r1", sessionId="s1", tool=tool, args={"url": "https://shop.example/", **args}
    )


def test_the_capability_and_version() -> None:
    assert "pageActionAlways" in CAPABILITIES
    assert tuple(map(int, PROTOCOL_VERSION.split("."))) >= (1, 19, 0)
    assert set(KINDS) == {
        "repl.send", "repl.upload", "repl.script", "repl.navigate", "repl.fetch", "repl.download",
    }


@pytest.mark.parametrize("tool", KINDS)
def test_always_is_kept_for_the_page_action_kinds(tool: str) -> None:
    assert guard_scope(_request(tool), Decision("allow", "always")).scope == "always"
    # A project rule would cover every site without saying so.
    assert guard_scope(_request(tool), Decision("allow", "project")).scope == "once"


def test_once_only_and_other_page_actions_stay_once() -> None:
    paying = guard_scope(_request("repl.send", onceOnly=True), Decision("allow", "always"))
    assert paying.scope == "once" and paying.picked == "always"
    assert guard_scope(_request("repl.click"), Decision("allow", "always")).scope == "once"


def _ask(session_id: str, tool: str, **args: Any) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "tool": tool,
        "permission": "send",
        "reason": "Send the message",
        "args": {"url": "https://mail.example/compose", **args},
    }


async def test_always_for_send_is_stored_and_honoured_in_a_new_session(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    host = await open_client(http, daemon, approval_mode="allow", approval_scope="always")
    try:
        await host.ok("tool.register", {"tools": [spec("repl", "read")]})
        first_session = await new_session(host, tmp_path / "a", mode="accept")
        first = await host.ok("approval.ask", _ask(first_session, "repl.send"))
        assert first == {"decision": "allow", "scope": "always", "by": "user"}
        listed = await host.ok("permission.allowlist.list", {"scope": "always"})
        assert [p for p in listed["patterns"] if "repl.send" in json.dumps(p)]

        asked = len(host.approval_requests)
        second_session = await new_session(host, tmp_path / "b", mode="accept")
        again = await host.ok(
            "approval.ask",
            dict(_ask(second_session, "repl.send"), args={"url": "https://other.example/"}),
        )
        assert again["decision"] == "allow" and again["by"] == "allowlist"
        assert len(host.approval_requests) == asked
    finally:
        await host.stop()


async def test_a_payment_is_never_stored(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    host = await open_client(http, daemon, approval_mode="allow", approval_scope="always")
    try:
        await host.ok("tool.register", {"tools": [spec("repl", "read")]})
        session_id = await new_session(host, tmp_path / "a", mode="accept")
        pay = _ask(session_id, "repl.send", onceOnly=True, reasonCode="payment")
        first = await host.ok("approval.ask", pay)
        assert first["decision"] == "allow"
        again = await host.ok("approval.ask", pay)
        assert again["by"] == "user", "a payment answer was remembered"
        listed = await host.ok("permission.allowlist.list", {"scope": "always"})
        assert not [p for p in listed["patterns"] if "repl.send" in json.dumps(p)]
    finally:
        await host.stop()


# ---------------------------------------------------------------------------
# hook payloads
# ---------------------------------------------------------------------------


def _hooked_core(tmp_path: Path, script: Path, tools: dict[str, Any]) -> Any:
    registry = hooks.HookRegistry()
    registry.add(
        hooks.Hook(event="PreToolUse", matcher="*", command=f"${{SNOWPEA_PYTHON}} {script}")
    )
    return SimpleNamespace(
        paths=SimpleNamespace(home=tmp_path),
        skills=SimpleNamespace(hooks=registry),
        tools=SimpleNamespace(get=lambda name, session=None: tools.get(name)),
    )


async def test_the_payload_says_who_serves_the_call(tmp_path: Path) -> None:
    seen = tmp_path / "payloads.jsonl"
    script = tmp_path / "record.py"
    script.write_text(
        "import sys\n"
        f"open({str(seen)!r}, 'a').write(sys.stdin.read() + '\\n')\n",
        encoding="utf-8",
    )
    tools = {
        "browser_click": SimpleNamespace(source="host:browser-1"),
        "read_file": SimpleNamespace(source="builtin"),
        "github__create_issue": SimpleNamespace(source="mcp:github"),
        "lsp_hover": SimpleNamespace(source="lsp"),
    }
    core = _hooked_core(tmp_path, script, tools)
    session = SimpleNamespace(id="s-hook", workdir=tmp_path)
    for name in (*tools, "no_such_tool"):
        assert await hooks.pre_tool_use(core, session, name, {}) is None
    payloads = {p["tool_name"]: p for p in map(json.loads, seen.read_text().splitlines())}
    assert payloads["browser_click"]["tool_provider"] == "host"
    assert payloads["read_file"]["tool_provider"] == "builtin"
    assert payloads["github__create_issue"]["tool_provider"] == "mcp"
    assert payloads["lsp_hover"]["tool_provider"] == "builtin"
    assert payloads["no_such_tool"]["tool_provider"] is None
    # Not a browser session: no browser provider.
    assert all("browser_provider" in p and p["browser_provider"] is None for p in payloads.values())


async def test_a_registered_host_tool_is_a_host_tool_to_the_payload(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    host = await open_client(http, daemon)
    try:
        await host.ok("tool.register", {"tools": [spec("repl", "read")]})
        session = daemon.core.sessions.get(await new_session(host, tmp_path / "w"))
        assert hooks.tool_provider(daemon.core, session, "repl") == "host"
        assert hooks.tool_provider(daemon.core, session, "read_file") == "builtin"
    finally:
        await host.stop()
