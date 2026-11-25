"""Remote terminal snapshot for Jarvis tools (M42 security C1).

Jarvis must run the same typing and prompt rules on a remote terminal as on a
local one. This module loads ``terminal.list`` + ``terminal.read`` through the
proxy and builds a small view object the local checks understand.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


@dataclass
class RemoteTerminalView:
    """Enough of a local terminal for ``_may_type``, prompts and the ledger."""

    qualified_id: str
    machine_id: str
    local_id: str
    profile: str
    dangerous: bool
    alive: bool
    status: SimpleNamespace
    screen_text: str
    hook_command: str | None
    spec: SimpleNamespace
    id: str
    jarvis_paused_until: str | None = None

    @property
    def screen(self) -> SimpleNamespace:
        return SimpleNamespace(text=lambda: self.screen_text)


def _status_from_row(row: dict[str, Any], read: dict[str, Any] | None = None) -> SimpleNamespace:
    status = dict(row.get("status") or (read or {}).get("status") or {})
    if not status.get("state"):
        raise ValueError("missing terminal status state")
    return SimpleNamespace(
        state=str(status.get("state")),
        kind=status.get("kind"),
        seen=status.get("seen", True),
    )


def _view_from_row(
    raw_id: str,
    machine_id: str,
    local_id: str,
    row: dict[str, Any],
    screen_text: str,
) -> RemoteTerminalView:
    external = row.get("external") or {}
    spec = SimpleNamespace(
        profile=str(row.get("profile") or "-"),
        dangerous=bool(row.get("dangerous")),
        parent=row.get("parent"),
        cwd=str(row.get("cwd") or ""),
        external=external if isinstance(external, dict) else {},
    )
    return RemoteTerminalView(
        qualified_id=raw_id,
        machine_id=machine_id,
        local_id=local_id,
        profile=spec.profile,
        dangerous=spec.dangerous,
        alive=bool(row["alive"]),
        status=_status_from_row(row),
        screen_text=screen_text,
        hook_command=None,
        spec=spec,
        id=raw_id,
        jarvis_paused_until=row.get("jarvisPausedUntil"),
    )


async def load_remote_terminal(service: Any, raw_id: str) -> RemoteTerminalView:
    """Fresh list + read, or refuse when the row or screen cannot be trusted."""
    from snowpea_core.jarvis.tools import Refused
    from snowpea_core.machines.jarvis_remote import jarvis_proxy_call, split_qualified_id

    machine_id, local_id = split_qualified_id(raw_id)
    if machine_id is None:
        raise Refused(f"no terminal {raw_id!r} that Jarvis supervises")

    listed = await jarvis_proxy_call(service.core, machine_id, "terminal.list", {})
    row: dict[str, Any] | None = None
    for item in listed.get("terminals") or []:
        if isinstance(item, dict) and str(item.get("terminalId")) == local_id:
            row = item
            break
    if row is None or not row.get("jarvisOn"):
        raise Refused(f"no terminal {raw_id!r} that Jarvis supervises")
    if "alive" not in row:
        raise Refused("the remote terminal snapshot did not include whether it is alive")
    if not bool(row.get("alive")):
        raise Refused(f"terminal {raw_id} has ended")
    if not isinstance(row.get("status"), dict) or not row["status"].get("state"):
        raise Refused("the remote terminal snapshot did not include a trustworthy status")

    read = await jarvis_proxy_call(
        service.core,
        machine_id,
        "terminal.read",
        {"terminalId": local_id, "lines": 400},
    )
    if not isinstance(read, dict):
        raise Refused("could not read the remote terminal screen")

    try:
        return _view_from_row(
            raw_id,
            machine_id,
            local_id,
            row,
            str(read.get("text") or ""),
        )
    except ValueError:
        raise Refused("the remote terminal snapshot did not include a trustworthy status") from None


def remote_key(view: RemoteTerminalView) -> str:
    """Machine-qualified delegation key (external id when the row carries one)."""
    ext = view.spec.external or {}
    if ext.get("cli") and ext.get("sessionId"):
        from snowpea_core.machines.jarvis_remote import qualify

        return qualify(view.machine_id, f"ext:{ext['cli']}:{ext['sessionId']}")
    return view.qualified_id


async def reload_screen(service: Any, view: RemoteTerminalView) -> str:
    """Re-list status and refresh the screen text right before keys."""
    from snowpea_core.jarvis.tools import Refused
    from snowpea_core.machines.jarvis_remote import jarvis_proxy_call

    listed = await jarvis_proxy_call(service.core, view.machine_id, "terminal.list", {})
    row: dict[str, Any] | None = None
    for item in listed.get("terminals") or []:
        if isinstance(item, dict) and str(item.get("terminalId")) == view.local_id:
            row = item
            break
    if row is None or not row.get("jarvisOn"):
        raise Refused("the remote terminal is no longer under Jarvis supervision")
    if "alive" not in row or not bool(row.get("alive")):
        raise Refused(f"terminal {view.qualified_id} has ended")
    if not isinstance(row.get("status"), dict) or not row["status"].get("state"):
        raise Refused("the remote terminal status could not be verified")
    try:
        view.status = _status_from_row(row)
    except ValueError as exc:
        raise Refused("the remote terminal status could not be verified") from exc
    view.alive = bool(row["alive"])
    view.jarvis_paused_until = row.get("jarvisPausedUntil")

    read = await jarvis_proxy_call(
        service.core,
        view.machine_id,
        "terminal.read",
        {"terminalId": view.local_id, "lines": 400},
    )
    text = str((read or {}).get("text") or "")
    view.screen_text = text
    return text


__all__ = ["RemoteTerminalView", "load_remote_terminal", "remote_key", "reload_screen"]