"""``integration.*`` (1.73.0, capability ``cliIntegrations``).

M37 T0 stubs: ``not_implemented`` ("not available yet") until T2 replaces them.
``integration.list|install|uninstall`` are a local person's
(``person_gate.PERSON_METHODS``): they edit the person's CLI config files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.server.terminal_handlers import unavailable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.rpc import RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "integration.list", "integration.install", "integration.uninstall",
)


def register_integration_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    for name in HANDLED_METHODS:
        if not dispatcher.has(name):
            dispatcher.register(name, unavailable(name))
    return dispatcher
