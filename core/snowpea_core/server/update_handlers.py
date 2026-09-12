"""RPC handlers for ``system.checkUpdate``, ``system.update`` and ``system.restart``.

Additive to protocol 1.2.0 (capability ``update``).  The work itself lives in
:mod:`snowpea_core.update`; this module only maps it onto the wire and keeps
the two safety rules the design asks for:

* a check never fails a call — a network problem comes back as
  ``available=false`` with an ``error`` string;
* an upgrade never touches a running turn — it writes new files on disk and
  the daemon reports ``restartRequired`` until someone restarts it.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import TYPE_CHECKING

from snowpea_core import update as update_mod
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    CheckUpdateParams,
    CheckUpdateResult,
    Empty,
    Ok,
    UpdateResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.server.update")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = (
    "system.checkUpdate",
    "system.update",
    "system.restart",
)


async def check_update_handler(
    _conn: RpcConnection, params: CheckUpdateParams, core: Core
) -> CheckUpdateResult:
    """``system.checkUpdate`` — cached for 24h unless ``force``."""
    answer = await update_mod.check_update(core.paths, core.settings, force=params.force)
    # Install/cache provenance is internal, not part of the strict RPC schema.
    public = {
        key: value
        for key, value in answer.items()
        if key not in {"installKey", "trackingSource", "configured"}
    }
    return CheckUpdateResult.model_validate(public)


async def update_handler(_conn: RpcConnection, _params: Empty, core: Core) -> UpdateResult:
    """``system.update`` — start the upgrade detached and report progress."""
    answer = await update_mod.check_update(core.paths, core.settings)
    if answer.get("error") or not answer.get("available"):
        return UpdateResult(
            started=False,
            command="",
            log=str(core.paths.update_log),
            error=str(
                answer.get("error")
                or "No newer update is available; refusing to reinstall or downgrade."
            ),
        )
    source = str(answer.get("source") or "")
    if not source:
        raise RpcError(errors.INTERNAL, "no install source is known for this build")

    log_path = str(core.paths.update_log)
    command = update_mod.update_command(core.paths, source)
    if command is None:
        manual = update_mod.manual_command(source)
        return UpdateResult(
            started=False,
            command=manual,
            log=log_path,
            error=(
                "uv is not on PATH and no install method was recorded in "
                f"{core.paths.install_json}; run this by hand: {manual}"
            ),
        )

    try:
        process = update_mod.start_update(core.paths, command)
    except OSError as exc:
        return UpdateResult(
            started=False,
            command=shlex.join(command),
            log=log_path,
            error=f"could not start the upgrade: {exc}",
        )

    latest = str(answer.get("latest") or answer.get("current") or "")
    await update_mod.notify_progress(core, "started", f"updating to v{latest.lstrip('v')}")
    task = asyncio.ensure_future(
        update_mod.watch_update(core, process, latest, answer.get("trackingSource"))
    )
    core.update_task = task
    return UpdateResult(started=True, command=shlex.join(command), log=log_path)


async def restart_handler(_conn: RpcConnection, _params: Empty, core: Core) -> Ok:
    """``system.restart`` — stop the daemon so the next launch runs new code."""
    if core.request_shutdown is not None:
        core.request_shutdown("restart")
    return Ok(ok=True)


def register_update_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("system.checkUpdate", check_update_handler)
    dispatcher.register("system.update", update_handler)
    dispatcher.register("system.restart", restart_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_update_handlers"]
