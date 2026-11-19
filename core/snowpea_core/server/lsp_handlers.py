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
from snowpea_core.lsp import toolchains
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
    LspToolchainCancelResult,
    LspToolchainOffer,
    LspToolchainParams,
    LspToolchainRemoveResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher, RpcError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.lsp.handlers")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = (
    "lsp.status", "lsp.catalog", "lsp.install", "lsp.restart",
    "lsp.toolchain.remove", "lsp.toolchain.cancel",
)

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
    missing = await asyncio.gather(
        *(
            lsp_servers.missing_toolchain(server) if hit is None else _none()
            for (server, _effective), hit in zip(rows, found, strict=True)
        )
    )
    entries = []
    for (server, effective), hit, toolchain in zip(rows, found, missing, strict=True):
        path = hit[0] if hit else None
        reasons: dict[str, Any] = {}
        needs = None if path else lsp_servers.needs(server)
        if toolchain is not None:
            reasons = toolchains.reason_fields(toolchain)
            needs = toolchains.TOOLCHAINS[toolchain].needs
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
                needs=needs,
                **reasons,
            )
        )
    return LspCatalogResult(servers=entries)


async def _none() -> None:
    return None


def _toolchain_gate(
    conn: RpcConnection | None,
    what: str,
    actor: str | None,
    person_confirm: str | None,
    *,
    confirm: bool = True,
) -> None:
    """Who may download, remove (``confirm``) or stop a toolchain (1.68.0).

    A local person (IDE, TUI, CLI); a local browser 0.4.0+ with ``actor:
    "person"`` and, for a download or removal, ``personConfirm: "window"``.
    Never a remote web login, a connector, Jarvis or an in-process caller —
    a model's tool cannot start a download without its person.
    """
    from snowpea_core.server import settings_gate
    from snowpea_core.server.person_gate import connection_kind, require_person

    if connection_kind(conn) == "browser":
        if settings_gate.browser_remote(conn):
            settings_gate.refuse(conn, what, reason=settings_gate.BROWSER_REMOTE)
        if not settings_gate.browser_confirms(conn):
            settings_gate.refuse(conn, what, reason=settings_gate.BROWSER_LEGACY)
        if actor != settings_gate.ACTOR_PERSON or (
            confirm and person_confirm != settings_gate.PERSON_CONFIRM_WINDOW
        ):
            settings_gate.refuse(conn, what, reason=settings_gate.BROWSER_UNCONFIRMED)
        return
    require_person(conn, what, local=True)


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
    if params.withToolchain:
        _toolchain_gate(conn, "install a toolchain", params.actor, params.personConfirm)
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
        toolchain = await lsp_servers.missing_toolchain(server)
        info = toolchains.TOOLCHAINS.get(toolchain) if toolchain else None
        if toolchain is not None and not (params.withToolchain and info and info.auto):
            offer = (
                LspToolchainOffer(**await toolchains.offer(toolchain))
                if info and info.auto else None
            )
            results.append(
                LspInstallItem(
                    id=server_id,
                    ok=False,
                    error=f"needs {info.needs if info else toolchain}",
                    needsToolchain=offer,
                    **toolchains.reason_fields(toolchain),
                )
            )
            continue
        await publish(
            {
                "id": server_id,
                "stage": "start",
                "line": lsp_servers.install_hint(spec),
            }
        )
        if toolchain is not None:
            failed = await _install_toolchain(toolchain, server_id, publish)
            if failed is not None:
                await publish(
                    {"id": server_id, "stage": "done", "ok": False, "error": failed.error}
                )
                results.append(failed)
                continue

        async def on_line(line: str, server_id: str = server_id) -> None:
            await publish(
                {"id": server_id, "stage": "output", "line": line[:500], "phase": "server"}
            )

        log.info("lsp.install %s (%s)", server_id, spec.package)
        details: dict[str, Any] = {}
        path, error = await lsp_servers.install_server(
            spec, home, on_line=on_line, details=details
        )
        ok = path is not None
        await publish({"id": server_id, "stage": "done", "ok": ok, "error": error})
        results.append(
            LspInstallItem(
                id=server_id,
                ok=ok,
                path=path,
                error=error,
                installed=True,
                **({} if ok else details),
            )
        )
    if any(item.ok for item in results):
        # Installed now, or by hand before the click: either way, look again.
        manager.invalidate()
    return LspInstallResult(results=results)


async def _install_toolchain(
    name: str, server_id: str, publish: Any
) -> LspInstallItem | None:
    """Install toolchain ``name`` for ``server_id``: ``None``, or the failed item."""

    async def progress(phase: str, percent: int | None, line: str | None) -> None:
        await publish(
            {
                "id": server_id,
                "stage": "output",
                "line": (line or "")[:500] or None,
                "phase": "toolchain" if phase == "done" else phase,
                "percent": percent,
                "toolchain": name,
            }
        )

    log.info("lsp.install %s: installing toolchain %s", server_id, name)
    try:
        await toolchains.install(name, progress)
    except toolchains.ToolchainError as exc:
        log.warning("toolchain %s not installed: %s", name, exc)
        return LspInstallItem(
            id=server_id,
            ok=False,
            error=str(exc),
            installed=True,
            reason=toolchains.REASON_TOOLCHAIN_FAILED,
            toolchain=name,
            toolchainError=exc.code,
            hint=toolchains.TOOLCHAINS[name].hint,
            url=toolchains.TOOLCHAINS[name].url,
        )
    return None


def _known_toolchain(name: str) -> str:
    name = (name or "").strip()
    if name not in toolchains.TOOLCHAINS:
        raise RpcError(errors.INVALID_PARAMS, f"no such toolchain: {name!r}")
    return name


async def lsp_toolchain_remove_handler(
    conn: RpcConnection, params: LspToolchainParams, core: Core
) -> LspToolchainRemoveResult:
    """``lsp.toolchain.remove`` — delete core's own copy of a toolchain (1.68.0)."""
    _refuse_gateway(conn, "lsp.toolchain.remove")
    _toolchain_gate(conn, "remove a toolchain", params.actor, params.personConfirm)
    name = _known_toolchain(params.name)
    try:
        removed = toolchains.remove(name)
    except toolchains.ToolchainError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc), {"reason": exc.code}) from exc
    except OSError as exc:
        raise RpcError(errors.INTERNAL_ERROR, f"could not remove {name}: {exc}") from exc
    if removed:
        manager_for(core).invalidate()
    return LspToolchainRemoveResult(name=name, removed=removed)


async def lsp_toolchain_cancel_handler(
    conn: RpcConnection, params: LspToolchainParams, _core: Core
) -> LspToolchainCancelResult:
    """``lsp.toolchain.cancel`` — stop a running toolchain install (1.68.0)."""
    _refuse_gateway(conn, "lsp.toolchain.cancel")
    _toolchain_gate(conn, "stop a toolchain install", params.actor, None, confirm=False)
    name = _known_toolchain(params.name)
    return LspToolchainCancelResult(name=name, cancelled=toolchains.cancel(name))


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
    dispatcher.register("lsp.toolchain.remove", lsp_toolchain_remove_handler)
    dispatcher.register("lsp.toolchain.cancel", lsp_toolchain_cancel_handler)
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "lsp_catalog_handler",
    "lsp_install_handler",
    "lsp_restart_handler",
    "lsp_status_handler",
    "lsp_toolchain_cancel_handler",
    "lsp_toolchain_remove_handler",
    "register_lsp_handlers",
]
