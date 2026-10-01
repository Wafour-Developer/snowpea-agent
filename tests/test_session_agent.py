"""An agent definition shapes the human session it is chosen for (addendum 17).

``session.create {agent}``, ``session.setAgent`` and ``/agent use`` apply the
definition's prompt, tools allowlist and round budget; a browser-kind client
gets ``browser.defaultAgent`` when it names none; the choice survives a resume.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
from test_host_tools import Daemon, daemon, new_session, open_client  # noqa: F401

BROWSER_AGENT = """---
name: browser
description: Works in the Snowpea browser's agent tab.
tools: [read_file, repl]
max_tool_rounds: 17
---
Always work in the agent tab. Never change browser settings.
"""

CODER_AGENT = """---
name: coder
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
    (agents / "coder.md").write_text(CODER_AGENT, encoding="utf-8")
    return workdir


async def test_create_with_an_agent_applies_prompt_tools_and_rounds(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    workdir = _project(tmp_path)
    client = await open_client(http, daemon)
    try:
        session_id = await new_session(client, workdir, agent="browser")
        session = daemon.core.sessions.get(session_id)
        assert session.agent == "browser"
        assert "Always work in the agent tab." in (session.system_prompt or "")
        assert session.allowed_tools == {"read_file", "repl"}
        assert session.tool_rounds == 17
        # What the model is offered: the allowlist plus the always-allowed tools.
        offered = {tool.name for tool in daemon.core.tools.active(session)}
        assert "shell" not in offered and "read_file" in offered
        rows = {r["sessionId"]: r for r in (await client.ok("session.list", {}))["sessions"]}
        assert rows[session_id]["agent"] == "browser"

        unknown = await client.call(
            "session.create", {"workdir": str(workdir), "agent": "nobody"}
        )
        assert unknown["error"]["code"] == -32602
    finally:
        await client.stop()


async def test_set_agent_switches_persists_and_announces(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    workdir = _project(tmp_path)
    client = await open_client(http, daemon)
    try:
        session_id = await new_session(client, workdir, agent="browser")
        switched = await client.ok("session.setAgent", {"sessionId": session_id, "agent": "coder"})
        assert switched == {"sessionId": session_id, "agent": "coder"}
        session = daemon.core.sessions.get(session_id)
        assert session.allowed_tools is None  # tools: "*"
        assert session.tool_rounds is None
        assert (session.system_prompt or "").startswith("You write code.")
        await asyncio.sleep(0.05)
        assert [
            e["payload"]["agent"] for e in client.of_kind("agent.changed")
        ] == ["coder"]
        changed = [
            n["params"] for n in client.notifications
            if n["method"] == "sessions.changed" and n["params"]["reason"] == "agent"
        ]
        assert changed and changed[-1]["sessionId"] == session_id
        rows = await daemon.core.store.list_sessions(include_closed=True)
        assert next(r for r in rows if r["id"] == session_id)["agent"] == "coder"

        cleared = await client.ok("session.setAgent", {"sessionId": session_id, "agent": None})
        assert cleared["agent"] is None and session.system_prompt is None

        # /agent use goes through the same switch.
        await client.ok(
            "command.run", {"sessionId": session_id, "name": "agent", "args": "use browser"}
        )
        for _ in range(100):
            if session.agent == "browser":
                break
            await asyncio.sleep(0.02)
        assert session.agent == "browser" and session.tool_rounds == 17

        # After a restore the stored agent is re-applied at the next turn.
        await daemon.core.sessions.close(session_id)
        restored = await daemon.core.sessions.restore(session_id)
        assert restored is not None and restored.agent == "browser"
        assert restored.system_prompt is None and restored.agent_applied is False
        from snowpea_core.agent.session_agent import ensure_applied

        ensure_applied(daemon.core, restored)
        assert restored.allowed_tools == {"read_file", "repl"}
    finally:
        await client.stop()


async def test_a_browser_client_gets_the_default_agent(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    workdir = _project(tmp_path)
    daemon.core.settings.browser.defaultAgent = "browser"
    browser = await open_client(http, daemon)  # clientKind "browser"
    try:
        session_id = await new_session(browser, workdir)
        assert daemon.core.sessions.get(session_id).agent == "browser"
        explicit = await new_session(browser, workdir, agent="coder")
        assert daemon.core.sessions.get(explicit).agent == "coder"
    finally:
        daemon.core.settings.browser.defaultAgent = None
        await browser.stop()


async def test_two_clients_resuming_one_thread_at_once_share_one_session(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    """A double restore made two Session objects for one thread, so two turns
    ran in it at once and one sent the other's unanswered tool call."""
    client = await open_client(http, daemon)
    try:
        session_id = await new_session(client, tmp_path / "w")
        await daemon.core.sessions.close(session_id)
        first, second, third = await asyncio.gather(
            daemon.core.sessions.restore(session_id),
            daemon.core.sessions.restore(session_id),
            client.ok("session.resume", {"sessionId": session_id}),
        )
        assert first is not None and first is second
        assert daemon.core.sessions.get(session_id) is first
        assert third["sessionId"] == session_id
    finally:
        await client.stop()
