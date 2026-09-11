"""Persistent command allowlist (contract §7, plan §3.1).

A pattern is a **regular expression**.  What it is matched against depends on
the entry's ``target``:

* ``shell`` — the command line of an ``exec``-tagged tool call, e.g.
  ``^ls( .*)?$`` matches ``ls`` and ``ls -la src``.
* ``tool:<name>`` — the tool name itself, for everything that is not a shell.

Two stores back the same API: ``project`` lives in
``<workdir>/.snowpea/settings.json`` and ``global`` in
``$SNOWPEA_HOME/settings.json``.  A match only ever promotes ``ask`` to
``allow``; :class:`~snowpea_core.permissions.policy.PermissionPolicy` never
consults the allowlist for a ``deny``.
"""

from __future__ import annotations

import logging
import re
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from snowpea_core.config.paths import Paths
from snowpea_core.config.project import AllowlistEntry, ProjectSettings
from snowpea_core.config.settings import Settings

log = logging.getLogger("snowpea.permissions.allowlist")

#: Where an entry is stored.  The wire protocol spells ``global`` as ``always``.
Scope = Literal["project", "global"]

#: ``target`` value for entries matched against a shell command line.
SHELL_TARGET = "shell"

#: Tools whose call is a shell command line even without the ``exec`` tag.
SHELL_TOOLS: frozenset[str] = frozenset({"shell", "bash", "run_command"})


def tool_target(name: str) -> str:
    """``"shell"`` -> ``"tool:shell"``."""
    return f"tool:{name}"


def new_id() -> str:
    return f"al-{uuid.uuid4().hex[:12]}"


def command_of(args: Any) -> str | None:
    """The shell command line inside a tool call's arguments, if any."""
    if not isinstance(args, dict):
        return None
    value = args.get("command")
    if not isinstance(value, str):
        return None
    command = value.strip()
    return command or None


def first_token(command: str) -> str:
    """``"ls -la src"`` -> ``"ls"``; never raises on a malformed quote."""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else command.strip()


def pattern_for_command(command: str) -> str:
    """The exact-command regex an approval scope promotion records."""
    return f"^{re.escape(first_token(command))}( .*)?$"


def pattern_for_tool(name: str) -> str:
    return f"^{re.escape(name)}$"


def _matches_pattern(pattern: str, subject: str) -> bool:
    try:
        return re.search(pattern, subject) is not None
    except re.error:
        log.warning("ignoring invalid allowlist pattern %r", pattern)
        return False


@dataclass(frozen=True)
class AllowlistItem:
    """One stored entry plus the store it came from."""

    id: str
    pattern: str
    target: str
    scope: Scope


class Allowlist:
    """Read/write access to the project and global allowlists."""

    def __init__(self, paths: Paths | None = None, settings: Settings | None = None) -> None:
        self.paths = paths
        self.settings = settings or Settings()

    def bind(self, paths: Paths, settings: Settings) -> None:
        """Late wiring from ``app_server`` once ``Core`` exists."""
        self.paths = paths
        self.settings = settings

    # -- stores --------------------------------------------------------
    def _project_items(self, workdir: Path | str | None) -> list[AllowlistItem]:
        if workdir is None:
            return []
        project = ProjectSettings.load(workdir)
        return [AllowlistItem(e.id, e.pattern, e.target, "project") for e in project.allowlist]

    def _global_items(self) -> list[AllowlistItem]:
        return [AllowlistItem(e.id, e.pattern, e.target, "global") for e in self.settings.allowlist]

    # -- queries -------------------------------------------------------
    def list(
        self, scope: Scope | None = None, *, workdir: Path | str | None = None
    ) -> list[AllowlistItem]:
        """Stored entries; ``scope=None`` returns project entries then global."""
        items: list[AllowlistItem] = []
        if scope in (None, "project"):
            items.extend(self._project_items(workdir))
        if scope in (None, "global"):
            items.extend(self._global_items())
        return items

    def matches(
        self, tool: Any, args: dict[str, Any] | None = None, *, workdir: Path | str | None = None
    ) -> bool:
        """True when some stored pattern covers this call."""
        name = tool if isinstance(tool, str) else str(getattr(tool, "name", ""))
        if not name:
            return False
        permission = None if isinstance(tool, str) else getattr(tool, "permission", None)
        command = command_of(args or {})
        is_shell = permission == "exec" or name in SHELL_TOOLS
        shell_subject = command if (command and is_shell) else None
        wanted_tool_target = tool_target(name)
        for item in self.list(workdir=workdir):
            if item.target == SHELL_TARGET:
                subject = shell_subject
            elif item.target == wanted_tool_target:
                subject = name
            else:
                continue
            if subject is not None and _matches_pattern(item.pattern, subject):
                return True
        return False

    def has(
        self, pattern: str, scope: Scope, target: str, *, workdir: Path | str | None = None
    ) -> bool:
        return any(
            item.pattern == pattern and item.target == target
            for item in self.list(scope, workdir=workdir)
        )

    # -- mutations -----------------------------------------------------
    def add(
        self,
        pattern: str,
        scope: Scope = "project",
        target: str = SHELL_TARGET,
        *,
        workdir: Path | str | None = None,
    ) -> str:
        """Store ``pattern`` and return its id (existing duplicates are reused)."""
        pattern = pattern.strip()
        if not pattern:
            raise ValueError("an allowlist pattern may not be empty")
        re.compile(pattern)  # fail loudly rather than storing a dead pattern
        for item in self.list(scope, workdir=workdir):
            if item.pattern == pattern and item.target == target:
                return item.id
        entry = AllowlistEntry(id=new_id(), pattern=pattern, target=target)
        if scope == "project":
            if workdir is None:
                raise ValueError("a project allowlist entry needs a workdir")
            project = ProjectSettings.load(workdir)
            project.allowlist = [*project.allowlist, entry]
            project.save(workdir)
        else:
            self.settings.allowlist = [*self.settings.allowlist, entry]
            self._save_global()
        log.info("allowlist += %s (%s, %s)", pattern, scope, target)
        return entry.id

    def remove(self, pattern_id: str, *, workdir: Path | str | None = None) -> bool:
        """Delete an entry from whichever store holds it."""
        removed = False
        if workdir is not None:
            project = ProjectSettings.load(workdir)
            kept = [e for e in project.allowlist if e.id != pattern_id]
            if len(kept) != len(project.allowlist):
                project.allowlist = kept
                project.save(workdir)
                removed = True
        kept_global = [e for e in self.settings.allowlist if e.id != pattern_id]
        if len(kept_global) != len(self.settings.allowlist):
            self.settings.allowlist = kept_global
            self._save_global()
            removed = True
        return removed

    def _save_global(self) -> None:
        if self.paths is None:
            return
        self.settings.save(self.paths)


__all__ = [
    "SHELL_TARGET",
    "SHELL_TOOLS",
    "Allowlist",
    "AllowlistItem",
    "Scope",
    "command_of",
    "first_token",
    "new_id",
    "pattern_for_command",
    "pattern_for_tool",
    "tool_target",
]
