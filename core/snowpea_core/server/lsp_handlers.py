"""RPC handler for ``lsp.*`` (M13 contract §4, additive to protocol 1.5.0).

One method.  The interesting LSP surface is the seven tools and the
``Diagnostics`` block; ``lsp.status`` exists so the TUI's ``lsp 2`` segment and
the IDE's settings card can show which servers are actually up without
guessing, and so a user whose diagnostics are missing can find out whether the
server is ``broken`` or simply never installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.lsp.manager import manager_for
from snowpea_core.server.protocol import Empty, LspServerStatus, LspStatusResult
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = ("lsp.status",)


async def lsp_status_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> LspStatusResult:
    """``lsp.status`` — every (server, root) the daemon has started or tried to."""
    manager = manager_for(core)
    return LspStatusResult(
        servers=[LspServerStatus(**row) for row in manager.status()]
    )


def register_lsp_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("lsp.status", lsp_status_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "lsp_status_handler", "register_lsp_handlers"]
