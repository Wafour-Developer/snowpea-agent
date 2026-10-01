"""Browser sessions never fall back to core's own browser (addendum 9).

The owner's rule: in a session the Snowpea browser opened, browser work always
happens in the browser's agent tab. When the browser is gone the tools fail
with ``host_unavailable`` and are hidden from the model; only the user's
explicit per-session opt-in (``session.setBrowserProvider``) allows core's own
browser, and a browser session may not change ``browser.*`` settings.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from test_host_tools import Daemon, daemon, new_session, open_client, spec  # noqa: F401

from snowpea_core.permissions.policy import MODE_MATRIX, UNPROMOTABLE
from snowpea_core.session.session import Session
from snowpea_core.tools import browser, browser_providers
from snowpea_core.tools.host_tools import HOST_TOOLS
from snowpea_core.tools.registry import ToolContext


def _browser_session(tmp_path: Path, **extra: Any) -> Session:
    session = Session(id="s-browser", workdir=tmp_path)
    session.origin_surface = "browser"
    session.host_tools_from = "snowpea-browser-A"
    for key, value in extra.items():
        setattr(session, key, value)
    return session


def _ctx(session: Session) -> ToolContext:
    settings = SimpleNamespace(browser=SimpleNamespace(provider="local_chromium"))
    core = SimpleNamespace(settings=settings)
    return ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]


async def test_a_browser_session_without_its_browser_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(HOST_TOOLS, "names_for", lambda session: [])
    called: list[str] = []

    class Local:
        meta = SimpleNamespace(id="local_chromium", label="local")

        def available(self, settings: Any = None) -> bool:
            return True

        async def navigate(self, session_id: str, url: str) -> Any:
            called.append(url)
            raise AssertionError("core's own browser must not run")

    monkeypatch.setattr(browser_providers, "resolve", lambda settings: Local())
    result = await browser.browser_navigate(
        _ctx(_browser_session(tmp_path)), {"url": "https://example.com"}
    )
    assert result.ok is False
    assert result.error and result.error.startswith("host_unavailable:")
    assert "브라우저 연결이 끊겼어요" in result.error
    assert called == []


def test_the_user_opt_in_is_the_only_way_to_core_s_own_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(HOST_TOOLS, "names_for", lambda session: [])
    settings = SimpleNamespace(browser=SimpleNamespace(provider="host"))
    locked = _browser_session(tmp_path)
    assert browser_providers.browser_locked(locked) is True
    opted = _browser_session(tmp_path, browser_provider="local")
    assert browser_providers.browser_locked(opted) is False
    # Even with browser.provider=host configured, "local" means core's own browser.
    assert browser_providers.resolve_for_session(settings, opted).meta.id == "local_chromium"
    ide = Session(id="s-ide", workdir=tmp_path)
    assert browser_providers.browser_locked(ide) is False
    assert browser_providers.browser_provider_of(ide) is None
    assert browser_providers.browser_provider_of(locked) == "host"
    assert browser_providers.browser_provider_of(opted) == "local"


def test_a_browser_session_may_not_change_browser_settings(tmp_path: Path) -> None:
    session = _browser_session(tmp_path)
    refusal = browser_providers.browser_settings_refusal(
        session, "settings_set", {"key": "browser.headless", "value": False}
    )
    assert refusal and "browser.headless" in refusal
    assert (
        browser_providers.browser_settings_refusal(
            session, "settings_set", {"key": "search.provider", "value": "ddgs"}
        )
        is None
    )
    ide = Session(id="s-ide", workdir=tmp_path)
    assert (
        browser_providers.browser_settings_refusal(
            ide, "settings_set", {"key": "browser.headless", "value": False}
        )
        is None
    )


def test_settings_changes_always_ask_and_are_never_remembered() -> None:
    """settings_set carries the config tag: asked in accept and auto, refused in
    plan, and never promoted by the allowlist or cached for the session."""
    assert MODE_MATRIX["accept"]["config"] == "ask"
    assert MODE_MATRIX["auto"]["config"] == "ask"
    assert MODE_MATRIX["plan"]["config"] == "deny"
    assert "config" in UNPROMOTABLE


async def test_browser_tools_are_hidden_until_the_browser_is_back_and_opt_in_works(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id="snowpea-browser-A")
    try:
        session_id = await new_session(
            host, tmp_path / "w", hostToolsFrom="snowpea-browser-A", originSurface="browser"
        )
        listed = await host.ok("tool.list", {"sessionId": session_id})
        names = {tool["name"] for tool in listed["tools"]}
        assert not any(name.startswith("browser_") for name in names)
        ide_session = await new_session(host, tmp_path / "ide")
        rows0 = {r["sessionId"]: r for r in (await host.ok("session.list", {}))["sessions"]}
        assert rows0[session_id]["browserProvider"] == "host"
        # Opened by a browser-kind connection, so it is a browser session too.
        assert rows0[ide_session]["browserProvider"] == "host"

        await host.ok("tool.register", {"tools": [spec("browser_navigate")]})
        listed = await host.ok("tool.list", {"sessionId": session_id})
        assert "browser_navigate" in {tool["name"] for tool in listed["tools"]}
        await host.ok("tool.unregister", {"names": ["browser_navigate"]})

        chosen = await host.ok(
            "session.setBrowserProvider", {"sessionId": session_id, "provider": "local"}
        )
        assert chosen == {"sessionId": session_id, "browserProvider": "local"}
        listed = await host.ok("tool.list", {"sessionId": session_id})
        assert "browser_navigate" in {tool["name"] for tool in listed["tools"]}
        rows = await daemon.core.store.list_sessions()
        assert next(r for r in rows if r["id"] == session_id)["browser_provider"] == "local"
        listed_rows = {r["sessionId"]: r for r in (await host.ok("session.list", {}))["sessions"]}
        assert listed_rows[session_id]["browserProvider"] == "local"
        await asyncio.sleep(0.05)
        changed = [
            n["params"] for n in host.notifications
            if n["method"] == "sessions.changed" and n["params"]["reason"] == "browserProvider"
        ]
        assert changed and changed[-1]["browserProvider"] == "local"

        await host.ok("session.setBrowserProvider", {"sessionId": session_id, "provider": "host"})
        await asyncio.sleep(0)
        listed = await host.ok("tool.list", {"sessionId": session_id})
        assert not any(t["name"].startswith("browser_") for t in listed["tools"])
    finally:
        await host.stop()


# ---------------------------------------------------------------------------
# addendum 14: team and named agents started from a browser session
# ---------------------------------------------------------------------------


def _browser_parent(tmp_path: Path) -> Session:
    parent = _browser_session(tmp_path, browser_provider="local")
    parent.workspace_dir = str(tmp_path / "ws")
    parent.origin_client_id = "snowpea-browser-A"
    return parent


def test_team_worker_and_reviewer_anchors_keep_the_browser_host(tmp_path: Path) -> None:
    from snowpea_core.agent.team import TeamManager

    lead = _browser_parent(tmp_path)
    manager = TeamManager(SimpleNamespace())  # type: ignore[arg-type]
    run = SimpleNamespace(session=lead, workers=2, repo=tmp_path)
    worker = manager._anchor(run, SimpleNamespace(path=tmp_path, n=1))  # type: ignore[arg-type]
    reviewer = manager._review_anchor(run)  # type: ignore[arg-type]
    for anchor in (worker, reviewer):
        assert anchor.host_tools_from == "snowpea-browser-A"
        assert anchor.origin_client_id == "snowpea-browser-A"
        assert anchor.workspace_dir == str(tmp_path / "ws")
        assert anchor.browser_provider == "local"
        assert browser_providers.is_browser_session(anchor)


async def test_a_named_agent_created_in_a_browser_session_keeps_its_host(
    daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    from snowpea_core.agent import named as named_agents

    parent = _browser_parent(tmp_path)
    registry = named_agents.registry(daemon.core)
    agent = await registry.create("scout", None, workdir=tmp_path, parent=parent)
    session = daemon.core.sessions.get(agent.session_id)
    assert session.host_tools_from == "snowpea-browser-A"
    assert session.origin_client_id == "snowpea-browser-A"
    assert session.workspace_dir == str(tmp_path / "ws")
    # Without its browser the named agent fails fast instead of using core's own.
    assert browser_providers.browser_locked(session) is False  # parent had opted in
    rows = {r["id"]: r for r in await daemon.core.store.list_sessions(include_closed=True)}
    assert rows[agent.session_id]["host_tools_from"] == "snowpea-browser-A"

    # A re-opened session (daemon restart) keeps the host it was created with.
    await daemon.core.sessions.close(agent.session_id)
    reopened = await registry._ensure_session(agent)
    assert reopened.host_tools_from == "snowpea-browser-A"
    assert reopened.browser_provider == "local"


async def test_a_browser_session_may_queue_team_and_other_slash_commands(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The browser agent (tools: "*") starts /team through queue_command; core
    has no browser-specific block on slash commands."""
    from snowpea_core.agent import loop as agent_loop
    from snowpea_core.tools.ask_user import queue_command

    started: list[str] = []
    monkeypatch.setattr(
        agent_loop, "start_turn", lambda core, session, text: started.append(text) or "t-1"
    )
    session = await daemon.core.sessions.create(
        workdir=tmp_path, origin_surface="browser", origin_conn=None
    )
    session.host_tools_from = "snowpea-browser-A"
    ctx = ToolContext(session=session, core=daemon.core, backend=None)  # type: ignore[arg-type]
    for command in ('/team 2 "add docstrings"', "/plan sketch it"):
        result = await queue_command(ctx, {"command": command})
        assert result.ok, result.error
    assert started == ['/team 2 "add docstrings"', "/plan sketch it"]
