"""Per-project settings (``<workdir>/.snowpea/settings.json``)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class ProjectAgentsSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_concurrent: int | None = None
    teams: dict[str, list[str]] = Field(default_factory=dict)
    activeTeam: str | None = None


class ProjectSettings(BaseModel):
    """Project overrides; every field is optional so absence means "inherit"."""

    model_config = ConfigDict(extra="allow")

    defaultMode: Mode | None = None
    allowlist: list[AllowlistEntry] = Field(default_factory=list)
    backend: dict[str, Any] = Field(default_factory=dict)
    agents: ProjectAgentsSettings = Field(default_factory=ProjectAgentsSettings)

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
        return cls.model_validate(raw)

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
    "AllowlistEntry",
    "Mode",
    "ProjectAgentsSettings",
    "ProjectSettings",
]
