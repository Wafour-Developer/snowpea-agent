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
