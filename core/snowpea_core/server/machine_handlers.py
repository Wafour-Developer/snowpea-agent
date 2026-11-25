"""``machine.*`` RPC handlers (M40)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from snowpea_core.machines.gates import (
    ensure_machine_proxy_async,
    ensure_remote_session_prompt_cap,
    filter_proxy_result,
    proxy_params_dump,
    run_proxy_gates,
)
from snowpea_core.machines.proxy import refuse_unknown_proxy
from snowpea_core.machines.registry import validate_key_path
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    METHODS,
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
    try:
        loop = __import__("asyncio").get_running_loop()
        if manager._loop is None:  # noqa: SLF001
            manager.bind_loop(loop)
    except RuntimeError:
        pass
    return manager


def _hook_conn_close(manager: Any, conn: RpcConnection) -> None:
    if getattr(conn, "_machine_close_hooked", False):
        return
    conn.on_close.append(lambda c: manager.close_connection(c))
    conn._machine_close_hooked = True  # type: ignore[attr-defined]


async def machine_list_handler(_conn: RpcConnection, _params: Any, core: Core) -> MachineListResult:
    manager = ensure_manager(core)
    return MachineListResult(machines=manager.rows())


async def machine_add_handler(
    _conn: RpcConnection, params: MachineAddParams, core: Core
) -> MachineRow:
    manager = ensure_manager(core)
    if params.keyPath:
        validate_key_path(params.keyPath)
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
    if params.sessionView is not None:
        rec.session_view = params.sessionView
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
    manager._cancel_asks(params.id)  # noqa: SLF001
    manager.disconnect(params.id)
    await manager.announce()
    return Ok()


async def machine_connect_handler(
    _conn: RpcConnection, params: MachineConnectParams, core: Core
) -> MachineConnectResult:
    manager = ensure_manager(core)
    rec = manager.registry.get(params.id)
    if rec is None:
        raise RpcError(errors.NOT_FOUND, "machine not found", {"id": params.id})
    if rec.key_path:
        validate_key_path(rec.key_path)
    manager.connect(params.id)
    row = next((r for r in manager.rows() if r.id == params.id), None)
    if row is None:
        raise RpcError(errors.INTERNAL, "machine row missing after connect")
    return MachineConnectResult(status=row.status, ask=row.ask, error=row.error)


async def machine_disconnect_handler(
    _conn: RpcConnection, params: MachineIdParams, core: Core
) -> Ok:
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
    refuse_unknown_proxy(method)
    manager = ensure_manager(core)
    rec = manager.registry.get(machine_id)
    if rec is None:
        raise RpcError(errors.NOT_FOUND, "machine not found", {"id": machine_id})

    spec = METHODS.get(method)
    if spec is None:
        raise RpcError(errors.METHOD_NOT_FOUND_CODE, f"unknown method: {method}")
    validated = spec.params.model_validate(params)
    run_proxy_gates(method, conn, validated, core, machine_id)
    _hook_conn_close(manager, conn)

    terminal_id = getattr(validated, "terminalId", None)
    client = await manager._ensure_client(machine_id, conn)  # noqa: SLF001
    await ensure_machine_proxy_async(
        method,
        conn,
        validated,
        core,
        machine_id,
        client=client,
        terminal_id=str(terminal_id) if terminal_id else None,
    )
    if method in ("session.prompt", "session.steer"):
        session_id = str(getattr(validated, "sessionId", "") or "")
        if session_id:
            await ensure_remote_session_prompt_cap(
                conn, core, machine_id, session_id, client=client
            )
    clean = proxy_params_dump(conn, core, validated, method=method)
    result = await manager.proxy_call(machine_id, method, clean, conn)
    result = await filter_proxy_result(conn, core, method, result)
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
    elif method in ("session.resume", "session.create"):
        session_id = str(
            result.get("sessionId")
            or (result.get("session") or {}).get("sessionId")
            or params.get("sessionId")
            or ""
        )
        if session_id:
            manager.register_session_viewer(machine_id, session_id, conn)
    elif method == "session.close":
        session_id = str(params.get("sessionId") or "")
        if session_id:
            manager.unregister_session_viewer(machine_id, session_id, conn)
    # The person writing into a remote terminal or session themselves pauses Jarvis there.
    # (Jarvis's own calls take jarvis_proxy_call, never this path, so a proxied prompt
    # or steer here is the person's.)
    if method == "terminal.write" and bool(getattr(validated, "human", False)):
        await _note_person_typed(conn, core, machine_id, str(getattr(validated, "terminalId", "")))
    elif method in ("terminal.send", "terminal.keys"):
        await _note_person_typed(conn, core, machine_id, str(getattr(validated, "terminalId", "")))
    elif method in ("session.prompt", "session.steer"):
        await _note_person_typed(conn, core, machine_id, str(getattr(validated, "sessionId", "")))
    elif method == "external.send":
        from snowpea_core.machines.jarvis_remote import external_local_key

        ref = getattr(validated, "ref", None)
        cli = getattr(ref, "cli", None) if ref is not None else None
        ext_id = getattr(ref, "id", None) if ref is not None else None
        if cli and ext_id:
            await _note_person_typed(
                conn, core, machine_id, external_local_key(str(cli), str(ext_id))
            )
    return result


async def _note_person_typed(
    conn: RpcConnection, core: Core, machine_id: str, local_id: str
) -> None:
    jarvis = getattr(core, "jarvis", None)
    from snowpea_core.server.person_gate import connection_kind, is_person

    if jarvis is None or not jarvis.running or not local_id:
        return
    if connection_kind(conn) != "person":
        return
    if not (is_person(conn, local=True) or getattr(conn, "remote_login", False)):
        return
    from snowpea_core.machines.jarvis_remote import delegation_key

    with contextlib.suppress(Exception):
        await jarvis.person_typed(
            delegation_key(machine_id, local_id),
            None,
            surface=getattr(conn, "surface_id", None),
        )


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