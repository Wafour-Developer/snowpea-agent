"""Role picking: prefer → general → anonymous / parent."""

from __future__ import annotations

from snowpea_core.agent.role_pick import (
    PARENT,
    general_agents_for,
    missing_role_policy,
    pick_agent,
)


class _Defn:
    def __init__(self, name: str) -> None:
        self.name = name


class _Manager:
    def __init__(self, names: set[str], *, core: object | None = None) -> None:
        self.names = names
        self.core = core

    def definition(self, _session: object, name: str | None) -> object | None:
        return _Defn(name) if name in self.names else None

    def _is_named(self, name: str) -> bool:
        return False


class _Session:
    def __init__(self, team: tuple[str, ...] = ()) -> None:
        self.team_agents = team


class _Settings:
    def __init__(self, general: object = "executor", missing: str = "general") -> None:
        self.agents = type(
            "A",
            (),
            {"generalAgent": general, "missingRole": missing},
        )()


class _Core:
    def __init__(self, settings: _Settings) -> None:
        self.settings = settings


def test_pick_agent_prefers_specialised_then_general() -> None:
    core = _Core(_Settings())
    manager = _Manager({"architect", "executor", "explorer"}, core=core)
    team = _Session(("architect", "executor", "explorer"))
    assert pick_agent(team, manager, ("writer", "architect"), core=core) == "architect"
    assert pick_agent(team, manager, ("writer",), core=core) == "executor"
    assert pick_agent(team, manager, (), core=core) == "executor"


def test_pick_agent_respects_team_roster() -> None:
    core = _Core(_Settings())
    manager = _Manager({"architect", "executor", "explorer"}, core=core)
    explore_only = _Session(("explorer",))
    assert pick_agent(explore_only, manager, ("executor",), core=core) is None
    assert (
        pick_agent(explore_only, manager, ("executor",), core=core, allow_general=False)
        is None
    )


def test_pick_agent_without_team_uses_builtins() -> None:
    core = _Core(_Settings())
    manager = _Manager({"executor", "architect"}, core=core)
    bare = _Session()
    assert pick_agent(bare, manager, ("architect",), core=core) == "architect"


def test_missing_role_and_general_settings() -> None:
    assert general_agents_for(_Core(_Settings(general="executor"))) == ("executor",)
    assert general_agents_for(_Core(_Settings(general=["helper", "executor"]))) == (
        "helper",
        "executor",
    )
    assert missing_role_policy(_Core(_Settings(missing="parent"))) == "parent"
    assert missing_role_policy(_Core(_Settings(missing="nope"))) == "general"


def test_parent_constant() -> None:
    assert PARENT == "parent"
