"""RPC handlers for ``mcp.*`` (M14 contract §3, additive to protocol 1.5.0).

Seven methods over one idea: ``.mcp.json`` is the source of truth, the running
:class:`~snowpea_core.tools.mcp_client.McpManager` is a cache of it, and every
write here keeps the two in step without a daemon restart — the old process is
stopped, its tools leave the registry, the new entry is registered lazily, and
``mcp.changed`` goes out to every connection the way ``settings.changed`` does.

Registered like ``lsp_handlers``.  Secrets never leave: ``env`` and ``headers``
are reported as key names only, and no handler logs a value.

``mcp.authorize``, ``mcp.authorize.complete`` and ``mcp.deauthorize`` (1.34.0)
run the OAuth sign-in of :mod:`snowpea_core.tools.mcp_oauth`; its tokens stay
in ``$SNOWPEA_HOME/mcp-oauth.json`` and only ``auth.state`` is ever reported.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from snowpea_core.config.settings import Settings
from snowpea_core.providers.auth_web import new_pkce_pair
from snowpea_core.providers.oauth_callback import OAuthCallbackServer
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    McpAddParams,
    McpAddResult,
    McpAuthDoneNotification,
    McpAuthInfo,
    McpAuthorizeCompleteParams,
    McpAuthorizeCompleteResult,
    McpAuthorizeParams,
    McpAuthorizeResult,
    McpCatalogEntry,
    McpCatalogResult,
    McpChangedNotification,
    McpDeauthorizeParams,
    McpDeauthorizeResult,
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
from snowpea_core.tools import mcp_catalog, mcp_client, mcp_config, mcp_oauth, mcp_security
from snowpea_core.tools.mcp_client import MANAGER, McpServer, McpServerConfig

log = logging.getLogger("snowpea.server.mcp")

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
    "mcp.authorize",
    "mcp.authorize.complete",
    "mcp.deauthorize",
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
    #: Another scope declares the same name and wins (1.33.0).
    shadowed: bool = False

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


def declarations(core: Core, workdir: Path | str | None) -> list[Declared]:
    """Every declaration of every server, lowest precedence first.

    The order is the one :func:`mcp_client.sync_tools` already applies —
    settings, then plugins, then ``$SNOWPEA_HOME``, then the workdir — so a
    later row with the same name is the one the daemon would actually start.
    """
    found: list[Declared] = []

    def take(name: str, entry: dict[str, Any], scope: McpScope, plugin: str | None = None) -> None:
        config = McpServerConfig.parse(str(name), entry, scope)
        if config is not None:
            found.append(Declared(config=config, scope=scope, entry=dict(entry), plugin=plugin))

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
    return found


def declared(core: Core, workdir: Path | str | None) -> dict[str, Declared]:
    """Every configured server, project entries winning over global ones."""
    found = {row.name: row for row in declarations(core, workdir)}
    _SCOPES.update({name: row.scope for name, row in found.items()})
    return found


def auth_of(
    home: Path | str | None, config: McpServerConfig, server: McpServer | None
) -> McpAuthInfo:
    """``McpServerInfo.auth`` (1.34.0): what is stored and what the server said.

    Reads only whether a record exists for this name and URL; no token leaves.
    """
    client_id = config.oauth.get("clientId") or None
    if not config.url or config.transport == "stdio":
        return McpAuthInfo(clientId=client_id)
    if server is not None and server.needs_auth:
        return McpAuthInfo(required=True, state="needs_auth", clientId=client_id)
    if home is not None and mcp_oauth.TokenStore(home).get(config.name, config.url) is not None:
        return McpAuthInfo(required=True, state="ok", clientId=client_id)
    return McpAuthInfo(clientId=client_id)


def info_of(core: Core, row: Declared) -> McpServerInfo:
    """One ``mcp.list`` row; env and header values never appear here (AC-51)."""
    server = MANAGER.get(row.name)
    state: McpState = "stopped"
    error: str | None = None
    tools: list[McpToolInfo] = []
    current = server if server is not None and server.config == row.config else None
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
        shadowed=row.shadowed,
        auth=auth_of(core.paths.home, config, current),
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
    auth: McpAuthInfo | None = None,
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
        auth=auth,
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
            auth=auth_of(server.home_dir, server.config, server),
        )

    return listen


# ---------------------------------------------------------------------------
# entries
# ---------------------------------------------------------------------------


def _invalid(exc: mcp_config.McpConfigError) -> RpcError:
    return RpcError(errors.MCP_INVALID, f"{exc.field}: {exc.message}", {"field": exc.field})


def entry_from(
    fields: McpEntryFields, base: dict[str, Any] | None = None, *, merge: bool = False
) -> dict[str, Any]:
    """Merge the params' set keys onto ``base``; ``None`` means "leave alone".

    ``merge`` (``mcp.update``, 1.33.0) merges ``env`` and ``headers`` key by key
    instead of replacing them, so one secret can change without retyping the
    others — except that a new ``url`` with no ``headers`` drops the saved ones,
    so credentials for one endpoint are never sent to another.  An empty
    ``toolsInclude``/``toolsExclude`` clears that filter.
    """
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
        "oauth": fields.oauth.model_dump(exclude_none=True) if fields.oauth is not None else None,
    }
    for key, value in simple.items():
        if value is None:
            continue
        if merge and isinstance(value, dict) and isinstance(entry.get(key), dict):
            value = {**entry[key], **value}
        entry[key] = value
    if fields.command is not None:
        entry.pop("url", None)
        entry.pop("headers", None)
        entry.pop("oauth", None)
    if merge and fields.url is not None and fields.headers is None:
        if fields.url != (base or {}).get("url"):
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


#: ``mcp.update {unset}`` keys that live under the entry's ``tools`` object.
_TOOLS_KEYS = {"toolsInclude": "include", "toolsExclude": "exclude"}


def without(
    entry: dict[str, Any],
    unset: Sequence[str] | None,
    env_remove: Sequence[str] | None,
    headers_remove: Sequence[str] | None,
) -> dict[str, Any]:
    """``entry`` with the keys ``mcp.update`` was told to clear (1.33.0)."""
    out = dict(entry)
    for key in unset or []:
        if key in _TOOLS_KEYS:
            tools = dict(out.get("tools") or {}) if isinstance(out.get("tools"), dict) else {}
            tools.pop(_TOOLS_KEYS[key], None)
            if tools:
                out["tools"] = tools
            else:
                out.pop("tools", None)
        else:
            out.pop(key, None)
    for key, names in (("env", env_remove), ("headers", headers_remove)):
        if not names or not isinstance(out.get(key), dict):
            continue
        kept = {name: value for name, value in out[key].items() if name not in set(names)}
        if kept:
            out[key] = kept
        else:
            out.pop(key, None)
    return out


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
        auth=info.auth,
    )
    return info


async def reconcile(core: Core, name: str, workdir: Path, before: Declared | None) -> None:
    """Make the runtime run whatever declaration of ``name`` wins now (1.33.0).

    A file write can touch a copy that is not in effect — the global entry a
    project one shadows.  Then the winner is unchanged and so is the running
    server.  When the winner goes, the copy it shadowed starts; when the last
    declaration goes, the server stops.
    """
    after = declared(core, workdir).get(name)
    if after is None:
        if before is not None:
            await MANAGER.remove(name)
            mcp_client.drop_tools(core, name)
            await notify_changed(core, name, scope=before.scope, state="stopped", removed=True)
        return
    if before is not None and after.scope == before.scope and after.config == before.config:
        return
    await apply(core, after)


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
    """``mcp.list`` — every configured server, whatever scope declares it.

    With ``includeShadowed``, a declaration another scope overrides follows
    its winner as ``shadowed: true`` (1.33.0), highest precedence first.
    """
    workdir = workdir_of(core, params.sessionId, params.workdir)
    every = declarations(core, workdir)
    rows = {row.name: row for row in every}
    _SCOPES.update({name: row.scope for name, row in rows.items()})
    servers: list[McpServerInfo] = []
    for name in sorted(rows):
        servers.append(info_of(core, rows[name]))
        if not params.includeShadowed:
            continue
        for row in reversed(every):
            if row.name == name and row is not rows[name]:
                servers.append(info_of(core, replace(row, shadowed=True)))
    return McpListResult(servers=servers)


async def mcp_add_handler(
    _conn: RpcConnection | None, params: McpAddParams, core: Core
) -> McpAddResult:
    """``mcp.add`` — validate, probe, write the file, then start the server."""
    try:
        name = mcp_config.validate_name(params.name)
        entry = mcp_config.validate_entry(entry_from(params, preset_entry(params.preset)))
    except mcp_config.McpConfigError as exc:
        raise _invalid(exc) from exc

    # ``force`` is both answers at once; each refusal has its own (1.33.0).  The
    # messages still say ``force``: the agent's ``mcp_add`` tool reads them,
    # and what a model is sent must not change for a client's sake.
    accept_findings = params.force or params.acceptFindings
    replace_existing = params.force or params.replace
    warnings = mcp_security.findings(entry, name)
    if warnings and not accept_findings:
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
    if params.name in mcp_config.read_servers(path) and not replace_existing:
        raise RpcError(
            errors.MCP_EXISTS, f"'{name}' is already declared in {path}; pass force to replace it"
        )

    config = McpServerConfig.parse(name, entry, params.scope)
    assert config is not None  # validate_entry guarantees a command or a url
    tools: list[McpToolInfo] = []
    MANAGER.bind(core)
    if params.test and not config.disabled:
        ok, tools, error, _elapsed = await probe(config)
        # A server that wants a sign-in is saved anyway (1.34.0): there is no
        # token before the entry exists, and mcp.authorize needs the entry.
        if not ok and not needs_auth(error):
            raise RpcError(
                errors.MCP_START_FAILED,
                f"{name} did not start: {error}; nothing was written",
                {"error": error},
            )

    before = declared(core, workdir).get(name)
    mcp_config.save_entry(path, name, entry)
    if params.permission is not None:
        await set_permission(core, name, params.permission)
    await reconcile(core, name, workdir, before)
    info = info_of(core, Declared(config=config, scope=params.scope, entry=entry))
    return McpAddResult(
        ok=True,
        path=str(path),
        state=info.state,
        tools=tools or info.tools,
        warnings=warnings,
        error=info.error,
        auth=info.auth,
    )


def scope_for(core: Core, name: str, scope: str | None, workdir: Path) -> str:
    """``scope`` as given, else the writable scope that declares ``name``.

    ``project`` wins where both declare it, as it does when servers start;
    an undeclared name falls back to ``global``.
    """
    if scope:
        return scope
    row = declared(core, workdir).get(name)
    return row.scope if row is not None and row.scope in ("project", "global") else "global"


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
    scope = scope_for(core, params.name, params.scope, workdir)
    path = _writable(core, params.name, scope, workdir)
    before = declared(core, workdir).get(params.name)
    if not mcp_config.delete_entry(path, params.name):
        raise RpcError(errors.MCP_NOT_FOUND, f"'{params.name}' is not declared in {path}")
    await reconcile(core, params.name, workdir, before)
    return Ok(ok=True)


async def mcp_update_handler(
    _conn: RpcConnection | None, params: McpUpdateParams, core: Core
) -> Ok:
    """``mcp.update`` — merge a patch into an entry and restart the server."""
    workdir = workdir_of(core, params.sessionId, params.workdir)
    scope = scope_for(core, params.name, params.scope, workdir)
    path = _writable(core, params.name, scope, workdir)
    current = mcp_config.read_servers(path).get(params.name)
    if current is None:
        raise RpcError(errors.MCP_NOT_FOUND, f"'{params.name}' is not declared in {path}")
    merged = without(
        entry_from(params.patch, current, merge=True),
        params.unset,
        params.envRemove,
        params.headersRemove,
    )
    try:
        entry = mcp_config.validate_entry(merged)
    except mcp_config.McpConfigError as exc:
        raise _invalid(exc) from exc
    # The same check as mcp.add, on what would be saved (1.33.0).  Only a
    # finding the patch brings in asks again: disabling an entry that was
    # added with acceptFindings does not.
    warnings = mcp_security.findings(entry, params.name)
    known = set(mcp_security.findings(current, params.name))
    accepted = params.force or params.acceptFindings
    if any(item not in known for item in warnings) and not accepted:
        raise RpcError(
            errors.MCP_UNSAFE,
            "; ".join(warnings) + " — pass force to add it anyway",
            {"findings": warnings},
        )
    before = declared(core, workdir).get(params.name)
    mcp_config.save_entry(path, params.name, entry)
    if params.patch.permission is not None:
        await set_permission(core, params.name, params.patch.permission)
    await reconcile(core, params.name, workdir, before)
    return Ok(ok=True)


async def mcp_test_handler(
    _conn: RpcConnection | None, params: McpTestParams, core: Core
) -> McpTestResult:
    """``mcp.test`` — probe a saved server or an unsaved draft; never raises for it.

    ``name`` alone probes what is saved.  With draft fields too (the Add/Edit
    sheet sends both) the draft is probed: merged onto the saved entry when
    there is one, so a write-only secret the sheet never echoes still counts,
    and on its own for a name not saved yet.  With ``scope`` (1.33.0) the
    saved entry is that scope's declaration, not whichever wins; a scope that
    has none probes the draft on its own, with nothing merged in.
    """
    workdir = workdir_of(core, params.sessionId, params.workdir)
    row: Declared | None = None
    if params.name and params.scope is not None:
        row = next(
            (
                found
                for found in declarations(core, workdir)
                if found.name == params.name and found.scope == params.scope
            ),
            None,
        )
    elif params.name:
        row = declared(core, workdir).get(params.name)
    drafted = any(
        value is not None
        for value in (
            params.command,
            params.url,
            params.args,
            params.env,
            params.headers,
            params.envRemove,
            params.headersRemove,
            params.unset,
            params.oauth,
        )
    )
    if row is not None and not drafted:
        config = row.config
    elif params.name and row is None and not drafted:
        raise RpcError(errors.MCP_NOT_FOUND, f"'{params.name}' is not a configured server")
    else:
        try:
            entry = mcp_config.validate_entry(
                without(
                    entry_from(params, row.entry if row is not None else None, merge=True),
                    params.unset,
                    params.envRemove,
                    params.headersRemove,
                )
            )
        except mcp_config.McpConfigError as exc:
            raise _invalid(exc) from exc
        parsed = McpServerConfig.parse(params.name or "draft", entry, "draft")
        assert parsed is not None
        # A draft of a trusted saved entry may keep using that entry's own
        # variables; anything new needs settings.mcp.allowVars (1.34.0).
        inherited: set[str] = set()
        if row is not None and row.config.source in mcp_client.TRUSTED_SOURCES:
            for value in mcp_client.expandable_fields(row.config):
                inherited |= mcp_client.var_names(value)
        config = replace(parsed, inherited_vars=tuple(sorted(inherited)))
    MANAGER.bind(core)
    ok, tools, error, elapsed = await probe(config)
    auth = auth_of(core.paths.home, config, None)
    if not ok and needs_auth(error):
        auth = McpAuthInfo(required=True, state="needs_auth", clientId=auth.clientId)
    return McpTestResult(
        ok=ok,
        state="ready" if ok else "error",
        tools=tools,
        error=error,
        elapsedMs=elapsed,
        auth=auth,
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
                auth=row.get("auth") or "none",
            )
            for row in mcp_catalog.CATALOG
        ]
    )


def needs_auth(error: str | None) -> bool:
    """True for a probe error that is a 401, not a broken server (1.34.0)."""
    return bool(error) and mcp_client.NEEDS_AUTH_TEXT in str(error)


# ---------------------------------------------------------------------------
# OAuth (1.34.0)
# ---------------------------------------------------------------------------

#: How long a started sign-in waits for its redirect.
AUTH_TIMEOUT = 600.0


@dataclass
class PendingAuth:
    """One ``mcp.authorize`` flow waiting for its code; never leaves the daemon."""

    core: Any
    name: str
    url: str
    workdir: Path
    #: What the token record will hold, minus the tokens.
    record: dict[str, Any]
    verifier: str
    deadline: float
    listener: OAuthCallbackServer | None = None
    waiter: asyncio.Task[None] | None = None


#: ``state`` -> its flow.
_PENDING: dict[str, PendingAuth] = {}


def _auth_failed(name: str, exc: mcp_oauth.OAuthError | str, stage: str = "") -> RpcError:
    text = str(exc)
    stage = getattr(exc, "stage", stage) or "exchange"
    return RpcError(errors.MCP_AUTH_FAILED, f"{name}: {text}", {"stage": stage})


async def _auth_done(core: Any, name: str, ok: bool, error: str | None = None) -> None:
    hub = getattr(core, "hub", None)
    if hub is None:  # pragma: no cover - a core without a hub is a test double
        return
    payload = McpAuthDoneNotification(name=name, ok=ok, error=error)
    try:
        await hub.notify("mcp.auth.done", payload.model_dump(mode="json"))
    except Exception:  # noqa: BLE001 - a dead socket must not break the sign-in
        return


async def _discard(state: str) -> PendingAuth | None:
    """Forget one flow: stop its waiter and its listener."""
    pending = _PENDING.pop(state, None)
    if pending is None:
        return None
    waiter = pending.waiter
    if waiter is not None and waiter is not asyncio.current_task() and not waiter.done():
        waiter.cancel()
    if pending.listener is not None:
        with contextlib.suppress(Exception):
            await pending.listener.close()
    return pending


async def close_pending() -> None:
    """Drop every flow still waiting (daemon shutdown, tests)."""
    for state in list(_PENDING):
        await _discard(state)


async def _wait(state: str) -> None:
    """The loopback redirect, or the deadline; reports ``mcp.auth.done`` either way."""
    pending = _PENDING.get(state)
    if pending is None:
        return
    remaining = max(0.0, pending.deadline - time.monotonic())
    if pending.listener is None:
        await asyncio.sleep(remaining)
        if state in _PENDING:
            await _discard(state)
            await _auth_done(pending.core, pending.name, False, "the sign-in timed out")
        return
    try:
        code = await pending.listener.wait(remaining)
    except TimeoutError:
        if await _discard(state) is not None:
            await _auth_done(pending.core, pending.name, False, "the sign-in timed out")
        return
    except RpcError as exc:
        if await _discard(state) is not None:
            await _auth_done(pending.core, pending.name, False, exc.message)
        return
    with contextlib.suppress(RpcError):
        await _finish(state, code, pending.listener.iss)


async def _finish(state: str, code: str, iss: str | None = None) -> McpAuthorizeCompleteResult:
    """Exchange ``code``, store the tokens and restart the server with them."""
    pending = await _discard(state)
    if pending is None:
        raise RpcError(
            errors.MCP_AUTH_FAILED,
            "no sign-in is waiting under that state; it finished, expired or was replaced",
            {"stage": "state"},
        )
    core, name = pending.core, pending.name
    if time.monotonic() > pending.deadline:
        await _auth_done(core, name, False, "the sign-in timed out")
        raise RpcError(errors.MCP_AUTH_FAILED, f"{name}: the sign-in timed out", {"stage": "state"})
    try:
        mcp_oauth.check_iss(pending.record, iss)
        record = await mcp_oauth.exchange(pending.record, code, pending.verifier)
    except mcp_oauth.OAuthError as exc:
        await _auth_done(core, name, False, str(exc))
        raise _auth_failed(name, exc) from None
    mcp_oauth.TokenStore(core.paths.home).put(name, record)
    log.info("mcp server %s: signed in", name)
    state_after: McpState = "stopped"
    row = declared(core, pending.workdir).get(name)
    if row is not None and row.config.url == pending.url and not row.config.disabled:
        state_after = (await apply(core, row)).state
    await _auth_done(core, name, True)
    return McpAuthorizeCompleteResult(ok=True, name=name, state=state_after)


#: Custom URI schemes are allowed as private-use redirects (RFC 8252 §7.1),
#: except these, which a browser would run or read rather than hand back.
_BAD_SCHEMES = frozenset({"javascript", "data", "file", "vbscript", "blob", "about"})
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*$")


def check_redirect_uri(uri: str) -> str:
    """``uri`` when it is a redirect a host may catch, else ``invalid_params``.

    Loopback http on any port (127.0.0.1, localhost, [::1]), https, or a
    private-use scheme such as ``com.example.app:/cb``; never a fragment.
    """
    parts = urlsplit(uri)
    scheme = parts.scheme.lower()
    reason = ""
    if "#" in uri:
        reason = "a redirect URI may not carry a fragment"
    elif not scheme or not _SCHEME.match(parts.scheme):
        reason = "a redirect URI needs a scheme"
    elif scheme in _BAD_SCHEMES:
        reason = f"{scheme}: is not a redirect URI scheme"
    elif scheme == "http":
        if (parts.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            reason = "an http redirect URI must be on 127.0.0.1, localhost or [::1]"
    elif scheme == "https":
        if not parts.hostname:
            reason = "an https redirect URI needs a host"
    if reason:
        raise RpcError(errors.INVALID_PARAMS, f"redirectUri: {reason}", {"field": "redirectUri"})
    return uri


async def mcp_authorize_handler(
    _conn: RpcConnection | None, params: McpAuthorizeParams, core: Core
) -> McpAuthorizeResult:
    """``mcp.authorize`` — discover, register, and hand back the consent URL.

    Without ``redirectUri`` a one-shot listener on 127.0.0.1 catches the
    redirect and the flow finishes on its own; with one, the host catches it
    and calls ``mcp.authorize.complete``.  ``mcp.auth.done`` reports the end.
    """
    workdir = workdir_of(core, params.sessionId, params.workdir)
    MANAGER.bind(core)
    row = declared(core, workdir).get(params.name)
    if row is None:
        raise RpcError(errors.MCP_NOT_FOUND, f"'{params.name}' is not a configured server")
    name = row.name
    if not row.config.url or row.config.transport == "stdio":
        raise RpcError(
            errors.MCP_INVALID,
            "url: only an http or sse server signs in with OAuth",
            {"field": "url"},
        )
    try:
        config = mcp_client.resolve_config(row.config, core.paths.home, MANAGER.allow_vars())
    except mcp_client.McpError as exc:
        raise _auth_failed(name, str(exc), "client") from None
    for state, pending in list(_PENDING.items()):
        if pending.name == name:
            await _discard(state)

    state = mcp_oauth.new_state()
    verifier, challenge = new_pkce_pair()
    listener: OAuthCallbackServer | None = None
    if params.redirectUri:
        redirect_uri = check_redirect_uri(params.redirectUri)
    else:
        listener = OAuthCallbackServer(
            state, path="/callback", redirect_host="127.0.0.1", vendor=name
        )
        await listener.start()
        redirect_uri = listener.redirect_uri
    try:
        async with mcp_oauth.new_client() as http:
            found = await mcp_oauth.discover(http, config.url or "", config.headers)
            scope = params.scope or config.oauth.get("scope") or found.scope
            client_id = config.oauth.get("clientId")
            if client_id:
                secret = config.oauth.get("clientSecret") or None
                client: dict[str, Any] = {
                    "client_id": client_id,
                    "client_secret": secret,
                    "auth_method": "client_secret_post" if secret else "none",
                }
            elif found.registration_endpoint:
                client = await mcp_oauth.register(http, found, redirect_uri, scope)
            else:
                raise mcp_oauth.OAuthError(
                    "client",
                    "the authorization server offers no dynamic client registration; "
                    "set oauth.clientId on the entry",
                )
    except mcp_oauth.OAuthError as exc:
        if listener is not None:
            await listener.close()
        raise _auth_failed(name, exc) from None
    except BaseException:
        if listener is not None:
            await listener.close()
        raise

    url = mcp_oauth.authorize_url(
        found,
        client_id=str(client["client_id"]),
        redirect_uri=redirect_uri,
        challenge_s256=challenge,
        state=state,
        scope=scope,
    )
    record = {
        "url": row.config.url,
        "resource": found.resource,
        "issuer": found.issuer,
        "iss_required": found.iss_required,
        "token_endpoint": found.token_endpoint,
        "redirect_uri": redirect_uri,
        "scope": scope,
        **client,
    }
    pending = PendingAuth(
        core=core,
        name=name,
        url=row.config.url,
        workdir=workdir,
        record=record,
        verifier=verifier,
        deadline=time.monotonic() + AUTH_TIMEOUT,
        listener=listener,
    )
    _PENDING[state] = pending
    pending.waiter = asyncio.create_task(_wait(state), name=f"mcp-auth:{name}")
    expires_at = (
        (datetime.now(UTC) + timedelta(seconds=AUTH_TIMEOUT))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return McpAuthorizeResult(url=url, state=state, expiresAt=expires_at, redirectUri=redirect_uri)


async def mcp_authorize_complete_handler(
    _conn: RpcConnection | None, params: McpAuthorizeCompleteParams, _core: Core
) -> McpAuthorizeCompleteResult:
    """``mcp.authorize.complete`` — the code (or error) the host's redirect caught."""
    if params.error or not params.code:
        pending = await _discard(params.state)
        if pending is None:
            raise RpcError(
                errors.MCP_AUTH_FAILED,
                "no sign-in is waiting under that state; it finished, expired or was replaced",
                {"stage": "state"},
            )
        reason = (
            f"the authorization server refused the sign-in ({params.error})"
            if params.error
            else "the redirect carried no authorization code"
        )
        await _auth_done(pending.core, pending.name, False, reason)
        raise RpcError(errors.MCP_AUTH_FAILED, f"{pending.name}: {reason}", {"stage": "callback"})
    return await _finish(params.state, params.code, params.iss)


async def mcp_deauthorize_handler(
    _conn: RpcConnection | None, params: McpDeauthorizeParams, core: Core
) -> McpDeauthorizeResult:
    """``mcp.deauthorize`` — delete the stored sign-in; a running server restarts without it."""
    for state, pending in list(_PENDING.items()):
        if pending.name == params.name:
            await _discard(state)
    workdir = workdir_of(core, params.sessionId, params.workdir)
    row = declared(core, workdir).get(params.name)
    # The declaration in effect names which record goes; with none, all of them.
    url = row.config.url if row is not None else None
    removed = mcp_oauth.TokenStore(core.paths.home).drop(params.name, url)
    if removed:
        if row is not None and MANAGER.get(params.name) is not None:
            await apply(core, row)
    return McpDeauthorizeResult(ok=True, removed=removed)


def register_mcp_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS` and the state listener."""
    dispatcher.register("mcp.list", mcp_list_handler)
    dispatcher.register("mcp.add", mcp_add_handler)
    dispatcher.register("mcp.remove", mcp_remove_handler)
    dispatcher.register("mcp.update", mcp_update_handler)
    dispatcher.register("mcp.test", mcp_test_handler)
    dispatcher.register("mcp.reload", mcp_reload_handler)
    dispatcher.register("mcp.catalog", mcp_catalog_handler)
    dispatcher.register("mcp.authorize", mcp_authorize_handler)
    dispatcher.register("mcp.authorize.complete", mcp_authorize_complete_handler)
    dispatcher.register("mcp.deauthorize", mcp_deauthorize_handler)
    MANAGER.bind(dispatcher.core)
    # One listener per daemon: replacing the list keeps a previous daemon in
    # the same process (the test suite starts several) from broadcasting onto
    # a core that is already gone.
    MANAGER.listeners = [state_broadcaster(dispatcher.core)]
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "TEST_TIMEOUT",
    "Declared",
    "AUTH_TIMEOUT",
    "PendingAuth",
    "apply",
    "auth_of",
    "close_pending",
    "declarations",
    "declared",
    "entry_from",
    "info_of",
    "needs_auth",
    "mcp_add_handler",
    "mcp_authorize_complete_handler",
    "mcp_authorize_handler",
    "mcp_deauthorize_handler",
    "mcp_catalog_handler",
    "mcp_list_handler",
    "mcp_reload_handler",
    "mcp_remove_handler",
    "mcp_test_handler",
    "mcp_update_handler",
    "notify_changed",
    "probe",
    "register_mcp_handlers",
    "without",
    "workdir_of",
]
