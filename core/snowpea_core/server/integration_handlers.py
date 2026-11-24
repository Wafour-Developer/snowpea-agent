"""``integration.*`` (1.73.0, capability ``cliIntegrations``): CLI hook integrations.

``integration.list|install|uninstall`` are a local person's
(``person_gate.PERSON_METHODS``): they edit the person's own CLI config files
(``~/.claude/settings.json``, ``~/.codex/hooks.json`` …).  The work is
:mod:`snowpea_core.terminal.integrations.manager` (the same code as
``snowpea integration``), run off the event loop.  Install / uninstall publish
``integration.changed`` with every CLI's row when a file changed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    IntegrationActionParams,
    IntegrationActionResult,
    IntegrationListResult,
    IntegrationRow,
)
from snowpea_core.terminal.integrations import manager

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

log = logging.getLogger(__name__)

HANDLED_METHODS: tuple[str, ...] = (
    "integration.list", "integration.install", "integration.uninstall",
)
CHANGED_EVENT = "integration.changed"


def _env(core: Core) -> manager.Env:
    return manager.Env(snowpea_home=core.paths.home)


def _rows(env: manager.Env) -> list[IntegrationRow]:
    return [IntegrationRow(**row.wire()) for row in manager.status_all(env)]


async def integration_list_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> IntegrationListResult:
    """``integration.list`` — every CLI's hook integration state and the files it touches."""
    env = _env(core)
    return IntegrationListResult(integrations=await asyncio.to_thread(_rows, env))


async def _act(verb: str, params: IntegrationActionParams, core: Core) -> IntegrationActionResult:
    env = _env(core)
    clis = manager.resolve_targets(params.cli)
    action = manager.install if verb == "install" else manager.uninstall

    def run() -> tuple[list[str], list[str]]:
        changed: list[str] = []
        failures: list[str] = []
        for cli in clis:
            if params.cli == "all" and verb == "install" and not env.config_dir(cli).is_dir():
                continue  # that CLI is not set up on this machine
            try:
                result = action(cli, env)
            except (manager.IntegrationError, OSError) as exc:
                failures.append(f"{manager.LABELS[cli]}: {exc}")
                continue
            changed.extend(result.changed + result.removed)
        return changed, failures

    changed, failures = await asyncio.to_thread(run)
    rows = await asyncio.to_thread(_rows, env)
    if changed:
        hub = getattr(core, "hub", None)
        if hub is not None:
            try:
                await hub.notify(CHANGED_EVENT, {
                    "integrations": [row.model_dump(mode="json") for row in rows]})
            except Exception:  # noqa: BLE001 - a dead surface must not fail the edit
                log.debug("could not publish %s", CHANGED_EVENT, exc_info=True)
    if failures and not changed:
        raise RpcError(errors.UNAVAILABLE, "; ".join(failures),
                       {"reason": "integration_failed"})
    if failures:
        log.warning("integration.%s: %s", verb, "; ".join(failures))
    return IntegrationActionResult(integrations=rows, changedFiles=changed)


async def integration_install_handler(
    _conn: RpcConnection, params: IntegrationActionParams, core: Core
) -> IntegrationActionResult:
    """``integration.install {cli}`` — add (or bring up to date) snowpea's hook entries."""
    return await _act("install", params, core)


async def integration_uninstall_handler(
    _conn: RpcConnection, params: IntegrationActionParams, core: Core
) -> IntegrationActionResult:
    """``integration.uninstall {cli}`` — remove exactly what install added."""
    return await _act("uninstall", params, core)


def register_integration_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("integration.list", integration_list_handler)
    dispatcher.register("integration.install", integration_install_handler)
    dispatcher.register("integration.uninstall", integration_uninstall_handler)
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "integration_install_handler",
    "integration_list_handler",
    "integration_uninstall_handler",
    "register_integration_handlers",
]
