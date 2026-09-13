from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.setup import wizard
from snowpea_core.setup.state import WizardState


def test_state_keeps_multiple_models_and_agent_assignments(tmp_path):
    state = WizardState(vendor="openai", model="gpt-fast", api_key="openai-key")
    fast = state.add_current_model_profile()
    state.select_vendor("anthropic")
    state.api_key = "anthropic-key"
    state.model = "claude-deep"
    deep = state.add_current_model_profile(make_default=True)
    state.assign_agent_model("architect", deep)
    settings = state.write(Paths(home=tmp_path), Settings())
    assert set(settings.models.profiles) == {fast, deep}
    assert settings.models.default == deep
    assert settings.agents.models == {"architect": deep}
    assert settings.providers["openai"]["api_key"] == "openai-key"
    assert settings.providers["anthropic"]["api_key"] == "anthropic-key"


def test_switching_providers_does_not_leak_credentials():
    state = WizardState(
        vendor="openai",
        api_key="secret-one",
        model="gpt-a",
        base_url="https://one.invalid",
    )
    state.remember_current_provider()
    state.select_vendor("anthropic")
    assert state.api_key is None
    assert state.model is None
    assert state.base_url is None
    state.select_vendor("openai")
    assert state.model == "gpt-a"
    assert state.base_url == "https://one.invalid"
    assert state.api_key is None
    assert state.has_saved_key is True


def test_noninteractive_flags_create_the_default_profile(tmp_path):
    result = wizard.run(
        "full",
        home=tmp_path,
        vendor="openai",
        key="secret",
        model="gpt-fast",
        interactive=False,
    )
    assert result.settings.models.default == "openai:gpt-fast"
    profile = result.settings.models.profiles["openai:gpt-fast"]
    assert (profile.provider, profile.model) == ("openai", "gpt-fast")
