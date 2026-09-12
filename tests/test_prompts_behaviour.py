"""What the composed prompts make the agent *do* (CORE-prompts).

The golden snapshots in ``test_prompts_compose.py`` prove the text; these prove
the behaviour that text and the denial path are supposed to produce, against a
real in-process daemon and the deterministic scripted provider.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.agent import agent, loop
from snowpea_core.server.app_server import Daemon

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures" / "providers" / "fake"
SCRIPT = FIXTURES / "prompts.json"
RETRY_SCRIPT = FIXTURES / "prompts_retry.json"
TIMEOUT = 15.0

#: The model tries to write in plan mode, is refused, and — because a refusal
#: now comes back as a tool result instead of ending the turn — picks a
#: different action inside the same turn.  ``retry`` is the opposite: a model
#: that only ever re-sends the refused call, which the denial budget stops.
FAKE_SCRIPT = {
    "steps": [
        {
            "match": "adapt after the refusal",
            "text": "writing",
            "tool_calls": [
                {"name": "write_file", "arguments": {"path": "out.txt", "content": "x"}}
            ],
        },
        {
            "after_tool": "write_file",
            "text": "reading instead",
            "tool_calls": [{"name": "read_file", "arguments": {"path": "note.txt"}}],
        },
        {"after_tool": "read_file", "text": "Goal — leave note.txt alone. Steps — 1. nothing."},
    ],
    "default": {"text": "nothing to do"},
}

#: A model that answers every refusal by re-sending the refused call.
RETRY_FAKE_SCRIPT = {
    "steps": [
        {
            "match": "keep retrying",
            "text": "writing",
            "repeat": True,
            "tool_calls": [
                {"name": "write_file", "arguments": {"path": "out.txt", "content": "x"}}
            ],
        },
        {
            "after_tool": "write_file",
            "text": "writing again",
            "repeat": True,
            "tool_calls": [
                {"name": "write_file", "arguments": {"path": "out.txt", "content": "x"}}
            ],
        },
    ],
    "default": {"text": "nothing to do"},
}


@pytest.fixture(scope="module", autouse=True)
def _script() -> None:
    SCRIPT.write_text(json.dumps(FAKE_SCRIPT, indent=2) + "\n", encoding="utf-8")
    RETRY_SCRIPT.write_text(json.dumps(RETRY_FAKE_SCRIPT, indent=2) + "\n", encoding="utf-8")


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(SCRIPT):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            await instance.stop()


@pytest_asyncio.fixture
async def retry_daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(RETRY_SCRIPT):
        instance = await make_daemon(tmp_path / "retry-home")
        try:
            yield instance
        finally:
            await instance.stop()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "note.txt").write_text("hello\n", encoding="utf-8")
    return root


async def open_session(client: RpcClient, workdir: Path, mode: str) -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": mode})
    return str(result["sessionId"])


# ---------------------------------------------------------------------------
# plan mode writes nothing, and a refusal teaches rather than terminates
# ---------------------------------------------------------------------------


async def test_plan_mode_makes_no_write_calls(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "plan")

    turn_id = await client.ok(
        "session.prompt", {"sessionId": session_id, "text": "adapt after the refusal"}
    )
    await client.wait_turn(str(turn_id["turnId"]), TIMEOUT)

    assert not (workdir / "out.txt").exists()
    ran = [event["payload"]["name"] for event in client.of_kind("tool.call")]
    assert "write_file" not in ran
    await client.stop()


async def test_a_denied_call_is_followed_by_a_different_action(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """The regression test for gap 3: the turn survives and the model adapts."""
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "plan")

    turn_id = await client.ok(
        "session.prompt", {"sessionId": session_id, "text": "adapt after the refusal"}
    )
    assert await client.wait_turn(str(turn_id["turnId"]), TIMEOUT) == "complete"

    results = client.of_kind("tool.result")
    assert [item["payload"]["name"] for item in results] == ["write_file", "read_file"]
    assert results[0]["payload"]["ok"] is False
    assert "do not retry the same call" in results[0]["payload"]["error"]
    # The second call is a *different* tool, and it actually ran.
    assert results[1]["payload"]["ok"] is True
    await client.stop()


async def test_a_model_that_only_retries_ends_the_turn(
    retry_daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """Feeding denials back must not let a stubborn model loop forever."""
    client = await connect(http, retry_daemon)
    session_id = await open_session(client, workdir, "plan")

    turn_id = await client.ok(
        "session.prompt", {"sessionId": session_id, "text": "keep retrying"}
    )
    assert await client.wait_turn(str(turn_id["turnId"]), TIMEOUT) == "denied"

    refused = client.of_kind("tool.result")
    assert len(refused) == loop.MAX_DENIALS_PER_TURN
    assert all(item["payload"]["ok"] is False for item in refused)
    assert not (workdir / "out.txt").exists()
    await client.stop()


# ---------------------------------------------------------------------------
# the prompt a turn would actually send
# ---------------------------------------------------------------------------


async def test_the_reply_language_setting_reaches_the_prompt(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "accept")
    session = daemon.core.sessions.get(session_id)
    assert session is not None

    # "auto" leaves the base rule to do the work.
    assert "Reply in Korean" not in agent.build_system_prompt(session, [], core=daemon.core)
    assert "Reply in the language the user wrote in." in agent.build_system_prompt(
        session, [], core=daemon.core
    )

    await client.ok(
        "settings.set", {"scope": "global", "patch": {"agent": {"replyLanguage": "ko"}}}
    )
    assert "Reply in Korean" in agent.build_system_prompt(session, [], core=daemon.core)
    await client.stop()


async def test_context_pressure_adds_the_brevity_line(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "accept")
    session = daemon.core.sessions.get(session_id)
    assert session is not None
    marker = "The context window is nearly full."

    session.context_window = 100_000
    session.context_used = 10_000
    assert marker not in agent.build_system_prompt(session, [], core=daemon.core)

    session.context_used = 90_000
    assert marker in agent.build_system_prompt(session, [], core=daemon.core)
    await client.stop()


async def test_the_prompt_lists_exactly_the_registered_tools(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """A newly registered tool can never be invisible to the model."""
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "accept")
    session = daemon.core.sessions.get(session_id)
    assert session is not None

    specs = daemon.core.tools.specs(session)
    prompt = agent.build_system_prompt(session, specs, core=daemon.core)
    listed = [
        line[2:].split(":", 1)[0]
        for line in prompt.split("Available tools:\n", 1)[1].splitlines()
        if line.startswith("- ")
    ]
    assert listed == [spec.name for spec in specs]
    await client.stop()


async def test_the_environment_block_names_the_workdir_and_the_branch(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "accept")
    session = daemon.core.sessions.get(session_id)
    assert session is not None
    agent.invalidate_environment(session)

    prompt = agent.build_system_prompt(session, [], core=daemon.core)
    assert f"- Working directory: {workdir}" in prompt
    assert "- Today: " in prompt
    # Not a git repository, so the workspace snapshot is dropped rather than guessed.
    assert "Workspace" not in prompt
    await client.stop()


async def test_project_context_files_reach_the_prompt(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    (workdir / "AGENTS.md").write_text("Always use tabs in this repo.", encoding="utf-8")
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "accept")
    session = daemon.core.sessions.get(session_id)
    assert session is not None
    agent.invalidate_environment(session)

    prompt = agent.build_system_prompt(session, [], core=daemon.core)
    assert "Always use tabs in this repo." in prompt
    assert '<context file="AGENTS.md">' in prompt
    await client.stop()
