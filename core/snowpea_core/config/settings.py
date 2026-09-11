"""Global settings (``$SNOWPEA_HOME/settings.json``).

Defaults are the ones fixed by ``docs/design/m1-core-contract.md``; the field
spelling intentionally mixes ``snake_case`` and ``camelCase`` because the
contract does.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from snowpea_core.config.paths import Paths


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class AgentsSettings(_Model):
    max_concurrent: int = 3


class TeamSettings(_Model):
    max_conflict_retries: int = 2


class ApprovalsSettings(_Model):
    timeoutSec: int = 300


class AgentSettings(_Model):
    max_tool_rounds: int = 50


class DaemonSettings(_Model):
    idleTimeoutSec: int = 1800


class SearchSettings(_Model):
    provider: str = "ddgs"


class BrowserSettings(_Model):
    provider: str = "local_chromium"


class Settings(_Model):
    """Daemon-wide settings, persisted as JSON."""

    agents: AgentsSettings = Field(default_factory=AgentsSettings)
    team: TeamSettings = Field(default_factory=TeamSettings)
    approvals: ApprovalsSettings = Field(default_factory=ApprovalsSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    daemon: DaemonSettings = Field(default_factory=DaemonSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    providers: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, paths: Paths) -> Settings:
        """Read ``settings.json``; missing or corrupt files yield defaults."""
        path = paths.settings_json
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls.model_validate(raw)

    def save(self, paths: Paths) -> None:
        """Write ``settings.json`` atomically."""
        paths.ensure()
        target = paths.settings_json
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)


__all__ = [
    "AgentSettings",
    "AgentsSettings",
    "ApprovalsSettings",
    "BrowserSettings",
    "DaemonSettings",
    "SearchSettings",
    "Settings",
    "TeamSettings",
]
