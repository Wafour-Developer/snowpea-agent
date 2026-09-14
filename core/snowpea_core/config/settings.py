"""Global settings (``$SNOWPEA_HOME/settings.json``).

Defaults are the ones fixed by ``docs/design/m1-core-contract.md``; the field
spelling intentionally mixes ``snake_case`` and ``camelCase`` because the
contract does.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from snowpea_core.config.paths import Paths
from snowpea_core.config.project import AllowlistEntry, ModelProfile, normalise_teams

log = logging.getLogger("snowpea.settings")

#: Owner read/write only; ``settings.json`` holds provider credentials.
FILE_MODE = 0o600


def _chmod_quietly(path: Path) -> None:
    """Best-effort ``chmod 0600``; a filesystem without modes must not break saving."""
    try:
        os.chmod(path, FILE_MODE)
    except OSError:  # pragma: no cover - Windows/exotic filesystems
        log.debug("could not chmod %s to %o", path, FILE_MODE)

#: Regexes (ko/en) that mark a user message as something to remember by
#: itself (M5 contract §1).  They live here rather than in ``memory`` so the
#: memory package can depend on settings and not the other way round.
DEFAULT_REMEMBER_PATTERNS: tuple[str, ...] = (
    r"기억해",
    r"(?i)\bremember\s+(that\s+)?",
    r"(?i)\bnote\s+that\s+",
    r"내\s+\S+(은|는)\s+\S+.*(이다|다|야|예요|입니다)",
)

#: Output tokens one provider call may produce, before per-model clamping.
DEFAULT_MAX_TOKENS = 16384

#: Values ``agent.thinking`` and ``providers.<vendor>.thinking`` accept.
THINKING_CHOICES: tuple[str, ...] = ("on", "off", "auto")

DEFAULT_AGENT_TEAM: tuple[str, ...] = (
    "architect",
    "critic",
    "executor",
    "explorer",
    "test-engineer",
    "verifier",
)


def _drop_unresolvable_model_refs(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Remove ``models.default`` / ``agents.models`` entries with no profile.

    Returns the repaired document and a human-readable list of what went, so
    the caller can log it.  Only these two keys are touched: any other
    validation failure is a real error and must still raise.
    """
    models = raw.get("models")
    profiles = models.get("profiles") if isinstance(models, dict) else None
    known = set(profiles) if isinstance(profiles, dict) else set()
    repaired = dict(raw)
    dropped: list[str] = []

    if isinstance(models, dict) and isinstance(models.get("default"), str):
        if models["default"] not in known:
            dropped.append(f"models.default={models['default']}")
            repaired["models"] = {k: v for k, v in models.items() if k != "default"}

    agents = raw.get("agents")
    if isinstance(agents, dict) and isinstance(agents.get("models"), dict):
        assignments = agents["models"]
        keep = {
            agent: profile
            for agent, profile in assignments.items()
            if isinstance(profile, str) and profile in known
        }
        if len(keep) != len(assignments):
            gone = sorted(set(assignments) - set(keep))
            dropped += [f"agents.models.{agent}={assignments[agent]}" for agent in gone]
            repaired["agents"] = {**agents, "models": keep}
    return repaired, dropped


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class AgentsSettings(_Model):
    max_concurrent: int = 3
    #: Per-agent model profile assignment, e.g. {"executor": "openai-fast"}.
    models: dict[str, str] = Field(default_factory=dict)
    #: Tool rounds one delegated turn may make before it has to stop and
    #: report (CORE-subagent-budget).  Either a number for every agent or, like
    #: :attr:`models`, a mapping of agent name -> number with ``"default"`` as
    #: the catch-all key.  Unset falls back to ``agent.max_tool_rounds``,
    #: floored at ``loop.SUBAGENT_TOOL_ROUNDS`` for a child session.
    toolRounds: int | dict[str, int] | None = None
    #: Reusable global teams. The starter team keeps automatic delegation
    #: bounded to the built-in roles instead of every custom definition.
    teams: dict[str, list[str]] = Field(default_factory=dict)
    default_team: str | None = None

    _normalise_teams = field_validator("teams", mode="before")(normalise_teams)


class ModelsSettings(_Model):
    #: Default profile id used for new sessions when no provider/model is explicit.
    default: str | None = None
    #: Named model profiles, keyed by user-chosen ids.
    profiles: dict[str, ModelProfile] = Field(default_factory=dict)


class TeamSettings(_Model):
    max_conflict_retries: int = 2


class RalphSettings(_Model):
    """The ``/ralph`` loop's stopping rule (M7 contract §4)."""

    max_iterations: int = 10


class ApprovalsSettings(_Model):
    timeoutSec: int = 300


class QuestionsSettings(_Model):
    """``ask_user``.  Longer than an approval: a real question needs thinking about."""

    timeoutSec: int = 600


class AgentSettings(_Model):
    #: Tool calls one turn may make before the loop checks in with the person
    #: (a picker: continue or stop). An unattended turn stops at the budget.
    max_tool_rounds: int = 200
    #: Language the model answers in.  ``"auto"`` follows whatever the user
    #: wrote; a tag like ``"ko"`` emits a directed override (CORE-prompts).
    replyLanguage: str = "auto"
    #: Output budget for one provider call.  A reasoning model spends this on
    #: hidden thinking *before* it writes anything, so the old 4096 produced
    #: empty and half-written answers; the default is generous and clamped per
    #: model by ``providers/context_windows.py`` (CORE-reasoning-budget).
    max_tokens: int = DEFAULT_MAX_TOKENS
    #: ``"on"`` | ``"off"`` | ``"auto"``.  ``"auto"`` thinks in the session a
    #: person is watching and stays quiet in a delegated one, where the report
    #: is the whole output.  ``settings.providers.<vendor>.thinking`` overrides
    #: it per vendor, and an agent definition's ``thinking:`` outranks both.
    thinking: str = "auto"
    #: Characters of one project instruction file (AGENTS.md, CLAUDE.md,
    #: .snowpea/instructions.md, .cursorrules) that reach the prompt, and the
    #: ceiling on the merged block.  ``None`` derives it from the session's
    #: context window, clamped to [20_000, 500_000] (CORE-context-files).
    contextFileMaxChars: int | None = None
    #: Skip project instruction files entirely, the way Hermes'
    #: ``--ignore-rules`` does — for a session that must run on snowpea's own
    #: defaults, or to reproduce a problem without the project's prose.
    ignoreContextFiles: bool = False


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
    #: Lines of ``shell`` / ``grep`` / ``glob`` / ``list_dir`` output kept in
    #: context before the middle is spilled to a file the agent can read back
    #: (M15 §A4).
    maxResultLines: int = 400
    #: False disables the read-before-write guard (M15 §A3): ``edit_file`` and
    #: ``write_file`` stop refusing a write to a file this session has not read
    #: in full.  The prompt rule stays either way.
    readBeforeWrite: bool = True
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


class SttSettings(_Model):
    """Speech to text (CORE-multimodal).

    ``provider`` is ``"auto"`` (local whisper, then OpenAI, then a command),
    one backend's name, or ``"off"``.  The OpenAI credentials are not repeated
    here: the provider registry's ``openai`` key is reused.
    """

    provider: str = "auto"
    #: ``command`` backend only: a template containing ``{path}``.
    command: str | None = None
    #: Backend-specific model id (``whisper-1``, ``base``, …).
    model: str | None = None


class TtsSettings(_Model):
    """Text to speech (CORE-multimodal)."""

    enabled: bool = True
    #: ``"auto"`` (studio, then OpenAI, then a local CLI), a backend name, or ``"off"``.
    provider: str = "auto"
    #: ``command`` backend only: a template containing ``{text}`` and ``{out}``.
    command: str | None = None
    model: str | None = None
    voice: str | None = None
    #: Speak every assistant reply without being asked.
    autoSpeak: bool = False


class AudioSettings(_Model):
    """Voice in and out; every part degrades to off with a reason."""

    stt: SttSettings = Field(default_factory=SttSettings)
    tts: TtsSettings = Field(default_factory=TtsSettings)
    #: Force one player / recorder instead of the first one found on PATH.
    player: str | None = None
    recorder: str | None = None


class MemorySettings(_Model):
    """Long-term memory (M5 contract §1)."""

    enabled: bool = True
    #: How many memories the retrieval hook puts in the system prompt.
    top_k: int = 8
    #: Regexes (ko/en) that make a user message worth remembering by itself.
    auto_remember_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_REMEMBER_PATTERNS)
    )
    #: True lets ``memory_write`` ask the human whether a note belongs to this
    #: project or to every project when the model did not say (M5 §1b).
    #: False skips the question and files it under the project.
    askScope: bool = True
    #: Project memories listed in the standing digest every turn starts with.
    digestEntries: int = 30
    #: Character budget for that digest's project section; the rest is counted
    #: in a "… and K more" line rather than dropped silently.
    digestChars: int = 6000


class SchedulerSettings(_Model):
    """The scheduler loop (M5 contract §2)."""

    enabled: bool = True
    #: How often the daemon looks for due jobs.
    tickSec: int = 15
    #: A ``once`` job missed while the daemon was down still runs if it is less
    #: than this late; anything older is marked missed and disabled.
    catchUpSec: int = 3600


class UpdatesSettings(_Model):
    """Update checking and the channel upgrades come from (CORE-update)."""

    #: False turns the daily background check off; the RPC still works on demand.
    check: bool = True
    #: ``"auto"`` prefers PyPI and falls back to git tags; ``"pypi"`` and
    #: ``"git"`` pin one source.
    channel: str = "auto"


class ContextSettings(_Model):
    """Context-window accounting and compaction (CORE-context)."""

    #: False turns automatic compaction off entirely; ``/compact`` still works.
    autoCompact: bool = True
    #: Percentage of the model's context window at which a turn compacts first.
    autoCompactPercent: int = 85
    #: Messages kept verbatim after the summary when compacting.
    keepLastMessages: int = 4


class LspSettings(_Model):
    """Language-server integration (M13 contract §3).

    Off-by-default auto-install is deliberate: AC-46 requires that nothing is
    ever downloaded unless the user asked for it, so a server that is not on
    PATH simply produces no diagnostics.
    """

    #: False takes the seven ``lsp_*`` tools out of ``tool.list`` and stops the
    #: ``Diagnostics`` block being appended to an edit.
    enabled: bool = True
    #: True lets snowpea install a missing server with npm, pip or go into
    #: ``$SNOWPEA_HOME/lsp``.  False never touches the network.
    autoInstall: bool = False
    #: Server ids to leave alone, e.g. ``["eslint"]``.
    disabled: list[str] = Field(default_factory=list)
    #: User-defined servers, and overrides for builtin ones:
    #: ``{"clangd": {"command": ["clangd", "--header-insertion=never"]}}``.
    servers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: Seconds a server may sit unused before it is shut down; 0 disables the
    #: sweep and keeps every started server alive for the daemon's lifetime.
    idleTimeoutSec: int = 600


class McpSettings(_Model):
    """Extra MCP servers and their permission tags (M2 contract §6)."""

    #: ``{"server": "network"}``; anything absent defaults to ``network``.
    permissions: dict[str, str] = Field(default_factory=dict)
    #: Servers declared inline, same shape as ``.mcp.json``'s ``mcpServers``.
    servers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    enabled: bool = True


class SkillRegistrySettings(_Model):
    """The hosted skill registry ``skill.search``/``skill publish`` talk to.

    ``url`` and ``token`` are both overridable per call: ``url`` by
    ``SNOWPEA_REGISTRY_URL`` or ``--registry``, ``token`` by
    ``SNOWPEA_REGISTRY_TOKEN`` or ``--token``.  ``None`` means "use the
    built-in default" (``registry_client.REGISTRY_URL``), not "no registry".
    """

    url: str | None = None
    token: str | None = None


class SkillsSettings(_Model):
    """Skill discovery/publishing configuration (M6-M7 §1)."""

    registry: SkillRegistrySettings = Field(default_factory=SkillRegistrySettings)


class Settings(_Model):
    """Daemon-wide settings, persisted as JSON."""

    agents: AgentsSettings = Field(default_factory=AgentsSettings)
    ralph: RalphSettings = Field(default_factory=RalphSettings)
    team: TeamSettings = Field(default_factory=TeamSettings)
    approvals: ApprovalsSettings = Field(default_factory=ApprovalsSettings)
    questions: QuestionsSettings = Field(default_factory=QuestionsSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    daemon: DaemonSettings = Field(default_factory=DaemonSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    lsp: LspSettings = Field(default_factory=LspSettings)
    skills: SkillsSettings = Field(default_factory=SkillsSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    updates: UpdatesSettings = Field(default_factory=UpdatesSettings)
    providers: dict[str, Any] = Field(default_factory=dict)
    #: Multi-model profiles and the default profile for new sessions.
    models: ModelsSettings = Field(default_factory=ModelsSettings)
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
        try:
            settings = cls.model_validate(raw)
        except ValidationError:
            # A settings file the strict validator rejects used to escape out
            # of ``load``, and the daemon died on startup — which left no
            # ``settings.set`` to repair it with, so the only way back was
            # hand-editing JSON.  Boot degraded instead: drop the references
            # that do not resolve, keep everything else, and say so loudly
            # (CORE-model-assignment B-P1-2).
            raw, dropped = _drop_unresolvable_model_refs(raw)
            if not dropped:
                raise
            log.error(
                "ignoring unusable model routing in %s (%s); "
                "fix it with `snowpea model profiles` or by editing the file",
                path,
                "; ".join(dropped),
            )
            settings = cls.model_validate(raw)
        # Existing installations predate teams. Loading their settings is the
        # first-setup migration point; explicit empty/disabled teams can still
        # be represented with ``default_team: null`` after a team is defined.
        if not settings.agents.teams and "agents" in raw:
            settings.agents.teams["default"] = list(DEFAULT_AGENT_TEAM)
            settings.agents.default_team = "default"
        return settings

    @model_validator(mode="after")
    def _validate_model_profile_refs(self) -> Settings:
        profiles = self.models.profiles
        if self.models.default and self.models.default not in profiles:
            raise ValueError(f"models.default references unknown profile: {self.models.default}")
        missing = {
            agent: profile
            for agent, profile in self.agents.models.items()
            if profile not in profiles
        }
        if missing:
            pairs = ", ".join(f"{agent}={profile}" for agent, profile in sorted(missing.items()))
            raise ValueError(f"agents.models references unknown model profile(s): {pairs}")
        return self

    def save(self, paths: Paths) -> None:
        """Write ``settings.json`` atomically, owner-readable only.

        The document holds ``providers.<vendor>.api_key`` and, since the web
        login work, ``oauth_token`` as well, so it is written with the same
        ``0600`` that :mod:`snowpea_core.config.credentials` and the daemon
        auth token already use (CORE-fixes-v017 R2).  The mode is applied to
        the temp file *before* the rename so the secret is never visible at
        the final path under a wider umask, and re-applied afterwards so an
        existing world-readable file is tightened too.
        """
        paths.ensure()
        target = paths.settings_json
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
        _chmod_quietly(tmp)
        tmp.replace(target)
        _chmod_quietly(target)


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_REMEMBER_PATTERNS",
    "FILE_MODE",
    "DEFAULT_AGENT_TEAM",
    "THINKING_CHOICES",
    "AgentSettings",
    "AgentsSettings",
    "ApprovalsSettings",
    "AudioSettings",
    "BrowserSettings",
    "ContextSettings",
    "DaemonSettings",
    "LspSettings",
    "McpSettings",
    "MediaMcpSettings",
    "MediaSettings",
    "MemorySettings",
    "ModelProfile",
    "ModelsSettings",
    "RalphSettings",
    "SchedulerSettings",
    "SearchSettings",
    "Settings",
    "SkillRegistrySettings",
    "SkillsSettings",
    "SttSettings",
    "TeamSettings",
    "ToolsSettings",
    "TtsSettings",
    "UpdatesSettings",
]
