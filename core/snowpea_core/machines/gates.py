"""Local gates before proxying to a remote machine (M40)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.person_gate import connection_kind


def ensure_machine_proxy_allowed(
    method: str,
    conn: Any,
    params: BaseModel,
    core: Any,
    machine_id: str,
) -> None:
    """Apply this daemon's gates before a proxied call leaves for the remote."""
    if connection_kind(conn) == "remote_login" and not core.settings.web.remote.machines:
        raise RpcError(
            errors.UNAUTHORIZED,
            "remote web logins cannot use machines unless web.remote.machines is on",
            {"reason": "remote_machines_off"},
        )
    if method.startswith("terminal."):
        from snowpea_core.server.terminal_handlers import _tunnel_gate

        dangerous = method in {"terminal.create", "terminal.write", "terminal.send"} and bool(
            getattr(params, "confirmDangerous", False) or getattr(params, "options", None)
        )
        kind = "create" if method == "terminal.create" else "other"
        _tunnel_gate(conn, core, dangerous=dangerous, kind=kind)
    _ = machine_id