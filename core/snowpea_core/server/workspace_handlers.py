"""``workspace.*``, ``session.pin`` and ``session.archive`` (1.47.0; M17 2d)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    Ok,
    SessionFlagParams,
    WorkspaceAddParams,
    WorkspaceIdParams,
    WorkspaceInfo,
    WorkspaceListResult,
    WorkspaceRenameParams,
    WorkspaceReorderParams,
    WorkspaceResult,
)
from snowpea_core.session import workspaces as ws

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "workspace.list",
    "workspace.add",
    "workspace.rename",
    "workspace.remove",
    "workspace.reorder",
    "session.pin",
    "session.archive",
)


def _store(core: Core):  # type: ignore[no-untyped-def]
    store = getattr(core, "store", None)
    if store is None:
        raise RpcError(errors.INTERNAL, "no session store")
    return store


async def _announce(core: Core, reason: str, session_id: str = "") -> None:
    sessions = getattr(core, "sessions", None)
    if sessions is not None and hasattr(sessions, "announce_sessions_changed"):
        await sessions.announce_sessions_changed(reason, session_id)


async def _listing(core: Core) -> WorkspaceListResult:
    return WorkspaceListResult(
        workspaces=[WorkspaceInfo(**item) for item in await ws.list_all(_store(core))]
    )


async def workspace_list_handler(
    _conn: RpcConnection | None, _params: Empty, core: Core
) -> WorkspaceListResult:
    return await _listing(core)


async def workspace_add_handler(
    _conn: RpcConnection | None, params: WorkspaceAddParams, core: Core
) -> WorkspaceResult:
    if not params.path.strip():
        raise RpcError(errors.INVALID_PARAMS, "workspace.add needs a path")
    item = await ws.add(_store(core), params.path, params.title)
    await _announce(core, "workspaces")
    counts = await ws.session_counts(_store(core))
    return WorkspaceResult(workspace=WorkspaceInfo(**item, sessions=counts.get(item["path"], 0)))


async def workspace_rename_handler(
    _conn: RpcConnection | None, params: WorkspaceRenameParams, core: Core
) -> WorkspaceResult:
    try:
        item = await ws.rename(_store(core), params.id, params.title)
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    if item is None:
        raise RpcError(errors.NOT_FOUND, f"no workspace {params.id}")
    await _announce(core, "workspaces")
    counts = await ws.session_counts(_store(core))
    return WorkspaceResult(workspace=WorkspaceInfo(**item, sessions=counts.get(item["path"], 0)))


async def workspace_remove_handler(
    _conn: RpcConnection | None, params: WorkspaceIdParams, core: Core
) -> Ok:
    if not await ws.remove(_store(core), params.id):
        raise RpcError(errors.NOT_FOUND, f"no workspace {params.id}")
    await _announce(core, "workspaces")
    return Ok(ok=True)


async def workspace_reorder_handler(
    _conn: RpcConnection | None, params: WorkspaceReorderParams, core: Core
) -> WorkspaceListResult:
    await ws.reorder(_store(core), list(params.ids))
    await _announce(core, "workspaces")
    return await _listing(core)


async def _flag(core: Core, params: SessionFlagParams, flag: str) -> Ok:
    store = _store(core)
    live = core.sessions.get(params.sessionId)
    if live is None and await store.session(params.sessionId) is None:
        raise RpcError(errors.NOT_FOUND, f"no such session: {params.sessionId}")
    if flag == "archived" and params.value and live is not None:
        task = getattr(live, "turn_task", None)
        if task is not None and not task.done():
            if not params.stop:
                raise RpcError(
                    errors.INVALID_PARAMS,
                    "the conversation is running; pass stop to stop it and archive",
                )
            live.interrupt.set()
    await ws.set_flag(store, params.sessionId, flag, params.value)
    await _announce(core, flag, params.sessionId)
    return Ok(ok=True)


async def session_pin_handler(
    _conn: RpcConnection | None, params: SessionFlagParams, core: Core
) -> Ok:
    return await _flag(core, params, "pinned")


async def session_archive_handler(
    _conn: RpcConnection | None, params: SessionFlagParams, core: Core
) -> Ok:
    return await _flag(core, params, "archived")


async def decorate(core: Core, rows: list) -> list:  # type: ignore[type-arg]
    """``session.list`` rows with ``workspaceId``, ``pinned`` and ``archived`` (1.47.0)."""
    store = getattr(core, "store", None)
    if store is None or not rows:
        return rows
    try:
        by_path = {item["path"]: item["id"] for item in await ws.list_all(store)}
        flags = await ws.session_flags(store)
    except Exception:  # noqa: BLE001 - the list must not fail for decoration
        return rows
    out = []
    for row in rows:
        flag = flags.get(row.sessionId, {})
        out.append(
            row.model_copy(
                update={
                    "workspaceId": by_path.get(ws.canonical(row.workdir)),
                    "pinned": bool(flag.get("pinned")),
                    "archived": bool(flag.get("archived")),
                }
            )
        )
    return out


def register_workspace_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("workspace.list", workspace_list_handler)
    dispatcher.register("workspace.add", workspace_add_handler)
    dispatcher.register("workspace.rename", workspace_rename_handler)
    dispatcher.register("workspace.remove", workspace_remove_handler)
    dispatcher.register("workspace.reorder", workspace_reorder_handler)
    dispatcher.register("session.pin", session_pin_handler)
    dispatcher.register("session.archive", session_archive_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "decorate", "register_workspace_handlers"]
