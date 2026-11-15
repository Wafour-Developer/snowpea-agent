"""``jarvis.delegation.*``, ``jarvis.takeover`` / ``jarvis.handback`` and ``jarvis.ledger``.

M19 WP2 (1.53.0, capability ``jarvisDelegation``).  Thin RPC wrappers over
:mod:`snowpea_core.jarvis.delegation`: the engine decides who may change
what, these only translate its refusals into wire errors —
``jarvis_person_only`` when the calling connection is not the person (a
browser host, the remote connector, a remote web login handing over guarded
classes), ``invalid_params`` for a malformed grant.

``jarvis.delegation.changed`` and ``jarvis.decision`` are sent by the engine's
listeners (:func:`~snowpea_core.jarvis.delegation.engine_for`), so a change
made anywhere — here, by the gateway, by a session ending — is announced the
same way.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.jarvis.delegation import (
    Caller,
    DelegationError,
    PersonOnly,
    engine_for,
)
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    JarvisDelegationListParams,
    JarvisDelegationListResult,
    JarvisDelegationRevokeParams,
    JarvisDelegationRevokeResult,
    JarvisDelegationSetParams,
    JarvisDelegationSetResult,
    JarvisLedgerParams,
    JarvisLedgerResult,
    JarvisPauseResult,
    JarvisSessionTarget,
    JarvisTakeoverParams,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "jarvis.delegation.list",
    "jarvis.delegation.set",
    "jarvis.delegation.revoke",
    "jarvis.takeover",
    "jarvis.handback",
    "jarvis.ledger",
)


def _refused(exc: Exception) -> RpcError:
    if isinstance(exc, PersonOnly):
        return RpcError(errors.JARVIS_PERSON_ONLY, str(exc))
    return RpcError(errors.INVALID_PARAMS, str(exc))


async def delegation_list_handler(
    _conn: RpcConnection | None, params: JarvisDelegationListParams, core: Core
) -> JarvisDelegationListResult:
    engine = engine_for(core)
    try:
        grants = engine.list_grants(params.scope, params.target, params.includeExpired)
    except DelegationError as exc:
        raise _refused(exc) from exc
    return JarvisDelegationListResult(grants=grants, default=engine.default_level)


async def delegation_set_handler(
    conn: RpcConnection | None, params: JarvisDelegationSetParams, core: Core
) -> JarvisDelegationSetResult:
    engine = engine_for(core)
    try:
        grant, replaced = engine.set_grant(
            params.scope,
            params.target,
            params.level,
            params.allow,
            caller=Caller.from_connection(conn),
            expires_at=params.expiresAt,
            note=params.note,
            project=_session_project(core, params.target) if params.scope == "session" else None,
        )
    except (PersonOnly, DelegationError) as exc:
        raise _refused(exc) from exc
    return JarvisDelegationSetResult(grant=grant, replaced=replaced)


async def delegation_revoke_handler(
    conn: RpcConnection | None, params: JarvisDelegationRevokeParams, core: Core
) -> JarvisDelegationRevokeResult:
    engine = engine_for(core)
    if engine.get_grant(params.id) is None:
        raise RpcError(errors.NOT_FOUND, f"no grant {params.id}")
    try:
        revoked = engine.revoke(params.id, caller=Caller.from_connection(conn))
    except PersonOnly as exc:
        raise _refused(exc) from exc
    return JarvisDelegationRevokeResult(revoked=revoked)


def _session_project(core: Core, session_id: str | None) -> str | None:
    """The workdir of a live snowpea session, used as its project root."""
    if not session_id:
        return None
    session = core.sessions.get(session_id)
    workdir = getattr(session, "workdir", None)
    return str(workdir) if workdir else None


async def _target(core: Core, params: JarvisSessionTarget) -> tuple[str | None, Any, str | None]:
    """``(sessionId, external, project)``; exactly one of the first two."""
    if (params.sessionId is None) == (params.external is None):
        raise RpcError(errors.INVALID_PARAMS, "give exactly one of sessionId and external")
    if params.external is not None:
        return None, params.external, None
    session_id = str(params.sessionId)
    project = _session_project(core, session_id)
    if project is None:
        store = getattr(core, "store", None)
        row = await store.session(session_id) if store is not None else None
        if row is None:
            raise RpcError(errors.NOT_FOUND, f"no such session: {session_id}")
        workdir = row.get("workdir")
        project = str(workdir) if workdir else None
    return session_id, None, project


def _exposed(project: str | None) -> bool:
    if not project:
        return True
    try:
        from snowpea_core.config.project import ProjectSettings

        return bool(ProjectSettings.load(Path(project)).jarvis.expose)
    except Exception:  # noqa: BLE001 - an unreadable project file keeps the default
        return True


async def takeover_handler(
    conn: RpcConnection | None, params: JarvisTakeoverParams, core: Core
) -> JarvisPauseResult:
    engine = engine_for(core)
    session_id, external, project = await _target(core, params)
    try:
        engine.takeover(
            session_id,
            external=external,
            minutes=params.minutes,
            project=project,
            caller=Caller.from_connection(conn),
        )
    except DelegationError as exc:
        raise _refused(exc) from exc
    state = engine.session_state(
        session_id, external=external, project=project, exposed=_exposed(project)
    )
    return JarvisPauseResult(state=state)


async def handback_handler(
    conn: RpcConnection | None, params: JarvisSessionTarget, core: Core
) -> JarvisPauseResult:
    engine = engine_for(core)
    session_id, external, project = await _target(core, params)
    try:
        engine.handback(session_id, external=external, caller=Caller.from_connection(conn))
    except (PersonOnly, DelegationError) as exc:
        raise _refused(exc) from exc
    state = engine.session_state(
        session_id, external=external, project=project, exposed=_exposed(project)
    )
    return JarvisPauseResult(state=state)


async def ledger_handler(
    _conn: RpcConnection | None, params: JarvisLedgerParams, core: Core
) -> JarvisLedgerResult:
    engine = engine_for(core)
    try:
        entries, next_before = engine.ledger.decisions(
            session_id=params.sessionId,
            decided_by=params.decidedBy,
            before=params.before,
            limit=params.limit,
        )
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return JarvisLedgerResult(entries=entries, nextBefore=next_before)


def register_jarvis_delegation_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register the six WP2 methods."""
    dispatcher.register("jarvis.delegation.list", delegation_list_handler)
    dispatcher.register("jarvis.delegation.set", delegation_set_handler)
    dispatcher.register("jarvis.delegation.revoke", delegation_revoke_handler)
    dispatcher.register("jarvis.takeover", takeover_handler)
    dispatcher.register("jarvis.handback", handback_handler)
    dispatcher.register("jarvis.ledger", ledger_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_jarvis_delegation_handlers"]
