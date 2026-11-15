"""Who may read and write which memory namespace (M19 WP4).

Every session reads and writes as :mod:`snowpea_core.memory.tools` always did,
except the Jarvis session (and anything below it): it **reads** global memory,
its own ``agent:jarvis`` namespace and the project memory of every project
exposed to it (``jarvis.expose``), and it **writes only** to ``agent:jarvis``.
A write that names another scope is refused, so nothing Jarvis read can make
it file a note into a project's or the global memory.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.memory.scopes import AGENT_PREFIX, GLOBAL_NAMESPACE, project_namespace

#: Jarvis's own memory namespace (the reserved named agent ``jarvis``).
JARVIS_NAMESPACE = f"{AGENT_PREFIX}jarvis"

#: What ``save_memory`` answers when Jarvis names another scope.
WRITE_REFUSAL = (
    "Jarvis keeps notes only in its own memory (agent:jarvis); project and global "
    "memory are written by the person or their worker sessions. Omit scope to save it there."
)


def jarvis_service(core: Any, session: Any) -> Any:
    """The Jarvis service when ``session`` is Jarvis's (or below it), else ``None``."""
    service = getattr(core, "jarvis", None) if core is not None else None
    if service is None:
        return None
    try:
        return service if service.in_jarvis_family(session) else None
    except Exception:  # noqa: BLE001 - a half-built core is not Jarvis
        return None


async def read_namespaces(core: Any, session: Any) -> list[str] | None:
    """The namespaces Jarvis reads, or ``None`` for any other session (its usual set)."""
    service = jarvis_service(core, session)
    if service is None:
        return None
    found = [GLOBAL_NAMESPACE, JARVIS_NAMESPACE]
    for root in await service.projects.roots():
        if not service.projects.exposed(root):
            continue
        namespace = project_namespace(root)
        if namespace and namespace not in found:
            found.append(namespace)
    return found


def write_namespace(core: Any, session: Any, scope: str | None) -> tuple[str | None, str]:
    """``(namespace, refusal)`` for a Jarvis write; ``(None, "")`` for any other session.

    Jarvis's writes go to ``agent:jarvis``; asking for ``project`` or
    ``global`` is refused with :data:`WRITE_REFUSAL`.
    """
    if jarvis_service(core, session) is None:
        return None, ""
    if scope not in (None, "", "agent", "jarvis"):
        return None, WRITE_REFUSAL
    return JARVIS_NAMESPACE, ""


__all__ = [
    "JARVIS_NAMESPACE",
    "WRITE_REFUSAL",
    "jarvis_service",
    "read_namespaces",
    "write_namespace",
]
