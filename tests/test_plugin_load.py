"""M6 US-017: the Claude Code plugin loader (contract §1, plan AC-10 / AC-14).

Every test drives a real in-process daemon over its WebSocket with the scripted
fake provider, exactly like ``test_session_loop``.  The pinned fixture plugin in
``tests/fixtures/plugins/sample-plugin`` exercises all five kinds the loader has
to understand: ``plugin.json``, a skill, an agent definition, a command file, a
``PreToolUse`` hook and an ``.mcp.json`` server.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from test_session_loop import Client, connect, make_daemon, prompt, start_session

from snowpea_core.server.app_server import Daemon
from snowpea_core.skills import loader as skill_loader
from snowpea_core.skills import marketplace

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures"
SCRIPT = FIXTURES / "providers" / "fake" / "plugin.json"
SAMPLE_PLUGIN = FIXTURES / "plugins" / "sample-plugin"
MARKETPLACE_DIR = FIXTURES / "marketplace"

TIMEOUT = 15.0


class FixtureFetcher(marketplace.HttpFetcher):
    """Offline stand-in for the HTTP layer the three search sources share."""

    ENDPOINTS = {
        "agentskills.io": MARKETPLACE_DIR / "agentskills.json",
        "hermes-hub": MARKETPLACE_DIR / "hermes.json",
    }

    async def get_json(self, url: str) -> Any:
        for needle, path in self.ENDPOINTS.items():
            if needle in url:
                return json.loads(path.read_text(encoding="utf-8"))
        return await super().get_json(url)


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{SCRIPT}"
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


@pytest_asyncio.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def install_sample(client: Client) -> None:
    await client.ok("skill.install", {"source": str(SAMPLE_PLUGIN)})


def by_name(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row["name"]): row for row in rows}


# ---------------------------------------------------------------------------
# (a) the five kinds the loader has to understand
# ---------------------------------------------------------------------------


async def test_install_loads_plugin_skill_agent_and_command(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    await start_session(client, workdir)

    await install_sample(client)
    rows = by_name((await client.ok("skill.list"))["skills"])

    assert rows["sample-plugin"]["kind"] == "plugin"
    assert rows["hello"]["kind"] == "skill"
    assert rows["fixture-cmd"]["kind"] == "command"
    assert rows["fixture-agent"]["kind"] == "agent"
    for name in ("hello", "fixture-cmd", "fixture-agent"):
        assert rows[name]["source"] == "plugin:sample-plugin"
    # Exactly one of each kind comes from the fixture plugin; the built-in
    # skills that ship with the daemon are loaded alongside it.
    mine = [
        row["kind"]
        for row in rows.values()
        if row["source"] == "plugin:sample-plugin" and row["kind"] != "plugin"
    ]
    assert sorted(mine) == ["agent", "command", "skill"]
    assert [row["kind"] for row in rows.values()].count("plugin") == 1

    assert (daemon.paths.home / "plugins" / "sample-plugin" / "plugin.json").is_file()
    await client.stop()


async def test_plugin_commands_are_in_command_list(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir)

    await install_sample(client)
    commands = by_name((await client.ok("command.list", {"sessionId": session_id}))["commands"])

    assert commands["hello"]["source"] == "plugin:sample-plugin"
    assert commands["fixture-cmd"]["source"] == "plugin:sample-plugin"
    assert "help" in commands  # the builtins survive a reload
    await client.stop()


async def test_fixture_command_runs_a_turn(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir)
    await install_sample(client)

    result = await client.ok(
        "command.run", {"sessionId": session_id, "name": "fixture-cmd", "args": ""}
    )
    turn_id = str(result["turnId"])
    assert await client.wait_turn(turn_id, TIMEOUT) == "complete"
    assert client.of_kind("message.done")[-1]["payload"]["text"] == "fixture"
    await client.stop()


# ---------------------------------------------------------------------------
# (b) hooks and the plugin's MCP server
# ---------------------------------------------------------------------------


async def test_mcp_tool_round_trip_and_pre_tool_hook_marker(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir, mode="auto")
    await install_sample(client)

    tools = by_name((await client.ok("tool.list", {"sessionId": session_id}))["tools"])
    assert "mcp__fixture-echo__echo" in tools
    assert tools["mcp__fixture-echo__echo"]["source"] == "mcp:fixture-echo"

    turn_id = await prompt(client, session_id, "echo via mcp")
    assert await client.wait_turn(turn_id, TIMEOUT) == "complete"

    results = [
        event
        for event in client.of_kind("tool.result")
        if event["payload"]["name"] == "mcp__fixture-echo__echo"
    ]
    assert results, client.kinds()
    assert results[0]["payload"]["ok"] is True
    assert "ping-42" in results[0]["payload"]["output"]

    marker = daemon.paths.home / "fixture-hook.marker"
    assert marker.is_file(), "the PreToolUse hook did not run"
    assert marker.read_text(encoding="utf-8").strip() == "mcp__fixture-echo__echo"
    await client.stop()


async def test_pre_tool_hook_can_block_a_call(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """Exit status 2 refuses the call and the stderr reaches the model."""
    from snowpea_core.skills.hooks import Hook

    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session_id = await start_session(client, workdir, mode="auto")
    await install_sample(client)

    daemon.core.skills.hooks.add(
        Hook(
            event="PreToolUse",
            matcher="mcp__.*",
            command="python3 -c 'import sys; sys.stderr.write(\"nope\"); sys.exit(2)'",
            plugin="test",
        )
    )
    turn_id = await prompt(client, session_id, "echo via mcp")
    assert await client.wait_turn(turn_id, TIMEOUT) == "complete"

    results = [
        event
        for event in client.of_kind("tool.result")
        if event["payload"]["name"] == "mcp__fixture-echo__echo"
    ]
    assert results and results[0]["payload"]["ok"] is False
    assert "hook_blocked" in results[0]["payload"]["error"]
    assert "nope" in results[0]["payload"]["error"]
    await client.stop()


# ---------------------------------------------------------------------------
# (c) search, reload, precedence
# ---------------------------------------------------------------------------


async def test_search_aggregates_three_sources(
    daemon: Daemon, http: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(marketplace, "FETCHER", FixtureFetcher())
    marketplace.save_marketplaces(
        daemon.paths.home,
        [{"name": "fixture-market", "url": str(MARKETPLACE_DIR / "claude.json")}],
    )
    client = await connect(http, daemon)

    result = await client.ok("skill.search", {"query": "pdf"})
    skills = result["skills"]
    assert result["unavailable"] == []
    sources = {str(row["source"]) for row in skills}
    assert sources == {"claude-marketplace", "agentskills.io", "hermes-hub"}
    assert all(row["installSpec"] for row in skills)
    assert {row["name"] for row in skills} >= {"pdf-toolkit", "pdf-extract", "pdf-ocr"}
    assert "spreadsheet-toolkit" not in {row["name"] for row in skills}
    await client.stop()


async def test_search_names_unreachable_sources(
    daemon: Daemon, http: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline must read as offline, not as "nothing matched"."""

    class DeadFetcher(marketplace.HttpFetcher):
        async def get_json(self, url: str) -> Any:
            raise OSError("network is unreachable")

    monkeypatch.setattr(marketplace, "FETCHER", DeadFetcher())
    marketplace.save_marketplaces(
        daemon.paths.home,
        [{"name": "fixture-market", "url": str(MARKETPLACE_DIR / "claude.json")}],
    )
    client = await connect(http, daemon)

    result = await client.ok("skill.search", {"query": "pdf"})
    assert result["skills"] == []
    reported = " ".join(result["unavailable"])
    for source in ("claude-marketplace", "agentskills.io", "hermes-hub"):
        assert source in reported
    assert "network is unreachable" in reported
    await client.stop()


async def test_relative_install_path_is_made_absolute() -> None:
    """AC-10 types a relative path; the daemon's cwd is $SNOWPEA_HOME."""
    from snowpea_core.cli.commands import resolve_install_source

    repo = Path(__file__).resolve().parent.parent
    relative = SAMPLE_PLUGIN.relative_to(repo)
    previous = os.getcwd()
    os.chdir(repo)
    try:
        resolved = resolve_install_source(f"./{relative}")
    finally:
        os.chdir(previous)
    assert Path(resolved).is_absolute()
    assert Path(resolved) == SAMPLE_PLUGIN
    # Everything that is not a local directory is passed through untouched.
    assert resolve_install_source("oh-my-claudecode") == "oh-my-claudecode"
    assert (
        resolve_install_source("https://github.com/Yeachan-Heo/oh-my-claudecode")
        == "https://github.com/Yeachan-Heo/oh-my-claudecode"
    )
    assert resolve_install_source("fixture-market/pdf-toolkit") == "fixture-market/pdf-toolkit"


async def test_install_accepts_an_absolute_path_and_reloads(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon)
    await start_session(client, tmp_path)
    await client.ok("skill.install", {"source": str(SAMPLE_PLUGIN.resolve())})
    rows = by_name((await client.ok("skill.list"))["skills"])
    assert rows["sample-plugin"]["kind"] == "plugin"
    await client.stop()


async def test_reload_emits_commands_changed(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    await start_session(client, workdir)
    await install_sample(client)
    client.notifications.clear()

    await client.ok("skill.reload")
    for _ in range(100):
        changed = [n for n in client.notifications if n["method"] == "commands.changed"]
        if changed:
            break
        await asyncio.sleep(0.02)
    assert changed, [n["method"] for n in client.notifications]
    names = {command["name"] for command in changed[0]["params"]["commands"]}
    assert {"hello", "fixture-cmd", "help"} <= names
    await client.stop()


async def test_precedence_project_beats_global_beats_builtin(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    builtin = tmp_path / "builtin_skills"
    write_skill(builtin / "clash", "clash", "the builtin one")
    monkeypatch.setattr(skill_loader, "builtin_root", lambda: builtin)

    client = await connect(http, daemon)
    await start_session(client, workdir)
    loader = daemon.core.skills

    await loader.reload()
    assert loader.skills["clash"].source == "builtin"

    global_skill = daemon.paths.home / "skills" / "clash"
    write_skill(global_skill, "clash", "the global one")
    await loader.reload()
    assert loader.skills["clash"].source == "global"

    project_skill = workdir / ".snowpea" / "skills" / "clash"
    write_skill(project_skill, "clash", "the project one")
    await loader.reload()
    assert loader.skills["clash"].source == "project"
    assert loader.skills["clash"].description == "the project one"
    assert daemon.core.commands.get("clash") is not None
    await client.stop()


def write_skill(directory: Path, name: str, description: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nDo the thing with $ARGUMENTS.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# (d) the parser, without a daemon
# ---------------------------------------------------------------------------


async def test_skill_md_frontmatter_is_parsed() -> None:
    from snowpea_core.skills.skill_md import parse_skill_md

    doc = parse_skill_md(
        (SAMPLE_PLUGIN / "skills" / "hello" / "SKILL.md").read_text(encoding="utf-8"),
        default_name="hello",
    )
    assert doc.name == "hello"
    assert doc.description == "Greet whoever the arguments name."
    assert doc.argument_hint == "[name]"
    assert doc.user_invocable is True
    assert doc.allowed_tools == ["read_file", "glob"]
    assert "Greet Yuna" in doc.render("Yuna")


async def test_plugin_root_variables_expand() -> None:
    entry = json.loads((SAMPLE_PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    expanded = skill_loader.expand_tree(entry["mcpServers"]["fixture-echo"], SAMPLE_PLUGIN)
    assert expanded["args"][0] == f"{SAMPLE_PLUGIN}/mcp/echo_server.py"
    assert "${" not in expanded["command"]
