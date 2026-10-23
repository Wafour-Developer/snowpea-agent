"""RPC handlers for ``lsp.*`` (M13 contract §4, additive to protocol 1.5.0).

The interesting LSP surface is the seven tools and the ``Diagnostics`` block;
``lsp.status`` exists so the TUI's ``lsp 2`` segment and the IDE's settings
card can show which servers are actually up without guessing, and so a user
whose diagnostics are missing can find out whether the server is ``broken``
or simply never installed.  ``lsp.install`` and ``lsp.restart`` (1.29.0) are
that card's buttons.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from snowpea_core.lsp import servers as lsp_servers
from snowpea_core.lsp.language import language_id
from snowpea_core.lsp.manager import manager_for
from snowpea_core.server import errors
from snowpea_core.server.protocol import (
    Empty,
    LspCatalogEntry,
    LspCatalogResult,
    LspInstallItem,
    LspInstallParams,
    LspInstallResult,
    LspRestartParams,
    LspRestartResult,
    LspServerStatus,
    LspStatusResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher, RpcError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.lsp.handlers")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = ("lsp.status", "lsp.catalog", "lsp.install", "lsp.restart")

INSTALL_PROGRESS = "lsp.install.progress"


def _refuse_gateway(conn: RpcConnection | None, method: str) -> None:
    if str(getattr(conn, "surface_id", "") or "").startswith("gateway:"):
        raise RpcError(errors.UNAUTHORIZED, f"a chat gateway may not call {method}")


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
    enabled = manager_for(core).catalog()
    home = core.paths.home
    rows = [(server, enabled.get(server.id)) for server in lsp_servers.BUILTIN]
    # A user override may point a builtin at another command: look for that one.
    found = await asyncio.gather(
        *(lsp_servers.locate(effective or server, home) for server, effective in rows)
    )
    entries = []
    for (server, effective), hit in zip(rows, found, strict=True):
        path = hit[0] if hit else None
        entries.append(
            LspCatalogEntry(
                id=server.id,
                languageIds=sorted({language_id(ext) for ext in server.extensions}),
                extensions=list(server.extensions),
                installable=server.install is not None,
                installHint=lsp_servers.install_hint(server.install),
                disabled=effective is None,
                available=path is not None,
                path=path,
                managed=lsp_servers.is_managed(path, home),
                needs=None if path else lsp_servers.needs(server),
            )
        )
    return LspCatalogResult(servers=entries)


async def lsp_install_handler(
    conn: RpcConnection, params: LspInstallParams, core: Core
) -> LspInstallResult:
    """``lsp.install`` — install the servers the user clicked (AC-46: the click is consent).

    ``ids`` null means every enabled, installable server that is not found.
    Each id answers ``{id, ok, path?, error?}``; a failure is a result, not
    an error.  Output streams as ``lsp.install.progress``; when anything was
    installed, ``lsp.changed`` follows and missing servers are tried again.
    """
    _refuse_gateway(conn, "lsp.install")
    manager = manager_for(core)
    home = core.paths.home
    if params.ids is None:
        enabled = manager.catalog()
        candidates = [
            server
            for server in lsp_servers.BUILTIN
            # A linter is installed only when asked for by name.
            if server.install is not None and not server.install.when and server.id in enabled
        ]
        found = await asyncio.gather(*(lsp_servers.locate(s, home) for s in candidates))
        ids = [server.id for server, hit in zip(candidates, found, strict=True) if hit is None]
    else:
        ids = list(dict.fromkeys(str(item).strip() for item in params.ids if str(item).strip()))

    async def publish(payload: dict[str, Any]) -> None:
        hub = getattr(core, "hub", None)
        if hub is None:
            return
        try:
            await hub.notify(INSTALL_PROGRESS, payload)
        except Exception:  # noqa: BLE001 - a dead surface must not stop the install
            log.debug("could not publish %s", INSTALL_PROGRESS, exc_info=True)

    results: list[LspInstallItem] = []
    for server_id in ids:
        server = lsp_servers.builtin(server_id)
        if server is None:
            results.append(
                LspInstallItem(id=server_id, ok=False, error="no such language server")
            )
            continue
        hit = await lsp_servers.locate(server, home)
        if hit is not None:
            results.append(LspInstallItem(id=server_id, ok=True, path=hit[0]))
            continue
        if server.install is None:
            need = lsp_servers.needs(server)
            results.append(
                LspInstallItem(
                    id=server_id,
                    ok=False,
                    error=f"not installable by snowpea; needs {need}" if need
                    else "not installable by snowpea",
                )
            )
            continue
        spec = server.install
        await publish(
            {
                "id": server_id,
                "stage": "start",
                "line": lsp_servers.install_hint(spec),
            }
        )

        async def on_line(line: str, server_id: str = server_id) -> None:
            await publish({"id": server_id, "stage": "output", "line": line[:500]})

        log.info("lsp.install %s (%s)", server_id, spec.package)
        path, error = await lsp_servers.install_server(spec, home, on_line=on_line)
        ok = path is not None
        await publish({"id": server_id, "stage": "done", "ok": ok, "error": error})
        results.append(
            LspInstallItem(id=server_id, ok=ok, path=path, error=error, installed=True)
        )
    if any(item.ok for item in results):
        # Installed now, or by hand before the click: either way, look again.
        manager.invalidate()
    return LspInstallResult(results=results)


async def lsp_restart_handler(
    conn: RpcConnection, params: LspRestartParams, core: Core
) -> LspRestartResult:
    """``lsp.restart`` — stop a server (or all) so it starts afresh on its next edit."""
    _refuse_gateway(conn, "lsp.restart")
    server_id = (params.id or "").strip() or None
    restarted = await manager_for(core).restart(server_id)
    return LspRestartResult(restarted=restarted)


def register_lsp_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("lsp.status", lsp_status_handler)
    dispatcher.register("lsp.catalog", lsp_catalog_handler)
    dispatcher.register("lsp.install", lsp_install_handler)
    dispatcher.register("lsp.restart", lsp_restart_handler)
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "lsp_catalog_handler",
    "lsp_install_handler",
    "lsp_restart_handler",
    "lsp_status_handler",
    "register_lsp_handlers",
]
