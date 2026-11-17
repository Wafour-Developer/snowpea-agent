"""A host tool's code is the host's: the security check does not read it as local code.

Regression in 0.2.88: the check read the ``code`` argument of EVERY tool as
local code, so the Snowpea browser's ``repl`` (JavaScript the browser runs in
its page, tag ``read``) became ``code:parse`` and, with ``security.unknownCode:
ask`` and the essential lists, every call asked a person — and an unattended
turn (a routine, a messenger message, an owned job) could not run it at all.

Whether a call goes to a host is the engine's own fact (the tool object the
registry dispatches for that session), never the name or the arguments: a core
or MCP tool named ``repl`` still has its code read, a host tool tagged ``exec``
too, and a host tool's path-like arguments are still judged.

Also: JavaScript / TypeScript given to a LOCAL code tool as ``language:
"javascript"`` is read by the node table (top-level ``await`` and ``for
await`` included) instead of failing as Python.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import fake_provider, make_daemon
from test_host_tools import open_client, owned_job, results_for, run_turn, spec

from snowpea_core.jarvis.classify import ClassifyRequest, classify
from snowpea_core.security import code as code_reader
from snowpea_core.security import shell as sh
from snowpea_core.security.check import Context, check_call
from snowpea_core.security.lists import from_raw
from snowpea_core.server.app_server import Daemon
from snowpea_core.tools.registry import Tool, ToolResult

#: The real call from the approvals log that asked in 0.2.88.
REPL_ARGS: dict[str, Any] = {
    "title": "Open the wiki",
    "code": (
        "const p = await openTab('http://127.0.0.1:47231/s/wiki/');\n"
        "await p.waitForLoadState();\nconst snap = await snapshot(p);\n"
        "console.log(snap.tree);"
    ),
}
UNREADABLE = "eval(atob(window.payload)); new Function(location.hash.slice(1))();"


def _script(home: Path) -> dict[str, Any]:
    def call(match: str, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"match": match, "text": "calling", "repeat": True,
             "tool_calls": [{"name": name, "arguments": args}]},
            {"after_tool": name, "text": f"{name} done", "repeat": True},
        ]

    return {
        "steps": [
            *call("use repl secret", "repl",
                  {**REPL_ARGS, "path": str(home / "credentials.json")}),
            *call("use host repl", "repl", REPL_ARGS),
            *call("use mcp repl", "mcp__srv__repl", REPL_ARGS),
            *call("use host runjs", "host_runjs", {"code": UNREADABLE}),
        ],
        "default": {"text": "fake default reply"},
    }


@pytest.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    home = tmp_path / "home"
    script = tmp_path / "script.json"
    script.write_text(json.dumps(_script(home)), encoding="utf-8")
    with fake_provider(script):
        daemon = await make_daemon(
            home, {"approvals": {"timeout_sec": 5}, "memory": {"enabled": False}}
        )
        try:
            yield daemon
        finally:
            await daemon.stop()


class _Spy:
    """Every ``approvals.request`` the engine makes, then the real one."""

    def __init__(self, daemon: Daemon) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._real = daemon.core.approvals.request

    async def __call__(self, session: Any, tool: str, args: dict[str, Any],
                       **kwargs: Any) -> Any:
        self.calls.append((tool, kwargs))
        return await self._real(session, tool, args, **kwargs)

    def tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]


@pytest.fixture
def spy(daemon: Daemon, monkeypatch: pytest.MonkeyPatch) -> _Spy:
    found = _Spy(daemon)
    monkeypatch.setattr(daemon.core.approvals, "request", found)
    return found


async def _register_repl(host: Any) -> None:
    host.handlers["repl"] = lambda p: {"ok": True, "output": "page tree"}
    await host.ok("tool.register", {"tools": [{
        **spec("repl", "read"),
        "inputSchema": {"type": "object", "properties": {
            "title": {"type": "string"}, "code": {"type": "string"},
            "path": {"type": "string"}}},
    }]})


async def _wait_for(predicate: Any, seconds: float = 20.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


# ---------------------------------------------------------------------------
# the engine: interactive sessions
# ---------------------------------------------------------------------------


async def test_a_host_repl_call_runs_without_an_approval_in_accept_mode(
    http: aiohttp.ClientSession, daemon: Daemon, spy: _Spy, tmp_path: Path
) -> None:
    host = await open_client(http, daemon, client_id="browser-H", approval_mode="deny")
    try:
        await _register_repl(host)
        session_id = await _new(host, tmp_path / "w")
        assert await run_turn(host, session_id, "use host repl") == "complete"
        assert [i["name"] for i in host.invocations] == ["repl"]
        assert host.invocations[0]["args"] == REPL_ARGS
        assert results_for(host, "repl")[-1]["output"] == "page tree"
        assert spy.tools() == [] and host.approval_requests == []
    finally:
        await host.stop()


@pytest.mark.parametrize("target", ["browser-H", "browser"])
async def test_host_tools_from_a_client_id_or_the_browser_alias(
    http: aiohttp.ClientSession, daemon: Daemon, spy: _Spy, tmp_path: Path, target: str
) -> None:
    """``hostToolsFrom`` naming the clientId, or the kind alias ``browser`` (job templates)."""
    host = await open_client(http, daemon, client_id="browser-H", approval_mode="deny")
    driver = await open_client(http, daemon, kind="ide", approval_mode="deny")
    try:
        await _register_repl(host)
        session_id = await _new(driver, tmp_path / "d", hostToolsFrom=target)
        assert await run_turn(driver, session_id, "use host repl") == "complete"
        assert [i["name"] for i in host.invocations] == ["repl"]
        assert spy.tools() == [] and driver.approval_requests == []
    finally:
        await driver.stop()
        await host.stop()


async def test_a_core_tool_named_repl_still_has_its_code_read(
    http: aiohttp.ClientSession, daemon: Daemon, spy: _Spy, tmp_path: Path
) -> None:
    ran: list[dict[str, Any]] = []

    async def run(ctx: Any, args: dict[str, Any]) -> ToolResult:
        ran.append(args)
        return ToolResult(ok=True, output="ran locally")

    daemon.core.tools.register(Tool(
        name="repl", category="test", description="a local repl",
        input_schema={"type": "object", "properties": {"code": {"type": "string"}}},
        permission="read", run=run,
    ))
    driver = await open_client(http, daemon, kind="ide", approval_mode="deny")
    try:
        session_id = await _new(driver, tmp_path / "w")
        await run_turn(driver, session_id, "use host repl")
        assert spy.tools() == ["repl"]
        request = driver.approval_requests[-1]
        assert request["tool"] == "repl" and request.get("personOnly") is True
        assert ran == []
    finally:
        await driver.stop()


async def test_an_mcp_tool_named_repl_still_has_its_code_read(
    http: aiohttp.ClientSession, daemon: Daemon, spy: _Spy, tmp_path: Path
) -> None:
    ran: list[dict[str, Any]] = []

    async def run(ctx: Any, args: dict[str, Any]) -> ToolResult:
        ran.append(args)
        return ToolResult(ok=True, output="mcp ran")

    daemon.core.tools.register(Tool(
        name="mcp__srv__repl", category="mcp", description="an MCP repl",
        input_schema={"type": "object", "properties": {"code": {"type": "string"}}},
        permission="read", run=run, source="mcp:srv",
    ))
    # A host connected to the same session registers its own repl: the MCP
    # tool is still not the host's.
    host = await open_client(http, daemon, client_id="browser-H", approval_mode="deny")
    try:
        await _register_repl(host)
        session_id = await _new(host, tmp_path / "w")
        await run_turn(host, session_id, "use mcp repl")
        assert spy.tools() == ["mcp__srv__repl"]
        assert host.approval_requests[-1].get("personOnly") is True
        assert ran == [] and host.invocations == []
    finally:
        await host.stop()


async def test_a_host_tool_tagged_exec_still_has_its_code_read(
    http: aiohttp.ClientSession, daemon: Daemon, spy: _Spy, tmp_path: Path
) -> None:
    host = await open_client(http, daemon, client_id="browser-H", approval_mode="deny")
    try:
        host.handlers["host_runjs"] = lambda p: {"ok": True, "output": "ran"}
        await host.ok("tool.register", {"tools": [{
            **spec("host_runjs", "exec"),
            "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}},
        }]})
        session_id = await _new(host, tmp_path / "w")
        await run_turn(host, session_id, "use host runjs")
        assert spy.tools() == ["host_runjs"]
        request = host.approval_requests[-1]
        assert request["tool"] == "host_runjs" and request.get("personOnly") is True
        assert request.get("reason") == "unknown_code" or request.get("reason")
        assert host.invocations == []
    finally:
        await host.stop()


async def test_a_host_tools_protected_path_is_still_stopped(
    http: aiohttp.ClientSession, daemon: Daemon, spy: _Spy, tmp_path: Path
) -> None:
    host = await open_client(http, daemon, client_id="browser-H", approval_mode="allow")
    try:
        await _register_repl(host)
        session_id = await _new(host, tmp_path / "w")
        await run_turn(host, session_id, "use repl secret")
        assert host.invocations == [] and spy.tools() == []
        assert "blocked by the security list" in (
            results_for(host, "repl")[-1].get("error") or results_for(host, "repl")[-1]["output"]
        )
    finally:
        await host.stop()


async def test_a_hosts_own_approval_ask_for_its_repl_is_not_person_only(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path
) -> None:
    """``approval.ask`` from the host for a ``repl`` action: judged as the host's tool."""
    host = await open_client(http, daemon, client_id="browser-H", approval_mode="allow")
    try:
        await _register_repl(host)
        session_id = await _new(host, tmp_path / "w")
        answer = await host.ok("approval.ask", {
            "sessionId": session_id, "tool": "repl.upload", "permission": "send",
            "reason": "upload a file", "args": dict(REPL_ARGS),
        })
        assert answer["decision"] == "allow"
        assert host.approval_requests[-1].get("personOnly") is None
    finally:
        await host.stop()


# ---------------------------------------------------------------------------
# the engine: nobody to ask — routines, messenger turns, owned jobs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["browser-R", "browser"])
async def test_a_routine_runs_the_host_repl_unattended(
    http: aiohttp.ClientSession, daemon: Daemon, spy: _Spy, tmp_path: Path, target: str
) -> None:
    host = await open_client(http, daemon, client_id="browser-R", approval_mode="deny")
    watcher = await open_client(http, daemon, kind="ide", approval_mode="deny")
    try:
        await _register_repl(host)
        (tmp_path / "w").mkdir()
        job = await watcher.ok("job.schedule", {
            "spec": "every 1 hour", "task": "use host repl", "workdir": str(tmp_path / "w"),
            "sessionTemplate": {"hostToolsFrom": target, "hostWaitSec": 5},
        })
        await watcher.ok("job.runNow", {"jobId": job["jobId"]})
        await _wait_for(lambda: bool(host.invocations))
        assert [i["name"] for i in host.invocations] == ["repl"]
        assert spy.tools() == []
    finally:
        await watcher.stop()
        await host.stop()


async def test_a_messenger_turn_runs_the_host_repl_unattended(
    http: aiohttp.ClientSession, daemon: Daemon, spy: _Spy, tmp_path: Path
) -> None:
    from snowpea_core.agent import loop as agent_loop

    host = await open_client(http, daemon, client_id="browser-M", approval_mode="deny")
    try:
        await _register_repl(host)
        (tmp_path / "w").mkdir()
        session = await daemon.core.sessions.create(tmp_path / "w")
        session.host_tools_from = "browser-M"
        session.origin_conn = None
        # As the gateway starts a bound conversation's turn (gateway/router.py).
        agent_loop.start_turn(daemon.core, session, "use host repl", unattended=True,
                              origin="human", surface="telegram")
        await _wait_for(lambda: bool(host.invocations))
        assert [i["name"] for i in host.invocations] == ["repl"]
        assert spy.tools() == []
    finally:
        await host.stop()


async def test_an_owned_browser_job_runs_the_host_repl(
    http: aiohttp.ClientSession, daemon: Daemon, spy: _Spy, tmp_path: Path
) -> None:
    host = await open_client(http, daemon, client_id="snowpea-browser-S", approval_mode="deny")
    try:
        await _register_repl(host)
        job = await owned_job(host, tmp_path / "w", task="use host repl")
        await host.ok("job.runNow", {"jobId": job["jobId"]})
        await _wait_for(lambda: bool(host.invocations))
        assert [i["name"] for i in host.invocations] == ["repl"]
        session = daemon.core.sessions.get(host.invocations[0]["sessionId"])
        assert session is None or session.owned_by == "snowpea-browser-S"
        assert spy.tools() == []
    finally:
        await host.stop()


async def _new(client: Any, workdir: Path, **extra: Any) -> str:
    workdir.mkdir(parents=True, exist_ok=True)
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept",
                                                **extra})
    return str(result["sessionId"])


# ---------------------------------------------------------------------------
# the check itself
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path, host: bool) -> Context:
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "w").mkdir(exist_ok=True)
    return Context(workdir=tmp_path / "w", home=tmp_path / "home", host_tool=host)


def test_the_check_skips_a_host_tools_code_only_when_told_and_not_exec(tmp_path: Path) -> None:
    lists = from_raw({})
    assert check_call("repl", REPL_ARGS, lists, _ctx(tmp_path, False), tags=("read",)).opaque
    assert not check_call("repl", REPL_ARGS, lists, _ctx(tmp_path, True), tags=("read",)).opaque
    assert check_call("repl", REPL_ARGS, lists, _ctx(tmp_path, True), tags=("exec",)).opaque
    # The CODE_TOOL_NAMES fallback (a bare string) is the host's too.
    assert not check_call("execute_code", {"x": UNREADABLE}, lists, _ctx(tmp_path, True),
                          tags=("read",)).opaque
    assert check_call("execute_code", {"x": UNREADABLE}, lists, _ctx(tmp_path, False),
                      tags=("read",)).opaque


def test_a_host_tools_paths_are_still_judged(tmp_path: Path) -> None:
    lists = from_raw({})
    ctx = _ctx(tmp_path, True)
    secret = {**REPL_ARGS, "path": str(tmp_path / "home" / "credentials.json")}
    assert check_call("repl", secret, lists, ctx, tags=("read",)).blocked
    settings = {"path": str(tmp_path / "home" / "settings.json"),
                "content": json.dumps({"security": {"unknownCode": "allow"}})}
    assert check_call("host_write", {**REPL_ARGS, **settings}, lists, ctx,
                      tags=("write",)).blocked
    # The same verdict as without the exemption, whatever the code says.
    for args, tags in ((secret, ("read",)), ({**REPL_ARGS, **settings}, ("write",))):
        assert check_call("repl", {**args, "code": "1"}, lists, _ctx(tmp_path, False),
                          tags=tags).blocked


def test_jarvis_classifies_a_host_repl_by_its_tag() -> None:
    verdict = classify(ClassifyRequest(tool="repl", args=dict(REPL_ARGS), tags=("read",)))
    assert verdict.classification == "safe"


# ---------------------------------------------------------------------------
# the JavaScript reader for LOCAL code tools
# ---------------------------------------------------------------------------

TOP_LEVEL = [
    REPL_ARGS["code"],
    "for await (const line of readLines()) { console.log(line); }",
    "const r = await fetch('https://example.com');\nexport default await r.json();",
]


@pytest.mark.parametrize("language", ["javascript", "js", "JavaScript", "typescript", "ts",
                                      "mjs", "node"])
@pytest.mark.parametrize("code", TOP_LEVEL)
def test_top_level_await_is_read_as_javascript(language: str, code: str) -> None:
    view = sh.code_view(code, cwd="/tmp/w", home="/tmp/h", user_home="/home/u",
                        language=language)
    assert view.opaque == []


def test_a_local_code_tools_javascript_is_judged_not_waved_through(tmp_path: Path) -> None:
    lists = from_raw({})
    ctx = _ctx(tmp_path, False)
    benign = {"code": REPL_ARGS["code"], "language": "javascript"}
    assert not check_call("run_code", benign, lists, ctx, tags=("exec",)).opaque
    target = tmp_path / "home" / "settings.json"
    harmful = {"code": f"await fs.promises.rm('{target}');", "language": "javascript"}
    assert check_call("run_code", harmful, lists, ctx, tags=("exec",)).blocked
    unreadable = {"code": UNREADABLE, "language": "typescript"}
    assert check_call("run_code", unreadable, lists, ctx, tags=("exec",)).opaque


def test_command_line_program_names_are_unchanged() -> None:
    # ``ts`` on a command line is moreutils' filter, not TypeScript.
    assert "ts" not in code_reader.LANGUAGES and "javascript" not in code_reader.LANGUAGES
    assert code_reader.CODE_LANGUAGE_NAMES["ts"] == "node"
