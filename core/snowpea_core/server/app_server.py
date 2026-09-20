"""Daemon assembly: core singletons, handler registration, process lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web
from pydantic import Field

from snowpea_core import __version__
from snowpea_core import update as update_mod
from snowpea_core.agent.named import NamedAgentRegistry
from snowpea_core.commands.registry import CommandRegistry
from snowpea_core.config import hot_reload
from snowpea_core.config.patch import reject_masked_secrets
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.gateway.router import GatewayRouter
from snowpea_core.permissions.allowlist import SHELL_TARGET, Allowlist
from snowpea_core.permissions.allowlist import Scope as AllowlistStore
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers import auth_web
from snowpea_core.providers.base import ProviderError
from snowpea_core.providers.registry import ProviderRegistry
from snowpea_core.scheduler import start_scheduler, stop_scheduler
from snowpea_core.server import errors
from snowpea_core.server.agent_handlers import register_agent_handlers
from snowpea_core.server.audio_handlers import register_audio_handlers
from snowpea_core.server.auth import ensure_token, write_token
from snowpea_core.server.errors import RpcError
from snowpea_core.server.gateway_handlers import register_gateway_handlers
from snowpea_core.server.job_handlers import register_job_handlers
from snowpea_core.server.lifecycle import Lifecycle
from snowpea_core.server.lsp_handlers import register_lsp_handlers
from snowpea_core.server.mcp_handlers import register_mcp_handlers
from snowpea_core.server.protocol import (
    METHODS as PROTOCOL_METHODS,
)
from snowpea_core.server.protocol import (
    PROTOCOL_VERSION,
    SERVER_VERSION,
    AllowlistAddParams,
    AllowlistAddResult,
    AllowlistListParams,
    AllowlistListResult,
    AllowlistPattern,
    AllowlistRemoveParams,
    ApprovalRequest,
    Empty,
    HealthResult,
    InfoResult,
    LifecycleStatus,
    Ok,
    Payload,
    ProviderConfigureParams,
    ProviderLoginWebParams,
    ProviderLoginWebResult,
    ProviderRemoveParams,
    SettingsChangedNotification,
    SettingsReloadResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.server.session_handlers import (
    register_session_handlers,
    wire_core,
)
from snowpea_core.server.settings_handlers import register_settings_handlers
from snowpea_core.server.skill_handlers import register_skill_handlers
from snowpea_core.server.team_handlers import register_team_handlers
from snowpea_core.server.transport_http import (
    CONNECTIONS_KEY,
    SOCKETS_KEY,
    create_app,
)
from snowpea_core.server.transport_ws import hello_handler
from snowpea_core.server.update_handlers import register_update_handlers
from snowpea_core.session.manager import EventHub, SessionManager
from snowpea_core.session.questions import QuestionQueue
from snowpea_core.tools.registry import ToolRegistry

log = logging.getLogger("snowpea.server")

HOST = "127.0.0.1"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Core:
    """The daemon's singletons. Later stories replace the stub collaborators."""

    settings: Settings
    paths: Paths
    token: str
    store: Any = None
    #: Chat gateway router (US-016); ``Daemon.start`` builds and wires it.
    gateway: Any = None
    #: Long-term memory services (US-014); ``wire_core`` builds them.
    memory: Any = None
    #: Scheduled jobs (US-015); ``wire_core`` builds it, ``Daemon.start`` ticks it.
    scheduler: Any = None
    #: Plugins, skills, agent definitions and hooks (US-017); ``wire_core``
    #: builds it and ``Daemon.start`` does the first full reload.
    skills: Any = None
    #: Language servers (M13); ``wire_core`` builds the manager, and
    #: ``Daemon.stop`` shuts every server down.
    lsp: Any = None
    #: Named persistent agents (US-021); ``Daemon.start`` builds it and
    #: restores its sessions before the gateway re-attaches its bindings.
    named_agents: Any = None
    sessions: SessionManager = field(default_factory=SessionManager)
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    commands: CommandRegistry = field(default_factory=CommandRegistry)
    providers: ProviderRegistry = field(default_factory=ProviderRegistry)
    approvals: ApprovalQueue = field(default_factory=ApprovalQueue)
    #: Questions the ``ask_user`` tool is waiting on (see session/questions.py).
    questions: QuestionQueue = field(default_factory=QuestionQueue)
    allowlist: Allowlist = field(default_factory=Allowlist)
    policy: PermissionPolicy = field(default_factory=PermissionPolicy)
    hub: EventHub = field(default_factory=EventHub)
    lifecycle: Lifecycle = field(default_factory=Lifecycle)
    pid: int = field(default_factory=os.getpid)
    port: int = 0
    started_at: str = field(default_factory=_utc_now)
    request_shutdown: Any = None
    #: Set once ``system.update`` finished successfully; ``system.info``
    #: reports it so a surface can tell the user to restart (CORE-update).
    restart_required: bool = False
    #: The task watching a running upgrade, kept so it is not garbage collected.
    update_task: Any = None
    #: The daily background update check; separate from the upgrade watcher so
    #: one never replaces the other.
    update_check_task: Any = None
    #: ``(mtime_ns, size)`` of ``settings.json`` as of the last load or save;
    #: :meth:`settings_file_changed` compares against it (CORE-settings-reload).
    settings_stamp: tuple[int, int] = hot_reload.MISSING
    #: Set at the start of ``Daemon.stop`` so in-flight background work (e.g.
    #: the memory nudge) can decline to start once shutdown has begun, instead
    #: of racing the services it depends on being closed (CORE-memory-race).
    stopping: bool = False

    # -- settings hot reload (CORE-settings-reload) ---------------------

    def mark_settings_saved(self) -> None:
        """Record the file as it now is, after the daemon itself wrote it.

        Without this an in-daemon write (``settings.set``, ``provider.configure``,
        ``/model``) would look like an outside edit on the next prompt and cost
        a pointless re-read.
        """
        self.settings_stamp = hot_reload.stamp(self.paths.settings_json)

    def settings_file_changed(self) -> bool:
        """True when ``settings.json`` differs from what was last loaded or saved."""
        return hot_reload.stamp(self.paths.settings_json) != self.settings_stamp

    async def reload_settings(self, *, force: bool = False) -> tuple[bool, list[str]]:
        """Re-read ``settings.json`` and rebind everything that captured it.

        Returns ``(reloaded, changed_keys)``.  An unchanged file (or one whose
        content is identical to what is already held) reloads nothing and
        notifies nobody, so this is safe to call on every prompt.
        """
        current = hot_reload.stamp(self.paths.settings_json)
        if not force and current == self.settings_stamp:
            return False, []
        fresh = Settings.load(self.paths)
        keys = hot_reload.changed_keys(
            self.settings.model_dump(mode="json"), fresh.model_dump(mode="json")
        )
        self.settings_stamp = current
        if not keys:
            return False, []
        hot_reload.rebind(self, fresh)
        log.info("settings.json reloaded; changed: %s", ", ".join(keys))
        await self._announce_settings(keys)
        return True, keys

    async def adopt_settings(self, settings: Settings, keys: list[str]) -> None:
        """Install a document the daemon itself just wrote, and announce it.

        ``settings.set`` and the setup wizard's RPC equivalents persist first
        and then call this, so the in-memory state matches the file without a
        round trip back through disk.
        """
        hot_reload.rebind(self, settings)
        self.mark_settings_saved()
        if keys:
            await self.notify_settings_changed(keys)

    async def _announce_settings(self, keys: list[str]) -> None:
        """Re-sync the gateway and tell every client what changed."""
        if "gateway" in keys and self.gateway is not None:
            try:
                await self.gateway.sync_from_settings(self.settings)
            except Exception as exc:  # noqa: BLE001 - a dead platform is not fatal
                log.warning("could not apply the reloaded messenger settings: %s", exc)
        await self.notify_settings_changed(keys)

    async def notify_settings_changed(self, keys: list[str]) -> None:
        """Broadcast ``settings.changed`` to every authenticated connection."""
        hub = getattr(self, "hub", None)
        if hub is None:  # pragma: no cover - a core without a hub is a test double
            return
        params = SettingsChangedNotification(scope="global", keys=list(keys))
        try:
            await hub.notify("settings.changed", params.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - a dead socket must not break the reload
            log.debug("could not broadcast settings.changed", exc_info=True)


class EchoRequestParams(Payload):
    """Test-only params for ``system.echoRequest`` (``SNOWPEA_TEST=1``)."""

    sessionId: str = "test-session"
    tool: str = "shell"
    args: dict[str, Any] = Field(default_factory=dict)
    risk: str = "low"
    timeoutSec: int = 10
    scopeHint: str = "once"


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


async def info_handler(_conn: RpcConnection, _params: Empty, core: Core) -> InfoResult:
    """``system.info``."""
    status = core.lifecycle.status()
    return InfoResult(
        version=SERVER_VERSION,
        protocolVersion=PROTOCOL_VERSION,
        pid=core.pid,
        port=core.port,
        startedAt=core.started_at,
        home=str(core.paths.home),
        counters=dict(status["counters"]),
        lifecycle=LifecycleStatus(
            willExit=bool(status["willExit"]),
            reason=str(status["reason"]),
            secondsUntilExit=status["secondsUntilExit"],
            reasons=list(status.get("reasons") or []),
            summary=status.get("summary"),
        ),
        restartRequired=core.restart_required,
    )


async def health_handler(_conn: RpcConnection, _params: Empty, _core: Core) -> HealthResult:
    """``system.health``."""
    return HealthResult()


async def reload_settings_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> SettingsReloadResult:
    """``system.reloadSettings`` — pick up an outside edit to ``settings.json``.

    ``snowpea setup`` writes the file from its own process, so a daemon that is
    already running would otherwise keep serving the previous provider, model
    and tool configuration until it was restarted (CORE-settings-reload).
    """
    reloaded, keys = await core.reload_settings()
    return SettingsReloadResult(reloaded=reloaded, changedKeys=keys)


async def shutdown_handler(_conn: RpcConnection, _params: Empty, core: Core) -> Ok:
    """``system.shutdown`` — answer first, then tear the daemon down."""
    if core.request_shutdown is not None:
        core.request_shutdown("rpc")
    return Ok(ok=True)


def _not_implemented(name: str) -> Any:
    async def handler(_conn: RpcConnection, _params: Any, _core: Core) -> dict[str, Any]:
        raise RpcError(errors.NOT_IMPLEMENTED, f"{name} is not implemented yet")

    return handler


async def echo_request_handler(
    conn: RpcConnection, params: EchoRequestParams, _core: Core
) -> dict[str, Any]:
    """Test-only: make a server->client ``approval.request`` and return the answer."""
    request = ApprovalRequest(
        requestId=f"req-{int(time.time() * 1000)}",
        sessionId=params.sessionId,
        tool=params.tool,
        args=params.args,
        risk=params.risk,
        timeoutSec=params.timeoutSec,
        scopeHint="once",
    )
    return await conn.call(
        "approval.request", request.model_dump(mode="json"), timeout=float(params.timeoutSec)
    )


# ---------------------------------------------------------------------------
# permission.allowlist.* (contract §7)
# ---------------------------------------------------------------------------

#: The wire spells the global store ``always``; ``session`` has no persistent
#: home, so it is stored per project like a normal project entry.
WIRE_SCOPE: dict[str, AllowlistStore] = {
    "project": "project",
    "session": "project",
    "always": "global",
}
#: Inverse of :data:`WIRE_SCOPE` for reporting stored entries back.
STORE_SCOPE: dict[str, str] = {"project": "project", "global": "always"}


def _workdir_for(core: Core, conn: RpcConnection) -> Path | None:
    """Project directory the caller means.

    ``permission.allowlist.*`` carries no session id (the protocol models are
    fixed), so the workdir is taken from the session this connection started;
    failing that, from the only open session.
    """
    sessions = [
        session
        for session in (core.sessions.get(row.sessionId) for row in core.sessions.list())
        if session is not None
    ]
    mine = [session for session in sessions if session.origin_conn is conn]
    if mine:
        return Path(mine[-1].workdir)
    if len(sessions) == 1:
        return Path(sessions[0].workdir)
    return None


async def allowlist_add_handler(
    conn: RpcConnection, params: AllowlistAddParams, core: Core
) -> AllowlistAddResult:
    """``permission.allowlist.add`` — store a pattern, return its id."""
    store = WIRE_SCOPE.get(params.scope, "project")
    workdir = _workdir_for(core, conn)
    if store == "project" and workdir is None:
        raise RpcError(
            errors.INVALID_PARAMS,
            "a project allowlist entry needs an open session to locate the project",
        )
    try:
        pattern_id = core.allowlist.add(params.pattern, store, SHELL_TARGET, workdir=workdir)
    except (ValueError, re.error) as exc:
        raise RpcError(errors.INVALID_PARAMS, f"bad allowlist pattern: {exc}") from exc
    return AllowlistAddResult(patternId=pattern_id)


async def allowlist_list_handler(
    conn: RpcConnection, params: AllowlistListParams, core: Core
) -> AllowlistListResult:
    """``permission.allowlist.list`` — stored patterns, optionally by scope."""
    store = WIRE_SCOPE.get(params.scope, "project") if params.scope else None
    workdir = _workdir_for(core, conn)
    items = core.allowlist.list(store, workdir=workdir)
    return AllowlistListResult(
        patterns=[
            AllowlistPattern(
                patternId=item.id,
                pattern=item.pattern,
                scope=STORE_SCOPE[item.scope],  # type: ignore[arg-type]
            )
            for item in items
        ]
    )


async def allowlist_remove_handler(
    conn: RpcConnection, params: AllowlistRemoveParams, core: Core
) -> Ok:
    """``permission.allowlist.remove`` — delete a pattern by id."""
    removed = core.allowlist.remove(params.patternId, workdir=_workdir_for(core, conn))
    if not removed:
        raise RpcError(errors.NOT_FOUND, f"no allowlist pattern {params.patternId}")
    return Ok(ok=True)


def register_permission_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register the three ``permission.allowlist.*`` methods."""
    dispatcher.register("permission.allowlist.add", allowlist_add_handler)
    dispatcher.register("permission.allowlist.list", allowlist_list_handler)
    dispatcher.register("permission.allowlist.remove", allowlist_remove_handler)
    return dispatcher


# ---------------------------------------------------------------------------
# provider.* (M3 contract §1–§3)
# ---------------------------------------------------------------------------


def _persist_provider(core: Core, vendor: str, config: dict[str, Any]) -> None:
    """Merge ``config`` into the vendor's settings and write ``settings.json``."""
    try:
        core.providers.configure(vendor, config)
    except ProviderError as exc:
        raise RpcError(exc.code, str(exc)) from exc
    core.settings.providers.setdefault("default", vendor)
    try:
        core.settings.save(core.paths)
    except OSError as exc:  # pragma: no cover - disk failure
        raise RpcError(errors.INTERNAL, f"could not write settings.json: {exc}") from exc
    core.mark_settings_saved()


async def provider_configure_handler(
    _conn: RpcConnection, params: ProviderConfigureParams, core: Core
) -> Ok:
    """``provider.configure`` — store a vendor's key, base URL and model."""
    config = {
        key: value
        for key, value in (params.config or {}).items()
        if key
        in (
            "api_key",
            "base_url",
            "model",
            "models",
            "variant",
            # A named OpenAI-compatible server is described by its own block:
            # ``preset`` is what declares it one, ``label`` is what pickers
            # show, and ``context_window`` pins a window no table knows.
            "preset",
            "label",
            "context_window",
            # Whether this server's model may be sent images, and the opt-in
            # for a reasoning-effort field (CORE-vision, CORE-effort).
            "vision",
            "effort_param",
            "parallelToolCalls",
            "supportsParallelTools",
            "max_tokens",
            "thinking",
            "token",
            "oauth_token",
            "refresh_token",
            "auth_method",
            # The OAuth session record a browser login produces: a client must
            # be able to write it, and to clear it by sending ``None``.
            "access_token",
            "id_token",
            "expires_at",
            "account_id",
            "plan_type",
            "project_id",
        )
    }
    # A client that reads back a masked credential (e.g. from ``settings.get``
    # or a UI that echoes the vendor's current config) and sends it straight
    # into ``provider.configure`` must not be able to persist the literal
    # "***" mask as the real api_key / refresh_token / etc.
    try:
        reject_masked_secrets(config)
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc

    if config and all(value is None for value in config.values()):
        # A patch of nothing but ``None`` is a deliberate "forget this vendor",
        # not the empty call the guard below refuses.
        _persist_provider(core, params.vendor, config)
        return Ok(ok=True)
    if not config:
        raise RpcError(
            errors.INVALID_PARAMS,
            "provider.configure needs credentials, base_url, or model",
        )
    _persist_provider(core, params.vendor, config)
    return Ok(ok=True)


async def provider_remove_handler(
    _conn: RpcConnection, params: ProviderRemoveParams, core: Core
) -> Ok:
    """``provider.remove`` — forget a provider, usually a named local server."""
    vendor = (params.vendor or "").strip()
    if not vendor or vendor == "default":
        raise RpcError(errors.INVALID_PARAMS, "provider.remove needs a vendor")
    if not core.providers.remove(vendor):
        raise RpcError(errors.NOT_FOUND, f"no configured provider {vendor!r}")
    try:
        core.settings.save(core.paths)
    except OSError as exc:  # pragma: no cover - disk failure
        raise RpcError(errors.INTERNAL, f"could not write settings.json: {exc}") from exc
    core.mark_settings_saved()
    return Ok(ok=True)


async def provider_login_web_handler(
    conn: RpcConnection, params: ProviderLoginWebParams, core: Core
) -> ProviderLoginWebResult:
    """``provider.loginWeb`` — start the interactive login a vendor supports.

    ``method`` selects the flow (``browser_pkce`` / ``google_oauth`` for a
    browser, ``device_code`` / ``google_adc`` for a headless machine,
    ``oauth_pkce`` for OpenRouter); omitting it, or passing ``web``, picks the
    best one this machine can actually complete.

    Answers as soon as the consent URL / device code is known, then finishes in
    a background task that reports each phase via ``provider.loginProgress``
    and persists the credentials when they land.  Persisting goes through
    ``ProviderRegistry.configure``, so the ``None`` fields a login result
    carries **remove** the credentials of whatever auth method it replaced.
    """

    async def on_progress(fields: dict[str, Any]) -> None:
        hub = getattr(core, "hub", None)
        if hub is None:  # pragma: no cover - a core without a hub is a test double
            return
        try:
            await hub.notify("provider.loginProgress", fields)
        except Exception:  # noqa: BLE001 - a dead socket must not break the login
            log.debug("could not broadcast loginProgress", exc_info=True)

    started = await auth_web.login_started(
        params.vendor,
        params.method or None,
        on_prompt=lambda message: log.info("provider login: %s", message),
        open_browser=os.environ.get("SNOWPEA_TEST") != "1",
        on_progress=on_progress,
    )

    async def finish_and_persist() -> None:
        try:
            result = await started.finish()
        except Exception:  # noqa: BLE001 - already reported via loginProgress(failed)
            log.warning("provider login for %s failed", params.vendor, exc_info=True)
            return
        try:
            _persist_provider(core, result.vendor, result.credentials)
        except RpcError:
            log.warning(
                "could not persist credentials for %s", params.vendor, exc_info=True
            )

    conn.spawn(finish_and_persist())
    return ProviderLoginWebResult(
        ok=True,
        status="await_user",
        userCode=started.user_code,
        verificationUri=started.verification_uri,
        verificationUriComplete=started.verification_uri_complete,
        expiresInSec=started.expires_in_sec,
    )


def build_dispatcher(core: Core) -> RpcDispatcher:
    """Register ``system.*`` plus a ``not_implemented`` stub for every other method."""
    dispatcher = RpcDispatcher(core)
    dispatcher.register("system.hello", hello_handler)
    dispatcher.register("system.info", info_handler)
    dispatcher.register("system.health", health_handler)
    dispatcher.register("system.shutdown", shutdown_handler)
    dispatcher.register("system.reloadSettings", reload_settings_handler)
    register_session_handlers(dispatcher)
    from snowpea_core.server.file_handlers import register_file_handlers

    register_file_handlers(dispatcher)
    register_audio_handlers(dispatcher)
    register_skill_handlers(dispatcher)
    register_job_handlers(dispatcher)
    register_permission_handlers(dispatcher)
    register_agent_handlers(dispatcher)
    register_gateway_handlers(dispatcher)
    register_team_handlers(dispatcher)
    register_settings_handlers(dispatcher)
    register_lsp_handlers(dispatcher)
    register_mcp_handlers(dispatcher)
    register_update_handlers(dispatcher)
    dispatcher.register("provider.configure", provider_configure_handler)
    dispatcher.register("provider.remove", provider_remove_handler)
    dispatcher.register("provider.loginWeb", provider_login_web_handler)
    for name, method in PROTOCOL_METHODS.items():
        if method.direction != "c2s" or dispatcher.has(name):
            continue
        dispatcher.register(name, _not_implemented(name))
    if os.environ.get("SNOWPEA_TEST") == "1":
        dispatcher.register("system.echoRequest", echo_request_handler, EchoRequestParams)
    return dispatcher


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


class Daemon:
    """Owns the listening socket, ``daemon.json`` and the shutdown signal."""

    def __init__(
        self, port: int = 0, home: Path | str | None = None, token: str | None = None
    ) -> None:
        self._requested_port = port
        self._home = home
        self._preissued_token = token
        self._runner: web.AppRunner | None = None
        self._closed = asyncio.Event()
        self.core: Core | None = None
        self.app: web.Application | None = None
        self.shutdown_reason: str | None = None

    @property
    def port(self) -> int:
        return self.core.port if self.core else 0

    @property
    def token(self) -> str:
        return self.core.token if self.core else ""

    @property
    def paths(self) -> Paths:
        if self.core is None:
            raise RuntimeError("daemon not started")
        return self.core.paths

    async def start(self) -> Core:
        """Bind the socket, publish ``daemon.json`` and start the idle timer."""
        paths = Paths.create(self._home)
        _configure_logging(paths)
        settings = Settings.load(paths)
        if self._preissued_token:
            write_token(paths, self._preissued_token)
        token = ensure_token(paths)
        core = Core(settings=settings, paths=paths, token=token)
        core.settings_stamp = hot_reload.stamp(paths.settings_json)
        core.allowlist.bind(paths, settings)
        core.policy.bind(core.allowlist)
        core.approvals.allowlist = core.allowlist
        wire_core(core)
        core.lifecycle = Lifecycle(
            idle_timeout_sec=settings.daemon.idleTimeoutSec,
            on_idle=self._on_idle,
        )
        core.request_shutdown = self.request_shutdown
        core.gateway = GatewayRouter()
        core.gateway.bind_core(core)
        core.named_agents = NamedAgentRegistry(core)
        self.core = core

        dispatcher = build_dispatcher(core)
        self.app = create_app(dispatcher, daemon=self, core=core)
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, HOST, self._requested_port)
        await site.start()
        self._runner = runner
        core.port = _resolve_port(runner, self._requested_port)
        self._write_daemon_json()
        # Before anything reads the event log: a daemon that was killed
        # mid-turn left ``turn.started`` with no ``turn.done``, and every
        # client that replays that history would show a turn running forever
        # (CORE-dangling-turns).
        if core.store is not None:
            try:
                repaired = await core.store.repair_dangling_turns()
            except Exception:  # noqa: BLE001 - a bad row must not stop the daemon
                log.warning("could not repair unfinished turns", exc_info=True)
            else:
                if repaired:
                    log.info("closed %d turn(s) a previous run left open", len(repaired))
        # Before the gateway: a binding that targets a named agent's session
        # needs that session to exist again (M7 contract §6).
        await core.named_agents.restore()
        await core.gateway.restore()
        # A messenger the setup wizard enabled goes live here, without anyone
        # having to call ``gateway.bind`` by hand (CORE-gateway-autostart).
        await core.gateway.sync_from_settings(core.settings)
        await core.skills.reload()
        # Global ``$SNOWPEA_HOME/.mcp.json`` servers start at boot, so their
        # tools are in ``tool.list`` before anyone opens a session; a project's
        # own ``.mcp.json`` still starts at ``session.create`` (M15 §B5e).
        from snowpea_core.tools import mcp_client

        try:
            await mcp_client.sync_tools(core, None)
        except Exception:  # noqa: BLE001 - one bad server must not stop the daemon
            log.warning("could not start global MCP servers", exc_info=True)
        core.lifecycle.start()
        await start_scheduler(core)
        # Lazy and non-blocking: the answer is cached for 24h, so a daemon that
        # is restarted often still asks the network at most once a day.
        if update_mod.check_enabled(settings):
            core.update_check_task = asyncio.ensure_future(update_mod.background_check(core))
        log.info(
            "snowpea daemon listening on http://%s:%s (ws ws://%s:%s/ws)",
            HOST,
            core.port,
            HOST,
            core.port,
        )
        return core

    def _write_daemon_json(self) -> None:
        assert self.core is not None
        payload = {
            "port": self.core.port,
            "pid": self.core.pid,
            "token": self.core.token,
            "startedAt": self.core.started_at,
            "protocolVersion": PROTOCOL_VERSION,
            "version": __version__,
        }
        path = self.core.paths.daemon_json
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _remove_daemon_json(self) -> None:
        if self.core is None:
            return
        with contextlib.suppress(OSError):
            self.core.paths.daemon_json.unlink()

    async def _on_idle(self) -> None:
        self.request_shutdown("idle")

    def request_shutdown(self, reason: str = "requested") -> None:
        """Ask the daemon to stop; safe to call from a handler."""
        if self.shutdown_reason is None:
            self.shutdown_reason = reason
        self._closed.set()

    async def wait_closed(self) -> None:
        """Block until :meth:`request_shutdown` is called."""
        await self._closed.wait()

    async def stop(self) -> None:
        """Close sockets, drop ``daemon.json`` and release the port."""
        self.request_shutdown(self.shutdown_reason or "requested")
        if self.core is not None:
            # Flip this first: any turn still in flight (and anything it
            # schedules, like the memory nudge) must see shutdown has begun
            # before we start tearing down the services it depends on
            # (CORE-memory-race).
            self.core.stopping = True
            # Close every turn still in flight while the hub and the store are
            # both still up.  ``close_all`` below cancels the turn tasks, and a
            # turn cancelled during shutdown skips its own final write
            # (CORE-session-race) — without this the thread would replay as
            # permanently running (CORE-dangling-turns).
            with contextlib.suppress(Exception):
                await self.core.sessions.finish_open_turns()
            # Cancel and close every live session's turn *before* the session
            # store, memory and gateway are torn down below: a turn task left
            # running past that point can still land its final ``turn.done``
            # write on an already-closed store (CORE-session-race, the
            # session-store analogue of CORE-memory-race).
            await self.core.sessions.close_all()
            # The update watcher polls rather than blocking a thread, so
            # cancelling it returns straight away (CORE-update).
            for attribute in ("update_task", "update_check_task"):
                task = getattr(self.core, attribute)
                if task is None:
                    continue
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                setattr(self.core, attribute, None)
            await self.core.lifecycle.stop()
            await stop_scheduler(self.core)
            if self.core.lsp is not None:
                # Language servers are children of this process; leaving them
                # running would leak a gopls per daemon restart.
                await self.core.lsp.shutdown()
            if self.core.gateway is not None:
                await self.core.gateway.stop()
            if self.core.named_agents is not None:
                self.core.named_agents.close()
        if self.app is not None:
            sockets: set[web.WebSocketResponse] = self.app[SOCKETS_KEY]
            connections: set[RpcConnection] = self.app[CONNECTIONS_KEY]
            for ws in list(sockets):
                with contextlib.suppress(Exception):
                    await ws.close()
            for conn in list(connections):
                await conn.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self.core is not None and self.core.store is not None:
            with contextlib.suppress(Exception):
                self.core.store.close()
        if self.core is not None and self.core.memory is not None:
            with contextlib.suppress(Exception):
                await self.core.memory.close()
        await _close_tool_subprocesses()
        self._remove_daemon_json()
        log.info("snowpea daemon stopped (%s)", self.shutdown_reason)


async def _close_tool_subprocesses() -> None:
    """Stop the browsers and MCP servers the tool layer started (M2 §4, §6).

    Both hold real child processes, so leaving them behind would outlive the
    daemon that spawned them.
    """
    from snowpea_core.tools import browser_providers, mcp_client

    for provider in browser_providers.all_providers():
        with contextlib.suppress(Exception):
            await provider.close()
    with contextlib.suppress(Exception):
        await mcp_client.MANAGER.close_all()


def _resolve_port(runner: web.AppRunner, requested: int) -> int:
    for address in runner.addresses:
        if isinstance(address, tuple) and len(address) >= 2:
            return int(address[1])
    return requested


def _configure_logging(paths: Paths) -> None:
    logger = logging.getLogger("snowpea")
    target = str(paths.daemon_log)
    for handler in logger.handlers:
        if getattr(handler, "baseFilename", None) == target:
            return
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _install_signal_handlers(daemon: Daemon) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, daemon.request_shutdown, f"signal:{sig.name}")


async def run_daemon(
    port: int = 0, home: Path | str | None = None, token: str | None = None
) -> None:
    """Run the daemon until a signal, ``system.shutdown`` or the idle timeout."""
    daemon = Daemon(port=port, home=home, token=token)
    await daemon.start()
    _install_signal_handlers(daemon)
    try:
        await daemon.wait_closed()
    finally:
        await daemon.stop()


__all__ = [
    "HOST",
    "Core",
    "Daemon",
    "build_dispatcher",
    "register_gateway_handlers",
    "register_permission_handlers",
    "run_daemon",
]
