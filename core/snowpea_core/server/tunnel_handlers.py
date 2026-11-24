"""``web.tunnel.*`` (1.73.0, capability ``webTunnel``).

M37 T0 stubs: ``not_implemented`` ("not available yet") until T4 replaces them.
All but ``web.tunnel.unlock`` are a local person's (``person_gate.PERSON_METHODS``);
``web.tunnel.unlock`` is how a remote web login re-enters its password, so it is
open to a remote login (the real handler checks the password).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.server.terminal_handlers import unavailable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.rpc import RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "web.tunnel.status", "web.tunnel.start", "web.tunnel.stop", "web.tunnel.setToken",
    "web.tunnel.install", "web.tunnel.unlock",
)


def register_tunnel_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    for name in HANDLED_METHODS:
        if not dispatcher.has(name):
            dispatcher.register(name, unavailable(name))
    return dispatcher
