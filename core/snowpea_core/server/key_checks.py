"""Background checks of stored provider keys (1.75.0, capability providerKeyVerify).

A key stored before 1.75.0 (or by hand in ``settings.json``) was never asked
about.  :func:`start_key_checks` asks the vendor about every such key that has
no cached answer — at daemon start and on ``provider.list`` — waits at most
``wait`` seconds, and announces each answer that arrives later with
``provider.verified`` so a settings page updates its row without polling.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import TYPE_CHECKING, Any

from snowpea_core.providers.key_verify import LIST_WAIT_SEC, VerifyResult

if TYPE_CHECKING:
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.server.key_checks")

#: Checks that already have someone announcing their answer.
_WATCHED: weakref.WeakSet[asyncio.Task[VerifyResult]] = weakref.WeakSet()
#: Strong references to the announcers until they finish.
_ANNOUNCERS: set[asyncio.Task[None]] = set()


async def announce(core: Core, vendor: str, result: VerifyResult) -> None:
    """``provider.verified {vendor, verify}`` to every connection."""
    hub = getattr(core, "hub", None)
    if hub is None:
        return
    try:
        await hub.notify("provider.verified", {"vendor": vendor, "verify": result.wire()})
    except Exception:  # noqa: BLE001 - a dead socket must not break a check
        log.debug("could not broadcast provider.verified", exc_info=True)


def _watch(core: Core, vendor: str, task: asyncio.Task[VerifyResult]) -> None:
    if task in _WATCHED:
        return
    _WATCHED.add(task)

    async def run() -> None:
        try:
            result = await task
        except Exception:  # noqa: BLE001 - verify_credentials never raises; be sure
            log.debug("key check for %s failed", vendor, exc_info=True)
            return
        if result.state != "unchecked":
            await announce(core, vendor, result)

    announcer = asyncio.ensure_future(run())
    _ANNOUNCERS.add(announcer)
    announcer.add_done_callback(_ANNOUNCERS.discard)


async def start_key_checks(core: Core, *, wait: float | None = LIST_WAIT_SEC) -> int:
    """Start a check for every stored key with no cached answer; returns how many.

    Waits up to ``wait`` seconds for them (``None``: not at all), so
    ``provider.list`` can carry the answers that come back quickly.
    """
    registry: Any = getattr(core, "providers", None)
    if registry is None or not hasattr(registry, "verify_task"):
        return 0
    tasks: list[asyncio.Task[VerifyResult]] = []
    for vendor in registry.known_vendors():
        try:
            task = registry.verify_task(vendor)
        except Exception:  # noqa: BLE001 - one odd vendor block must not stop the rest
            log.debug("could not start a key check for %s", vendor, exc_info=True)
            continue
        if task is None:
            continue
        _watch(core, vendor, task)
        tasks.append(task)
    if tasks and wait:
        await asyncio.wait(tasks, timeout=wait)
    return len(tasks)


__all__ = ["announce", "start_key_checks"]
