"""``session.trajectory`` (1.45.0, capability ``trajectory``; M17 2b)."""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    SessionTrajectoryParams,
    SessionTrajectoryResult,
    TrajectoryRow,
)
from snowpea_core.session import trajectory

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = ("session.trajectory",)

#: Builders kept for the sessions read most recently; each folds only what was
#: appended since its last read.
CACHE_SIZE = 32
_BUILDERS: OrderedDict[str, trajectory.TrajectoryBuilder] = OrderedDict()


def _builder(session_id: str) -> trajectory.TrajectoryBuilder:
    builder = _BUILDERS.pop(session_id, None) or trajectory.TrajectoryBuilder()
    _BUILDERS[session_id] = builder
    while len(_BUILDERS) > CACHE_SIZE:
        _BUILDERS.popitem(last=False)
    return builder


async def session_trajectory_handler(
    _conn: RpcConnection | None, params: SessionTrajectoryParams, core: Core
) -> SessionTrajectoryResult:
    store = getattr(core, "store", None)
    if store is None:
        raise RpcError(errors.INTERNAL, "no session store")
    known = core.sessions.get(params.sessionId) is not None
    if not known and await store.session(params.sessionId) is None:
        raise RpcError(errors.NOT_FOUND, f"no such session: {params.sessionId}")
    builder = _builder(params.sessionId)
    builder.apply_all(
        await store.events_of_kinds(params.sessionId, trajectory.KINDS, builder.last_seq)
    )
    after = max(0, int(params.afterSeq or 0))
    rows = [
        row
        for row in builder.rows
        if row.seq > after or (row.endSeq is not None and row.endSeq > after)
    ]
    limit = max(1, int(params.limit or 2000))
    more = len(rows) > limit
    rows = rows[-limit:]
    return SessionTrajectoryResult(
        rows=[TrajectoryRow(**row.wire()) for row in rows],
        lastSeq=builder.last_seq,
        more=more,
    )


def register_trajectory_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("session.trajectory", session_trajectory_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_trajectory_handlers", "session_trajectory_handler"]
