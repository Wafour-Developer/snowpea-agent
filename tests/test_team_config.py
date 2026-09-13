from snowpea_core.agent.team_config import active_team, teams_for
from snowpea_core.config.paths import Paths
from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings
from snowpea_core.setup.state import WizardState


def test_default_team_is_available_after_first_setup(tmp_path):
    settings = Settings.model_validate({
        "agents": {"teams": {"default": ["executor", "verifier"]}, "default_team": "default"}
    })
    team = active_team(settings, tmp_path)
    assert team is not None
    assert team.name == "default"
    assert "executor" in team.agents


def test_project_team_overrides_and_becomes_active(tmp_path):
    project = ProjectSettings()
    project.agents.teams["core"] = ["architect", "executor", "verifier"]
    project.agents.activeTeam = "core"
    project.save(tmp_path)
    assert teams_for(Settings(), tmp_path)["core"] == ["architect", "executor", "verifier"]
    team = active_team(Settings(), tmp_path)
    assert team is not None
    assert team.name == "core"
    assert team.agents == ("architect", "executor", "verifier")


def test_first_setup_writes_the_builtin_default_team(tmp_path):
    settings = WizardState().write(Paths(home=tmp_path), Settings())
    assert settings.agents.default_team == "default"
    assert "executor" in settings.agents.teams["default"]


def test_existing_settings_gain_the_default_team_on_load(tmp_path):
    paths = Paths(home=tmp_path)
    paths.ensure()
    paths.settings_json.write_text('{"agents": {"max_concurrent": 3}}')
    settings = Settings.load(paths)
    assert settings.agents.default_team == "default"
    assert "verifier" in settings.agents.teams["default"]


def test_malformed_teams_are_rejected_at_load(tmp_path):
    """CORE-fixes-v017 R11: team membership is schema-checked like model profiles.

    Only the *shape* is checked here.  Whether an agent exists cannot be known
    at settings-load time — definitions are discovered per workdir at runtime —
    so an unknown but well-formed name stays a delegation-time refusal.
    """
    import pytest
    from pydantic import ValidationError

    from snowpea_core.config.settings import Settings

    with pytest.raises(ValidationError, match="must contain only agent names"):
        Settings.model_validate({"agents": {"teams": {"delivery": [{"name": "executor"}]}}})
    with pytest.raises(ValidationError, match="invalid agent name"):
        Settings.model_validate({"agents": {"teams": {"delivery": ["exec utor"]}}})
    with pytest.raises(ValidationError, match="is empty"):
        Settings.model_validate({"agents": {"teams": {"delivery": []}}})
    with pytest.raises(ValidationError, match="must be a list"):
        Settings.model_validate({"agents": {"teams": {"delivery": "executor"}}})

    # A well-formed roster loads, de-duplicated and stripped.
    settings = Settings.model_validate(
        {"agents": {"teams": {"delivery": [" executor ", "executor", "critic"]}}}
    )
    assert settings.agents.teams["delivery"] == ["executor", "critic"]


def test_an_invalid_project_settings_file_degrades_to_defaults(tmp_path):
    """Project settings are read on every session create; they must not raise."""
    import json

    from snowpea_core.config.project import ProjectSettings

    (tmp_path / ".snowpea").mkdir()
    (tmp_path / ".snowpea" / "settings.json").write_text(
        json.dumps({"agents": {"teams": {"delivery": "executor"}}}), encoding="utf-8"
    )
    assert ProjectSettings.load(tmp_path).agents.teams == {}
