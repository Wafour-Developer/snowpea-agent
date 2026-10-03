"""Configurable budget defaults and legacy settings compatibility."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from snowpea_core.agent.loop import tool_rounds_for
from snowpea_core.config.settings import Settings


def test_hermes_compatible_budget_defaults() -> None:
    settings = Settings()
    core = SimpleNamespace(settings=settings)
    assert tool_rounds_for(core) == 500
    for name in ("explore", "architect", "reviewer", "executor", "custom"):
        session = SimpleNamespace(agent=name, is_subagent=True)
        assert tool_rounds_for(core, session) == 50
    assert settings.agent.auto_budget_continuations == 10


def test_hermes_max_turns_and_legacy_rounds_are_supported() -> None:
    assert Settings.model_validate({"agent": {"max_turns": 700}}).agent.max_tool_rounds == 700
    assert Settings.model_validate({"agent": {"max_tool_rounds": 120}}).agent.max_tool_rounds == 120
    assert (
        Settings.model_validate(
            {"agent": {"max_turns": 700, "max_tool_rounds": 120}}
        ).agent.max_tool_rounds
        == 700
    )


def test_continuation_limit_is_configurable_and_zero_disables() -> None:
    for count in (0, 3, 20):
        settings = Settings.model_validate({"agent": {"auto_budget_continuations": count}})
        assert settings.agent.auto_budget_continuations == count
    with pytest.raises(ValidationError):
        Settings.model_validate({"agent": {"auto_budget_continuations": -1}})


def test_subagent_default_and_role_override_are_independent_of_main() -> None:
    settings = Settings.model_validate(
        {
            "agent": {"max_turns": 700},
            "agents": {"defaultToolRounds": 60, "maxToolRoundsBy": {"reviewer": 12}},
        }
    )
    core = SimpleNamespace(settings=settings)
    assert tool_rounds_for(core) == 700
    assert tool_rounds_for(core, SimpleNamespace(agent="executor", is_subagent=True)) == 60
    assert tool_rounds_for(core, SimpleNamespace(agent="reviewer", is_subagent=True)) == 12
