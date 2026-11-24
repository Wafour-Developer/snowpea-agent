"""``machine.*`` RPC handlers (M40)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from snowpea_core.machines.proxy import refuse_unknown_proxy
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    MachineAddParams,
    MachineAnswerParams,
    MachineConnectParams,
    MachineConnectResult,
    MachineIdParams,
    MachineListResult,
    MachineRow,
    MachineUpdateParams,
    Ok,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "machine.list",
    "machine.add",
    "machine.update",
    "machine.remove",
    "machine.connect",
    "machine.disconnect",
    "machine.answer",
)


def ensure_manager(core: Core):
    manager = getattr(core, "machines", None)
    if manager is None:
        from snowpea_core.machines.manager import MachineManager

        manager = MachineManager(core)
        core.machines = manager
    return manager


async def machine_list_handler(_conn: RpcConnection, _params: Any, core: Core) -> MachineListResult:
    manager = ensure_manager(core)
    return MachineListResult(machines=manager.rows())


async def machine_add_handler(_conn: RpcConnection, params: MachineAddParams, core: Core) -> MachineRow:
    manager = ensure_manager(core)
    rec = manager.registry.add(
        params.target,
        label=params.label,
        key_path=params.keyPath,
        pasted_key=params.pastedKey,
        remote_cwd=params.remoteCwd,
        auto_connect=params.autoConnect,
    )
    await manager.announce()
    return MachineRow.model_validate(rec.to_row())


async def machine_update_handler(
    _conn: RpcConnection, params: MachineUpdateParams, core: Core
) -> MachineRow:
    manager = ensure_manager(core)
    rec = manager.registry.get(params.id)
    if rec is None:
        raise RpcError(errors.NOT_FOUND, "machine not found", {"id": params.id})
    if params.label is not None:
        rec.label = params.label
    if params.remoteCwd is not None:
        rec.remote_cwd = params.remoteCwd
    if params.autoConnect is not None:
        rec.auto_connect = params.autoConnect
    if params.jarvisAllowed is not None:
        rec.jarvis_allowed = params.jarvisAllowed
    manager.registry.save()
    await manager.announce()
    row = next((r for r in manager.rows() if r.id == params.id), None)
    if row is None:
        raise RpcError(errors.INTERNAL, "machine row missing after update")
    return row


async def machine_remove_handler(_conn: RpcConnection, params: MachineIdParams, core: Core) -> Ok:
    manager = ensure_manager(core)
    if manager.registry.remove(params.id) is None:
        raise RpcError(errors.NOT_FOUND, "machine not found", {"id": params.id})
    manager.disconnect(params.id)
    await manager.announce()
    return Ok()


async def machine_connect_handler(
    _conn: RpcConnection, params: MachineConnectParams, core: Core
) -> MachineConnectResult:
    manager = ensure_manager(core)
    if manager.registry.get(params.id) is None:
        raise RpcError(errors.NOT_FOUND, "machine not found", {"id": params.id})
    manager.connect(params.id)
    row = next((r for r in manager.rows() if r.id == params.id), None)
    if row is None:
        raise RpcError(errors.INTERNAL, "machine row missing after connect")
    return MachineConnectResult(status=row.status, ask=row.ask, error=row.error)


async def machine_disconnect_handler(_conn: RpcConnection, params: MachineIdParams, core: Core) -> Ok:
    manager = ensure_manager(core)
    manager.disconnect(params.id)
    live = manager._live.get(params.id)  # noqa: SLF001
    if live:
        live.status = "off"
    await manager.announce()
    return Ok()


async def machine_answer_handler(
    _conn: RpcConnection, params: MachineAnswerParams, core: Core
) -> MachineConnectResult:
    manager = ensure_manager(core)
    if not manager.answer(params.id, params.askId, params.answer):
        raise RpcError(errors.INVALID_PARAMS, "no pending ask", {"askId": params.askId})
    row = next((r for r in manager.rows() if r.id == params.id), None)
    if row is None:
        raise RpcError(errors.NOT_FOUND, "machine not found", {"id": params.id})
    return MachineConnectResult(status=row.status, ask=row.ask, error=row.error)


async def proxy_call(
    conn: RpcConnection,
    method: str,
    machine_id: str,
    params: dict[str, Any],
    core: Core,
) -> dict[str, Any]:
    from pydantic import BaseModel

    from snowpea_core.machines.gates import ensure_machine_proxy_allowed

    refuse_unknown_proxy(method)
    manager = ensure_manager(core)
    rec = manager.registry.get(machine_id)
    if rec is None:
        raise RpcError(errors.NOT_FOUND, "machine not found", {"id": machine_id})

    class _Params(BaseModel):
        model_config = {"extra": "allow"}

    ensure_machine_proxy_allowed(method, conn, _Params.model_validate(params), core, machine_id)

    if method.startswith("terminal.") and method in {"terminal.create", "terminal.write", "terminal.send"}:
        if not rec.jarvis_allowed and getattr(conn, "client_kind", None) == "jarvis":
            raise RpcError(errors.UNAUTHORIZED, "jarvis is off for this machine")
    result = await manager.proxy_call(machine_id, method, params)
    if method == "terminal.attach":
        term_id = str(result.get("terminalId") or params.get("terminalId") or "")
        if term_id:
            manager.register_viewer(machine_id, term_id, conn)
    elif method == "terminal.detach":
        term_id = str(params.get("terminalId") or "")
        if term_id:
            manager.unregister_viewer(machine_id, term_id, conn)
    elif method == "terminal.ack":
        term_id = str(params.get("terminalId") or "")
        if term_id:
            manager.register_viewer(machine_id, term_id, conn)
    return result


def register_machine_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    for name, handler in (
        ("machine.list", machine_list_handler),
        ("machine.add", machine_add_handler),
        ("machine.update", machine_update_handler),
        ("machine.remove", machine_remove_handler),
        ("machine.connect", machine_connect_handler),
        ("machine.disconnect", machine_disconnect_handler),
        ("machine.answer", machine_answer_handler),
    ):
        dispatcher.register(name, handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "ensure_manager", "proxy_call", "register_machine_handlers"]