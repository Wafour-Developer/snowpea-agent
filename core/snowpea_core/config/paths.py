"""Filesystem layout for the snowpea home directory.

``SNOWPEA_HOME`` (default ``~/.snowpea``) holds every piece of daemon state:
``daemon.json`` (discovery), ``token`` (0600 shared secret), ``settings.json``,
``state.db`` and ``logs/``.  See ``docs/design/m1-core-contract.md`` §0.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_HOME = Path("~/.snowpea")


def utc_now() -> str:
    """The daemon's wire timestamp: UTC ISO-8601 with milliseconds and a ``Z``.

    Every row the daemon writes (sessions, memories, team tasks, gateway
    bindings, the approval log) and every ``ts`` on the wire uses this one
    format, so it is defined once here.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_home(home: Path | str | None = None) -> Path:
    """Return the snowpea home directory without creating it."""
    if home is not None:
        return Path(home).expanduser().resolve()
    env = os.environ.get("SNOWPEA_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_HOME.expanduser().resolve()


@dataclass(frozen=True)
class Paths:
    """Resolved paths under ``SNOWPEA_HOME``."""

    home: Path = field(default_factory=resolve_home)

    @classmethod
    def create(cls, home: Path | str | None = None) -> Paths:
        """Resolve the home directory and make sure the directories exist."""
        paths = cls(home=resolve_home(home))
        paths.ensure()
        return paths

    @property
    def daemon_json(self) -> Path:
        return self.home / "daemon.json"

    @property
    def token_file(self) -> Path:
        return self.home / "token"

    @property
    def settings_json(self) -> Path:
        return self.home / "settings.json"

    @property
    def credentials_json(self) -> Path:
        """Gateway credentials, kept at 0600 (M5 contract §3)."""
        return self.home / "credentials.json"

    @property
    def update_check_json(self) -> Path:
        """Cached ``system.checkUpdate`` answer, refreshed at most daily."""
        return self.home / "update-check.json"

    @property
    def install_json(self) -> Path:
        """How snowpea was installed: ``{"method", "source", "time"}``.

        Written by ``installer/install.sh`` and ``installer/install.ps1`` so
        ``system.update`` can fall back to the original installer when ``uv``
        is not on PATH.
        """
        return self.home / "install.json"

    @property
    def state_db(self) -> Path:
        return self.home / "state.db"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def daemon_log(self) -> Path:
        return self.logs_dir / "daemon.log"

    @property
    def approvals_log(self) -> Path:
        return self.logs_dir / "approvals.jsonl"

    @property
    def update_log(self) -> Path:
        """Where ``system.update`` sends the installer's stdout and stderr."""
        return self.logs_dir / "update.log"

    @property
    def jobs_log(self) -> Path:
        """Where scheduled-job output goes when no chat channel takes it."""
        return self.logs_dir / "jobs.log"

    def ensure(self) -> None:
        """Create ``home`` and ``logs/`` (idempotent)."""
        self.home.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


__all__ = ["DEFAULT_HOME", "Paths", "resolve_home"]
