"""MCP client: discover servers, start them lazily, expose their tools.

Config is the Claude Code shape — ``{"mcpServers": {"name": {...}}}`` — read
from ``$SNOWPEA_HOME/.mcp.json`` and ``<workdir>/.mcp.json``, the workdir
winning on a name clash.  A server is started the first time one of its tools
is called and then kept for the life of the daemon.

Transport: stdio, through the official ``mcp`` package.  The session lives in a
supervisor task because ``stdio_client`` and ``ClientSession`` are async
context managers that must be entered and exited in the same task, while tool
calls arrive from whichever task is running a turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.server.protocol import PermissionTag
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.tools.mcp")

#: Tool names are ``mcp__<server>__<tool>``; the prefix identifies the source.
TOOL_PREFIX = "mcp__"
CONFIG_NAME = ".mcp.json"

DEFAULT_PERMISSION: PermissionTag = "network"
START_TIMEOUT = 30.0
CALL_TIMEOUT = 300.0


class McpError(RuntimeError):
    """A server could not be started, or a call to it failed."""


@dataclass
class McpServerConfig:
    """One entry of ``mcpServers``."""

    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    cwd: str | None = None

    @property
    def transport(self) -> str:
        return "stdio" if self.command else "sse"

    @classmethod
    def parse(cls, name: str, raw: dict[str, Any]) -> McpServerConfig | None:
        command = raw.get("command")
        url = raw.get("url")
        if not command and not url:
            return None
        args = raw.get("args") or []
        env = raw.get("env") or {}
        return cls(
            name=name,
            command=str(command) if command else None,
            args=[str(a) for a in args] if isinstance(args, list) else [],
            env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
            url=str(url) if url else None,
            cwd=str(raw["cwd"]) if raw.get("cwd") else None,
        )


def _read_config(path: Path) -> dict[str, McpServerConfig]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        return {}
    out: dict[str, McpServerConfig] = {}
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        config = McpServerConfig.parse(str(name), entry)
        if config is not None:
            out[config.name] = config
    return out


def discover(home: Path | str | None, workdir: Path | str | None) -> dict[str, McpServerConfig]:
    """Servers from ``$SNOWPEA_HOME`` then ``<workdir>``; the workdir wins."""
    found: dict[str, McpServerConfig] = {}
    for base in (home, workdir):
        if base is None:
            continue
        found.update(_read_config(Path(base).expanduser() / CONFIG_NAME))
    return found


# ---------------------------------------------------------------------------
# one running server
# ---------------------------------------------------------------------------


class McpServer:
    """A started MCP server plus its tool list."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.tools: list[dict[str, Any]] = []
        self._session: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._session is not None

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            if self.config.transport != "stdio":
                raise McpError(
                    f"{self.config.name}: the SSE transport is not implemented yet; "
                    "declare the server with a command instead"
                )
            self._stop = asyncio.Event()
            self._ready = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(self._supervise(), name=f"mcp:{self.config.name}")
            try:
                await asyncio.wait_for(asyncio.shield(self._ready), START_TIMEOUT)
            except TimeoutError as exc:
                await self._abort()
                raise McpError(
                    f"{self.config.name}: did not start within {START_TIMEOUT:g}s"
                ) from exc

    async def _supervise(self) -> None:
        """Hold the stdio session open until :meth:`close` sets the event."""
        ready = self._ready
        assert ready is not None
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - mcp is a hard dependency
            if not ready.done():
                ready.set_exception(McpError(f"the mcp package is not installed: {exc}"))
            return

        params = StdioServerParameters(
            command=self.config.command or "",
            args=list(self.config.args),
            env=dict(self.config.env) or None,
            cwd=self.config.cwd,
        )
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                self.tools = [
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": _schema_of(tool),
                    }
                    for tool in listing.tools
                ]
                self._session = session
                if not ready.done():
                    ready.set_result(None)
                await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any startup failure is reported once
            detail = _flatten(exc)
            log.info("mcp server %s failed: %s", self.config.name, detail)
            if not ready.done():
                ready.set_exception(McpError(f"{self.config.name}: {detail}"))
        finally:
            self._session = None

    async def call(self, tool: str, args: dict[str, Any]) -> str:
        await self.start()
        session = self._session
        if session is None:
            raise McpError(f"{self.config.name}: the server is not running")
        result = await asyncio.wait_for(session.call_tool(tool, args), CALL_TIMEOUT)
        return render_content(result)

    async def _abort(self) -> None:
        task = self._task
        self._task = None
        self._session = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def close(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, 10.0)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                task.cancel()
        self._session = None


def _schema_of(tool: Any) -> dict[str, Any]:
    """The tool's JSON schema; the field is ``input_schema`` or ``inputSchema``."""
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
    return dict(schema) if isinstance(schema, dict) else {"type": "object", "properties": {}}


def _flatten(exc: BaseException) -> str:
    """Readable text for an exception, unwrapping anyio's exception groups."""
    if isinstance(exc, BaseExceptionGroup):
        inner = "; ".join(_flatten(sub) for sub in exc.exceptions)
        return inner or str(exc)
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def render_content(result: Any) -> str:
    """Flatten an MCP ``CallToolResult`` into text the model can read."""
    blocks = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
            continue
        data = getattr(block, "data", None)
        uri = getattr(block, "uri", None)
        if uri:
            parts.append(str(uri))
        elif data:
            kind = getattr(block, "mimeType", "binary")
            parts.append(f"[{kind} content, {len(str(data))} bytes]")
    structured = getattr(result, "structuredContent", None)
    if not parts and structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
    if getattr(result, "isError", False):
        raise McpError("\n".join(parts) or "the MCP tool reported an error")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# the daemon's servers
# ---------------------------------------------------------------------------


class McpManager:
    """Every MCP server this daemon knows about, started lazily and cached."""

    def __init__(self) -> None:
        self._servers: dict[str, McpServer] = {}

    def register(self, config: McpServerConfig) -> McpServer:
        existing = self._servers.get(config.name)
        if existing is not None and existing.config == config:
            return existing
        if existing is not None:
            # Configuration changed; retire the old process in the background.
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(existing.close())
        server = McpServer(config)
        self._servers[config.name] = server
        return server

    def get(self, name: str) -> McpServer | None:
        return self._servers.get(name)

    def names(self) -> list[str]:
        return list(self._servers)

    async def close_all(self) -> None:
        for server in list(self._servers.values()):
            await server.close()
        self._servers.clear()


#: One manager per daemon process.
MANAGER = McpManager()


def tool_name(server: str, tool: str) -> str:
    return f"{TOOL_PREFIX}{server}__{tool}"


def _permission_for(core: Core, server: str) -> PermissionTag:
    table = getattr(getattr(core.settings, "mcp", None), "permissions", None) or {}
    value = table.get(server) if isinstance(table, dict) else None
    allowed: tuple[PermissionTag, ...] = ("read", "write", "exec", "network", "send")
    if isinstance(value, str) and value in allowed:
        return value  # type: ignore[return-value]
    return DEFAULT_PERMISSION


def _make_runner(server_name: str, tool: str) -> Any:
    async def run(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        server = MANAGER.get(server_name)
        if server is None:
            return ToolResult(ok=False, error=f"mcp server {server_name} is no longer configured")
        try:
            output = await server.call(tool, args)
        except McpError as exc:
            return ToolResult(ok=False, error=str(exc))
        except TimeoutError:
            return ToolResult(ok=False, error=f"{server_name}.{tool} timed out")
        except Exception as exc:  # noqa: BLE001 - a server can raise anything
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return ToolResult(ok=True, output=output)

    return run


async def sync_tools(core: Core, workdir: Path | str | None = None) -> list[str]:
    """Discover servers, start them and (re)register their tools.

    Returns the tool names now registered.  A server that fails to start is
    logged and skipped, so one broken entry never costs the others.
    """
    settings_mcp = getattr(core.settings, "mcp", None)
    if settings_mcp is not None and not getattr(settings_mcp, "enabled", True):
        return []

    configs = discover(core.paths.home, workdir)
    inline = getattr(settings_mcp, "servers", None) or {}
    if isinstance(inline, dict):
        for name, entry in inline.items():
            if isinstance(entry, dict):
                parsed = McpServerConfig.parse(str(name), entry)
                if parsed is not None:
                    configs.setdefault(parsed.name, parsed)

    registered: list[str] = []
    for config in configs.values():
        server = MANAGER.register(config)
        try:
            await server.start()
        except McpError as exc:
            log.info("skipping mcp server %s: %s", config.name, exc)
            continue
        permission = _permission_for(core, config.name)
        for spec in server.tools:
            name = tool_name(config.name, str(spec["name"]))
            core.tools.register(
                Tool(
                    name=name,
                    category="mcp",
                    description=str(spec.get("description") or f"{config.name}: {spec['name']}"),
                    input_schema=dict(spec.get("input_schema") or {"type": "object"}),
                    permission=permission,
                    run=_make_runner(config.name, str(spec["name"])),
                    source=f"mcp:{config.name}",
                )
            )
            registered.append(name)
    return registered


__all__ = [
    "CONFIG_NAME",
    "MANAGER",
    "TOOL_PREFIX",
    "McpError",
    "McpManager",
    "McpServer",
    "McpServerConfig",
    "discover",
    "render_content",
    "sync_tools",
    "tool_name",
]
