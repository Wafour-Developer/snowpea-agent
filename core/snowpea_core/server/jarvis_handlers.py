"""``jarvis.*`` handlers of Jarvis mode's service (1.53.0, capability ``jarvis``; M19 WP3).

``jarvis.status|enable|disable|overview|takeover|handback`` and
``jarvis.project.list|rename|alias``.  The delegation methods (``jarvis.ledger``,
``jarvis.delegation.*``) and the briefing belong to other work packages and keep
the dispatcher's ``not_implemented`` stub until they land.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.jarvis.projects import ProjectError
from snowpea_core.jarvis.service import JarvisError, service_of
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    JarvisEnableParams,
    JarvisOverviewParams,
    JarvisOverviewResult,
    JarvisPauseResult,
    JarvisProjectAliasParams,
    JarvisProjectListResult,
    JarvisProjectRenameParams,
    JarvisProjectResult,
    JarvisSessionTarget,
    JarvisStatusResult,
    JarvisTakeoverParams,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "jarvis.status",
    "jarvis.enable",
    "jarvis.disable",
    "jarvis.overview",
    "jarvis.takeover",
    "jarvis.handback",
    "jarvis.project.list",
    "jarvis.project.rename",
    "jarvis.project.alias",
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
    _conn: RpcConnection | None, _params: Empty, core: Core
) -> JarvisStatusResult:
    return await service_of(core).status()


async def jarvis_enable_handler(
    _conn: RpcConnection | None, params: JarvisEnableParams, core: Core
) -> JarvisStatusResult:
    service = service_of(core)
    try:
        await service.enable(model=params.model, folder=params.folder)
    except JarvisError as exc:
        raise _rpc_error(exc) from exc
    return await service.status()


async def jarvis_disable_handler(
    _conn: RpcConnection | None, _params: Empty, core: Core
) -> JarvisStatusResult:
    service = service_of(core)
    try:
        await service.disable()
    except JarvisError as exc:
        raise _rpc_error(exc) from exc
    return await service.status()


async def jarvis_overview_handler(
    _conn: RpcConnection | None, params: JarvisOverviewParams, core: Core
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
    return JarvisOverviewResult(rows=rows, generatedAt=service.now_iso())


async def jarvis_takeover_handler(
    _conn: RpcConnection | None, params: JarvisTakeoverParams, core: Core
) -> JarvisPauseResult:
    try:
        state = await service_of(core).takeover(params.sessionId, params.external, params.minutes)
    except JarvisError as exc:
        raise _rpc_error(exc) from exc
    return JarvisPauseResult(state=state)


async def jarvis_handback_handler(
    _conn: RpcConnection | None, params: JarvisSessionTarget, core: Core
) -> JarvisPauseResult:
    try:
        state = await service_of(core).handback(params.sessionId, params.external)
    except JarvisError as exc:
        raise _rpc_error(exc) from exc
    return JarvisPauseResult(state=state)


async def jarvis_project_list_handler(
    _conn: RpcConnection | None, _params: Empty, core: Core
) -> JarvisProjectListResult:
    return JarvisProjectListResult(projects=await service_of(core).projects.list())


async def jarvis_project_rename_handler(
    _conn: RpcConnection | None, params: JarvisProjectRenameParams, core: Core
) -> JarvisProjectResult:
    try:
        project = await service_of(core).projects.rename(params.root, params.name)
    except ProjectError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return JarvisProjectResult(project=project)


async def jarvis_project_alias_handler(
    _conn: RpcConnection | None, params: JarvisProjectAliasParams, core: Core
) -> JarvisProjectResult:
    try:
        project = await service_of(core).projects.alias(params.root, params.add, params.remove)
    except ProjectError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return JarvisProjectResult(project=project)


def register_jarvis_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("jarvis.status", jarvis_status_handler)
    dispatcher.register("jarvis.enable", jarvis_enable_handler)
    dispatcher.register("jarvis.disable", jarvis_disable_handler)
    dispatcher.register("jarvis.overview", jarvis_overview_handler)
    dispatcher.register("jarvis.takeover", jarvis_takeover_handler)
    dispatcher.register("jarvis.handback", jarvis_handback_handler)
    dispatcher.register("jarvis.project.list", jarvis_project_list_handler)
    dispatcher.register("jarvis.project.rename", jarvis_project_rename_handler)
    dispatcher.register("jarvis.project.alias", jarvis_project_alias_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_jarvis_handlers"]
