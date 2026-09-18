"""Per-project settings (``<workdir>/.snowpea/settings.json``)."""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger("snowpea.config.project")

Mode = Literal["plan", "accept", "auto"]

PROJECT_DIR_NAME = ".snowpea"
PROJECT_SETTINGS_NAME = "settings.json"


class AllowlistEntry(BaseModel):
    """One persisted allowlist pattern (contract §7, M4).

    ``target`` is ``"shell"`` for entries matched against an exec tool's command
    line, or ``"tool:<name>"`` for entries matched against a tool name.  A bare
    string in the JSON file is still accepted and read as a shell pattern.
    """

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: f"al-{uuid.uuid4().hex[:12]}")
    pattern: str
    target: str = "shell"


def _coerce_allowlist(value: Any) -> Any:
    """Accept the pre-M4 ``["^ls.*$"]`` spelling as well as full entries."""
    if isinstance(value, list):
        return [{"pattern": item} if isinstance(item, str) else item for item in value]
    return value


#: An agent name is a definition-file stem: ``architect``, ``test-engineer``,
#: ``my.agent_2``.  Anything else could never resolve to a definition.
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def normalise_teams(value: Any) -> Any:
    """Validate and tidy an ``agents.teams`` mapping.

    ``models.default`` and ``agents.models`` are schema-validated at load time
    but team membership was not, so a hand-edited settings file loaded happily
    and only failed much later, at delegation (CORE-fixes-v017 R11).  What can
    be checked here is shape: a non-empty team name, a list of well-formed,
    non-empty, de-duplicated agent names.  Whether such an agent *exists* is
    deliberately not checked — definitions are discovered per workdir at
    runtime, so an unknown name stays a delegation-time refusal.
    """
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, list[str]] = {}
    for raw_name, raw_members in value.items():
        name = str(raw_name).strip()
        if not name:
            raise ValueError("agents.teams has a team with an empty name")
        if not isinstance(raw_members, list):
            raise ValueError(f"agents.teams[{name}] must be a list of agent names")
        members: list[str] = []
        for entry in raw_members:
            if not isinstance(entry, str):
                raise ValueError(f"agents.teams[{name}] must contain only agent names")
            member = entry.strip()
            if not AGENT_NAME_RE.match(member):
                raise ValueError(f"agents.teams[{name}] has an invalid agent name: {entry!r}")
            if member not in members:
                members.append(member)
        if not members:
            raise ValueError(f"agents.teams[{name}] is empty; delete the team instead")
        cleaned[name] = members
    return cleaned


class ModelProfile(BaseModel):
    """A named ``provider``/``model`` pair, referenced everywhere by its id.

    Defined here rather than in :mod:`snowpea_core.config.settings` so the
    project document can hold profiles too without importing the global one;
    ``settings.ModelProfile`` re-exports it, so the old name still works.
    """

    model_config = ConfigDict(extra="allow")

    provider: str
    model: str
    supportsParallelTools: bool | None = Field(
        default=None,
        description=(
            "When set, overrides whether parallel_tool_calls is sent for this "
            "provider/model pair."
        ),
    )

    @field_validator("provider", "model")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("model profile provider/model must be non-empty")
        return text


class ProjectModelsSettings(BaseModel):
    """Project-scoped model routing (CORE-model-assignment B-P2-4).

    The same three keys as the global ``models`` block plus ``agents``, so a
    repository can pin its own default and its own per-agent assignments
    without touching ``$SNOWPEA_HOME``.  Each key merges over the global one
    the way ``team_config.teams_for`` already merges teams: project wins.
    """

    model_config = ConfigDict(extra="allow")

    default: str | None = None
    profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    #: Per-agent profile ids, e.g. ``{"executor": "fast"}``.
    agents: dict[str, str] = Field(default_factory=dict)


class ProjectAgentsSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_concurrent: int | None = None
    teams: dict[str, list[str]] = Field(default_factory=dict)
    activeTeam: str | None = None

    _normalise_teams = field_validator("teams", mode="before")(normalise_teams)


class ProjectSettings(BaseModel):
    """Project overrides; every field is optional so absence means "inherit"."""

    model_config = ConfigDict(extra="allow")

    defaultMode: Mode | None = None
    allowlist: list[AllowlistEntry] = Field(default_factory=list)
    backend: dict[str, Any] = Field(default_factory=dict)
    agents: ProjectAgentsSettings = Field(default_factory=ProjectAgentsSettings)
    models: ProjectModelsSettings = Field(default_factory=ProjectModelsSettings)

    _normalise_allowlist = field_validator("allowlist", mode="before")(_coerce_allowlist)

    @staticmethod
    def path_for(workdir: Path | str) -> Path:
        return Path(workdir).expanduser() / PROJECT_DIR_NAME / PROJECT_SETTINGS_NAME

    @classmethod
    def load(cls, workdir: Path | str) -> ProjectSettings:
        """Read ``<workdir>/.snowpea/settings.json``; absent or bad -> defaults."""
        path = cls.path_for(workdir)
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except ValidationError:
            # Project settings are read on every session create; a hand-edited
            # file must degrade to "inherit everything", not break the session.
            log.warning("ignoring invalid project settings at %s", path, exc_info=True)
            return cls()

    def save(self, workdir: Path | str) -> Path:
        """Write the project settings file, creating ``.snowpea/`` if needed."""
        path = self.path_for(workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path


__all__ = [
    "PROJECT_DIR_NAME",
    "PROJECT_SETTINGS_NAME",
    "AGENT_NAME_RE",
    "AllowlistEntry",
    "Mode",
    "ModelProfile",
    "ProjectAgentsSettings",
    "ProjectModelsSettings",
    "ProjectSettings",
    "normalise_teams",
]
