"""``web.tunnel.*`` (1.73.0, capability ``webTunnel``).

All but ``web.tunnel.unlock`` are a local person's (``person_gate.PERSON_METHODS``);
``web.tunnel.unlock`` is how a remote web login re-enters its password for terminals
over the tunnel.  The service is :class:`snowpea_core.server.tunnel.TunnelService`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from snowpea_core.config import hot_reload
from snowpea_core.config.credentials import CredentialStore
from snowpea_core.config.settings import Settings
from snowpea_core.jarvis import remote_grant
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    TerminalUnlockParams,
    TerminalUnlockResult,
    TunnelInstallParams,
    TunnelSetTokenParams,
    TunnelStartParams,
    TunnelStopParams,
    TunnelUnlockParams,
    TunnelUnlockResult,
    WebTunnelInstallResult,
    WebTunnelStatus,
)
from snowpea_core.server.tunnel import TOKEN_KEYS, TunnelError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "web.tunnel.status", "web.tunnel.start", "web.tunnel.stop", "web.tunnel.setToken",
    "web.tunnel.install", "web.tunnel.unlock",
)


def _service(core: Core) -> Any:
    service = getattr(core, "tunnel", None)
    if service is None:
        raise RpcError(errors.UNAVAILABLE, "this daemon has no web tunnel",
                       {"reason": "no_tunnel"})
    return service


def _status(raw: dict[str, Any]) -> WebTunnelStatus:
    fields = WebTunnelStatus.model_fields
    return WebTunnelStatus(**{k: v for k, v in raw.items() if k in fields})


async def _save_tunnel(core: Core, **values: Any) -> None:
    before = core.settings.model_dump(mode="json")
    web = dict(before.get("web", {}))
    remote = dict(web.get("remote", {}))
    remote["tunnel"] = {**remote.get("tunnel", {}), **values}
    patch = dict(before)
    patch["web"] = {**web, "remote": remote}
    updated = Settings.model_validate(patch).carry_degraded(core.settings, ["web"])
    updated.save(core.paths)
    await core.adopt_settings(updated, hot_reload.changed_keys(before, patch))


async def status_handler(_conn: RpcConnection, _params: Empty, core: Core) -> WebTunnelStatus:
    return _status(_service(core).status())


async def start_handler(
    _conn: RpcConnection, params: TunnelStartParams, core: Core
) -> WebTunnelStatus:
    service = _service(core)
    try:
        result = await service.start(params.provider)
    except TunnelError as exc:
        raise exc.to_rpc() from exc
    if params.save:
        await _save_tunnel(core, provider=params.provider or result.get("provider"))
    return _status(result)


async def stop_handler(
    _conn: RpcConnection, params: TunnelStopParams, core: Core
) -> WebTunnelStatus:
    result = await _service(core).stop()
    if params.save:
        await _save_tunnel(core, provider="off", autoStart=False)
    return _status(result)


async def set_token_handler(
    _conn: RpcConnection, params: TunnelSetTokenParams, core: Core
) -> WebTunnelStatus:
    token = params.token.strip()
    if not token or any(ch.isspace() for ch in token):
        raise RpcError(errors.INVALID_PARAMS, "a token is one word with no spaces",
                       {"field": "token", "reason": "invalid"})
    CredentialStore(core.paths).set(TOKEN_KEYS[params.provider], token)
    return _status(_service(core).status())


async def install_handler(
    _conn: RpcConnection, params: TunnelInstallParams, core: Core
) -> WebTunnelInstallResult:
    try:
        result = await _service(core).install(params.provider)
    except TunnelError as exc:
        raise exc.to_rpc() from exc
    fields = WebTunnelInstallResult.model_fields
    return WebTunnelInstallResult(**{k: v for k, v in result.items() if k in fields})


async def unlock_handler(
    conn: RpcConnection, params: TunnelUnlockParams, _core: Core
) -> TunnelUnlockResult:
    """The web password again (checked like a sign-in, same limits); unlocks terminals
    for this sign-in session.  Off the tunnel there is nothing to unlock."""
    await remote_grant.reauthenticate(conn, params.password)
    mark = getattr(conn, "tunnel_mark_unlocked", None)
    if callable(mark):
        mark()
    return TunnelUnlockResult(unlocked=True)


async def terminal_unlock_handler(
    conn: RpcConnection, params: TerminalUnlockParams, _core: Core
) -> TerminalUnlockResult:
    """``terminal.unlock``: the same check as ``web.tunnel.unlock`` (an alias)."""
    await remote_grant.reauthenticate(conn, params.password)
    mark = getattr(conn, "tunnel_mark_unlocked", None)
    if callable(mark):
        mark()
    return TerminalUnlockResult(unlocked=True, viaTunnel=getattr(conn, "via_tunnel", False) is True)


def register_tunnel_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("web.tunnel.status", status_handler)
    dispatcher.register("web.tunnel.start", start_handler)
    dispatcher.register("web.tunnel.stop", stop_handler)
    dispatcher.register("web.tunnel.setToken", set_token_handler)
    dispatcher.register("web.tunnel.install", install_handler)
    dispatcher.register("web.tunnel.unlock", unlock_handler)
    return dispatcher
