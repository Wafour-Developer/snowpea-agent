"""Team guides: persona, routing, injection, commands, and RPC."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from _support import connect, make_daemon

from snowpea_core.agent import agent as agent_prompt
from snowpea_core.agent.definition import DefinitionError
from snowpea_core.agent.team_guide import (
    TRUNCATED_MARKER,
    delete_guide,
    guide_for_session,
    load_guide,
    render_team_guide,
    save_guide,
)
from snowpea_core.cli import commands as cli_commands
from snowpea_core.cli.main import build_parser
from snowpea_core.commands import team_cmd
from snowpea_core.commands.registry import CommandContext
from snowpea_core.config.paths import Paths
from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings
from snowpea_core.prompts import compose
from snowpea_core.server.app_server import Core, Daemon
from snowpea_core.server.protocol import dump_schema
from snowpea_core.session.session import Session


def _write_guide(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_parse_routing_from_body_with_all_arrow_spellings(tmp_path: Path) -> None:
    text = """---
description: Delivery team
---
# Persona
Ship carefully.

## Routing
- database migrations -> sql-reviewer
- UI work → designer
- docs: writer
"""
    path = tmp_path / ".snowpea" / "teams" / "delivery.md"
    _write_guide(path, text)
    guide = load_guide(tmp_path / "home", tmp_path, "delivery")
    assert guide is not None
    assert guide.description == "Delivery team"
    assert "Ship carefully." in guide.persona
    assert [rule.when for rule in guide.routing] == [
        "database migrations",
        "UI work",
        "docs",
    ]
    assert [rule.agent for rule in guide.routing] == ["sql-reviewer", "designer", "writer"]


@pytest.mark.parametrize("heading", ["Routing", "라우팅", "역할 분담"])
def test_routing_parser_handles_colons_localized_headings_and_section_end(
    tmp_path: Path, heading: str
) -> None:
    text = (
        "Persona with Windows endings.\r\n\r\n"
        f"## {heading}\r\n"
        "- API: breaking changes -> architect   \r\n"
        "- docs: writer\r\n"
        "## Notes\r\n"
        "- must not be parsed -> executor\r\n"
    )
    path = tmp_path / ".snowpea" / "teams" / "core.md"
    _write_guide(path, text)
    guide = load_guide(tmp_path / "home", tmp_path, "core")
    assert guide is not None
    assert [(rule.when, rule.agent) for rule in guide.routing] == [
        ("API: breaking changes", "architect"),
        ("docs", "writer"),
    ]
    assert "Notes" not in guide.persona


def test_save_omits_empty_frontmatter_but_keeps_description_and_reads_legacy_empty(
    tmp_path: Path,
) -> None:
    plain = save_guide(
        tmp_path / "home",
        tmp_path,
        "plain",
        scope="project",
        description="",
        persona="Persona.",
        routing=[],
    )
    assert plain.read_text(encoding="utf-8") == "Persona.\n"
    described = save_guide(
        tmp_path / "home",
        tmp_path,
        "described",
        scope="project",
        description="Delivery",
        persona="Persona.",
        routing=[],
    )
    assert described.read_text(encoding="utf-8").startswith("---\ndescription: Delivery\n---\n")
    _write_guide(tmp_path / ".snowpea" / "teams" / "legacy.md", "---\n---\n\nLegacy persona.\n")
    legacy = load_guide(tmp_path / "home", tmp_path, "legacy")
    assert legacy is not None and legacy.persona == "Legacy persona."


def test_body_routing_wins_over_frontmatter_duplicates(tmp_path: Path) -> None:
    text = """---
routing:
  - shared task -> ghost
---
Keep this persona.

## Routing
- shared task -> executor
"""
    path = tmp_path / ".snowpea" / "teams" / "core.md"
    _write_guide(path, text)
    guide = load_guide(tmp_path / "home", tmp_path, "core")
    assert guide is not None
    assert len(guide.routing) == 1
    assert guide.routing[0].agent == "executor"


def test_persona_truncation_marker_and_limit(tmp_path: Path) -> None:
    persona = "x" * 9000
    save_guide(
        tmp_path / "home",
        tmp_path,
        "default",
        scope="project",
        description="",
        persona=persona,
        routing=[],
    )
    guide = load_guide(tmp_path / "home", tmp_path, "default")
    assert guide is not None
    assert TRUNCATED_MARKER in guide.persona
    assert len(guide.persona) < 9000


def test_invalid_team_name_rejected(tmp_path: Path) -> None:
    with pytest.raises(DefinitionError):
        save_guide(
            tmp_path / "home",
            tmp_path,
            "../escape",
            scope="project",
            description="",
            persona="x",
            routing=[],
        )


def test_symlink_escape_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("# Persona\nOutside\n", encoding="utf-8")
    teams = tmp_path / ".snowpea" / "teams"
    teams.mkdir(parents=True)
    link = teams / "default.md"
    link.symlink_to(outside)
    assert load_guide(tmp_path / "home", tmp_path, "default") is None


def test_project_guide_beats_global(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_guide(home / "teams" / "default.md", "# Persona\nGlobal\n")
    _write_guide(tmp_path / ".snowpea" / "teams" / "default.md", "# Persona\nProject\n")
    guide = load_guide(home, tmp_path, "default")
    assert guide is not None
    assert guide.source == "project"
    assert "Project" in guide.persona


def test_default_guide_only_without_active_team(tmp_path: Path) -> None:
    settings = Settings()
    _write_guide(tmp_path / ".snowpea" / "teams" / "default.md", "# Persona\nDefault rules\n")
    project = ProjectSettings()
    project.agents.teams["core"] = ["executor"]
    project.agents.activeTeam = None
    project.save(tmp_path)
    core = Core(settings=settings, paths=Paths(home=tmp_path / "home"), token="t")
    session = Session(id="s1", workdir=tmp_path)
    guide = guide_for_session(core, session)
    assert guide is not None
    assert guide.team == "default"


def test_active_team_without_guide_does_not_fall_back_to_default(tmp_path: Path) -> None:
    settings = Settings()
    _write_guide(tmp_path / ".snowpea" / "teams" / "default.md", "# Persona\nDefault only\n")
    project = ProjectSettings()
    project.agents.teams["core"] = ["executor"]
    project.agents.activeTeam = "core"
    project.save(tmp_path)
    core = Core(settings=settings, paths=Paths(home=tmp_path / "home"), token="t")
    session = Session(id="s1", workdir=tmp_path, team="core", team_agents=("executor",))
    assert guide_for_session(core, session) is None


def test_unknown_and_off_roster_agents_are_flagged(tmp_path: Path) -> None:
    text = """# Persona
Route wisely.

## Routing
- sql -> missing-agent
- edits -> architect
"""
    settings = Settings()
    project = ProjectSettings()
    project.agents.teams["core"] = ["executor"]
    project.agents.activeTeam = "core"
    project.save(tmp_path)
    core = Core(settings=settings, paths=Paths(home=tmp_path / "home"), token="t")
    _write_guide(tmp_path / ".snowpea" / "teams" / "core.md", text)
    guide = load_guide(tmp_path / "home", tmp_path, "core", core=core)
    assert guide is not None
    by_agent = {rule.agent: rule.known for rule in guide.routing}
    assert by_agent["missing-agent"] is False
    assert by_agent["architect"] is False
    lead = render_team_guide(guide, audience="lead")
    assert UNKNOWN_NOTE in lead
    worker = render_team_guide(guide, audience="worker")
    assert "Who does what" not in worker


UNKNOWN_NOTE = "(unknown agent — ignore this rule)"


def test_lead_system_prompt_includes_persona_and_routing(tmp_path: Path) -> None:
    _write_guide(
        tmp_path / ".snowpea" / "teams" / "default.md",
        "# Persona\nPlatform team tone.\n\n## Routing\n- migrations -> executor\n",
    )
    settings = Settings()
    core = Core(settings=settings, paths=Paths(home=tmp_path / "home"), token="t")
    session = Session(id="s1", workdir=tmp_path)
    prompt = agent_prompt.build_system_prompt(session, [], core=core)
    assert "Platform team tone." in prompt
    assert "Who does what" in prompt
    assert "migrations" in prompt


def test_subagent_worker_rendering_omits_routing(tmp_path: Path) -> None:
    _write_guide(
        tmp_path / ".snowpea" / "teams" / "default.md",
        "# Persona\nWorker tone.\n\n## Routing\n- sql -> executor\n",
    )
    settings = Settings()
    core = Core(settings=settings, paths=Paths(home=tmp_path / "home"), token="t")
    parent = Session(id="p1", workdir=tmp_path)
    loaded = guide_for_session(core, parent)
    assert loaded is not None
    worker = render_team_guide(loaded, audience="worker")
    assert "Worker tone." in worker
    assert "Who does what" not in worker
    assert "part of team default" in worker


def test_child_reads_the_guide_once_from_its_system_prompt(tmp_path: Path) -> None:
    """A worker gets the persona in its system prompt and nowhere else.

    It used to arrive three times — system prompt, delegation brief and the
    workflow brief — which only cost tokens.
    """
    _write_guide(
        tmp_path / ".snowpea" / "teams" / "default.md",
        "Pipeline tone.\n\n## Routing\n- migrations -> executor\n",
    )
    core = Core(settings=Settings(), paths=Paths(home=tmp_path / "home"), token="t")
    parent = Session(id="lead", workdir=tmp_path)
    core.sessions._sessions[parent.id] = parent
    child = Session(id="kid", workdir=tmp_path, parent_session_id="lead", is_subagent=True)
    prompt = agent_prompt.build_system_prompt(child, [], core=core)
    assert prompt.count("Pipeline tone.") == 1
    assert "Who does what" not in prompt

    brief = compose.workflow_brief(
        "team-pipeline-task",
        TASK="t",
        TASK_ID="T1",
        TASK_TITLE="t",
        TASK_BRIEF="b",
        FILES="- a",
        HANDOFF="",
        WORKDIR=str(tmp_path),
    )
    assert "TEAM_GUIDE" not in brief
    assert "${" not in brief


def test_only_the_plan_prompts_carry_the_guide_placeholder() -> None:
    from snowpea_core.prompts.loader import load

    assert "${TEAM_GUIDE}" in load("workflows/team-plan")
    assert "${TEAM_GUIDE}" in load("workflows/team-pipeline-plan")
    for name in ("team-task", "team-review", "team-pipeline-task", "team-pipeline-review"):
        assert "${TEAM_GUIDE}" not in load(f"workflows/{name}")


def test_mtime_cache_picks_up_edits(tmp_path: Path) -> None:
    path = tmp_path / ".snowpea" / "teams" / "default.md"
    _write_guide(path, "# Persona\nVersion one.\n")
    first = load_guide(tmp_path / "home", tmp_path, "default")
    assert first is not None and "Version one." in first.persona
    _write_guide(path, "# Persona\nVersion two — edited.\n")
    second = load_guide(tmp_path / "home", tmp_path, "default")
    assert second is not None and "Version two — edited." in second.persona


def test_save_route_unroute_delete_round_trip(  # noqa: E501
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SNOWPEA_HOME", str(tmp_path / "home"))
    save_guide(
        tmp_path / "home",
        tmp_path,
        "delivery",
        scope="project",
        description="Ship",
        persona="Careful.",
        routing=[],
    )
    save_guide(
        tmp_path / "home",
        tmp_path,
        "delivery",
        scope="project",
        description="Ship",
        persona="Careful.",
        routing=[("migrations", "executor")],
    )
    guide = load_guide(tmp_path / "home", tmp_path, "delivery")
    assert guide is not None and len(guide.routing) == 1
    save_guide(
        tmp_path / "home",
        tmp_path,
        "delivery",
        scope="project",
        description="Ship",
        persona="Careful.",
        routing=[],
    )
    assert load_guide(tmp_path / "home", tmp_path, "delivery") is not None
    assert delete_guide(tmp_path / "home", tmp_path, "delivery", scope="project")
    assert load_guide(tmp_path / "home", tmp_path, "delivery") is None


def test_global_scope_writes_under_snowpea_home(  # noqa: E501
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SNOWPEA_HOME", str(home))
    path = save_guide(
        home, tmp_path, "default", scope="global", description="", persona="Global.", routing=[]
    )
    assert path == home / "teams" / "default.md"
    assert path.is_file()


def test_protocol_declares_team_guide_methods() -> None:
    schema = dump_schema()
    names = set(schema["methods"])
    assert "team.guide.get" in names
    assert "team.guide.set" in names
    assert "team.guide.delete" in names
    assert "team.guide.list" in names
    assert "teams.changed" in schema["events"]


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()


async def test_team_guide_rpc_round_trip(daemon: Daemon, tmp_path: Path) -> None:
    import aiohttp

    workdir = tmp_path / "project"
    workdir.mkdir()
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            saved = await client.ok(
                "team.guide.set",
                {
                    "workdir": str(workdir),
                    "team": "default",
                    "scope": "project",
                    "persona": "RPC persona.",
                    "routing": [{"when": "tests", "agent": "executor"}],
                },
            )
            assert saved["guide"]["persona"] == "RPC persona."
            listed = await client.ok("team.guide.list", {"workdir": str(workdir)})
            assert any(row["team"] == "default" for row in listed["guides"])
            fetched = await client.ok(
                "team.guide.get", {"workdir": str(workdir), "team": "default"}
            )
            assert fetched["guide"]["routing"][0]["agent"] == "executor"
            await client.ok(
                "team.guide.delete",
                {"workdir": str(workdir), "team": "default", "scope": "project"},
            )
            missing = await client.ok(
                "team.guide.get", {"workdir": str(workdir), "team": "default"}
            )
            assert missing["guide"] is None
        finally:
            await client.stop()


async def _run_cli(home: Path, *argv: str) -> int:
    args = build_parser().parse_args(list(argv))
    return await cli_commands.dispatch(args, home)


async def test_cli_route_and_unroute_by_agent_and_index(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workdir = tmp_path / "project"
    workdir.mkdir()
    assert (
        await _run_cli(
            home,
            "team",
            "guide",
            "route",
            "default",
            "critic",
            "code review",
            "--workdir",
            str(workdir),
        )
        == 0
    )
    assert (
        await _run_cli(
            home,
            "team",
            "guide",
            "route",
            "default",
            "architect",
            "API: breaking changes",
            "--workdir",
            str(workdir),
        )
        == 0
    )
    guide = load_guide(home, workdir, "default")
    assert guide is not None
    assert [(rule.when, rule.agent) for rule in guide.routing] == [
        ("code review", "critic"),
        ("API: breaking changes", "architect"),
    ]
    assert (
        await _run_cli(
            home, "team", "guide", "unroute", "default", "critic", "--workdir", str(workdir)
        )
        == 0
    )
    assert (
        await _run_cli(
            home, "team", "guide", "unroute", "default", "1", "--workdir", str(workdir)
        )
        == 0
    )
    guide = load_guide(home, workdir, "default")
    assert guide is not None and not guide.routing


async def test_cli_route_global_and_rejects_unknown_or_path_like_team(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workdir = tmp_path / "project"
    workdir.mkdir()
    assert (
        await _run_cli(
            home,
            "team",
            "guide",
            "route",
            "default",
            "critic",
            "review",
            "--global",
            "--workdir",
            str(workdir),
        )
        == 0
    )
    assert (home / "teams" / "default.md").is_file()

    async def empty_global_settings(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"settings": {"agents": {"teams": {}}}}

    monkeypatch.setattr(cli_commands, "_call", empty_global_settings)
    for team in ("missing", "../escape"):
        assert (
            await _run_cli(
                home,
                "team",
                "guide",
                "route",
                team,
                "critic",
                "review",
                "--workdir",
                str(workdir),
            )
            != 0
        )
        error = capsys.readouterr().err
        assert "unknown team" in error or "not usable" in error


async def test_slash_guide_missing_messages_are_explicit_and_copyable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = Core(settings=Settings(), paths=Paths(home=tmp_path / "home"), token="t")
    session = Session(id="s1", workdir=tmp_path)
    ctx = CommandContext(core=core, session=session, turn_id="t1")
    said: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    monkeypatch.setattr(ctx, "say", say)
    await team_cmd.cmd_team(ctx, "guide")
    assert "# Persona" in said[-1]
    assert "- code review -> critic" in said[-1]
    assert "- implementation -> executor" in said[-1]

    project = ProjectSettings()
    project.agents.teams["core"] = ["executor"]
    project.agents.activeTeam = "core"
    project.save(tmp_path)
    session.team = "core"
    await team_cmd.cmd_team(ctx, "guide")
    assert "team core has no guide — the default guide is NOT applied to a named team" in said[-1]
    assert "create one with /team guide set core" in said[-1].lower()
