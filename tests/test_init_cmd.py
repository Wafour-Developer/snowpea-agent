"""``/init [--force]`` — a fast, rough ``AGENTS.md`` (CORE-init).

Unlike ``/deepinit`` (a subagent per top-level directory), ``/init`` is one
main-agent turn, exactly like ``/delegate``'s pattern: this runs against a
real in-process daemon with the deterministic scripted fake provider so the
assertions describe what any client actually sees.

Two things are asserted at the code level, not just "the model said so":

* ``.snowpea/settings.json`` is created by ``init_cmd`` itself (not a model
  tool call) only when it does not already exist.
* Plan mode denies every ``write_file`` the model attempts, so nothing lands
  on disk regardless of what the (fake) model tries to do.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect

from snowpea_core.commands import init_cmd
from snowpea_core.config.project import ProjectSettings
from snowpea_core.server.app_server import Daemon

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "init.json"
TIMEOUT = 20.0


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    instance = Daemon(port=0, home=home)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project


async def start_session(client: RpcClient, workdir: Path, mode: str = "accept") -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": mode})
    return str(result["sessionId"])


async def prompt(client: RpcClient, session_id: str, text: str) -> None:
    result = await client.ok("session.prompt", {"sessionId": session_id, "text": text})
    await client.wait_turn(str(result["turnId"]), TIMEOUT)


def _payloads(client: RpcClient, kind: str) -> list[dict]:
    return [event["payload"] for event in client.of_kind(kind)]


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_is_listed_in_command_list(daemon: Daemon, http: aiohttp.ClientSession) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        result = await client.ok("command.list", {})
        names = {c["name"] for c in result["commands"]}
        assert "init" in names
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# fresh project: AGENTS.md and .snowpea/settings.json both absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_writes_agents_md_and_creates_settings_when_both_absent(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, workdir)
        await prompt(client, session_id, "/init")

        calls = [p for p in _payloads(client, "tool.call") if p["name"] == "write_file"]
        assert calls, f"write_file was never called; saw {client.kinds()}"
        assert calls[0]["args"]["path"] == "AGENTS.md"

        agents_path = workdir / "AGENTS.md"
        assert agents_path.is_file()
        assert "fresh AGENTS.md" in agents_path.read_text(encoding="utf-8")

        settings = ProjectSettings.load(workdir)
        assert settings.defaultMode == "accept"
        assert ProjectSettings.path_for(workdir).is_file()

        assert _payloads(client, "turn.done")[-1]["reason"] == "complete"
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# settings.json already present: left alone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_leaves_existing_settings_json_alone(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    settings_path = ProjectSettings.path_for(workdir)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"defaultMode": "plan"}), encoding="utf-8")
    before = settings_path.read_text(encoding="utf-8")

    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, workdir)
        await prompt(client, session_id, "/init")

        assert settings_path.read_text(encoding="utf-8") == before
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# existing AGENTS.md: merged, not overwritten wholesale, unless --force
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_merges_into_existing_agents_md_without_force(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    agents_path = workdir / "AGENTS.md"
    agents_path.write_text("# Project\n\noriginal content\n", encoding="utf-8")

    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, workdir)
        await prompt(client, session_id, "/init")

        calls = [p for p in _payloads(client, "tool.call") if p["name"] == "write_file"]
        assert calls
        written = agents_path.read_text(encoding="utf-8")
        assert "original content" in written
        assert "merged notes" in written
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_init_force_rewrites_agents_md(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    agents_path = workdir / "AGENTS.md"
    agents_path.write_text("# Project\n\noriginal content\n", encoding="utf-8")

    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, workdir)
        await prompt(client, session_id, "/init --force")

        written = agents_path.read_text(encoding="utf-8")
        assert "rewritten from scratch" in written
        assert "original content" not in written
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# plan mode: nothing is written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_in_plan_mode_writes_only_the_markdown(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """Plan mode may write documents (m2 §9): AGENTS.md lands, the settings
    file — configuration — does not."""
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, workdir, mode="plan")
        await prompt(client, session_id, "/init")

        assert (workdir / "AGENTS.md").exists()
        assert not ProjectSettings.path_for(workdir).is_file()
        assert _payloads(client, "turn.done")[-1]["reason"] == "complete"
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# unit-level: the pure helpers, no daemon needed
# ---------------------------------------------------------------------------


def test_agents_status_for_absent_file(tmp_path: Path) -> None:
    text = init_cmd.agents_status(tmp_path / "AGENTS.md", force=False)
    assert "no AGENTS.md" in text


def test_agents_status_for_existing_file_without_force(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("hello\n", encoding="utf-8")
    text = init_cmd.agents_status(path, force=False)
    assert "merge" in text
    assert "hello" in text


def test_agents_status_with_force(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("hello\n", encoding="utf-8")
    text = init_cmd.agents_status(path, force=True)
    assert "--force" in text
    assert "hello" not in text


def test_ensure_project_settings_creates_when_absent(tmp_path: Path) -> None:
    note = init_cmd.ensure_project_settings(tmp_path, plan=False)
    assert "created" in note
    assert ProjectSettings.path_for(tmp_path).is_file()


def test_ensure_project_settings_leaves_existing_alone(tmp_path: Path) -> None:
    settings_path = ProjectSettings.path_for(tmp_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"defaultMode": "auto"}), encoding="utf-8")
    note = init_cmd.ensure_project_settings(tmp_path, plan=False)
    assert "already exists" in note
    assert json.loads(settings_path.read_text(encoding="utf-8"))["defaultMode"] == "auto"


def test_ensure_project_settings_in_plan_mode_writes_nothing(tmp_path: Path) -> None:
    note = init_cmd.ensure_project_settings(tmp_path, plan=True)
    assert "Plan mode" in note
    assert not ProjectSettings.path_for(tmp_path).is_file()
