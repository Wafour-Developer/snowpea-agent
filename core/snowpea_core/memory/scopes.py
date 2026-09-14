"""Memory scopes: which namespace a memory belongs to (M5 contract §1b).

Three scopes share one store, told apart by the namespace string alone:

* ``"default"``                  → **global**, every project sees it.
* ``"project:<realpath>"``       → **project**, only sessions rooted in that
  directory see it.  The root is the git root of the session's workdir when
  there is one, so ``repo/`` and ``repo/core/`` share one memory.
* ``"agent:<name>"``             → **agent**, a named persistent agent's own
  memory (M7 §6), unchanged by this section.

Nothing here touches SQLite: a namespace is a string, and every derivation is
pure, so :class:`~snowpea_core.memory.store.MemoryEntry` can label itself on
read without asking anyone.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The one global namespace, kept as ``"default"`` so every memory written
#: before scopes existed stays exactly where it was.
GLOBAL_NAMESPACE = "default"
PROJECT_PREFIX = "project:"
AGENT_PREFIX = "agent:"

#: Scope labels, in the order recall renders them.
SCOPES = ("project", "global", "agent")


def _resolved(workdir: Path | str) -> Path | None:
    """``workdir`` as a real path, or ``None`` when it is not a usable directory."""
    try:
        path = Path(workdir).expanduser()
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    try:
        return Path(os.path.realpath(path))
    except OSError:  # pragma: no cover - defensive
        return None


def _snowpea_home() -> Path | None:
    raw = os.environ.get("SNOWPEA_HOME")
    return _resolved(raw) if raw else None


def project_root(workdir: Path | str | None) -> Path | None:
    """The project root for ``workdir``: its git root, else the directory itself.

    ``None`` means "this session is not in a project": the workdir does not
    exist, or it *is* the user's home directory or ``$SNOWPEA_HOME``.  A
    session like that has nowhere sensible to file a project memory, so it
    only ever writes globally.
    """
    if workdir is None:
        return None
    resolved = _resolved(workdir)
    if resolved is None or not resolved.is_dir():
        return None
    home = _resolved(Path.home())
    if home is not None and resolved == home:
        return None
    snowpea_home = _snowpea_home()
    if snowpea_home is not None and resolved == snowpea_home:
        return None
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return resolved


def project_namespace(workdir: Path | str | None) -> str:
    """``"project:<realpath>"`` for ``workdir``, or ``""`` when it has no root."""
    root = project_root(workdir)
    return f"{PROJECT_PREFIX}{root}" if root is not None else ""


def scope_of(namespace: str) -> str:
    """``"project"`` / ``"global"`` / ``"agent"`` for a namespace string."""
    if namespace.startswith(PROJECT_PREFIX):
        return "project"
    if namespace.startswith(AGENT_PREFIX):
        return "agent"
    return "global"


def project_of(namespace: str) -> str:
    """The project root a namespace names, or ``""`` for the other scopes."""
    if namespace.startswith(PROJECT_PREFIX):
        return namespace[len(PROJECT_PREFIX) :]
    return ""


def project_name(namespace_or_path: str) -> str:
    """Short display name for a project: the last path segment.

    Accepts either a ``project:`` namespace or a bare path, so callers do not
    have to know which one they are holding.
    """
    raw = project_of(namespace_or_path) or namespace_or_path
    if not raw:
        return ""
    return Path(raw).name or raw


def label(namespace: str) -> str:
    """``"[project]"`` / ``"[global]"`` / ``"[agent]"`` for a namespace."""
    return f"[{scope_of(namespace)}]"


def namespace_for_scope(scope: str, *, project: str = "", agent: str = "") -> str:
    """The namespace a scope name means, or ``""`` when it cannot be resolved."""
    normalised = (scope or "").strip().lower()
    if normalised == "project":
        return project or ""
    if normalised == "agent":
        return f"{AGENT_PREFIX}{agent}" if agent else ""
    if normalised == "global":
        return GLOBAL_NAMESPACE
    return ""


__all__ = [
    "AGENT_PREFIX",
    "GLOBAL_NAMESPACE",
    "PROJECT_PREFIX",
    "SCOPES",
    "label",
    "namespace_for_scope",
    "project_name",
    "project_namespace",
    "project_of",
    "project_root",
    "scope_of",
]
