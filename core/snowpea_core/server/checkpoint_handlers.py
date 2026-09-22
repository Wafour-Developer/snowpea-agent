"""RPC handlers for ``checkpoint.*``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    CheckpointDeleteParams,
    CheckpointDiffParams,
    CheckpointDiffResult,
    CheckpointInfo,
    CheckpointListParams,
    CheckpointListResult,
    CheckpointRestoreParams,
    CheckpointRestoreResult,
    Ok,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.session import events

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

HANDLED_METHODS: tuple[str, ...] = (
    "checkpoint.list",
    "checkpoint.diff",
    "checkpoint.restore",
    "checkpoint.delete",
)


def _validate_id(value: str, label: str) -> None:
    from snowpea_core.session.checkpoints import _safe_id

    try:
        _safe_id(value)
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, f"invalid {label}") from exc


def _checkpoints(core: Core) -> Any:
    store = getattr(core, "checkpoints", None)
    if store is None:
        raise RpcError(errors.INTERNAL, "checkpoint store is not configured")
    return store


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def _call(store: Any, *names: str, **kwargs: Any) -> Any:
    for name in names:
        method = getattr(store, name, None)
        if method is None:
            continue
        return await _maybe_await(method(**kwargs))
    joined = ", ".join(names)
    raise RpcError(errors.INTERNAL, f"checkpoint store does not implement {joined}")


async def _session(core: Core, session_id: str) -> Any:
    session = core.sessions.get(session_id)
    if session is None:
        session = await core.sessions.restore(session_id)
    if session is None:
        raise RpcError(errors.NOT_FOUND, f"no such session: {session_id}")
    return session


def _ensure_idle(core: Core, session_id: str) -> None:
    session = core.sessions.get(session_id)
    if session is not None and session.current_turn is not None:
        raise RpcError(errors.SESSION_BUSY, f"session {session_id} has a running turn")


async def checkpoint_list_handler(
    _conn: RpcConnection, params: CheckpointListParams, core: Core
) -> CheckpointListResult:
    _validate_id(params.sessionId, "sessionId")
    result = await _call(
        _checkpoints(core), "list", "list_checkpoints", session_id=params.sessionId
    )
    if isinstance(result, CheckpointListResult):
        return result
    checkpoints = result.get("checkpoints", result) if isinstance(result, dict) else result
    return CheckpointListResult(
        checkpoints=[CheckpointInfo.model_validate(_dump(item)) for item in checkpoints or []]
    )


async def checkpoint_diff_handler(
    _conn: RpcConnection, params: CheckpointDiffParams, core: Core
) -> CheckpointDiffResult:
    _validate_id(params.sessionId, "sessionId")
    _validate_id(params.id, "checkpoint id")
    session = await _session(core, params.sessionId)
    try:
        result = await _call(
            _checkpoints(core),
            "diff",
            "diff_checkpoint",
            session=session,
            checkpoint_id=params.id,
            paths=params.paths,
            through=params.through,
        )
    except KeyError as exc:
        raise RpcError(errors.NOT_FOUND, f"no such checkpoint: {params.id}") from exc
    if isinstance(result, CheckpointDiffResult):
        return result
    return CheckpointDiffResult.model_validate(_dump(result))


async def checkpoint_restore_handler(
    _conn: RpcConnection, params: CheckpointRestoreParams, core: Core
) -> CheckpointRestoreResult:
    _validate_id(params.sessionId, "sessionId")
    _validate_id(params.id, "checkpoint id")
    _ensure_idle(core, params.sessionId)
    session = await _session(core, params.sessionId)
    try:
        result = await _call(
            _checkpoints(core),
            "restore",
            "restore_checkpoint",
            session=session,
            checkpoint_id=params.id,
            paths=params.paths,
            through=params.through,
            force=params.force,
            dry_run=params.dryRun,
        )
    except KeyError as exc:
        raise RpcError(errors.NOT_FOUND, f"no such checkpoint: {params.id}") from exc
    if isinstance(result, CheckpointRestoreResult):
        restored = result
    else:
        restored = CheckpointRestoreResult.model_validate(_dump(result))
    if not params.dryRun:
        await core.hub.emit_event(
            session.id,
            events.checkpoint_restored(restored.checkpointId, restored.restored, restored.skipped),
        )
    return restored


async def checkpoint_delete_handler(
    _conn: RpcConnection, params: CheckpointDeleteParams, core: Core
) -> Ok:
    _validate_id(params.sessionId, "sessionId")
    if params.id is not None:
        _validate_id(params.id, "checkpoint id")
    await _call(
        _checkpoints(core),
        "delete",
        "delete_checkpoint",
        session_id=params.sessionId,
        checkpoint_id=params.id,
    )
    return Ok(ok=True)


def register_checkpoint_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("checkpoint.list", checkpoint_list_handler)
    dispatcher.register("checkpoint.diff", checkpoint_diff_handler)
    dispatcher.register("checkpoint.restore", checkpoint_restore_handler)
    dispatcher.register("checkpoint.delete", checkpoint_delete_handler)
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "checkpoint_delete_handler",
    "checkpoint_diff_handler",
    "checkpoint_list_handler",
    "checkpoint_restore_handler",
    "register_checkpoint_handlers",
]
