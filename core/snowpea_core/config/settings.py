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
from snowpea_core.config.project import AllowlistEntry

#: Regexes (ko/en) that mark a user message as something to remember by
#: itself (M5 contract §1).  They live here rather than in ``memory`` so the
#: memory package can depend on settings and not the other way round.
DEFAULT_REMEMBER_PATTERNS: tuple[str, ...] = (
    r"기억해",
    r"(?i)\bremember\s+(that\s+)?",
    r"(?i)\bnote\s+that\s+",
    r"내\s+\S+(은|는)\s+\S+.*(이다|다|야|예요|입니다)",
)


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
    """Which web-search provider ``web_search`` prefers (M2 contract §3)."""

    provider: str = "ddgs"
    #: Per-provider credentials, e.g. ``{"brave_free": {"api_key": "..."}}``.
    credentials: dict[str, dict[str, Any]] = Field(default_factory=dict)


class BrowserSettings(_Model):
    """Which browser provider the ``browser_*`` tools drive (M2 contract §4)."""

    provider: str = "local_chromium"
    headless: bool = True
    credentials: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ToolsSettings(_Model):
    """Limits shared by every tool that returns fetched or scanned text."""

    max_output_chars: int = 20_000
    #: Tool categories the setup wizard turned on (M3 contract §5).  Empty
    #: means "nothing was chosen yet", which every reader treats as the
    #: catalog defaults in ``setup/catalog.py``.
    enabled_categories: list[str] = Field(default_factory=list)


class MediaMcpSettings(_Model):
    """How to reach the snowpea-studio MCP server (M2 contract §5)."""

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    api_key: str | None = None
    env: dict[str, str] = Field(default_factory=dict)

    def configured(self) -> bool:
        """True once a command or a url is known; credentials gate the tools."""
        return bool(self.command) or bool(self.url)


class MediaSettings(_Model):
    mcp: MediaMcpSettings = Field(default_factory=MediaMcpSettings)


class MemorySettings(_Model):
    """Long-term memory (M5 contract §1)."""

    enabled: bool = True
    #: How many memories the retrieval hook puts in the system prompt.
    top_k: int = 8
    #: Regexes (ko/en) that make a user message worth remembering by itself.
    auto_remember_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_REMEMBER_PATTERNS)
    )


class SchedulerSettings(_Model):
    """The scheduler loop (M5 contract §2)."""

    enabled: bool = True
    #: How often the daemon looks for due jobs.
    tickSec: int = 15
    #: A ``once`` job missed while the daemon was down still runs if it is less
    #: than this late; anything older is marked missed and disabled.
    catchUpSec: int = 3600


class McpSettings(_Model):
    """Extra MCP servers and their permission tags (M2 contract §6)."""

    #: ``{"server": "network"}``; anything absent defaults to ``network``.
    permissions: dict[str, str] = Field(default_factory=dict)
    #: Servers declared inline, same shape as ``.mcp.json``'s ``mcpServers``.
    servers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    enabled: bool = True


class Settings(_Model):
    """Daemon-wide settings, persisted as JSON."""

    agents: AgentsSettings = Field(default_factory=AgentsSettings)
    team: TeamSettings = Field(default_factory=TeamSettings)
    approvals: ApprovalsSettings = Field(default_factory=ApprovalsSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    daemon: DaemonSettings = Field(default_factory=DaemonSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    providers: dict[str, Any] = Field(default_factory=dict)
    #: Chat gateways the setup wizard enabled, ``{"telegram": {"enabled":
    #: true, "token": "..."}}``.  Absent gateways are off.
    gateway: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: Global allowlist patterns (contract §7); the project store lives in
    #: ``<workdir>/.snowpea/settings.json``.
    allowlist: list[AllowlistEntry] = Field(default_factory=list)

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
    "DEFAULT_REMEMBER_PATTERNS",
    "AgentSettings",
    "AgentsSettings",
    "ApprovalsSettings",
    "BrowserSettings",
    "DaemonSettings",
    "McpSettings",
    "MediaMcpSettings",
    "MediaSettings",
    "MemorySettings",
    "SchedulerSettings",
    "SearchSettings",
    "Settings",
    "TeamSettings",
    "ToolsSettings",
]
