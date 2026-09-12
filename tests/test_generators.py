"""M6 US-018: the agent-definition and skill generators.

Everything runs against a real in-process daemon with the deterministic
scripted provider, so the assertions describe what a TUI or the SDK would see
on the wire.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.agent.definition import (
    AgentDefinition,
    DefinitionError,
    builtin_agent_definitions,
    parse_agent_md,
    parse_agent_text,
    parse_generated_json,
    render_agent_md,
    slugify,
    validate_name,
    write_definition,
)
from snowpea_core.server.app_server import Daemon

# ``asyncio_mode = "auto"`` in pyproject.toml runs the coroutine tests below.
FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "generators.json"
TIMEOUT = 15.0

KOREAN_BRIEF = "릴리즈 노트 작성 전담"


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            await instance.stop()


async def start_session(client: RpcClient, workdir: Path) -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept"})
    return str(result["sessionId"])


async def run(client: RpcClient, session_id: str, text: str) -> str:
    """Send ``text`` and wait for its turn to finish; returns the reason."""
    result = await client.ok("session.prompt", {"sessionId": session_id, "text": text})
    return await client.wait_turn(str(result["turnId"]))


def project(tmp_path: Path) -> Path:
    workdir = tmp_path / "project"
    workdir.mkdir(exist_ok=True)
    return workdir


def last_message(client: RpcClient) -> str:
    messages = client.of_kind("message.done")
    assert messages, f"no message.done; saw {client.kinds()}"
    return str(messages[-1]["payload"]["text"])


# ---------------------------------------------------------------------------
# pure functions: parse / render / slug / tolerant JSON
# ---------------------------------------------------------------------------


def test_parse_and_render_round_trip(tmp_path: Path) -> None:
    original = AgentDefinition(
        name="release-notes",
        description="Writes release notes: crisply",
        model="anthropic:claude-sonnet-4",
        tools=["read_file", "shell"],
        permission="plan",
        max_turns=12,
        prompt="You write release notes.\n\nOne line per entry.",
    )
    text = render_agent_md(original)
    assert text.startswith("---\n")

    path = tmp_path / "release-notes.md"
    path.write_text(text, encoding="utf-8")
    again = parse_agent_md(path)

    assert again.name == original.name
    assert again.description == original.description
    assert again.model == original.model
    assert again.tools == original.tools
    assert again.permission == original.permission
    assert again.max_turns == original.max_turns
    assert again.prompt == original.prompt
    assert render_agent_md(again) == text


def test_star_tools_and_block_lists_parse() -> None:
    assert parse_agent_text('---\nname: a\ntools: "*"\n---\nbody').tools == "*"
    block = "---\nname: a\ntools:\n  - shell\n  - read_file\n---\nbody"
    assert parse_agent_text(block).tools == ["shell", "read_file"]
    assert parse_agent_text(block).prompt == "body"


def test_slug_validation() -> None:
    assert validate_name("Release Notes") == "release-notes"
    assert slugify(KOREAN_BRIEF) == ""
    with pytest.raises(DefinitionError):
        validate_name(KOREAN_BRIEF)


def test_generated_json_is_parsed_out_of_prose() -> None:
    assert parse_generated_json('ok: {"name": "x", "nested": {"y": 1}} trailing')["name"] == "x"
    fenced = 'here\n```json\n{"name": "y"}\n```\n'
    assert parse_generated_json(fenced)["name"] == "y"
    with pytest.raises(DefinitionError):
        parse_generated_json("no object here")


def test_delegate_task_description_names_builtin_agents() -> None:
    from snowpea_core.tools.delegate import TOOLS

    delegate = TOOLS[0]
    agent_schema = delegate.input_schema["properties"]["agent"]
    assert "Built-in agents available by name" in delegate.description
    assert "executor" in delegate.description
    assert "critic" in str(agent_schema["description"])
    assert "custom agent names also resolve" in str(agent_schema["description"])


def test_builtin_agent_definitions_are_visible_and_overridable(tmp_path: Path) -> None:
    from snowpea_core.commands.agent_cmd import definitions_for

    builtins = {agent.name: agent for agent in builtin_agent_definitions()}
    assert {"architect", "critic", "executor", "explorer", "test-engineer", "verifier"} <= set(
        builtins
    )
    assert builtins["executor"].source == "builtin"
    assert builtins["executor"].prompt == ""
    assert builtins["executor"].path and builtins["executor"].path.name == "executor.md"

    workdir = project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    core = SimpleNamespace(paths=SimpleNamespace(home=home), skills=None)

    definitions = {agent.name: agent for agent in definitions_for(core, workdir)}
    assert definitions["executor"].source == "builtin"
    assert definitions["critic"].source == "builtin"

    write_definition(
        AgentDefinition(
            name="executor",
            description="Project executor override.",
            prompt="Project-specific executor persona.",
        ),
        workdir,
    )

    definitions = {agent.name: agent for agent in definitions_for(core, workdir)}
    assert definitions["executor"].source == "project"
    assert definitions["executor"].description == "Project executor override."
    assert definitions["critic"].source == "builtin"


# ---------------------------------------------------------------------------
# /agent create, /agent list, agent.list
# ---------------------------------------------------------------------------


async def test_agent_create_writes_a_definition(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await start_session(client, workdir)

    assert await run(client, session_id, f'/agent create "{KOREAN_BRIEF}"') == "complete"

    path = workdir / ".snowpea" / "agents" / "release-notes.md"
    assert path.is_file(), f"not written; reply was {last_message(client)}"
    defn = parse_agent_md(path)
    assert defn.name == "release-notes"
    assert defn.description == "릴리즈 노트 작성 전담 에이전트"
    assert defn.tools == ["read_file", "shell"]
    assert defn.model == "inherit"
    assert defn.permission == "inherit"
    assert "release notes" in defn.prompt.lower()
    assert str(path) in last_message(client)

    await client.stop()


async def test_agent_list_and_rpc_show_the_definition(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await start_session(client, workdir)
    assert await run(client, session_id, f'/agent create "{KOREAN_BRIEF}"') == "complete"

    assert await run(client, session_id, "/agent list") == "complete"
    listing = last_message(client)
    assert "release-notes" in listing
    assert "[project]" in listing

    result = await client.ok("agent.list")
    agents = {agent["name"]: agent for agent in result["agents"]}
    assert "release-notes" in agents
    assert agents["release-notes"]["source"] == "project"
    assert agents["release-notes"]["kind"] == "definition"
    assert agents["release-notes"]["path"].endswith("release-notes.md")
    assert agents["executor"]["source"] == "builtin"
    assert agents["executor"]["kind"] == "definition"

    await client.stop()


async def test_agent_create_rpc_matches_the_command(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    await start_session(client, workdir)

    result = await client.ok("agent.create", {"description": KOREAN_BRIEF})
    assert result["name"] == "release-notes"
    assert (workdir / ".snowpea" / "agents" / "release-notes.md").is_file()
    assert result["path"] == str(workdir / ".snowpea" / "agents" / "release-notes.md")

    await client.stop()


async def test_malformed_model_json_reports_an_error_and_writes_nothing(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await start_session(client, workdir)

    assert await run(client, session_id, '/agent create "not json at all"') == "complete"

    errors = client.of_kind("error")
    assert errors, f"no error event; saw {client.kinds()}"
    assert errors[-1]["payload"]["code"] == "invalid_params"
    assert "JSON" in errors[-1]["payload"]["message"]
    assert "Could not create the agent" in last_message(client)
    assert not (workdir / ".snowpea" / "agents").exists()

    await client.stop()


# ---------------------------------------------------------------------------
# /skill learn
# ---------------------------------------------------------------------------


async def test_skill_learn_writes_a_skill_md(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await start_session(client, workdir)

    # A short session first, so there is a history to summarise.
    assert await run(client, session_id, "remember this workflow: read the changelog") == "complete"
    assert await run(client, session_id, "/skill learn notes") == "complete"

    path = workdir / ".snowpea" / "skills" / "notes" / "SKILL.md"
    assert path.is_file(), f"not written; reply was {last_message(client)}"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\nname: notes\n")
    assert "description: Collect release notes for a project" in text
    assert "1. Read the changelog" in text
    assert "3. Write one line per entry" in text
    assert "git log --oneline" in text
    assert str(path) in last_message(client)

    await client.stop()


async def test_skill_learn_needs_a_session_history(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await start_session(client, workdir)

    assert await run(client, session_id, "/skill learn notes") == "complete"
    assert client.of_kind("error"), f"no error event; saw {client.kinds()}"
    assert "no history" in last_message(client)
    assert not (workdir / ".snowpea" / "skills").exists()

    await client.stop()


async def test_learned_skill_becomes_a_command(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """The loaded skill turns into ``/notes`` — needs the US-017 skill loader."""
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await start_session(client, workdir)
    assert await run(client, session_id, "remember this workflow: read the changelog") == "complete"
    assert await run(client, session_id, "/skill learn notes") == "complete"
    assert (workdir / ".snowpea" / "skills" / "notes" / "SKILL.md").is_file()

    if getattr(daemon.core, "skills", None) is None:
        pytest.xfail("the US-017 skill loader is not wired into Core yet")

    result = await client.ok("command.list", {"sessionId": session_id})
    assert "notes" in {command["name"] for command in result["commands"]}

    await client.stop()
