"""RPC handler for ``file.complete``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from snowpea_core.agent import file_complete
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    FileCompleteEntry,
    FileCompleteParams,
    FileCompleteResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

HANDLED_METHODS: tuple[str, ...] = ("file.complete",)


def _workdir(core: Core, params: FileCompleteParams) -> Path:
    if params.sessionId:
        session = core.sessions.get(params.sessionId)
        if session is None:
            raise RpcError(errors.NOT_FOUND, f"no such session: {params.sessionId}")
        return Path(session.workdir).resolve()
    if params.workdir:
        return Path(params.workdir).expanduser().resolve()
    raise RpcError(errors.INVALID_PARAMS, "file.complete: sessionId or workdir is required")


async def file_complete_handler(
    _conn: RpcConnection, params: FileCompleteParams, core: Core
) -> FileCompleteResult:
    workdir = _workdir(core, params)
    query = params.query or ""
    if file_complete.refuse_escape(workdir, query):
        raise RpcError(errors.INVALID_PARAMS, "path escapes the working directory")
    limit = params.limit if params.limit is not None else file_complete.DEFAULT_LIMIT
    entries, truncated = file_complete.complete_paths(workdir, query, limit=limit)
    return FileCompleteResult(
        entries=[
            FileCompleteEntry(path=item.path, kind=item.kind, size=item.size)  # type: ignore[arg-type]
            for item in entries
        ],
        truncated=truncated,
    )


def register_file_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("file.complete", file_complete_handler, FileCompleteParams)
    return dispatcher


__all__ = ["HANDLED_METHODS", "file_complete_handler", "register_file_handlers"]
