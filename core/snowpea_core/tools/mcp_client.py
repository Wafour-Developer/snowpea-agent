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
import base64
import binascii
import contextlib
import json
import logging
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.attachments.model import MAX_BYTES
from snowpea_core.server.protocol import McpState, PermissionTag
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
#: Keys ``.mcp.json`` entries may carry (M14 §1b); anything else is preserved
#: verbatim when an entry is rewritten but ignored by the client.
ENTRY_KEYS: tuple[str, ...] = (
    "type",
    "command",
    "args",
    "env",
    "url",
    "headers",
    "cwd",
    "disabled",
    "timeoutSec",
    "toolTimeoutSec",
    "tools",
)


class McpError(RuntimeError):
    """A server could not be started, or a call to it failed."""


def _str_map(value: Any) -> dict[str, str]:
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _str_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _seconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


@dataclass
class McpServerConfig:
    """One entry of ``mcpServers``."""

    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    cwd: str | None = None
    #: ``Authorization``-style headers for a ``url`` server (M14 §2).
    headers: dict[str, str] = field(default_factory=dict)
    #: Kept in the file but never started (M14 §3, ``mcp.update``).
    disabled: bool = False
    #: Explicit ``stdio``/``http``/``sse``; inferred from command/url when absent.
    kind: str | None = None
    #: Startup and ``tools/list`` cap, then per-call cap; ``None`` = the default.
    timeout_sec: float | None = None
    tool_timeout_sec: float | None = None
    #: Tool allow/deny lists applied when the server's tools are registered.
    tools_include: list[str] = field(default_factory=list)
    tools_exclude: list[str] = field(default_factory=list)

    @property
    def transport(self) -> str:
        """``stdio``, ``http`` or ``sse`` — what :class:`McpServer` will speak."""
        if self.kind in ("stdio", "http", "sse"):
            return self.kind
        return "stdio" if self.command else "http"

    def keeps_tool(self, tool: str) -> bool:
        """Whether ``tool`` survives this server's include/exclude lists."""
        if self.tools_include and tool not in self.tools_include:
            return False
        return tool not in self.tools_exclude

    def to_entry(self) -> dict[str, Any]:
        """The ``.mcp.json`` entry this config came from (or would be written as)."""
        entry: dict[str, Any] = {}
        if self.kind:
            entry["type"] = self.kind
        if self.command:
            entry["command"] = self.command
        if self.args:
            entry["args"] = list(self.args)
        if self.env:
            entry["env"] = dict(self.env)
        if self.url:
            entry["url"] = self.url
        if self.headers:
            entry["headers"] = dict(self.headers)
        if self.cwd:
            entry["cwd"] = self.cwd
        if self.disabled:
            entry["disabled"] = True
        if self.timeout_sec:
            entry["timeoutSec"] = self.timeout_sec
        if self.tool_timeout_sec:
            entry["toolTimeoutSec"] = self.tool_timeout_sec
        tools: dict[str, Any] = {}
        if self.tools_include:
            tools["include"] = list(self.tools_include)
        if self.tools_exclude:
            tools["exclude"] = list(self.tools_exclude)
        if tools:
            entry["tools"] = tools
        return entry

    @classmethod
    def parse(cls, name: str, raw: dict[str, Any]) -> McpServerConfig | None:
        command = raw.get("command")
        url = raw.get("url")
        if not command and not url:
            return None
        kind = raw.get("type")
        raw_tools = raw.get("tools")
        tools: dict[str, Any] = raw_tools if isinstance(raw_tools, dict) else {}
        return cls(
            name=name,
            command=str(command) if command else None,
            args=_str_list(raw.get("args")),
            env=_str_map(raw.get("env")),
            url=str(url) if url else None,
            cwd=str(raw["cwd"]) if raw.get("cwd") else None,
            headers=_str_map(raw.get("headers")),
            disabled=bool(raw.get("disabled")),
            kind=str(kind) if kind in ("stdio", "http", "sse") else None,
            timeout_sec=_seconds(raw.get("timeoutSec")),
            tool_timeout_sec=_seconds(raw.get("toolTimeoutSec")),
            tools_include=_str_list(tools.get("include")),
            tools_exclude=_str_list(tools.get("exclude")),
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

    def __init__(self, config: McpServerConfig, *, announce: bool = True) -> None:
        self.config = config
        #: ``mcp.test`` probes a draft with a throwaway server; a draft must
        #: not publish state transitions under a name nobody configured.
        self.announce = announce
        self.tools: list[dict[str, Any]] = []
        #: Request families the server advertised on ``initialize``, e.g.
        #: ``{"resources", "prompts"}``.  The helper tools below are registered
        #: only for what is in here (M15 §E).
        self.capabilities: set[str] = set()
        #: What a client sees in ``mcp.list``; :meth:`_set_state` publishes it.
        self.state: McpState = "stopped"
        #: Why the last start failed, kept so ``mcp.list`` can explain a dot.
        self.error: str | None = None
        self._session: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._session is not None

    @property
    def start_timeout(self) -> float:
        return self.config.timeout_sec or START_TIMEOUT

    def visible_tools(self) -> list[dict[str, Any]]:
        """The tools this server exposes after its include/exclude lists."""
        return [tool for tool in self.tools if self.config.keeps_tool(str(tool.get("name", "")))]

    def _set_state(self, state: McpState, error: str | None = None) -> None:
        """Record a transition and tell whoever is listening (M14 §3)."""
        self.state = state
        self.error = error
        if self.announce:
            MANAGER.announce(self)

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            if self.config.disabled:
                raise McpError(f"{self.config.name}: the server is disabled")
            self._stop = asyncio.Event()
            self._ready = asyncio.get_running_loop().create_future()
            self._set_state("starting")
            self._task = asyncio.create_task(self._supervise(), name=f"mcp:{self.config.name}")
            try:
                await asyncio.wait_for(asyncio.shield(self._ready), self.start_timeout)
            except TimeoutError as exc:
                await self._abort()
                message = f"{self.config.name}: did not start within {self.start_timeout:g}s"
                self._set_state("error", message)
                raise McpError(message) from exc
            except McpError as exc:
                self._set_state("error", str(exc))
                raise
            self._set_state("ready")

    @contextlib.asynccontextmanager
    async def _streams(self) -> Any:
        """The transport's ``(read, write)`` pair for this server's config."""
        if self.config.transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=self.config.command or "",
                args=list(self.config.args),
                env=dict(self.config.env) or None,
                cwd=self.config.cwd,
            )
            # ``errlog`` defaults to whatever ``sys.stderr`` was at import
            # time; binding it per call keeps a server's stderr going to the
            # stream that is live now rather than one closed since.
            async with stdio_client(params, errlog=sys.stderr) as streams:
                yield streams[0], streams[1]
            return
        url = self.config.url or ""
        headers = dict(self.config.headers) or None
        if self.config.transport == "sse":
            from mcp.client.sse import sse_client

            async with sse_client(url, headers=headers) as streams:
                yield streams[0], streams[1]
            return
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        async with create_mcp_http_client(headers=headers) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                yield streams[0], streams[1]

    async def _supervise(self) -> None:
        """Hold the session open until :meth:`close` sets the event."""
        ready = self._ready
        assert ready is not None
        try:
            from mcp import ClientSession
        except ImportError as exc:  # pragma: no cover - mcp is a hard dependency
            if not ready.done():
                ready.set_exception(McpError(f"the mcp package is not installed: {exc}"))
            return

        try:
            async with self._streams() as (read, write), ClientSession(read, write) as session:
                self.capabilities = _capabilities_of(await session.initialize())
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

    async def call(self, tool: str, args: dict[str, Any]) -> RenderedMcpContent:
        await self.start()
        session = self._session
        if session is None:
            raise McpError(f"{self.config.name}: the server is not running")
        timeout = self.config.tool_timeout_sec or CALL_TIMEOUT
        result = await asyncio.wait_for(session.call_tool(tool, args), timeout)
        return render_content(result)

    async def session_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """One ``ClientSession`` method, with the server started if it is not."""
        await self.start()
        session = self._session
        if session is None:
            raise McpError(f"{self.config.name}: the server is not running")
        timeout = self.config.tool_timeout_sec or CALL_TIMEOUT
        return await asyncio.wait_for(getattr(session, method)(*args, **kwargs), timeout)

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
        self._set_state("stopped")


#: Helper tool -> the ``initialize`` capability that must be advertised for it
#: to be registered.  Without the gate a tools-only server got all four, every
#: call came back JSON-RPC ``-32601``, and the model concluded the server was
#: broken while its real tools worked (hermes ``_UTILITY_CAPABILITY_ATTRS``).
UTILITY_CAPABILITY: dict[str, str] = {
    "list_resources": "resources",
    "read_resource": "resources",
    "list_prompts": "prompts",
    "get_prompt": "prompts",
}

#: ``handler -> (description, schema)`` for the four helper tools.
UTILITY_SCHEMAS: dict[str, tuple[str, dict[str, Any]]] = {
    "list_resources": (
        "List the resources MCP server {server} exposes, with their URIs.",
        {"type": "object", "properties": {}},
    ),
    "read_resource": (
        "Read one resource from MCP server {server} by its URI, as listed by "
        "mcp__{server}__list_resources.",
        {
            "type": "object",
            "properties": {"uri": {"type": "string", "description": "URI of the resource."}},
            "required": ["uri"],
        },
    ),
    "list_prompts": (
        "List the prompt templates MCP server {server} offers.",
        {"type": "object", "properties": {}},
    ),
    "get_prompt": (
        "Fetch one prompt template from MCP server {server} by name, filled in "
        "with the arguments it declares.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Prompt name, from list_prompts."},
                "arguments": {
                    "type": "object",
                    "description": "Arguments the prompt declares.",
                    "additionalProperties": True,
                },
            },
            "required": ["name"],
        },
    ),
}


def _capabilities_of(result: Any) -> set[str]:
    """The request families an ``initialize`` result advertised."""
    caps = getattr(result, "capabilities", None)
    if caps is None:
        return set()
    return {name for name in ("resources", "prompts") if getattr(caps, name, None) is not None}


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


MAX_MCP_IMAGES = 4


@dataclass(frozen=True)
class RenderedMcpContent:
    """Text plus any image blocks carried out of an MCP tool result."""

    text: str
    images: tuple[dict[str, Any], ...] = ()


def _decode_image_block(block: Any) -> dict[str, Any] | None:
    data = getattr(block, "data", None)
    if data is None:
        return None
    mime = str(getattr(block, "mimeType", None) or "image/png")
    if not mime.startswith("image/"):
        return None
    if isinstance(data, str):
        try:
            raw = base64.b64decode(data, validate=False)
        except (binascii.Error, ValueError):
            return None
    else:
        raw = bytes(data)
    if len(raw) > MAX_BYTES:
        return None
    name = str(getattr(block, "name", None) or getattr(block, "uri", None) or "image")
    return {"mime": mime, "bytes_b64": base64.b64encode(raw).decode("ascii"), "name": name}


def as_rendered(value: str | RenderedMcpContent) -> RenderedMcpContent:
    """Accept either a plain tool string or a rendered MCP payload."""
    if isinstance(value, RenderedMcpContent):
        return value
    return RenderedMcpContent(text=str(value))


def render_content(result: Any) -> RenderedMcpContent:
    """Flatten an MCP ``CallToolResult`` into text and optional image metadata."""
    blocks = getattr(result, "content", None) or []
    parts: list[str] = []
    images: list[dict[str, Any]] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
            continue
        image_meta = _decode_image_block(block)
        if image_meta is not None and len(images) < MAX_MCP_IMAGES:
            images.append(image_meta)
            continue
        data = getattr(block, "data", None)
        uri = getattr(block, "uri", None)
        if uri:
            parts.append(str(uri))
        elif data:
            kind = getattr(block, "mimeType", "binary")
            parts.append(f"[{kind} content, {len(str(data))} bytes]")
    structured = getattr(result, "structuredContent", None)
    if not parts and not images and structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
    if getattr(result, "isError", False):
        raise McpError("\n".join(parts) or "the MCP tool reported an error")
    if images:
        summary = f"{len(images)} image(s) attached"
        text = f"{summary}\n" + "\n".join(parts) if parts else summary
    else:
        text = "\n".join(parts)
    return RenderedMcpContent(text=text, images=tuple(images))


# ---------------------------------------------------------------------------
# the daemon's servers
# ---------------------------------------------------------------------------


#: What :meth:`McpManager.announce` hands a listener on every state transition.
StateListener = Callable[["McpServer"], Coroutine[Any, Any, None]]


class McpManager:
    """Every MCP server this daemon knows about, started lazily and cached."""

    def __init__(self) -> None:
        self._servers: dict[str, McpServer] = {}
        #: Called on every state transition; the daemon registers the
        #: ``mcp.changed`` broadcaster here (M14 §3).
        self.listeners: list[StateListener] = []
        #: Live notification tasks, held so the loop cannot collect them.
        self._tasks: set[asyncio.Task[None]] = set()

    def announce(self, server: McpServer) -> None:
        """Fan a state transition out to the listeners, never blocking."""
        if not self.listeners:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop in a sync test
            return
        for listener in list(self.listeners):
            task: asyncio.Task[None] = loop.create_task(listener(server))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

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

    async def remove(self, name: str) -> bool:
        """Stop one server and forget it; ``False`` when it was not running."""
        server = self._servers.pop(name, None)
        if server is None:
            return False
        await server.close()
        return True

    async def close_all(self) -> None:
        for server in list(self._servers.values()):
            await server.close()
        self._servers.clear()


#: One manager per daemon process.
MANAGER = McpManager()


def tool_name(server: str, tool: str) -> str:
    return f"{TOOL_PREFIX}{server}__{tool}"


def permission_for(core: Core, server: str) -> PermissionTag:
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
            rendered = await server.call(tool, args)
        except McpError as exc:
            return ToolResult(ok=False, error=str(exc))
        except TimeoutError:
            return ToolResult(ok=False, error=f"{server_name}.{tool} timed out")
        except Exception as exc:  # noqa: BLE001 - a server can raise anything
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        rendered = as_rendered(rendered)
        meta = {"images": list(rendered.images)} if rendered.images else None
        return ToolResult(ok=True, output=rendered.text, meta=meta)

    return run


def _make_utility_runner(server_name: str, handler: str) -> Any:
    async def run(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        server = MANAGER.get(server_name)
        if server is None:
            return ToolResult(ok=False, error=f"mcp server {server_name} is no longer configured")
        try:
            if handler == "read_resource":
                raw = await server.session_call("read_resource", str(args.get("uri") or ""))
            elif handler == "get_prompt":
                raw = await server.session_call(
                    "get_prompt", str(args.get("name") or ""), args.get("arguments") or {}
                )
            else:
                raw = await server.session_call(handler)
        except McpError as exc:
            return ToolResult(ok=False, error=str(exc))
        except TimeoutError:
            return ToolResult(ok=False, error=f"{server_name}.{handler} timed out")
        except Exception as exc:  # noqa: BLE001 - a server can raise anything
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return ToolResult(ok=True, output=render_listing(raw))

    return run


def render_listing(result: Any) -> str:
    """Readable text for a resource/prompt result, whatever shape it has."""
    for attribute in ("contents", "resources", "prompts", "messages"):
        items = getattr(result, attribute, None)
        if items:
            return "\n".join(_render_item(item) for item in items)
    text = getattr(result, "text", None) or getattr(result, "description", None)
    return str(text) if text else str(result)


def _render_item(item: Any) -> str:
    text = getattr(item, "text", None)
    if text:
        return str(text)
    name = getattr(item, "name", None) or getattr(item, "uri", None) or ""
    description = getattr(item, "description", None) or ""
    content = getattr(item, "content", None)
    if content is not None and not description:
        description = str(getattr(content, "text", None) or content)
    return f"- {name}: {description}".rstrip(": ") if name else str(item)


def warn_on_injection(server: str, tool: str, description: str) -> list[str]:
    """Log anything in a server-supplied description that reads as an order.

    Advisory by contract: the tool is registered either way (M15 §E).
    """
    from snowpea_core.tools import mcp_security

    found = mcp_security.description_findings(description, label=f"{server}.{tool}")
    for issue in found:
        log.warning("mcp description check: %s", issue)
    return found


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
        registered.extend(await register_config(core, config))
    if registered:
        _invalidate_tool_prompt()
    return registered


def _invalidate_tool_prompt() -> None:
    """The tool list just changed, so the cached tools fragment is stale."""
    from snowpea_core.agent import agent as agent_mod

    agent_mod.invalidate_tools()


def drop_tools(core: Core, name: str) -> list[str]:
    """Unregister every tool a server contributed; returns their names."""
    source = f"mcp:{name}"
    dropped = [info.name for info in core.tools.list() if info.source == source]
    for tool in dropped:
        core.tools.unregister(tool)
    if dropped:
        _invalidate_tool_prompt()
    return dropped


async def register_config(core: Core, config: McpServerConfig) -> list[str]:
    """Start one server and register its tools; returns the tool names.

    This is the only way a server reaches the tool registry, so the plugin
    loader (M6 §1) adds a plugin's ``.mcp.json`` entries through it rather than
    registering tools of its own.  A server that will not start is logged and
    contributes nothing.
    """
    if config.disabled:
        return []
    server = MANAGER.register(config)
    try:
        await server.start()
    except McpError as exc:
        log.info("skipping mcp server %s: %s", config.name, exc)
        return []
    permission = permission_for(core, config.name)
    registered: list[str] = []
    for spec in server.visible_tools():
        name = tool_name(config.name, str(spec["name"]))
        description = str(spec.get("description") or f"{config.name}: {spec['name']}")
        warn_on_injection(config.name, str(spec["name"]), description)
        core.tools.register(
            Tool(
                name=name,
                category="mcp",
                description=description,
                input_schema=dict(spec.get("input_schema") or {"type": "object"}),
                permission=permission,
                run=_make_runner(config.name, str(spec["name"])),
                source=f"mcp:{config.name}",
            )
        )
        registered.append(name)
    for handler, capability in UTILITY_CAPABILITY.items():
        if capability not in server.capabilities or not config.keeps_tool(handler):
            continue
        template, schema = UTILITY_SCHEMAS[handler]
        name = tool_name(config.name, handler)
        core.tools.register(
            Tool(
                name=name,
                category="mcp",
                description=template.format(server=config.name),
                input_schema=dict(schema),
                permission=permission,
                run=_make_utility_runner(config.name, handler),
                source=f"mcp:{config.name}",
            )
        )
        registered.append(name)
    return registered


__all__ = [
    "CONFIG_NAME",
    "ENTRY_KEYS",
    "MANAGER",
    "TOOL_PREFIX",
    "McpError",
    "McpManager",
    "McpServer",
    "McpServerConfig",
    "UTILITY_CAPABILITY",
    "UTILITY_SCHEMAS",
    "discover",
    "drop_tools",
    "permission_for",
    "register_config",
    "RenderedMcpContent",
    "as_rendered",
    "render_content",
    "render_listing",
    "sync_tools",
    "warn_on_injection",
    "tool_name",
]
