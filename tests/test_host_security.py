"""Host-security-review hardening for snowpea-browser sessions.

* ``session.attach`` / ``session.resume`` never replace a persisted
  ``origin_surface`` of ``"browser"``.
* ``session.setAgent`` is limited to the session's own surfaces.
* ``approval.ask`` for a local dev server navigation may be remembered for the
  project and that URL's origin only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
from _support import RpcClient
from test_host_tools import Daemon, daemon, new_session, open_client, spec  # noqa: F401

from snowpea_core.server.protocol import PROTOCOL_VERSION

BROWSER_AGENT = """---
name: browser
description: Works in the Snowpea browser's agent tab.
tools: [read_file, repl]
---
Browser rules.
"""

BROWSER_CODE_AGENT = """---
name: browser-code
description: Codes from the browser.
tools: "*"
---
You write code.
"""


def _project(tmp_path: Path) -> Path:
    workdir = tmp_path / "w"
    agents = workdir / ".snowpea" / "agents"
    agents.mkdir(parents=True)
    (agents / "browser.md").write_text(BROWSER_AGENT, encoding="utf-8")
    (agents / "browser-code.md").write_text(BROWSER_CODE_AGENT, encoding="utf-8")
    return workdir


async def open_surface(
    http: aiohttp.ClientSession, daemon: Daemon, kind: str = "tui"  # noqa: F811
) -> RpcClient:
    """A non-browser client (TUI, IDE) on the same daemon."""
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    client = RpcClient(ws)
    client.start()
    await client.ok(
        "system.hello",
        {
            "token": daemon.token,
            "clientVersion": f"{kind}-test",
            "protocolVersion": PROTOCOL_VERSION,
            "clientKind": kind,
        },
    )
    return client


# ---------------------------------------------------------------------------
# 1. origin_surface "browser" survives attach and resume
# ---------------------------------------------------------------------------


async def test_attach_never_replaces_a_browser_origin_surface(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    owner = await open_client(http, daemon, client_id="browser-A")
    other = await open_client(http, daemon, client_id="browser-B")
    tui = await open_surface(http, daemon)
    try:
        session_id = await new_session(owner, tmp_path / "w", originSurface="browser")
        session = daemon.core.sessions.get(session_id)
        assert session.origin_surface == "browser"
        await other.ok("tool.register", {"tools": [spec("repl")]})
        await other.ok("session.attach", {"sessionId": session_id, "hostToolsFrom": "browser-B"})
        assert session.origin_surface == "browser"
        await tui.ok("session.attach", {"sessionId": session_id})
        assert session.origin_surface == "browser"
        row = await daemon.core.store.session(session_id)
        assert row is not None and row["origin_surface"] == "browser"
    finally:
        await tui.stop()
        await other.stop()
        await owner.stop()


async def test_resume_from_another_surface_keeps_the_persisted_browser_origin(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    owner = await open_client(http, daemon, client_id="browser-A")
    session_id = await new_session(owner, tmp_path / "w", originSurface="browser")
    await daemon.core.sessions.close(session_id)
    await owner.stop()
    tui = await open_surface(http, daemon)
    try:
        await tui.ok("session.resume", {"sessionId": session_id})
        restored = daemon.core.sessions.get(session_id)
        assert restored is not None and restored.origin_surface == "browser"
        # The live origin connection moved; the origin surface did not.
        assert restored.origin_conn is not None
        row = await daemon.core.store.session(session_id)
        assert row is not None and row["origin_surface"] == "browser"
    finally:
        await tui.stop()


# ---------------------------------------------------------------------------
# 2. session.setAgent only from the session's own surfaces
# ---------------------------------------------------------------------------


async def test_set_agent_on_a_browser_root_is_refused_to_other_connections(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    workdir = _project(tmp_path)
    owner = await open_client(http, daemon, client_id="browser-A")
    stranger = await open_client(http, daemon, client_id="browser-B")
    tui = await open_surface(http, daemon)
    try:
        session_id = await new_session(
            owner, workdir, originSurface="browser", agent="browser"
        )
        for client in (stranger, tui):
            frame = await client.call(
                "session.setAgent", {"sessionId": session_id, "agent": "browser-code"}
            )
            assert frame["error"]["data"]["code"] == "unauthorized"
        assert daemon.core.sessions.get(session_id).agent == "browser"
        allowed = await owner.ok(
            "session.setAgent", {"sessionId": session_id, "agent": "browser-code"}
        )
        assert allowed["agent"] == "browser-code"
    finally:
        await tui.stop()
        await stranger.stop()
        await owner.stop()


async def test_set_agent_from_the_sessions_browser_host(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    workdir = _project(tmp_path)
    host = await open_client(http, daemon, client_id="browser-H")
    stranger = await open_client(http, daemon, client_id="browser-X")
    tui = await open_surface(http, daemon)
    try:
        await host.ok("tool.register", {"tools": [spec("repl")]})
        session_id = await new_session(tui, workdir, hostToolsFrom="browser-H")
        refused = await stranger.call(
            "session.setAgent", {"sessionId": session_id, "agent": "browser"}
        )
        assert refused["error"]["data"]["code"] == "unauthorized"
        assert (
            await host.ok("session.setAgent", {"sessionId": session_id, "agent": "browser"})
        )["agent"] == "browser"
        # The TUI that opened it is its origin connection.
        assert (
            await tui.ok("session.setAgent", {"sessionId": session_id, "agent": None})
        )["agent"] is None
    finally:
        await tui.stop()
        await stranger.stop()
        await host.stop()


async def test_owner_surfaces_may_set_agent_on_non_browser_sessions(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    workdir = _project(tmp_path)
    tui = await open_surface(http, daemon, "tui")
    ide = await open_surface(http, daemon, "ide")
    browser = await open_client(http, daemon, client_id="browser-B")
    try:
        session_id = await new_session(tui, workdir)
        switched = await ide.ok(
            "session.setAgent", {"sessionId": session_id, "agent": "browser-code"}
        )
        assert switched["agent"] == "browser-code"
        refused = await browser.call(
            "session.setAgent", {"sessionId": session_id, "agent": None}
        )
        assert refused["error"]["data"]["code"] == "unauthorized"
    finally:
        await browser.stop()
        await ide.stop()
        await tui.stop()


# ---------------------------------------------------------------------------
# 4. "always for this project" on a local dev server navigation
# ---------------------------------------------------------------------------


def _nav(session_id: str, url: str, project: Path | None, **args: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"url": url, "reasonCode": "localDevServer", **args}
    if project is not None:
        payload["project"] = str(project)
    return {
        "sessionId": session_id,
        "tool": "repl.navigate",
        "permission": "send",
        "reason": f"Open {url}",
        "args": payload,
    }


async def test_local_dev_server_project_answer_is_remembered_per_origin(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    host = await open_client(http, daemon, approval_mode="allow", approval_scope="project")
    try:
        await host.ok("tool.register", {"tools": [spec("repl", "read")]})
        workdir = tmp_path / "proj"
        session_id = await new_session(host, workdir, mode="accept")

        first = await host.ok("approval.ask", _nav(session_id, "http://localhost:5173/", workdir))
        assert first == {"decision": "allow", "scope": "project", "by": "user"}
        assert host.approval_requests[-1]["scopeHint"] == "project"
        stored = json.loads((workdir / ".snowpea" / "settings.json").read_text("utf-8"))
        [rule] = [e for e in stored["allowlist"] if e["target"] == "tool:repl.navigate"]
        assert rule["origin"] == "http://localhost:5173"
        assert rule["reason_code"] == "localDevServer"

        asked = len(host.approval_requests)
        again = await host.ok(
            "approval.ask", _nav(session_id, "http://localhost:5173/admin", workdir)
        )
        assert again["by"] == "allowlist" and len(host.approval_requests) == asked

        # Another origin, no reasonCode, or another action: a person is asked.
        host.approval_mode = "deny"
        other_port = await host.ok(
            "approval.ask", _nav(session_id, "http://localhost:3000/", workdir)
        )
        assert other_port["decision"] == "deny" and other_port["by"] == "user"
        plain = _nav(session_id, "http://localhost:5173/", workdir)
        plain["args"].pop("reasonCode")
        assert (await host.ok("approval.ask", plain))["by"] == "user"
        send = dict(_nav(session_id, "http://localhost:5173/", workdir), tool="repl.send")
        assert (await host.ok("approval.ask", send))["by"] == "user"
        assert len(host.approval_requests) == asked + 3
    finally:
        await host.stop()


async def test_local_dev_server_project_answer_needs_the_sessions_project(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    host = await open_client(http, daemon, approval_mode="allow", approval_scope="project")
    try:
        await host.ok("tool.register", {"tools": [spec("repl", "read")]})
        workdir = tmp_path / "proj"
        session_id = await new_session(host, workdir, mode="accept")
        elsewhere = tmp_path / "other"
        answer = await host.ok(
            "approval.ask", _nav(session_id, "http://localhost:5173/", elsewhere)
        )
        assert answer["scope"] == "once"
        assert host.approval_requests[-1]["scopeHint"] != "project"
        # An ordinary navigation answered "project" stays once, as before.
        plain = _nav(session_id, "http://localhost:5173/", workdir)
        plain["args"].pop("reasonCode")
        assert (await host.ok("approval.ask", plain))["scope"] == "once"
        listed = await host.ok(
            "permission.allowlist.list", {"scope": "project", "workdir": str(workdir)}
        )
        assert listed["patterns"] == []
    finally:
        await host.stop()
