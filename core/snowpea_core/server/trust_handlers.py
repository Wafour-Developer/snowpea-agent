"""``project.trust.*`` (1.55.0, capability ``projectTrust``; security review 5, C1).

``project.trust.status`` tells a person what a project folder would make
snowpea do and what of it is active; ``project.trust.set`` / ``.revoke`` are a
LOCAL person's (``person_gate.PERSON_METHODS``: never a remote web login, a
browser host or the connector).  Every change is ``project.trust.changed`` to
the people's surfaces.  The store is :mod:`snowpea_core.trust.projects`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    ProjectTrustParams,
    ProjectTrustRevokeParams,
    ProjectTrustSetParams,
    ProjectTrustStatus,
)
from snowpea_core.trust.projects import TrustStore, set_default_home, store_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

log = logging.getLogger("snowpea.trust")

#: The session event code of the once-per-session notice (a surface may localise it).
UNTRUSTED_CODE = "project_untrusted"

#: Sessions already told (so a resume does not repeat it).
_TOLD: set[str] = set()
#: The projects the one-time migration trusted, told with the first notice.
_MIGRATED: list[str] = []


def store_of(core: Any) -> TrustStore:
    return store_for(core.paths.home)


def wire_trust(core: Any) -> None:
    """Point the readers at this daemon's store and run the one-time migration (d).

    Projects that already have sessions in the store are trusted for what
    they bring now (their hooks already ran before this version); a new project
    starts untrusted.  Logged once, and told with the first session's notice.
    """
    set_default_home(core.paths.home)
    # Round-4 M3, re-checked in review 5: no PATH entry inside any known project.
    from snowpea_core.exec.local import set_project_roots
    from snowpea_core.security.enforce import live_projects

    set_project_roots(lambda: live_projects(core))
    store = store_of(core)
    if store.migrated():
        return
    roots: list[str] = []
    session_workdirs = getattr(getattr(core, "store", None), "session_workdirs", None)
    if session_workdirs is not None:
        try:
            roots = list(session_workdirs(limit=1000))
        except Exception:  # noqa: BLE001 - no history: nothing to migrate
            log.debug("could not list session workdirs for the trust migration", exc_info=True)
    trusted = store.migrate(roots)
    if trusted:
        _MIGRATED[:] = trusted
        log.warning(
            "project trust: %d project(s) that already had sessions were trusted as they are "
            "now (their hooks, MCP servers, commands, agents and settings stay active): %s",
            len(trusted), ", ".join(trusted),
        )


async def _changed(core: Any, workdir: str, status: dict[str, Any], *, reason: str,
                   by: str | None) -> None:
    hub = getattr(core, "hub", None)
    if hub is None:
        return
    try:
        await hub.notify("project.trust.changed", {
            "workdir": workdir, "scopes": status.get("scopes") or [], "by": by,
            "reason": reason, "projects": [],
        })
    except Exception:  # noqa: BLE001 - the change stands
        log.debug("could not broadcast project.trust.changed", exc_info=True)


def _root(workdir: str) -> Path:
    text = str(workdir or "").strip()
    if not text or not os.path.isabs(os.path.expanduser(text)):
        raise RpcError(errors.INVALID_PARAMS, "workdir must be an absolute path")
    path = Path(os.path.realpath(os.path.expanduser(text)))
    if not path.is_dir():
        raise RpcError(errors.INVALID_PARAMS, f"no such folder: {text}")
    return path


async def _after_change(core: Any, root: Path) -> None:
    """What the change switches on or off: hooks, commands, agents, MCP servers."""
    skills = getattr(core, "skills", None)
    if skills is not None and hasattr(skills, "reload"):
        try:
            await skills.reload()
        except Exception:  # noqa: BLE001 - the trust record stands
            log.warning("could not reload skills after a trust change", exc_info=True)
    try:
        from snowpea_core.tools import mcp_client

        await mcp_client.sync_tools_bounded(core, root)
    except Exception:  # noqa: BLE001 - the trust record stands
        log.debug("could not resync MCP servers after a trust change", exc_info=True)


async def project_trust_status_handler(conn: RpcConnection, params: ProjectTrustParams,
                                       core: Core) -> ProjectTrustStatus:
    del conn
    root = _root(params.workdir)
    return ProjectTrustStatus.model_validate(store_of(core).status(root))


async def project_trust_set_handler(conn: RpcConnection, params: ProjectTrustSetParams,
                                    core: Core) -> ProjectTrustStatus:
    from snowpea_core.server.person_gate import require_person

    require_person(conn, "trust a project", local=True)
    root = _root(params.workdir)
    by = str(getattr(conn, "client_kind", None) or "local")
    try:
        store_of(core).set(root, params.scopes, by=by)
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    await _after_change(core, root)
    status = store_of(core).status(root)
    await _changed(core, str(root), status, reason="set", by=by)
    return ProjectTrustStatus.model_validate(status)


async def project_trust_revoke_handler(conn: RpcConnection, params: ProjectTrustRevokeParams,
                                       core: Core) -> ProjectTrustStatus:
    from snowpea_core.server.person_gate import require_person

    require_person(conn, "take back a project's trust", local=True)
    root = _root(params.workdir)
    by = str(getattr(conn, "client_kind", None) or "local")
    store_of(core).revoke(root, params.scopes, by=by)
    await _after_change(core, root)
    status = store_of(core).status(root)
    await _changed(core, str(root), status, reason="revoke", by=by)
    return ProjectTrustStatus.model_validate(status)


async def session_notice(core: Any, session_id: str, workdir: Any) -> None:
    """Once per session: what the project brings that is not active, as a session event."""
    if not session_id or session_id in _TOLD:
        return
    _TOLD.add(session_id)
    try:
        line = store_of(core).notice(workdir)
        if _MIGRATED:
            moved = ", ".join(_MIGRATED[:5]) + (" …" if len(_MIGRATED) > 5 else "")
            first = ("snowpea now asks before a project's own hooks, MCP servers, commands, "
                     "agents and settings take effect; the projects you already worked in "
                     f"were trusted as they are: {moved}.")
            _MIGRATED.clear()
            line = f"{first} {line}" if line else first
        if not line:
            return
        from snowpea_core.session import events

        await core.hub.emit_event(session_id, events.error(UNTRUSTED_CODE, line))
    except Exception:  # noqa: BLE001 - a notice is best effort
        log.debug("could not tell the session about project trust", exc_info=True)


def register_trust_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("project.trust.status", project_trust_status_handler)
    dispatcher.register("project.trust.set", project_trust_set_handler)
    dispatcher.register("project.trust.revoke", project_trust_revoke_handler)
    return dispatcher


__all__ = [
    "UNTRUSTED_CODE",
    "project_trust_revoke_handler",
    "project_trust_set_handler",
    "project_trust_status_handler",
    "register_trust_handlers",
    "session_notice",
    "wire_trust",
]
