"""Per-project settings (``<workdir>/.snowpea/settings.json``)."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger("snowpea.config.project")

Mode = Literal["plan", "accept", "auto"]

PROJECT_DIR_NAME = ".snowpea"
PROJECT_SETTINGS_NAME = "settings.json"

#: The message of every refusal of snowpea's own home as a project (review 7, H-2).
NOT_A_PROJECT = "this folder is snowpea's own home, not a project"

#: The homes of the daemons in this process (:func:`register_home`).
_HOMES: set[Path] = set()


def register_home(home: Path | str) -> None:
    """Remember a daemon's home, so no project file is ever read from or written
    into it (:func:`home_problem`, :meth:`ProjectSettings.load`/``save``)."""
    _HOMES.add(Path(os.path.realpath(Path(home).expanduser())))


def home_problem(workdir: Path | str, home: Path | str | None = None) -> str | None:
    """Why ``workdir`` is not a project folder, or ``None`` when it is one.

    Security review 7, H-2 — on real paths (symlinks resolved):

    ``"home"``
        ``workdir`` is snowpea's own home or a folder inside it.
    ``"parent"``
        ``workdir``'s ``.snowpea`` directory IS the home or lies inside it
        (with the default home, ``workdir`` is the user's home directory): its
        "project" settings, skills, agents and commands are the GLOBAL ones.

    ``home``: one home to judge against; default every registered one.
    """
    homes = (
        {Path(os.path.realpath(Path(home).expanduser()))} if home is not None else _HOMES
    )
    if not homes:
        return None
    try:
        real = Path(os.path.realpath(Path(workdir).expanduser()))
        dot = Path(os.path.realpath(real / PROJECT_DIR_NAME))
    except (OSError, ValueError):
        return "home"  # a path nobody can resolve is not a project either
    if not homes.isdisjoint((real, *real.parents)):
        return "home"
    if not homes.isdisjoint((dot, *dot.parents)):
        return "parent"
    return None


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
    #: ``scheme://host`` the entry is limited to (an approval answered with the
    #: ``site`` scope, 1.6.0); ``None`` matches everywhere.
    origin: str | None = None
    #: UTC ISO time the entry was stored (1.7.0); ``None`` for older entries.
    created_at: str | None = None
    #: Browser profile (clientId) a global entry belongs to (addendum 8): it
    #: covers only that profile's sessions. ``None`` is an ordinary entry.
    host_tools_from: str | None = None
    #: The ``approval.ask`` ``args.reasonCode`` a rule is limited to (e.g.
    #: ``localDevServer``): such a rule covers only asks naming that reason.
    reason_code: str | None = None


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


class ProjectJarvisSettings(BaseModel):
    """Jarvis mode, per project (M19, 1.53.0)."""

    model_config = ConfigDict(extra="allow")

    #: False hides the project from Jarvis: it neither sees its sessions and
    #: memory nor decides for them.
    expose: bool = True


class ProjectSettings(BaseModel):
    """Project overrides; every field is optional so absence means "inherit"."""

    model_config = ConfigDict(extra="allow")

    defaultMode: Mode | None = None
    allowlist: list[AllowlistEntry] = Field(default_factory=list)
    agents: ProjectAgentsSettings = Field(default_factory=ProjectAgentsSettings)
    models: ProjectModelsSettings = Field(default_factory=ProjectModelsSettings)
    jarvis: ProjectJarvisSettings = Field(default_factory=ProjectJarvisSettings)
    #: Additions to the global security lists (1.54.0): ``{blockedCommands:
    #: {presets, custom}, protectedPaths: {presets, custom}}``, every part
    #: optional and empty by default.  A project can only add; read leniently
    #: by :func:`snowpea_core.security.lists.project_additions`.
    security: dict[str, Any] = Field(default_factory=dict)

    @field_validator("security", mode="before")
    @classmethod
    def _security_object(cls, value: Any) -> Any:
        return value if isinstance(value, dict) else {}

    _normalise_allowlist = field_validator("allowlist", mode="before")(_coerce_allowlist)

    @staticmethod
    def path_for(workdir: Path | str) -> Path:
        return Path(workdir).expanduser() / PROJECT_DIR_NAME / PROJECT_SETTINGS_NAME

    @classmethod
    def load(cls, workdir: Path | str) -> ProjectSettings:
        """Read ``<workdir>/.snowpea/settings.json``; absent or bad -> defaults.

        A folder that is snowpea's own home, or whose ``.snowpea`` is
        (:func:`home_problem`), has no project settings: the file there is the
        global ``settings.json``, never read as a project's.
        """
        if home_problem(workdir) is not None:
            return cls()
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

    @classmethod
    def load_effective(cls, workdir: Path | str) -> ProjectSettings:
        """What of the file APPLIES (security review 5, H2).

        Without the project's ``settings`` trust (:mod:`snowpea_core.trust`) a
        project file may only narrow: its allowlist rules, model profiles and
        routing, and any ``providers``/``mcp``/``hooks`` keys are left out;
        ``defaultMode`` is clamped by the session manager; ``jarvis.expose:
        false`` and security-list additions always apply.  Readers use this;
        writers keep :meth:`load` (so a save never drops what it did not read).
        """
        loaded = cls.load(workdir)
        try:
            from snowpea_core.trust.projects import settings_trusted

            if settings_trusted(workdir):
                return loaded
        except Exception:  # noqa: BLE001 - trust fails closed
            log.warning("could not read project trust for %s", workdir, exc_info=True)
        narrowed = loaded.model_copy(deep=True)
        narrowed.allowlist = []
        narrowed.models = ProjectModelsSettings()
        extra = narrowed.__pydantic_extra__
        if isinstance(extra, dict):
            for key in ("providers", "mcp", "hooks"):
                extra.pop(key, None)
        return narrowed

    @classmethod
    def exposed(cls, workdir: Path | str) -> bool:
        """``jarvis.expose`` for the exposure decision only, failing closed (security review M2).

        No settings file: exposed (the default).  A file that exists but cannot
        be read, parsed or validated: hidden — a broken file never shows a
        project the person may have hidden.
        """
        if home_problem(workdir) is not None:
            return True  # not a project: the file there is the global one
        path = cls.path_for(workdir)
        try:
            if not path.exists():
                return True
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return False
            return bool(cls.model_validate(raw).jarvis.expose)
        except (OSError, ValueError, ValidationError, RecursionError):
            return False

    def save(self, workdir: Path | str) -> Path:
        """Write the project settings file, creating ``.snowpea/`` if needed.

        ``ValueError`` for snowpea's own home (:func:`home_problem`): a project
        document is never written over the global ``settings.json`` (security
        review 7, H-2), whichever caller forgot to check.
        """
        if home_problem(workdir) is not None:
            raise ValueError(NOT_A_PROJECT)
        path = self.path_for(workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path


__all__ = [
    "NOT_A_PROJECT",
    "home_problem",
    "register_home",
    "PROJECT_DIR_NAME",
    "PROJECT_SETTINGS_NAME",
    "AGENT_NAME_RE",
    "AllowlistEntry",
    "Mode",
    "ModelProfile",
    "ProjectAgentsSettings",
    "ProjectJarvisSettings",
    "ProjectModelsSettings",
    "ProjectSettings",
    "normalise_teams",
]
