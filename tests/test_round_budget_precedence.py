"""Round budget precedence stays explicit after removing built-in role caps."""

from types import SimpleNamespace

import pytest

from snowpea_core.agent.loop import tool_rounds_for
from snowpea_core.config.settings import Settings
from snowpea_core.server.protocol import TurnDone


@pytest.mark.parametrize("is_subagent", [False, True])
def test_explicit_round_budget_precedence(is_subagent: bool) -> None:
    settings = Settings.model_validate(
        {"agents": {"maxToolRoundsBy": {"explore": 9}, "maxToolRounds": 30,
                    "toolRounds": {"explore": 40, "default": 45}}}
    )
    core = SimpleNamespace(settings=settings)
    session = SimpleNamespace(agent="explore", is_subagent=is_subagent, max_tool_rounds=20)
    assert tool_rounds_for(core, session) == 9
    settings.agents.maxToolRoundsBy = {"default": 10, "*": 11}
    assert tool_rounds_for(core, session) == 10
    settings.agents.maxToolRoundsBy = {"*": 11}
    assert tool_rounds_for(core, session) == 11
    settings.agents.maxToolRoundsBy = {}
    assert tool_rounds_for(core, session) == 20
    session.max_tool_rounds = None
    assert tool_rounds_for(core, session) == 30
    settings.agents.maxToolRounds = None
    assert tool_rounds_for(core, session) == 40
    settings.agents.toolRounds = {"default": 45, "*": 46}
    assert tool_rounds_for(core, session) == 45
    settings.agents.toolRounds = {"*": 46}
    assert tool_rounds_for(core, session) == 46
    settings.agents.toolRounds = 47
    assert tool_rounds_for(core, session) == 47
    settings.agents.toolRounds = None
    assert tool_rounds_for(core, session) == (50 if is_subagent else 500)


@pytest.mark.parametrize("role", ["explore", "explorer", "architect", "reviewer", "executor"])
def test_every_child_role_uses_shared_fifty_round_fallback(role: str) -> None:
    core = SimpleNamespace(settings=Settings())
    assert tool_rounds_for(core, SimpleNamespace(agent=role, is_subagent=True)) == 50


def test_stalled_turn_reason_is_valid_on_the_wire() -> None:
    event = TurnDone(turnId="turn", reason="stalled")
    assert event.model_dump()["reason"] == "stalled"
