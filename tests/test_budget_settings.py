"""Configurable budget defaults and legacy settings compatibility."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from snowpea_core.agent.loop import (
    _Attempt,
    _budget_probe_continue_window,
    _TurnProgress,
    tool_rounds_for,
)
from snowpea_core.config.settings import Settings
from snowpea_core.providers.base import ChatMessage, ToolCall


def test_hermes_compatible_budget_defaults() -> None:
    settings = Settings()
    core = SimpleNamespace(settings=settings)
    assert tool_rounds_for(core) == 500
    for name in ("explore", "architect", "reviewer", "executor", "custom"):
        session = SimpleNamespace(agent=name, is_subagent=True)
        assert tool_rounds_for(core, session) == 50
    assert settings.agent.auto_budget_continuations == 10
    assert settings.agent.verify_continue_rounds == 20
    assert settings.agent.no_progress_rounds == 40
    assert settings.agent.turn_max_minutes == 0
    assert settings.agent.turn_max_tokens == 0


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


def test_unattended_guard_settings_validate() -> None:
    settings = Settings.model_validate(
        {
            "agent": {
                "verify_continue_rounds": 3,
                "no_progress_rounds": 0,
                "turn_max_minutes": 1.5,
                "turn_max_tokens": 123,
            }
        }
    )
    assert settings.agent.verify_continue_rounds == 3
    assert settings.agent.no_progress_rounds == 0
    assert settings.agent.turn_max_minutes == 1.5
    assert settings.agent.turn_max_tokens == 123
    for key in ("no_progress_rounds", "turn_max_minutes", "turn_max_tokens"):
        with pytest.raises(ValidationError):
            Settings.model_validate({"agent": {key: -1}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"agent": {"verify_continue_rounds": 0}})


def test_budget_probe_verify_window_counts_against_auto_continuations() -> None:
    core = SimpleNamespace(
        settings=Settings.model_validate(
            {"agent": {"auto_budget_continuations": 2, "verify_continue_rounds": 3}}
        )
    )
    assert _budget_probe_continue_window(core, 0) == (1, 3)
    assert _budget_probe_continue_window(core, 1) == (2, 3)
    assert _budget_probe_continue_window(core, 2) is None


def test_no_progress_tracker_counts_calls_outputs_and_tokens() -> None:
    progress = _TurnProgress(no_progress_limit=2)
    progress.add_usage(_Attempt(input_tokens=7, output_tokens=11))
    assert progress.tokens == 18
    history = [
        ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})],
        ),
        ChatMessage(role="tool", name="read_file", tool_call_id="c1", content="same"),
    ]
    assert progress.observe_round(history) is True
    assert progress.observe_round(history) is False
    assert progress.stagnant_rounds == 1
    assert progress.observe_round(history) is False
    assert progress.stalled() is True
    # An edit is a new call with a new result: that is progress.
    history += [
        ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(id="e1", name="patch", arguments={"path": "a.txt"})],
        ),
        ChatMessage(role="tool", name="patch", tool_call_id="e1", content="-same\n+changed"),
    ]
    assert progress.observe_round(history) is True
    assert progress.stagnant_rounds == 0
    assert progress.stalled() is False
    # A compacted history can be shorter than the last observed index; the
    # tracker must not miss later productive calls after that shrink.
    compacted = [
        ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(id="c2", name="read_file", arguments={"path": "b.txt"})],
        )
    ]
    assert progress.observe_round(compacted) is True


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


def test_an_untouched_default_team_gains_new_built_in_roles(tmp_path) -> None:
    import json

    from snowpea_core.config.paths import Paths
    from snowpea_core.config.settings import DEFAULT_AGENT_TEAM

    paths = Paths(home=tmp_path)
    old = ["architect", "critic", "executor", "explorer", "test-engineer", "verifier"]
    paths.settings_json.write_text(
        json.dumps({"agents": {"teams": {"default": old}}}), encoding="utf-8"
    )
    loaded = Settings.load(paths)
    assert loaded.agents.teams["default"] == list(DEFAULT_AGENT_TEAM)
    assert "writer" in loaded.agents.teams["default"]
    custom = ["executor", "verifier"]
    paths.settings_json.write_text(
        json.dumps({"agents": {"teams": {"default": custom}}}), encoding="utf-8"
    )
    assert Settings.load(paths).agents.teams["default"] == custom
