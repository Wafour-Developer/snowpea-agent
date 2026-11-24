"""``terminal.*`` (1.73.0, capabilities ``terminalSessions`` / ``terminalStatus``).

The daemon's :class:`~snowpea_core.terminal.manager.TerminalManager` is built by
:func:`ensure_manager` (``Daemon.start`` restores the live holders through it).

Access (``settings_gate`` / ``person_gate``): every ``terminal.*`` method is a
person's, local or a remote web login; never a browser or connector.  Over the
web tunnel, create / interactive attach / write / send / keys / interrupt need
the terminal unlock (``server.tunnel.require_terminal_unlock``, when present).
``terminal.report`` carries the per-terminal token (``SNOWPEA_TERMINAL_TOKEN``)
and is checked against ``terminalId`` and nothing else.

Profiles: ``terminal.profiles`` / ``terminal.create`` use
``snowpea_core.terminal.profiles`` (``list_profiles(core, conn)`` and
``build_spec(core, conn, params, cwd) -> TerminalSpec``) when that module exists;
without it only the ``shell`` profile is offered.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import os
import secrets
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    TerminalAckParams,
    TerminalAttachParams,
    TerminalCloseParams,
    TerminalCreateParams,
    TerminalIdParams,
    TerminalKeysParams,
    TerminalListParams,
    TerminalReadParams,
    TerminalReportParams,
    TerminalResizeParams,
    TerminalSendParams,
    TerminalWaitParams,
    TerminalWriteParams,
)
from snowpea_core.terminal.manager import TerminalManager, TerminalSpec
from snowpea_core.terminal.status import HookReport

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

log = logging.getLogger(__name__)

HANDLED_METHODS: tuple[str, ...] = (
    "terminal.profiles", "terminal.list", "terminal.create", "terminal.attach",
    "terminal.detach", "terminal.write", "terminal.resize", "terminal.ack", "terminal.read",
    "terminal.send", "terminal.keys", "terminal.interrupt", "terminal.wait", "terminal.close",
    "terminal.seen", "terminal.unlock", "terminal.report",
)

#: Never handed to a terminal's program: the daemon's own secrets and switches
#: (every ``SNOWPEA_*``) and what makes a nested Claude Code behave differently.
_SCRUB_PREFIXES = ("SNOWPEA_",)
_SCRUB_EXACT = frozenset({"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"})


def unavailable(name: str) -> Any:
    """A handler that answers ``not_implemented`` with a clear message."""

    async def handler(_conn: RpcConnection, _params: Any, _core: Core) -> dict[str, Any]:
        raise RpcError(errors.NOT_IMPLEMENTED, f"{name} is not available yet")

    return handler


# ---------------------------------------------------------------------------
# the manager
# ---------------------------------------------------------------------------


def _rules_loader() -> Any:
    try:
        from snowpea_core.terminal.rules import engine  # type: ignore[attr-defined]
    except ImportError:  # T2's engine not here
        return None
    if hasattr(engine, "rules_loader"):
        return engine.rules_loader
    load_rules = engine.load_rules

    def load(rules_id: str) -> Any:
        try:
            return load_rules(rules_id)
        except Exception:  # noqa: BLE001 - no rules for this profile
            return None

    return load


def ensure_manager(core: Core) -> TerminalManager:
    """The daemon's :class:`TerminalManager`, created on first use."""
    manager = getattr(core, "terminals", None)
    if manager is None:
        hub = core.hub

        async def emit(method: str, params: dict[str, Any]) -> None:
            await hub.notify(method, params)

        async def send(conn: Any, method: str, params: dict[str, Any]) -> None:
            await conn.notify(method, params)

        def max_terminals() -> int:
            return int(getattr(getattr(core.settings, "terminals", None), "max", 16) or 16)

        manager = TerminalManager(
            core.paths.home,
            emit=emit,
            send=send,
            rules_loader=_rules_loader(),
            max_terminals=max_terminals,
            report_address=lambda: getattr(core, "terminal_report_address", None),
        )
        core.terminals = manager
    return manager


async def start_terminals(core: Core) -> TerminalManager:
    """``Daemon.start``: the hook report socket (when present), then the holders' restore."""
    manager = ensure_manager(core)
    try:
        from snowpea_core.terminal.report import ReportServer  # type: ignore[import-not-found]
    except ImportError:  # the hook integrations (T2) are not here
        ReportServer = None  # noqa: N806
    if ReportServer is not None and getattr(core, "terminal_report_server", None) is None:
        try:
            server = ReportServer(manager, core.paths.home)
            await server.start()
        except Exception:  # noqa: BLE001 - terminals work without hook reports
            log.warning("could not open the terminal report socket", exc_info=True)
        else:
            core.terminal_report_server = server
            core.terminal_report_address = server.address
    await manager.start()
    return manager


async def stop_terminals(core: Core) -> None:
    """``Daemon.stop``: close the report socket and drop the links (terminals keep running)."""
    server = getattr(core, "terminal_report_server", None)
    if server is not None:
        with contextlib.suppress(Exception):
            await server.stop()
        core.terminal_report_server = None
    manager = getattr(core, "terminals", None)
    if manager is not None:
        await manager.shutdown()


def detach_connection(core: Core, conn: Any) -> None:
    """A connection closed: it stops viewing every terminal (none is killed)."""
    manager = getattr(core, "terminals", None)
    if manager is not None:
        manager.detach_all(conn)


def _watch_close(core: Core, conn: Any) -> None:
    """Detach ``conn`` from every terminal once it closes (registered once)."""
    if getattr(conn, "_terminal_close_hooked", False):
        return
    callbacks = getattr(conn, "on_close", None)
    if isinstance(callbacks, list):
        callbacks.append(lambda c: detach_connection(core, c))
        with contextlib.suppress(AttributeError):
            conn._terminal_close_hooked = True


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def _tunnel_gate(conn: Any, core: Core, *, dangerous: bool = False) -> None:
    """Over the web tunnel, terminal calls need the unlock (T4's gate, when present)."""
    try:
        from snowpea_core.server.tunnel import (  # type: ignore[import-not-found]
            require_terminal_unlock,
        )
    except ImportError:  # T4's tunnel not here: nothing comes over a tunnel
        if getattr(conn, "via_tunnel", False):
            raise RpcError(errors.UNAVAILABLE,
                           "terminals are not available over the web tunnel") from None
        return
    require_terminal_unlock(conn, dangerous=dangerous, core=core)


# ---------------------------------------------------------------------------
# profiles and launch (T3 extends through snowpea_core.terminal.profiles)
# ---------------------------------------------------------------------------


def _profiles_module() -> Any:
    try:
        from snowpea_core.terminal import profiles  # type: ignore[attr-defined]
    except ImportError:  # T3's profiles not here
        return None
    return profiles


def person_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The person's environment minus the daemon's secrets and ``SNOWPEA_*``."""
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(_SCRUB_PREFIXES) and key not in _SCRUB_EXACT
    }
    env.update(extra or {})
    return env


def shell_argv() -> list[str]:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        for name in ("pwsh", "powershell"):
            found = shutil.which(name)
            if found:
                return [found, "-NoLogo"]
        return [os.environ.get("COMSPEC") or "cmd.exe"]
    shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    return [shell, "-l", "-i"] if sys.platform == "darwin" else [shell, "-i"]


def _shell_profile() -> dict[str, Any]:
    argv = shell_argv()
    return {"id": "shell", "label": "Shell", "installed": True, "path": argv[0],
            "resumable": False, "options": []}


async def _resolve_cwd(core: Core, params: TerminalCreateParams) -> str:
    if params.noProject or not params.cwd:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        folder = Path(core.paths.home) / "sessions" / f"{day}_terminal-{secrets.token_hex(3)}"
        for sub in ("tmp", "artifacts"):
            (folder / sub).mkdir(parents=True, exist_ok=True)
        return str(folder)
    from snowpea_core.external.proc import ExternalError, validate_cwd
    from snowpea_core.server.external_handlers import _roots

    try:
        return validate_cwd(params.cwd, await _roots(core))
    except ExternalError as exc:
        raise RpcError(exc.code, exc.message) from exc


def _size(core: Core, params: Any) -> tuple[int, int]:
    settings = getattr(core.settings, "terminals", None)
    cols = params.cols or int(getattr(settings, "headlessCols", 160) or 160)
    rows = params.rows or int(getattr(settings, "headlessRows", 48) or 48)
    return cols, rows


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


async def profiles_handler(conn: RpcConnection, _params: Any, core: Core) -> dict[str, Any]:
    module = _profiles_module()
    if module is not None and hasattr(module, "list_profiles"):
        return {"profiles": await _maybe_await(module.list_profiles(core, conn))}
    return {"profiles": [_shell_profile()]}


async def list_handler(_conn: RpcConnection, params: TerminalListParams,
                       core: Core) -> dict[str, Any]:
    return {"terminals": ensure_manager(core).list(parent=params.parent,
                                                   alive_only=params.aliveOnly)}


async def create_handler(conn: RpcConnection, params: TerminalCreateParams,
                         core: Core) -> dict[str, Any]:
    _tunnel_gate(conn, core)
    manager = ensure_manager(core)
    cwd = await _resolve_cwd(core, params)
    cols, rows = _size(core, params)
    module = _profiles_module()
    if module is not None and hasattr(module, "build_spec"):
        spec = await _maybe_await(module.build_spec(core, conn, params, cwd))
    elif params.profile == "shell":
        if params.options or params.confirmDangerous:
            raise RpcError(errors.INVALID_PARAMS, "the shell profile has no options")
        spec = TerminalSpec(argv=shell_argv(), env=person_env(), cwd=cwd, profile="shell")
    else:
        raise RpcError(errors.NOT_IMPLEMENTED,
                       f"the {params.profile} profile is not available yet")
    spec.cols, spec.rows = cols, rows
    if params.title:
        spec.title = params.title
    if params.labels:
        spec.labels = {**spec.labels, **params.labels}
    if params.parent and not spec.parent:
        spec.parent = params.parent
    if params.prompt and not spec.prompt:
        spec.prompt = params.prompt
    info = await manager.create(spec)
    return {"terminal": info}


async def attach_handler(conn: RpcConnection, params: TerminalAttachParams,
                         core: Core) -> dict[str, Any]:
    if params.mode == "interactive":
        _tunnel_gate(conn, core)
    manager = ensure_manager(core)
    result = await manager.attach(params.terminalId, conn, params.mode, params.cols, params.rows)
    _watch_close(core, conn)
    return result


async def detach_handler(conn: RpcConnection, params: TerminalIdParams,
                         core: Core) -> dict[str, Any]:
    ensure_manager(core).detach(params.terminalId, conn)
    return {"ok": True}


def _decode(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RpcError(errors.INVALID_PARAMS, "data must be base64") from exc


async def write_handler(conn: RpcConnection, params: TerminalWriteParams,
                        core: Core) -> dict[str, Any]:
    _tunnel_gate(conn, core)
    await ensure_manager(core).write(params.terminalId, conn, _decode(params.data),
                                     human=params.human)
    return {"ok": True}


async def resize_handler(conn: RpcConnection, params: TerminalResizeParams,
                         core: Core) -> dict[str, Any]:
    _tunnel_gate(conn, core)
    await ensure_manager(core).resize(params.terminalId, conn, params.cols, params.rows)
    return {"ok": True}


async def ack_handler(conn: RpcConnection, params: TerminalAckParams, core: Core) -> dict[str, Any]:
    ensure_manager(core).ack(params.terminalId, conn, params.bytes)
    return {"ok": True}


async def read_handler(_conn: RpcConnection, params: TerminalReadParams,
                       core: Core) -> dict[str, Any]:
    return ensure_manager(core).read(params.terminalId, params.lines)


async def send_handler(conn: RpcConnection, params: TerminalSendParams,
                       core: Core) -> dict[str, Any]:
    _tunnel_gate(conn, core)
    return await ensure_manager(core).send(params.terminalId, params.text, force=params.force,
                                           submit=params.submit)


async def keys_handler(conn: RpcConnection, params: TerminalKeysParams,
                       core: Core) -> dict[str, Any]:
    _tunnel_gate(conn, core)
    await ensure_manager(core).keys(params.terminalId, list(params.keys))
    return {"ok": True}


async def interrupt_handler(conn: RpcConnection, params: TerminalIdParams,
                            core: Core) -> dict[str, Any]:
    _tunnel_gate(conn, core)
    await ensure_manager(core).interrupt(params.terminalId)
    return {"ok": True}


async def wait_handler(_conn: RpcConnection, params: TerminalWaitParams,
                       core: Core) -> dict[str, Any]:
    return await ensure_manager(core).wait(params.terminalId, params.timeout, params.output)


async def close_handler(conn: RpcConnection, params: TerminalCloseParams,
                        core: Core) -> dict[str, Any]:
    _tunnel_gate(conn, core)
    await ensure_manager(core).close(params.terminalId, force=params.force)
    return {"ok": True}


async def seen_handler(_conn: RpcConnection, params: TerminalIdParams,
                       core: Core) -> dict[str, Any]:
    ensure_manager(core).seen(params.terminalId)
    return {"ok": True}


async def report_handler(_conn: RpcConnection, params: TerminalReportParams,
                         core: Core) -> dict[str, Any]:
    report = HookReport(event=params.event, state=params.state, kind=params.kind,
                        session_id=params.sessionId,
                        transcript_path=params.transcriptPath, message=params.message)
    ensure_manager(core).report(params.terminalId, params.token, report)
    return {"ok": True}


_HANDLERS: dict[str, Any] = {
    "terminal.profiles": profiles_handler,
    "terminal.list": list_handler,
    "terminal.create": create_handler,
    "terminal.attach": attach_handler,
    "terminal.detach": detach_handler,
    "terminal.write": write_handler,
    "terminal.resize": resize_handler,
    "terminal.ack": ack_handler,
    "terminal.read": read_handler,
    "terminal.send": send_handler,
    "terminal.keys": keys_handler,
    "terminal.interrupt": interrupt_handler,
    "terminal.wait": wait_handler,
    "terminal.close": close_handler,
    "terminal.seen": seen_handler,
    "terminal.report": report_handler,
}


def _unlock_handler(conn: Any, params: Any, core: Any) -> Any:
    """``terminal.unlock`` is ``web.tunnel.unlock`` under the terminal namespace (T4)."""
    from snowpea_core.server.tunnel_handlers import terminal_unlock_handler

    return terminal_unlock_handler(conn, params, core)


_HANDLERS["terminal.unlock"] = _unlock_handler


def register_terminal_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    for name in HANDLED_METHODS:
        if not dispatcher.has(name):
            dispatcher.register(name, _HANDLERS.get(name) or unavailable(name))
    return dispatcher


__all__ = ["HANDLED_METHODS", "ensure_manager", "register_terminal_handlers", "unavailable"]
