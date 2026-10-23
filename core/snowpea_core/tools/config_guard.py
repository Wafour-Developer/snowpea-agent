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

import posixpath
from pathlib import Path, PurePosixPath
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


#: Subdirectories of a session workspace (``sessions/<day>_<id>/``) that are
#: scratch and output: an agent writing a helper script to ``tmp/`` in AUTO
#: mode was asked for approval every time, as if it were editing settings.
SESSION_WORKING_DIRS: frozenset[str] = frozenset({"tmp", "artifacts"})


def _in_session_workspace(resolved: Path, snowpea_home: Path) -> bool:
    """True for a path inside ``sessions/<workspace>/tmp`` or ``/artifacts``."""
    try:
        parts = resolved.relative_to(snowpea_home / "sessions").parts
    except ValueError:
        return False
    return len(parts) >= 3 and parts[1] in SESSION_WORKING_DIRS


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
        if _in_session_workspace(resolved, snowpea_home):
            return False
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
    """``config`` for a snowpea config file, ``outside`` for a path outside the
    project (addendum 29), else ``write``.  ``config`` wins."""
    from snowpea_core.tools.call_paths import call_paths

    workdir = getattr(session, "workdir", None)
    home = home_of(core)
    paths = call_paths(args) or [""]
    if any(is_config_path(path, workdir=workdir, home=home) for path in paths):
        return "config"
    if outside_paths(args, session, home):
        return "outside"
    return "write"


def outside_paths(args: dict[str, Any], session: Any = None, home: Any = None) -> list[str]:
    """The resolved paths of a write call that lie outside the session's project.

    Symlinks are resolved, so a link inside the project pointing out counts
    as outside.  Core's own working areas are not outside: the session
    workspace's ``tmp``/``artifacts``, ``$SNOWPEA_HOME/plans``, and anything
    under the project (``.snowpea/worktrees`` included).  A session without a
    workdir has no project to be outside of.
    """
    from snowpea_core.tools.call_paths import call_paths

    workdir = getattr(session, "workdir", None)
    if not workdir:
        return []
    backend = getattr(session, "backend", None)
    if getattr(backend, "kind", "local") in REMOTE_KINDS:
        return _remote_outside_paths(args, backend)
    try:
        root = Path(workdir).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return []
    try:
        snowpea_home = Path(home).expanduser().resolve() if home else resolve_home()
    except (OSError, RuntimeError, ValueError):  # pragma: no cover - unresolvable $HOME
        snowpea_home = None
    found: list[str] = []
    for raw in call_paths(args):
        if not str(raw).strip():
            continue
        resolved = _resolve(str(raw), root)
        if resolved is None or _under(resolved, root):
            continue
        if snowpea_home is not None and (
            _in_session_workspace(resolved, snowpea_home)
            or any(_under(resolved, snowpea_home / name) for name in HOME_WORKING_DIRS)
        ):
            continue
        if str(resolved) not in found:
            found.append(str(resolved))
    return found


#: Backends whose files are not on this machine (``ExecutionBackend.kind``).
REMOTE_KINDS: frozenset[str] = frozenset({"ssh", "docker"})


def _remote_outside_paths(args: dict[str, Any], backend: Any) -> list[str]:
    """``outside_paths`` for an SSH or Docker session, judged against its own cwd.

    The remote filesystem is not here to resolve symlinks in, so the path is
    normalised as written; a ``~`` path is outside.
    """
    from snowpea_core.tools.call_paths import call_paths

    root = PurePosixPath(str(getattr(backend, "cwd", "") or "/"))
    found: list[str] = []
    for raw in call_paths(args):
        text = str(raw).strip()
        if not text:
            continue
        if text.startswith("~"):
            resolved = PurePosixPath(text)
        else:
            joined = text if text.startswith("/") else f"{root}/{text}"
            resolved = PurePosixPath(posixpath.normpath(joined))
        if (resolved == root or root in resolved.parents) and not text.startswith("~"):
            continue
        if str(resolved) not in found:
            found.append(str(resolved))
    return found


def permission_for_read(
    args: dict[str, Any], session: Any = None, core: Any = None
) -> PermissionTag:
    """``secret`` when the call's ``path`` is a credential or key location, else ``read``.

    The read is never refused outright — the owner may want an icon under
    ~/.ssh or the .env for a real reason — but outside auto mode it is asked
    for first, the way ``config`` guards writes to snowpea's own settings.
    """
    from snowpea_core.tools.call_paths import call_paths

    # A batched read is as sensitive as its most sensitive file.
    for raw in call_paths(args):
        if _secret_path(raw, session, core):
            return "secret"
    return "read"


def _secret_path(raw: str, session: Any, core: Any) -> bool:
    from snowpea_core.agent.prompt_refs import is_secret_pattern

    if not raw:
        return False
    backend = getattr(session, "backend", None)
    try:
        candidate = Path(backend.resolve(raw)) if backend is not None else Path(raw).expanduser()
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return False
    # The pattern rules (.ssh/.gnupg/.aws, .env*, *.pem/*.key, id_rsa*) plus
    # snowpea's own credentials and token; settings.json stays an ordinary read.
    if is_secret_pattern(resolved):
        return True
    home = home_of(core)
    if home:
        base = Path(home)
        if resolved in {(base / "credentials.json").resolve(), (base / "token").resolve()}:
            return True
    return _another_sessions_attachment(resolved, session, core)


def _another_sessions_attachment(resolved: Path, session: Any, core: Any) -> bool:
    """True for a file another conversation's user attached (1.23.0).

    A session's own attachments (its root's, for a delegated child) are an
    ordinary read; the rest of ``attachments/`` is asked for first.
    """
    paths = getattr(core, "paths", None)
    if paths is None or session is None:
        return False
    try:
        store = Path(paths.attachments_dir).resolve()
    except (OSError, RuntimeError, TypeError):
        return False
    if not _under(resolved, store) or resolved == store:
        return False
    from snowpea_core.attachments.model import safe_name
    from snowpea_core.tools.host_tools import root_session

    root = root_session(core, session) or session
    own = store / safe_name(str(getattr(root, "id", "") or "shared"))
    return not _under(resolved, own)


__all__ = [
    "CONFIG_NOTE",
    "HOME_WORKING_DIRS",
    "PROJECT_DIR",
    "PROJECT_FILES",
    "home_of",
    "is_config_path",
    "permission_for_read",
    "permission_for_write",
]
