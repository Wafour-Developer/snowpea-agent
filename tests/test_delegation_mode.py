"""Delegation mode: off by default, /delegation pins it, the lead's prompt follows."""

from __future__ import annotations

from pathlib import Path

import aiohttp
from _support import connect, fake_provider, make_daemon

from snowpea_core.agent import agent as agent_prompt
from snowpea_core.agent.delegation import (
    FALLBACK_ROSTER,
    delegation_roster,
    effective_delegation,
)
from snowpea_core.commands.delegation_cmd import describe
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session

MARKER = "Delegation mode is on"


def _core(tmp_path: Path, **agents: object) -> Core:
    settings = Settings.model_validate({"agents": agents}) if agents else Settings()
    return Core(settings=settings, paths=Paths(home=tmp_path / "home"), token="t")


def test_off_by_default_and_the_prompt_says_nothing(tmp_path: Path) -> None:
    core = _core(tmp_path)
    session = Session(id="s1", workdir=tmp_path)
    assert effective_delegation(core, session) == (False, "default")
    assert MARKER not in agent_prompt.build_system_prompt(session, [], core=core)


def test_a_session_pin_turns_the_lead_into_an_orchestrator(tmp_path: Path) -> None:
    core = _core(tmp_path, teams={"default": ["executor", "verifier"]}, default_team="default")
    session = Session(id="s1", workdir=tmp_path, delegation=True)
    assert effective_delegation(core, session) == (True, "session")
    prompt = agent_prompt.build_system_prompt(session, [], core=core)
    assert MARKER in prompt
    assert "Your team: executor, verifier." in prompt


def test_the_setting_turns_it_on_and_a_pin_turns_it_back_off(tmp_path: Path) -> None:
    core = _core(tmp_path, delegateByDefault=True)
    assert effective_delegation(core, Session(id="a", workdir=tmp_path)) == (True, "default")
    off = Session(id="b", workdir=tmp_path, delegation=False)
    assert effective_delegation(core, off) == (False, "session")
    assert MARKER not in agent_prompt.build_system_prompt(off, [], core=core)


def test_a_child_never_gets_the_orchestrator_brief(tmp_path: Path) -> None:
    core = _core(tmp_path, delegateByDefault=True)
    lead = Session(id="lead", workdir=tmp_path)
    core.sessions._sessions[lead.id] = lead
    child = Session(id="kid", workdir=tmp_path, parent_session_id="lead", is_subagent=True)
    assert MARKER not in agent_prompt.build_system_prompt(child, [], core=core)


def test_the_roster_falls_back_to_the_built_in_roles(tmp_path: Path) -> None:
    core = _core(tmp_path)
    assert delegation_roster(core, Session(id="s", workdir=tmp_path)) == FALLBACK_ROSTER
    team = Session(id="t", workdir=tmp_path, team="web", team_agents=("executor", "critic"))
    assert delegation_roster(core, team) == ("executor", "critic")


def test_describe_names_the_rule() -> None:
    assert describe(False, "default", ()) == "delegation: off (agents.delegateByDefault)"
    assert describe(True, "session", ("executor",)) == (
        "delegation: on (session pin) — team: executor"
    )


FIXTURE = [{"match": "", "text": "done"}]


async def test_the_command_pins_it_and_it_survives_a_resume(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    home = tmp_path / "home"
    workdir = tmp_path / "project"
    workdir.mkdir()
    with fake_provider(FIXTURE):
        daemon = await make_daemon(home)
        client = await connect(http, daemon)
        created = await client.ok("session.create", {"workdir": str(workdir)})
        assert created["delegation"] is False
        session_id = str(created["sessionId"])

        run = await client.ok(
            "command.run", {"sessionId": session_id, "name": "delegation", "args": "on"}
        )
        assert await client.wait_turn(str(run["turnId"])) == "complete"
        said = client.of_kind("message.done")[-1]["payload"]["text"]
        assert said.startswith("delegation: on (session pin)")
        changed = client.of_kind("mode.changed")[-1]["payload"]
        assert changed["delegation"] is True

        bad = await client.ok(
            "command.run", {"sessionId": session_id, "name": "delegation", "args": "maybe"}
        )
        assert await client.wait_turn(str(bad["turnId"])) == "complete"
        assert "unknown delegation" in client.of_kind("message.done")[-1]["payload"]["text"]
        await client.stop()
        await daemon.stop()

        daemon = await make_daemon(home)
        try:
            client = await connect(http, daemon)
            resumed = await client.ok("session.resume", {"sessionId": session_id})
            assert resumed["delegation"] is True
            run = await client.ok(
                "command.run", {"sessionId": session_id, "name": "delegation", "args": "auto"}
            )
            assert await client.wait_turn(str(run["turnId"])) == "complete"
            said = client.of_kind("message.done")[-1]["payload"]["text"]
            assert said.startswith("delegation: off (agents.delegateByDefault)")
            await client.stop()
        finally:
            await daemon.stop()


async def test_the_setting_is_what_a_new_session_reports(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    with fake_provider(FIXTURE):
        daemon = await make_daemon(tmp_path / "home", {"agents": {"delegateByDefault": True}})
        try:
            client = await connect(http, daemon)
            created = await client.ok("session.create", {"workdir": str(workdir)})
            assert created["delegation"] is True
            await client.stop()
        finally:
            await daemon.stop()
