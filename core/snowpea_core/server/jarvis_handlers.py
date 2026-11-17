"""``jarvis.*`` handlers of Jarvis mode's service (1.53.0, capability ``jarvis``; M19 WP3).

``jarvis.status|enable|disable|overview``, ``jarvis.project.list|rename|alias``
and ``jarvis.escalations.list|dismiss``.  ``jarvis.takeover`` / ``jarvis.handback``
and the delegation methods are the delegation engine's
(:mod:`snowpea_core.server.jarvis_delegation_handlers`); the briefing belongs to
WP8 and keeps the dispatcher's ``not_implemented`` stub until it lands.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from snowpea_core.jarvis.projects import ProjectError
from snowpea_core.jarvis.service import JarvisError, service_of
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    JarvisEnableParams,
    JarvisEscalationNotification,
    JarvisEscalationsDismissParams,
    JarvisEscalationsDismissResult,
    JarvisEscalationsListParams,
    JarvisEscalationsListResult,
    JarvisOverviewParams,
    JarvisOverviewResult,
    JarvisProjectAliasParams,
    JarvisProjectListResult,
    JarvisProjectRenameParams,
    JarvisProjectResult,
    JarvisSessionSetParams,
    JarvisSessionSetResult,
    JarvisStatusResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "jarvis.status",
    "jarvis.enable",
    "jarvis.disable",
    "jarvis.overview",
    "jarvis.project.list",
    "jarvis.project.rename",
    "jarvis.project.alias",
    "jarvis.escalations.list",
    "jarvis.escalations.dismiss",
    "jarvis.session.set",
)

_CODES = {
    "invalid_params": errors.INVALID_PARAMS,
    "not_found": errors.NOT_FOUND,
    "jarvis_disabled": errors.JARVIS_DISABLED,
    "internal": errors.INTERNAL,
}


def _rpc_error(exc: JarvisError) -> RpcError:
    return RpcError(_CODES.get(exc.code, errors.INTERNAL), str(exc))


async def jarvis_status_handler(
    conn: RpcConnection | None, _params: Empty, core: Core
) -> JarvisStatusResult:
    return await service_of(core).status()


async def jarvis_enable_handler(
    conn: RpcConnection | None, params: JarvisEnableParams, core: Core
) -> JarvisStatusResult:
    service = service_of(core)
    try:
        await service.enable(model=params.model, folder=params.folder)
    except JarvisError as exc:
        raise _rpc_error(exc) from exc
    return await service.status()


async def jarvis_disable_handler(
    conn: RpcConnection | None, _params: Empty, core: Core
) -> JarvisStatusResult:
    service = service_of(core)
    try:
        await service.disable()
    except JarvisError as exc:
        raise _rpc_error(exc) from exc
    return await service.status()


async def jarvis_overview_handler(
    conn: RpcConnection | None, params: JarvisOverviewParams, core: Core
) -> JarvisOverviewResult:
    service = service_of(core)
    try:
        rows = service.overview_rows(
            project=params.project,
            include_idle=params.includeIdle,
            include_external=params.includeExternal,
            limit=params.limit,
        )
    except JarvisError as exc:
        raise _rpc_error(exc) from exc
    return JarvisOverviewResult(rows=rows, generatedAt=service.now_iso(),
                                hiddenCount=await service.hidden_count())


async def jarvis_session_set_handler(
    conn: RpcConnection | None, params: JarvisSessionSetParams, core: Core
) -> JarvisSessionSetResult:
    """``jarvis.session.set`` (1.56.0): one session's Jarvis switch.

    A person's only: the dispatcher's ``PERSON_METHODS`` entry refuses a
    browser, a connector and an in-process caller before this runs, and it is
    checked again here (a local person and a remote web login pass).  Jarvis
    has no tool that reaches it.
    """
    from snowpea_core.server.person_gate import require_person

    require_person(conn, "call jarvis.session.set")
    service = service_of(core)
    if not service.enabled:
        raise RpcError(errors.JARVIS_DISABLED,
                       "Jarvis mode is off; turn it on with jarvis.enable")
    if (params.sessionId is None) == (params.external is None):
        raise RpcError(errors.INVALID_PARAMS, "give exactly one of sessionId and external")
    if params.sessionId is not None:
        session_id = params.sessionId
        live = core.sessions.get(session_id)
        if live is None:
            store = getattr(core, "store", None)
            row = await store.session(session_id) if store is not None else None
            if row is None:
                raise RpcError(errors.NOT_FOUND, f"no such session: {session_id}")
            standing = service.standing_of_row(row)
        else:
            standing = service.standing_of(live)
    else:
        standing = service.standing_of_external(params.external)
    locked = standing[1]
    if locked is not None:
        raise RpcError(errors.INVALID_PARAMS,
                       f"this session's Jarvis switch cannot be changed ({locked})",
                       {"reason": locked})
    try:
        enabled, _ = service.set_session_switch(
            session_id=params.sessionId, external=params.external,
            enabled=params.enabled, by="person",
        )
    except JarvisError as exc:
        raise _rpc_error(exc) from exc
    # The event, the hand-over (off) and the routing (on) run before the answer.
    await service.settle()
    summary = None
    if params.sessionId is not None:
        live = core.sessions.get(params.sessionId)
        if live is not None:
            summary = service.decorate(live.summary())
    return JarvisSessionSetResult(sessionId=params.sessionId, external=params.external,
                                  enabled=enabled, session=summary)


async def jarvis_project_list_handler(
    conn: RpcConnection | None, _params: Empty, core: Core
) -> JarvisProjectListResult:
    return JarvisProjectListResult(projects=await service_of(core).projects.list())


async def jarvis_project_rename_handler(
    conn: RpcConnection | None, params: JarvisProjectRenameParams, core: Core
) -> JarvisProjectResult:
    try:
        project = await service_of(core).projects.rename(params.root, params.name)
    except ProjectError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return JarvisProjectResult(project=project)


async def jarvis_project_alias_handler(
    conn: RpcConnection | None, params: JarvisProjectAliasParams, core: Core
) -> JarvisProjectResult:
    try:
        project = await service_of(core).projects.alias(params.root, params.add, params.remove)
    except ProjectError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return JarvisProjectResult(project=project)


async def jarvis_escalations_list_handler(
    conn: RpcConnection | None, params: JarvisEscalationsListParams, core: Core
) -> JarvisEscalationsListResult:
    try:
        items, next_before = service_of(core).escalations.page(
            before=params.before, limit=params.limit
        )
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    escalations = []
    for item in items:
        try:
            escalations.append(JarvisEscalationNotification.model_validate(item))
        except Exception:  # noqa: BLE001 - one unreadable entry never hides the rest
            continue
    return JarvisEscalationsListResult(escalations=escalations, nextBefore=next_before)


async def jarvis_escalations_dismiss_handler(
    conn: RpcConnection | None, params: JarvisEscalationsDismissParams, core: Core
) -> JarvisEscalationsDismissResult:
    if not params.all and not params.ids:
        raise RpcError(errors.INVALID_PARAMS, "give ids or all: true")
    dismissed = service_of(core).escalations.dismiss(params.ids, everything=params.all)
    if dismissed:
        with contextlib.suppress(Exception):
            await core.hub.notify("jarvis.escalation.dismissed", {"ids": dismissed})
    return JarvisEscalationsDismissResult(dismissed=dismissed)


def register_jarvis_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("jarvis.status", jarvis_status_handler)
    dispatcher.register("jarvis.enable", jarvis_enable_handler)
    dispatcher.register("jarvis.disable", jarvis_disable_handler)
    dispatcher.register("jarvis.overview", jarvis_overview_handler)
    dispatcher.register("jarvis.project.list", jarvis_project_list_handler)
    dispatcher.register("jarvis.project.rename", jarvis_project_rename_handler)
    dispatcher.register("jarvis.project.alias", jarvis_project_alias_handler)
    dispatcher.register("jarvis.escalations.list", jarvis_escalations_list_handler)
    dispatcher.register("jarvis.escalations.dismiss", jarvis_escalations_dismiss_handler)
    dispatcher.register("jarvis.session.set", jarvis_session_set_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_jarvis_handlers"]
