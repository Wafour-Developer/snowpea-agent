"""Which paths count as snowpea's own configuration (permission tag ``config``).

An agent asked to *show* a settings file has, more than once, rewritten it
instead: ``write_file`` carries the ``write`` tag, and ``write`` is a silent
``allow`` in both accept and auto mode.  The fix is a second tag.  Any write
that lands inside ``$SNOWPEA_HOME`` or on a project's ``.snowpea/settings.json``
is re-tagged ``config``, which the mode matrix resolves to ``deny`` in plan and
``ask`` everywhere else — never ``allow``, and never promotable by the
allowlist.

The check is on the resolved path, so ``../../.snowpea/settings.json`` and a
symlink into the home directory are caught too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from snowpea_core.config.paths import resolve_home
from snowpea_core.server.protocol import PermissionTag

#: What the approval prompt says when a write is re-tagged.
CONFIG_NOTE = "modifies snowpea configuration"

#: Directory name a project keeps its snowpea settings in.
PROJECT_DIR = ".snowpea"

#: Files inside a project's ``.snowpea/`` that are configuration.  The rest of
#: that directory is working state — ``worktrees/`` holds a team's checkouts,
#: and a worker writing source there must not be asked for permission.
PROJECT_FILES: frozenset[str] = frozenset({"settings.json", "credentials.json"})

#: Directories under ``$SNOWPEA_HOME`` that hold *working state*, not
#: configuration.  ``plans/`` is where a plan-mode session keeps a plan it
#: wants across projects (M2 §9): re-tagging that ``config`` would make the one
#: file plan mode exists to produce the one file it may never write.
HOME_WORKING_DIRS: frozenset[str] = frozenset({"plans"})


def _resolve(path: str, workdir: Any) -> Path | None:
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() and workdir:
            candidate = Path(workdir) / candidate
        return Path(candidate).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _under(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def is_config_path(path: str, *, workdir: Any = None, home: Any = None) -> bool:
    """True when ``path`` is one of snowpea's own state or settings files.

    That is everything under ``$SNOWPEA_HOME`` (``settings.json``,
    ``credentials.json``, ``state.db``, ``token``, ``logs/``) plus a project's
    ``.snowpea/settings.json`` and ``.snowpea/credentials.json``.  The rest of a
    project's ``.snowpea/`` is working state, not configuration: a team worker
    editing source inside ``.snowpea/worktrees/`` is doing ordinary work.
    """
    if not path.strip():
        return False
    resolved = _resolve(path, workdir)
    if resolved is None:
        return False
    try:
        snowpea_home = Path(home).expanduser().resolve() if home else resolve_home()
    except (OSError, RuntimeError, ValueError):  # pragma: no cover - unresolvable $HOME
        return False
    if _under(resolved, snowpea_home):
        return not any(
            _under(resolved, snowpea_home / name) for name in HOME_WORKING_DIRS
        )
    return resolved.parent.name == PROJECT_DIR and resolved.name in PROJECT_FILES


def home_of(core: Any) -> Any:
    """The daemon's own home directory, when the caller knows it.

    Falls back to ``None``, which makes :func:`is_config_path` resolve
    ``$SNOWPEA_HOME`` from the environment instead.
    """
    return getattr(getattr(core, "paths", None), "home", None)


def permission_for_write(
    args: dict[str, Any], session: Any = None, core: Any = None
) -> PermissionTag:
    """``config`` when the call's ``path`` is a snowpea config file, else ``write``."""
    path = str(args.get("path", "") or "")
    workdir = getattr(session, "workdir", None)
    return "config" if is_config_path(path, workdir=workdir, home=home_of(core)) else "write"


__all__ = [
    "CONFIG_NOTE",
    "HOME_WORKING_DIRS",
    "PROJECT_DIR",
    "PROJECT_FILES",
    "home_of",
    "is_config_path",
    "permission_for_write",
]
