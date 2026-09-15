"""Resolve configured model profiles for sessions and agents.

One function — :func:`route_for` — owns the whole policy, so there is exactly
one place that decides which provider and model a session talks to.  The chain
it implements, highest priority first (CORE-model-assignment):

1. **An explicit override.**  ``provider``/``model`` passed by the caller:
   ``delegate_task(model=…)``, ``agent.spawn(model=…)``, ``--provider``.
2. **The agent's own assignment.**  Project ``models.agents[<agent>]`` merged
   over global ``agents.models[<agent>]`` (project wins), then the agent
   definition's ``.md`` ``model:`` field.
3. **The session pin.**  What ``/model`` or ``session.setModel`` fixed on this
   session — inherited by its children and restored with it.
4. **The project default.**  ``.snowpea/settings.json`` ``models.default``.
5. **The global default.**  ``$SNOWPEA_HOME/settings.json`` ``models.default``.

Anything unresolved returns ``ModelRoute(None, None)``, which means "no opinion"
— the caller keeps the provider registry's defaults or the parent's route.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from snowpea_core.config.project import ModelProfile, ProjectSettings
from snowpea_core.config.settings import Settings
from snowpea_core.providers.presets import PRESETS, is_local_vendor_config

log = logging.getLogger("snowpea.config.model_routing")


@dataclass(frozen=True)
class ModelRoute:
    """Provider/model selected for a session, or ``None`` to preserve legacy flow."""

    provider: str | None = None
    model: str | None = None

    def resolved(self) -> bool:
        """True when this route actually says something."""
        return self.provider is not None or self.model is not None


@dataclass(frozen=True)
class ModelConfig:
    """Global model settings with a project's merged over them.

    Merging mirrors :func:`snowpea_core.agent.team_config.teams_for`: the
    project document wins key by key, so a repository can add a profile, change
    one agent's assignment or set its own default without restating the rest.
    """

    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    agents: dict[str, str] = field(default_factory=dict)
    default: str | None = None
    project_default: str | None = None


def model_config_for(settings: Settings, workdir: Path | str | None = None) -> ModelConfig:
    """Merge ``<workdir>/.snowpea/settings.json``'s ``models`` block over the global one."""
    profiles = dict(settings.models.profiles)
    agents = dict(settings.agents.models)
    project_default: str | None = None
    if workdir is not None:
        project = ProjectSettings.load(workdir).models
        profiles.update(project.profiles)
        agents.update(project.agents)
        project_default = project.default
    return ModelConfig(
        profiles=profiles,
        agents=agents,
        default=settings.models.default,
        project_default=project_default,
    )


def route_for(
    settings: Settings,
    *,
    provider: str | None = None,
    model: str | None = None,
    agent: str | None = None,
    definition_model: str | None = None,
    workdir: Path | str | None = None,
    session_pin: ModelRoute | None = None,
) -> ModelRoute:
    """Choose a model route for a new session; see the module docstring."""
    if provider is not None or model is not None:
        return ModelRoute(provider, model)

    config = model_config_for(settings, workdir)

    assignment = config.agents.get(agent) if agent else None
    for reference in (assignment, definition_model):
        route = resolve_reference(settings, reference, config=config)
        if route.resolved():
            return route

    if session_pin is not None and session_pin.resolved():
        return session_pin

    for reference in (config.project_default, config.default):
        route = resolve_reference(settings, reference, config=config)
        if route.resolved():
            return route
    return ModelRoute()


def resolve_reference(
    settings: Settings, reference: str | None, *, config: ModelConfig | None = None
) -> ModelRoute:
    """Resolve a profile id, a legacy ``vendor[:model]`` pair, or a bare vendor.

    ``config`` carries the project-merged profile table; without it only the
    global profiles are visible, which is what a caller holding nothing but
    ``Settings`` gets.
    """
    text = str(reference or "").strip()
    if not text or text == "inherit":
        return ModelRoute()
    profiles = config.profiles if config is not None else settings.models.profiles
    profile = profiles.get(text)
    if profile is not None:
        return ModelRoute(profile.provider, profile.model)
    if ":" in text:
        provider, _, model = text.partition(":")
        return ModelRoute(provider.strip() or None, model.strip() or None)
    # Legacy agent definitions used a bare vendor name.  Only a real vendor is
    # accepted: a typo used to be taken at face value and silently routed the
    # agent to a non-existent provider with ``model=None`` instead of falling
    # through to the configured default (CORE-fixes-v017 R12).
    if text in PRESETS or is_local_vendor_config(settings.providers.get(text)):
        # A named OpenAI-compatible server is as real a vendor as a preset one,
        # so ``hon2`` alone routes to it and lets the registry pick the model.
        return ModelRoute(text, None)
    log.warning(
        "ignoring unknown model reference %r: it is neither a profile id, "
        "a 'vendor:model' pair, nor a known vendor",
        text,
    )
    return ModelRoute()


def unknown_reference(
    settings: Settings, reference: str | None, *, workdir: Path | str | None = None
) -> bool:
    """True when ``reference`` is non-empty and resolves to nothing.

    Used by the surfaces that want to *report* a bad reference rather than
    quietly fall through to the default.
    """
    text = str(reference or "").strip()
    if not text or text == "inherit":
        return False
    return not resolve_reference(
        settings, text, config=model_config_for(settings, workdir)
    ).resolved()


__all__ = [
    "ModelConfig",
    "ModelRoute",
    "model_config_for",
    "resolve_reference",
    "route_for",
    "unknown_reference",
]
