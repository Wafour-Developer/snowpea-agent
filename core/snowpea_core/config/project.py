"""Per-project settings (``<workdir>/.snowpea/settings.json``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["plan", "accept", "auto"]

PROJECT_DIR_NAME = ".snowpea"
PROJECT_SETTINGS_NAME = "settings.json"


class ProjectAgentsSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_concurrent: int | None = None


class ProjectSettings(BaseModel):
    """Project overrides; every field is optional so absence means "inherit"."""

    model_config = ConfigDict(extra="allow")

    defaultMode: Mode | None = None
    allowlist: list[str] = Field(default_factory=list)
    backend: dict[str, Any] = Field(default_factory=dict)
    agents: ProjectAgentsSettings = Field(default_factory=ProjectAgentsSettings)

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
    "Mode",
    "ProjectAgentsSettings",
    "ProjectSettings",
]
