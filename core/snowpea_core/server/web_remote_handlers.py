"""``web.remote.*`` (1.54.0, capability ``webRemoteControl``): remote web access at runtime.

Owner decision 2b (2026-11-16): ``snowpea web --host/--port`` opens the
external listener now, without a daemon restart; without a password the page
opens with the web access link (a token of its own, never the daemon token);
with a username and password the login page is required.  Every method is a
local person's (``person_gate.PERSON_METHODS``): never a remote login, a
browser or a connector.  The listener itself is
:class:`snowpea_core.server.web_remote.RemoteListener`.
"""

from __future__ import annotations

import contextlib
import socket
from typing import TYPE_CHECKING, Any

from snowpea_core.config import hot_reload
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors, web_remote
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    WebRemoteListenParams,
    WebRemoteSetPasswordParams,
    WebRemoteStatus,
    WebRemoteStopParams,
    WebRemoteTokenResult,
)
from snowpea_core.server.transport_http import WEB_PATH

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "web.remote.status", "web.remote.listen", "web.remote.stop", "web.remote.setPassword",
    "web.remote.clearPassword", "web.remote.rotateToken",
)


def lan_ip() -> str:
    """This machine's address on its default route (no packet is sent)."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        return str(probe.getsockname()[0])
    return "127.0.0.1"


def listen_url(host: str, port: int) -> str:
    shown = lan_ip() if host in {"", "0.0.0.0", "::"} else host
    if ":" in shown:
        shown = f"[{shown}]"
    return f"http://{shown}:{port}{WEB_PATH}"


def _listener(core: Core) -> Any:
    listener = getattr(core, "web_remote", None)
    if listener is None:
        raise RpcError(errors.UNAVAILABLE, "this daemon has no external web listener")
    return listener


def status_of(core: Core) -> WebRemoteStatus:
    remote = core.settings.web.remote
    listener = getattr(core, "web_remote", None)
    listening = bool(listener is not None and listener.listening)
    host = listener.host if listening else remote.host
    port = listener.port if listening else remote.port
    username = listener.username if listening else remote.username
    password_set = bool(web_remote.stored_hash(core.paths))
    mode = "password" if web_remote.password_mode(username, core.paths) else "token"
    urls = [listen_url(host, port)] if listening else []
    access = None
    if listening and mode == "token":
        token = web_remote.access_token(core.paths, create=False)
        access = f"{urls[0]}?{web_remote.TOKEN_PARAM}={token}" if token else None
    needs_restart = bool(remote.enabled) != listening or (
        listening and (remote.host != host or (remote.port not in (0, port))
                       or remote.username != username)
    )
    turns = sum(1 for s in core.sessions.all() if getattr(s, "current_turn", None))
    return WebRemoteStatus(
        enabled=bool(remote.enabled), listening=listening, host=host, port=int(port),
        username=username or "", passwordSet=password_set, mode=mode,  # type: ignore[arg-type]
        listenUrls=urls, accessUrl=access,
        sessions=listener.sessions() if listener is not None else 0,
        needsRestart=needs_restart, plainHttp=True,
        warning=web_remote.PLAIN_HTTP_WARNING if listening else None,
        error=listener.error if listener is not None else None, turnsRunning=turns,
        maxMode=remote.maxMode,
    )


async def _save(core: Core, **values: Any) -> None:
    before = core.settings.model_dump(mode="json")
    patch = dict(before)
    patch["web"] = {**before.get("web", {}),
                    "remote": {**before.get("web", {}).get("remote", {}), **values}}
    updated = Settings.model_validate(patch).carry_degraded(core.settings, ["web"])
    updated.save(core.paths)
    await core.adopt_settings(updated, hot_reload.changed_keys(before, patch))


async def _status(core: Core) -> WebRemoteStatus:
    """:func:`status_of` plus the jobs remote logins left scheduled (review 6, M-4)."""
    status = status_of(core)
    scheduler = getattr(core, "scheduler", None)
    if scheduler is not None:
        with contextlib.suppress(Exception):  # the status itself must still answer
            status.remoteJobs = sum(
                1 for job in await scheduler.list() if job.created_by == "remote_login"
            )
    # Security review 8, M-3: everything marked as a remote login's, additively.
    status.remoteLeft.jobs = status.remoteJobs
    with contextlib.suppress(Exception):
        status.remoteLeft.mcpServers = remote_mcp_servers(core)
    with contextlib.suppress(Exception):
        from snowpea_core.server.skill_gate import remote_skills

        status.remoteLeft.skills = remote_skills(core.paths.home)
    status.settingsDegraded = list(getattr(core.settings, "degraded_sections", list)())
    return status


def remote_mcp_servers(core: Core) -> int:
    """MCP entries marked ``savedBy: "remote_login"`` in the global ``.mcp.json`` and
    ``settings.mcp.servers`` (the scopes a remote login may write)."""
    from snowpea_core.tools import mcp_config
    from snowpea_core.tools.mcp_client import SAVED_BY_KEY

    entries = list(mcp_config.read_servers(
        mcp_config.config_path("global", core.paths.home, None)).values())
    entries += list((getattr(core.settings.mcp, "servers", None) or {}).values())
    return sum(1 for entry in entries
               if isinstance(entry, dict) and entry.get(SAVED_BY_KEY) == "remote_login")


async def status_handler(_conn: RpcConnection, _params: Empty, core: Core) -> WebRemoteStatus:
    return await _status(core)


async def listen_handler(
    _conn: RpcConnection, params: WebRemoteListenParams, core: Core
) -> WebRemoteStatus:
    remote = core.settings.web.remote
    username = remote.username if params.username is None else params.username.strip()
    tunnel = getattr(core, "tunnel", None)
    listener_before = getattr(core, "web_remote", None)
    moved_from = (getattr(listener_before, "host", None), getattr(listener_before, "port", None))
    try:
        await _listener(core).start(
            host=params.host, port=params.port, username=username,
            session_hours=remote.sessionHours, trust_proxy=remote.trustProxy,
            allowed_origins=remote.allowedOrigins,
        )
    except ValueError as exc:
        # The one configuration problem: a password is set without a username.
        raise RpcError(errors.INVALID_PARAMS, str(exc),
                       {"field": "username", "reason": "required"}) from exc
    except web_remote.ListenError as exc:
        # Stable details (1.55.0): clients match on these, never on the wording.
        code = errors.UNAVAILABLE if exc.reason in ("in_use", "permission") \
            else errors.INVALID_PARAMS
        raise RpcError(code, str(exc), {"field": exc.field, "reason": exc.reason}) from exc
    except OSError as exc:
        raise RpcError(errors.UNAVAILABLE, f"could not listen on {params.host}:{params.port}: "
                       f"{exc}", {"field": "host", "reason": "bad_address"}) from exc
    moved = (getattr(listener_before, "host", None), getattr(listener_before, "port", None))
    if tunnel is not None and tunnel.active is True and moved != moved_from:
        # M37: the tunnel pointed at the old address; start it again at the new one.
        with contextlib.suppress(Exception):
            await tunnel.start(wait=0)
    if params.save:
        listener = _listener(core)
        await _save(core, enabled=True, host=params.host,
                    port=params.port if params.port else listener.port, username=username)
    return await _status(core)


async def stop_handler(
    _conn: RpcConnection, params: WebRemoteStopParams, core: Core
) -> WebRemoteStatus:
    tunnel = getattr(core, "tunnel", None)
    if tunnel is not None and tunnel.active is True:
        await tunnel.stop()  # M37: it would point at a closed port
    await _listener(core).stop()
    if params.save:
        await _save(core, enabled=False)
    return await _status(core)


async def set_password_handler(
    _conn: RpcConnection, params: WebRemoteSetPasswordParams, core: Core
) -> WebRemoteStatus:
    if params.username is not None:
        name = params.username.strip()
        if not name:
            raise RpcError(errors.INVALID_PARAMS, "username must not be empty",
                           {"field": "username", "reason": "required"})
        await _save(core, username=name)
        listener = getattr(core, "web_remote", None)
        if listener is not None and listener.listening:
            listener.username = name
            if listener.auth is not None:
                listener.auth.username = name
    elif not str(core.settings.web.remote.username or "").strip():
        raise RpcError(errors.INVALID_PARAMS, "set a username with the password",
                       {"field": "username", "reason": "required"})
    # The hash changes the sessions' fingerprint: everyone is logged out.
    web_remote.set_password(core.paths, params.password)
    await _logout_all(core)
    return await _status(core)


async def clear_password_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> WebRemoteStatus:
    web_remote.clear_password(core.paths)
    await _logout_all(core)
    return await _status(core)


async def rotate_token_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> WebRemoteTokenResult:
    web_remote.rotate_access_token(core.paths)
    await _logout_all(core)
    return WebRemoteTokenResult(accessUrl=status_of(core).accessUrl)


async def _logout_all(core: Core) -> None:
    listener = getattr(core, "web_remote", None)
    auth = getattr(listener, "auth", None)
    if auth is None:
        return
    # Every socket of the listener, not only those of the logins still listed
    # (security review 6, M-1).
    await auth.end_all()


def register_web_remote_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("web.remote.status", status_handler)
    dispatcher.register("web.remote.listen", listen_handler)
    dispatcher.register("web.remote.stop", stop_handler)
    dispatcher.register("web.remote.setPassword", set_password_handler)
    dispatcher.register("web.remote.clearPassword", clear_password_handler)
    dispatcher.register("web.remote.rotateToken", rotate_token_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "listen_url", "register_web_remote_handlers", "status_of"]
