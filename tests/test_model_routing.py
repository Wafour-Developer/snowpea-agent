from pathlib import Path

import pytest
from pydantic import ValidationError

from snowpea_core.config.model_routing import route_for
from snowpea_core.config.settings import Settings
from snowpea_core.providers.registry import ProviderRegistry
from snowpea_core.session.manager import SessionManager


def configured() -> Settings:
    return Settings.model_validate({
        "providers": {"default": "openai", "openai": {"model": "legacy"}},
        "models": {"default": "daily", "profiles": {
            "daily": {"provider": "openai", "model": "gpt-daily"},
            "deep": {"provider": "anthropic", "model": "claude-deep"},
        }},
        "agents": {"models": {"architect": "deep"}},
    })


def test_model_route_precedence_and_default() -> None:
    settings = configured()
    assert route_for(settings, agent="architect").provider == "anthropic"
    assert route_for(settings, agent="architect").model == "claude-deep"
    assert route_for(settings, agent="critic", definition_model="deep").model == "claude-deep"
    assert route_for(settings, agent="executor").model == "gpt-daily"
    explicit = route_for(settings, provider="local", model="qwen")
    assert (explicit.provider, explicit.model) == ("local", "qwen")


@pytest.mark.asyncio
async def test_new_sessions_use_default_and_agent_assignment(tmp_path: Path) -> None:
    manager = SessionManager(settings=configured())
    ordinary = await manager.create(tmp_path)
    architect = await manager.create(tmp_path, agent="architect")
    assert (ordinary.provider, ordinary.model) == ("openai", "gpt-daily")
    assert (architect.provider, architect.model) == ("anthropic", "claude-deep")


def test_registry_uses_default_profile() -> None:
    registry = ProviderRegistry(configured())
    assert registry.default_vendor() == "openai"
    assert registry.model_for("openai") == "gpt-daily"


def test_invalid_profile_references_are_rejected() -> None:
    with pytest.raises(ValidationError, match="models.default references unknown profile"):
        Settings.model_validate({"models": {"default": "missing"}})
    with pytest.raises(ValidationError, match="agents.models references unknown"):
        Settings.model_validate({"agents": {"models": {"architect": "missing"}}})


def test_legacy_settings_keep_inherited_route() -> None:
    route = route_for(Settings(), agent="executor")
    assert (route.provider, route.model) == (None, None)
