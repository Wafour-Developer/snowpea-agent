"""RPC handlers for ``mcp.*`` (M14 contract §3, additive to protocol 1.5.0).

Seven methods over one idea: ``.mcp.json`` is the source of truth, the running
:class:`~snowpea_core.tools.mcp_client.McpManager` is a cache of it, and every
write here keeps the two in step without a daemon restart — the old process is
stopped, its tools leave the registry, the new entry is registered lazily, and
``mcp.changed`` goes out to every connection the way ``settings.changed`` does.

Registered like ``lsp_handlers``.  Secrets never leave: ``env`` and ``headers``
are reported as key names only, and no handler logs a value.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from snowpea_core.config.settings import Settings
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    McpAddParams,
    McpAddResult,
    McpCatalogEntry,
    McpCatalogResult,
    McpChangedNotification,
    McpEntryFields,
    McpListParams,
    McpListResult,
    McpReloadParams,
    McpReloadResult,
    McpRemoveParams,
    McpScope,
    McpServerInfo,
    McpState,
    McpTestParams,
    McpTestResult,
    McpToolInfo,
    McpUpdateParams,
    Ok,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.tools import mcp_catalog, mcp_client, mcp_config, mcp_security
from snowpea_core.tools.mcp_client import MANAGER, McpServer, McpServerConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = (
    "mcp.list",
    "mcp.add",
    "mcp.remove",
    "mcp.update",
    "mcp.test",
    "mcp.reload",
    "mcp.catalog",
)

#: Hard cap on a probe, draft or saved (M14 §3, AC-49).
TEST_TIMEOUT = 20.0

#: Last known scope per server name, so the ``mcp.changed`` broadcaster can
#: label a transition that reaches it from deep inside the client.
_SCOPES: dict[str, McpScope] = {}


# ---------------------------------------------------------------------------
# reading the declarations
# ---------------------------------------------------------------------------


@dataclass
class Declared:
    """One server as some scope declares it."""

    config: McpServerConfig
    scope: McpScope
    entry: dict[str, Any]
    plugin: str | None = None

    @property
    def name(self) -> str:
        return self.config.name


def workdir_of(core: Core, session_id: str | None, workdir: str | None) -> Path:
    """The project directory a call means: its session's, its own, or the daemon's."""
    if session_id:
        session = core.sessions.get(session_id)
        if session is not None:
            return Path(session.workdir)
    if workdir:
        return Path(workdir).expanduser()
    return Path.cwd()


def declared(core: Core, workdir: Path | str | None) -> dict[str, Declared]:
    """Every configured server, project entries winning over global ones.

    The precedence is the one :func:`mcp_client.sync_tools` already applies —
    settings, then plugins, then ``$SNOWPEA_HOME``, then the workdir — so what
    ``mcp.list`` shows is what the daemon would actually start.
    """
    found: dict[str, Declared] = {}

    def take(name: str, entry: dict[str, Any], scope: McpScope, plugin: str | None = None) -> None:
        config = McpServerConfig.parse(str(name), entry)
        if config is not None:
            found[config.name] = Declared(
                config=config, scope=scope, entry=dict(entry), plugin=plugin
            )

    inline = getattr(getattr(core.settings, "mcp", None), "servers", None) or {}
    if isinstance(inline, dict):
        for name, entry in inline.items():
            if isinstance(entry, dict):
                take(name, entry, "settings")
    loader = getattr(core, "skills", None)
    plugins = getattr(loader, "mcp_servers", None) or {}
    attribution = getattr(loader, "mcp_server_plugins", None) or {}
    for name, entry in plugins.items():
        if isinstance(entry, dict):
            take(name, entry, "plugin", attribution.get(str(name)))
    for scope, base in (("global", core.paths.home), ("project", workdir)):
        if base is None:
            continue
        for name, entry in mcp_config.read_servers(Path(base) / mcp_client.CONFIG_NAME).items():
            take(name, entry, scope)  # type: ignore[arg-type]

    _SCOPES.update({name: row.scope for name, row in found.items()})
    return found


def info_of(core: Core, row: Declared) -> McpServerInfo:
    """One ``mcp.list`` row; env and header values never appear here (AC-51)."""
    server = MANAGER.get(row.name)
    state: McpState = "stopped"
    error: str | None = None
    tools: list[McpToolInfo] = []
    if server is not None and server.config == row.config:
        state = server.state
        error = server.error
        if state == "ready":
            tools = [
                McpToolInfo(name=str(spec["name"]), description=str(spec.get("description") or ""))
                for spec in server.visible_tools()
            ]
    config = row.config
    return McpServerInfo(
        name=config.name,
        scope=row.scope,
        transport=config.transport,  # type: ignore[arg-type]
        command=config.command,
        args=list(config.args),
        url=config.url,
        envKeys=sorted(config.env),
        headerKeys=sorted(config.headers),
        cwd=config.cwd,
        permission=mcp_client.permission_for(core, config.name),
        disabled=config.disabled,
        state=state,
        error=error,
        toolCount=len(tools),
        tools=tools,
        plugin=row.plugin,
        timeoutSec=config.timeout_sec,
        toolTimeoutSec=config.tool_timeout_sec,
        toolsInclude=list(config.tools_include),
        toolsExclude=list(config.tools_exclude),
    )


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def notify_changed(
    core: Core,
    name: str,
    *,
    scope: McpScope | None = None,
    state: McpState,
    tool_count: int = 0,
    error: str | None = None,
    removed: bool = False,
) -> None:
    """Broadcast ``mcp.changed`` to every authenticated connection."""
    hub = getattr(core, "hub", None)
    if hub is None:  # pragma: no cover - a core without a hub is a test double
        return
    payload = McpChangedNotification(
        name=name,
        scope=scope or _SCOPES.get(name, "project"),
        state=state,
        toolCount=tool_count,
        error=error,
        removed=removed,
    )
    try:
        await hub.notify("mcp.changed", payload.model_dump(mode="json"))
    except Exception:  # noqa: BLE001 - a dead socket must not break a write
        return


def state_broadcaster(core: Core) -> Any:
    """A :data:`mcp_client.MANAGER` listener that turns a transition into an event."""

    async def listen(server: McpServer) -> None:
        await notify_changed(
            core,
            server.config.name,
            state=server.state,
            tool_count=len(server.visible_tools()) if server.state == "ready" else 0,
            error=server.error,
        )

    return listen


# ---------------------------------------------------------------------------
# entries
# ---------------------------------------------------------------------------


def _invalid(exc: mcp_config.McpConfigError) -> RpcError:
    return RpcError(errors.MCP_INVALID, f"{exc.field}: {exc.message}", {"field": exc.field})


def entry_from(fields: McpEntryFields, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge the params' set keys onto ``base``; ``None`` means "leave alone"."""
    entry: dict[str, Any] = dict(base or {})
    simple = {
        "type": fields.type,
        "command": fields.command,
        "args": fields.args,
        "env": fields.env,
        "url": fields.url,
        "headers": fields.headers,
        "cwd": fields.cwd,
        "disabled": fields.disabled,
        "timeoutSec": fields.timeoutSec,
        "toolTimeoutSec": fields.toolTimeoutSec,
    }
    for key, value in simple.items():
        if value is not None:
            entry[key] = value
    if fields.command is not None:
        entry.pop("url", None)
        entry.pop("headers", None)
    if fields.url is not None:
        entry.pop("command", None)
        entry.pop("args", None)
        entry.pop("cwd", None)
    tools = dict(entry.get("tools") or {}) if isinstance(entry.get("tools"), dict) else {}
    if fields.toolsInclude is not None:
        tools["include"] = list(fields.toolsInclude)
    if fields.toolsExclude is not None:
        tools["exclude"] = list(fields.toolsExclude)
    tools = {key: value for key, value in tools.items() if value}
    if tools:
        entry["tools"] = tools
    else:
        entry.pop("tools", None)
    return entry


def preset_entry(preset: str | None) -> dict[str, Any]:
    """The catalog entry ``preset`` names, or ``{}``; an unknown id is invalid."""
    if not preset:
        return {}
    row = mcp_catalog.get(preset)
    if row is None:
        raise RpcError(
            errors.MCP_INVALID,
            f"preset: '{preset}' is not in the catalog ({', '.join(mcp_catalog.ids())})",
            {"field": "preset"},
        )
    return dict(row["entry"])


async def set_permission(core: Core, name: str, permission: str) -> None:
    """Persist ``mcp.permissions[name]``; the tag lives in settings, not in the file."""
    current = core.settings.model_dump(mode="json")
    merged = dict(current)
    mcp = dict(merged.get("mcp") or {})
    permissions = dict(mcp.get("permissions") or {})
    permissions[name] = permission
    mcp["permissions"] = permissions
    merged["mcp"] = mcp
    try:
        updated = Settings.model_validate(merged)
    except ValidationError as exc:  # pragma: no cover - the tag is a Literal already
        raise RpcError(errors.MCP_INVALID, f"permission: {exc}", {"field": "permission"}) from exc
    updated.save(core.paths)
    await core.adopt_settings(updated, ["mcp"])


# ---------------------------------------------------------------------------
# starting, stopping, probing
# ---------------------------------------------------------------------------


async def apply(core: Core, row: Declared) -> McpServerInfo:
    """Make the running daemon match ``row``: stop the old, register the new."""
    await MANAGER.remove(row.name)
    mcp_client.drop_tools(core, row.name)
    if not row.config.disabled:
        await mcp_client.register_config(core, row.config)
    info = info_of(core, row)
    await notify_changed(
        core,
        row.name,
        scope=row.scope,
        state=info.state,
        tool_count=info.toolCount,
        error=info.error,
    )
    return info


async def probe(config: McpServerConfig) -> tuple[bool, list[McpToolInfo], str | None, int]:
    """Start a throwaway copy of ``config``, list its tools, and always stop it.

    The child is closed in a ``finally`` even when the wait times out, so
    ``mcp.test`` on a server that hangs leaves nothing running (AC-49).
    """
    capped = replace(config, timeout_sec=min(config.timeout_sec or TEST_TIMEOUT, TEST_TIMEOUT))
    server = McpServer(capped, announce=False)
    started = time.monotonic()
    ok = False
    error: str | None = None
    tools: list[McpToolInfo] = []
    try:
        await asyncio.wait_for(server.start(), TEST_TIMEOUT)
        tools = [
            McpToolInfo(name=str(spec["name"]), description=str(spec.get("description") or ""))
            for spec in server.visible_tools()
        ]
        ok = True
    except TimeoutError:
        error = f"{config.name}: did not answer within {TEST_TIMEOUT:g}s"
    except Exception as exc:  # noqa: BLE001 - a broken server must not raise (M14 §3)
        error = str(exc) or type(exc).__name__
    finally:
        try:
            await asyncio.wait_for(server.close(), 10.0)
        except (TimeoutError, Exception):  # noqa: BLE001 - best effort teardown
            pass
    elapsed = int((time.monotonic() - started) * 1000)
    return ok, tools, error, elapsed


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


async def mcp_list_handler(
    _conn: RpcConnection | None, params: McpListParams, core: Core
) -> McpListResult:
    """``mcp.list`` — every configured server, whatever scope declares it."""
    workdir = workdir_of(core, params.sessionId, params.workdir)
    rows = declared(core, workdir)
    return McpListResult(servers=[info_of(core, rows[name]) for name in sorted(rows)])


async def mcp_add_handler(
    _conn: RpcConnection | None, params: McpAddParams, core: Core
) -> McpAddResult:
    """``mcp.add`` — validate, probe, write the file, then start the server."""
    try:
        name = mcp_config.validate_name(params.name)
        entry = mcp_config.validate_entry(entry_from(params, preset_entry(params.preset)))
    except mcp_config.McpConfigError as exc:
        raise _invalid(exc) from exc

    warnings = mcp_security.findings(entry, name)
    if warnings and not params.force:
        raise RpcError(
            errors.MCP_UNSAFE,
            "; ".join(warnings) + " — pass force to add it anyway",
            {"findings": warnings},
        )

    workdir = workdir_of(core, params.sessionId, params.workdir)
    try:
        path = mcp_config.config_path(params.scope, core.paths.home, workdir)
    except mcp_config.McpConfigError as exc:
        raise _invalid(exc) from exc
    if params.name in mcp_config.read_servers(path) and not params.force:
        raise RpcError(
            errors.MCP_EXISTS, f"'{name}' is already declared in {path}; pass force to replace it"
        )

    config = McpServerConfig.parse(name, entry)
    assert config is not None  # validate_entry guarantees a command or a url
    tools: list[McpToolInfo] = []
    if params.test and not config.disabled:
        ok, tools, error, _elapsed = await probe(config)
        if not ok:
            raise RpcError(
                errors.MCP_START_FAILED,
                f"{name} did not start: {error}; nothing was written",
                {"error": error},
            )

    mcp_config.save_entry(path, name, entry)
    if params.permission is not None:
        await set_permission(core, name, params.permission)
    row = Declared(config=config, scope=params.scope, entry=entry)
    _SCOPES[name] = params.scope
    info = await apply(core, row)
    return McpAddResult(
        ok=True,
        path=str(path),
        state=info.state,
        tools=tools or info.tools,
        warnings=warnings,
        error=info.error,
    )


def _writable(core: Core, name: str, scope: str, workdir: Path) -> Path:
    """The file ``scope`` writes, refusing the read-only scopes (AC-50)."""
    rows = declared(core, workdir)
    row = rows.get(name)
    if row is not None and row.scope in ("plugin", "settings"):
        detail = f" (from plugin {row.plugin})" if row.plugin else ""
        raise RpcError(
            errors.MCP_READ_ONLY,
            f"'{name}' is declared by the {row.scope} scope{detail} and cannot be edited here",
        )
    try:
        return mcp_config.config_path(scope, core.paths.home, workdir)
    except mcp_config.McpConfigError as exc:
        raise _invalid(exc) from exc


async def mcp_remove_handler(
    _conn: RpcConnection | None, params: McpRemoveParams, core: Core
) -> Ok:
    """``mcp.remove`` — drop the entry, stop the server, drop its tools."""
    workdir = workdir_of(core, params.sessionId, params.workdir)
    path = _writable(core, params.name, params.scope, workdir)
    if not mcp_config.delete_entry(path, params.name):
        raise RpcError(errors.MCP_NOT_FOUND, f"'{params.name}' is not declared in {path}")
    await MANAGER.remove(params.name)
    mcp_client.drop_tools(core, params.name)
    await notify_changed(
        core, params.name, scope=params.scope, state="stopped", removed=True
    )
    return Ok(ok=True)


async def mcp_update_handler(
    _conn: RpcConnection | None, params: McpUpdateParams, core: Core
) -> Ok:
    """``mcp.update`` — merge a patch into an entry and restart the server."""
    workdir = workdir_of(core, params.sessionId, params.workdir)
    path = _writable(core, params.name, params.scope, workdir)
    current = mcp_config.read_servers(path).get(params.name)
    if current is None:
        raise RpcError(errors.MCP_NOT_FOUND, f"'{params.name}' is not declared in {path}")
    try:
        entry = mcp_config.validate_entry(entry_from(params.patch, current))
    except mcp_config.McpConfigError as exc:
        raise _invalid(exc) from exc
    mcp_config.save_entry(path, params.name, entry)
    if params.patch.permission is not None:
        await set_permission(core, params.name, params.patch.permission)
    config = McpServerConfig.parse(params.name, entry)
    assert config is not None
    _SCOPES[params.name] = params.scope
    await apply(core, Declared(config=config, scope=params.scope, entry=entry))
    return Ok(ok=True)


async def mcp_test_handler(
    _conn: RpcConnection | None, params: McpTestParams, core: Core
) -> McpTestResult:
    """``mcp.test`` — probe a saved server or an unsaved draft; never raises for it."""
    workdir = workdir_of(core, params.sessionId, params.workdir)
    if params.name:
        row = declared(core, workdir).get(params.name)
        if row is None:
            raise RpcError(errors.MCP_NOT_FOUND, f"'{params.name}' is not a configured server")
        config = row.config
    else:
        try:
            entry = mcp_config.validate_entry(entry_from(params))
        except mcp_config.McpConfigError as exc:
            raise _invalid(exc) from exc
        parsed = McpServerConfig.parse("draft", entry)
        assert parsed is not None
        config = parsed
    ok, tools, error, elapsed = await probe(config)
    return McpTestResult(
        ok=ok, state="ready" if ok else "error", tools=tools, error=error, elapsedMs=elapsed
    )


async def mcp_reload_handler(
    _conn: RpcConnection | None, params: McpReloadParams, core: Core
) -> McpReloadResult:
    """``mcp.reload`` — restart one server, or re-read every declaration."""
    workdir = workdir_of(core, params.sessionId, params.workdir)
    rows = declared(core, workdir)
    if params.name:
        row = rows.get(params.name)
        if row is None:
            raise RpcError(errors.MCP_NOT_FOUND, f"'{params.name}' is not a configured server")
        await apply(core, row)
        return McpReloadResult(ok=True, servers=[params.name])
    for name in list(rows):
        await apply(core, rows[name])
    return McpReloadResult(ok=True, servers=sorted(rows))


async def mcp_catalog_handler(
    _conn: RpcConnection | None, _params: Empty, _core: Core
) -> McpCatalogResult:
    """``mcp.catalog`` — the curated presets, in display order."""
    return McpCatalogResult(
        entries=[
            McpCatalogEntry(
                id=str(row["id"]),
                label=str(row["label"]),
                description=str(row.get("description") or ""),
                transport=row["transport"],
                entry=dict(row["entry"]),
                needs=list(row.get("needs") or []),
                homepage=str(row.get("homepage") or ""),
            )
            for row in mcp_catalog.CATALOG
        ]
    )


def register_mcp_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS` and the state listener."""
    dispatcher.register("mcp.list", mcp_list_handler)
    dispatcher.register("mcp.add", mcp_add_handler)
    dispatcher.register("mcp.remove", mcp_remove_handler)
    dispatcher.register("mcp.update", mcp_update_handler)
    dispatcher.register("mcp.test", mcp_test_handler)
    dispatcher.register("mcp.reload", mcp_reload_handler)
    dispatcher.register("mcp.catalog", mcp_catalog_handler)
    # One listener per daemon: replacing the list keeps a previous daemon in
    # the same process (the test suite starts several) from broadcasting onto
    # a core that is already gone.
    MANAGER.listeners = [state_broadcaster(dispatcher.core)]
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "TEST_TIMEOUT",
    "Declared",
    "apply",
    "declared",
    "entry_from",
    "info_of",
    "mcp_add_handler",
    "mcp_catalog_handler",
    "mcp_list_handler",
    "mcp_reload_handler",
    "mcp_remove_handler",
    "mcp_test_handler",
    "mcp_update_handler",
    "notify_changed",
    "probe",
    "register_mcp_handlers",
    "workdir_of",
]
