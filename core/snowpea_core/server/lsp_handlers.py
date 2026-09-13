"""RPC handler for ``lsp.*`` (M13 contract §4, additive to protocol 1.5.0).

One method.  The interesting LSP surface is the seven tools and the
``Diagnostics`` block; ``lsp.status`` exists so the TUI's ``lsp 2`` segment and
the IDE's settings card can show which servers are actually up without
guessing, and so a user whose diagnostics are missing can find out whether the
server is ``broken`` or simply never installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.lsp import servers as lsp_servers
from snowpea_core.lsp.language import language_id
from snowpea_core.lsp.manager import manager_for
from snowpea_core.server.protocol import (
    Empty,
    LspCatalogEntry,
    LspCatalogResult,
    LspServerStatus,
    LspStatusResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = ("lsp.status", "lsp.catalog")


async def lsp_status_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> LspStatusResult:
    """``lsp.status`` — every (server, root) the daemon has started or tried to."""
    manager = manager_for(core)
    return LspStatusResult(
        servers=[LspServerStatus(**row) for row in manager.status()]
    )


async def lsp_catalog_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> LspCatalogResult:
    """``lsp.catalog`` — every registered server, whether or not it has started.

    ``disabled`` mirrors what :func:`~snowpea_core.lsp.servers.catalog` would
    actually start: a default-off id (``ty``, ``ruff``), one named in
    ``lsp.disabled``, or one an ``lsp.servers`` override explicitly disables
    all come back ``True`` here too, so a settings UI shows the same answer
    the manager would act on.
    """
    settings = core.settings.lsp
    enabled_ids = set(
        lsp_servers.catalog(disabled=settings.disabled, overrides=settings.servers)
    )
    entries = [
        LspCatalogEntry(
            id=server.id,
            languageIds=sorted({language_id(ext) for ext in server.extensions}),
            extensions=list(server.extensions),
            installable=server.install is not None,
            installHint=lsp_servers.install_hint(server.install),
            disabled=server.id not in enabled_ids,
        )
        for server in lsp_servers.BUILTIN
    ]
    return LspCatalogResult(servers=entries)


def register_lsp_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("lsp.status", lsp_status_handler)
    dispatcher.register("lsp.catalog", lsp_catalog_handler)
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "lsp_catalog_handler",
    "lsp_status_handler",
    "register_lsp_handlers",
]
