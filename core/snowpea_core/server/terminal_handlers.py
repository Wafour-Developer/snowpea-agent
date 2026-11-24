"""``terminal.*`` (1.73.0, capabilities ``terminalSessions`` / ``terminalStatus``).

M37 T0 stubs: every method answers ``not_implemented`` ("not available yet")
until the terminal runtime (T1) replaces this module's handlers.  The names in
:data:`HANDLED_METHODS` are the contract: T1 keeps the names, swaps the
handlers, and moves the capabilities out of ``PENDING_CAPABILITIES``.

Access (``settings_gate`` / ``person_gate``): every ``terminal.*`` method is a
person's, local or a remote web login (the tunnel's unlock is judged by the
real handler); never a browser or connector.  ``terminal.report`` is the
exception: it carries the per-terminal token (``SNOWPEA_TERMINAL_TOKEN``) and
the real handler must verify it against ``terminalId`` and nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "terminal.profiles", "terminal.list", "terminal.create", "terminal.attach",
    "terminal.detach", "terminal.write", "terminal.resize", "terminal.ack", "terminal.read",
    "terminal.send", "terminal.keys", "terminal.interrupt", "terminal.wait", "terminal.close",
    "terminal.seen", "terminal.unlock", "terminal.report",
)


def unavailable(name: str) -> Any:
    """A handler that answers ``not_implemented`` with a clear message."""

    async def handler(_conn: RpcConnection, _params: Any, _core: Core) -> dict[str, Any]:
        raise RpcError(errors.NOT_IMPLEMENTED, f"{name} is not available yet")

    return handler


def register_terminal_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    for name in HANDLED_METHODS:
        if not dispatcher.has(name):
            dispatcher.register(name, unavailable(name))
    return dispatcher
