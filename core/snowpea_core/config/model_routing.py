"""Resolve configured model profiles for sessions and agents."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from snowpea_core.config.settings import Settings
from snowpea_core.providers.presets import PRESETS

log = logging.getLogger("snowpea.config.model_routing")


@dataclass(frozen=True)
class ModelRoute:
    """Provider/model selected for a session, or ``None`` to preserve legacy flow."""

    provider: str | None = None
    model: str | None = None


def route_for(
    settings: Settings,
    *,
    provider: str | None = None,
    model: str | None = None,
    agent: str | None = None,
    definition_model: str | None = None,
) -> ModelRoute:
    """Choose a model route for a new session.

    Explicit ``provider``/``model`` arguments are already-resolved caller intent
    and always win. Otherwise named agent assignments win over an agent
    definition's ``model`` field, and the global default profile is used last.
    If no multi-model settings are present, return ``None`` values so callers
    keep the old provider registry defaults or parent inheritance.
    """

    if provider is not None or model is not None:
        return ModelRoute(provider, model)

    assignment = None
    if agent:
        assignment = settings.agents.models.get(agent)
    for reference in (assignment, definition_model, settings.models.default):
        route = resolve_reference(settings, reference)
        if route.provider is not None or route.model is not None:
            return route
    return ModelRoute()


def resolve_reference(settings: Settings, reference: str | None) -> ModelRoute:
    """Resolve a profile id or legacy ``vendor[:model]`` reference."""

    text = str(reference or "").strip()
    if not text or text == "inherit":
        return ModelRoute()
    profile = settings.models.profiles.get(text)
    if profile is not None:
        return ModelRoute(profile.provider, profile.model)
    if ":" in text:
        provider, _, model = text.partition(":")
        return ModelRoute(provider.strip() or None, model.strip() or None)
    # Legacy agent definitions used a bare vendor name.  Only a real vendor is
    # accepted: a typo used to be taken at face value and silently routed the
    # agent to a non-existent provider with ``model=None`` instead of falling
    # through to the configured default (CORE-fixes-v017 R12).
    if text in PRESETS:
        return ModelRoute(text, None)
    log.warning(
        "ignoring unknown model reference %r: it is neither a profile id, "
        "a 'vendor:model' pair, nor a known vendor",
        text,
    )
    return ModelRoute()


__all__ = ["ModelRoute", "resolve_reference", "route_for"]
