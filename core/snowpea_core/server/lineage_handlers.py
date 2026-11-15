"""``session.lineage`` (1.48.0, capability ``lineage``; M17 2e).

The ancestors of a session (for breadcrumbs: lead → agent → its agent) and its
descendants as a tree (for the "N delegated" menu), read from the
``parent_session_id`` links on the stored session rows.  A delegated child is its
own session already; this only walks the links.

1.50.0: the ancestor chain also follows ``forked_from`` (each breadcrumb says
whether the step below it was a ``fork`` or a ``delegation``), and ``forks``
lists the direct forks of the session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    LineageAncestor,
    LineageFork,
    LineageNode,
    SessionLineageParams,
    SessionLineageResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = ("session.lineage",)

#: Deepest descendant level walked; delegation is capped well below this.
MAX_DEPTH = 8


def _node(row: dict[str, Any], live: Any) -> dict[str, Any]:
    running = False
    if live is not None:
        task = getattr(live, "turn_task", None)
        running = task is not None and not task.done()
    return {
        "sessionId": str(row["id"]),
        "title": row.get("title") or (getattr(live, "title", None) if live else None),
        "agent": row.get("agent"),
        "kind": row.get("kind") or "chat",
        "createdAt": str(row.get("created_at") or ""),
        "running": running,
    }


async def session_lineage_handler(
    _conn: RpcConnection | None, params: SessionLineageParams, core: Core
) -> SessionLineageResult:
    store = getattr(core, "store", None)
    if store is None:
        raise RpcError(errors.INTERNAL, "no session store")
    rows = {str(row["id"]): row for row in await store.list_sessions(include_closed=True)}
    if params.sessionId not in rows:
        raise RpcError(errors.NOT_FOUND, f"no such session: {params.sessionId}")
    children: dict[str, list[str]] = {}
    for session_id, row in rows.items():
        parent = row.get("parent_session_id")
        if parent:
            children.setdefault(str(parent), []).append(session_id)

    def up(row: dict[str, Any]) -> tuple[str | None, str]:
        parent = row.get("parent_session_id")
        if parent:
            return str(parent), "delegation"
        forked = row.get("forked_from")
        return (str(forked) if forked else None), "fork"

    ancestors: list[LineageAncestor] = []
    seen = {params.sessionId}
    parent, link = up(rows[params.sessionId])
    while parent and parent in rows and parent not in seen:
        seen.add(parent)
        node = _node(rows[parent], core.sessions.get(parent))
        node["sessionKind"], node["kind"] = node["kind"], link
        ancestors.insert(0, LineageAncestor(**node))
        parent, link = up(rows[parent])

    def tree(session_id: str, depth: int) -> list[LineageNode]:
        if depth >= MAX_DEPTH:
            return []
        out = []
        for child in sorted(children.get(session_id, []), key=lambda i: rows[i]["created_at"]):
            node = LineageNode(**_node(rows[child], core.sessions.get(child)))
            node.children = tree(child, depth + 1)
            out.append(node)
        return out

    forks = [
        LineageFork(
            sessionId=session_id,
            title=row.get("title"),
            atTurnId=row.get("forked_at_turn_id"),
            createdAt=str(row.get("created_at") or ""),
        )
        for session_id, row in sorted(rows.items(), key=lambda item: item[1]["created_at"])
        if row.get("forked_from") == params.sessionId
    ]
    return SessionLineageResult(
        ancestors=ancestors, descendants=tree(params.sessionId, 0), forks=forks
    )


def register_lineage_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("session.lineage", session_lineage_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_lineage_handlers", "session_lineage_handler"]
