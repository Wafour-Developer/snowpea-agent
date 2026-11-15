"""The extension API: what ``apply(ctx)`` receives (M16 Phase 3).

An extension is a folder ``$SNOWPEA_HOME/extensions/<name>/`` holding
``plugin.py`` and, optionally, ``plugin.json``.  ``plugin.py`` defines
``apply(ctx: PluginContext)`` (a plain or an ``async`` function).  Everything it
registers through ``ctx`` is recorded; disabling, reloading or removing the
extension undoes exactly that, newest first, without a daemon restart.

What an extension can add, each call returning a disposer that removes it
early:

* ``ctx.tools.register(tool)`` — a tool the model can call;
* ``ctx.prompt.section(section)`` — a system-prompt section;
* ``ctx.prompt.context(provider)`` — a runtime-context provider;
* ``ctx.hooks.post_execute(name, fn, order=)`` — see or change a tool result;
* ``ctx.hooks.turn_stopping(name, fn, order=)`` — keep a stopping turn going;
* ``ctx.commands.register(command)`` — a slash command;
* ``ctx.mcp.register(name, entry)`` — an MCP server that runs while it is loaded;
* ``ctx.on_dispose(fn)`` — anything else to undo (``fn`` may be ``async``).

A registration that clashes with something already there — a built-in tool or
command, another extension's section — raises :class:`ExtensionError`, so
``apply`` fails and nothing of the extension stays registered.  Code that runs
later (a tool, a section, a provider, a hook) is guarded: an exception is
logged with the extension's name and costs only that call.

Extensions run in the daemon's process with its permissions: install only code
you trust.  Only the home folder is scanned, never a project's, so opening a
cloned repository never runs its code.

Versioning
----------

:data:`API_VERSION` is the major version of this API.  It changes only when
something an extension relies on is removed or changes meaning — a ``ctx``
member, an argument, a field of a re-exported class, an ordering promise.
Adding members, optional arguments or fields does not change it.  An extension
states the major it was written for in ``plugin.json`` (``"engines":
{"snowpea": "^1"}``); one written for another major is not loaded and is
listed as ``incompatible``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.agent.runtime_context import ContextProvider
    from snowpea_core.agent.turn_stopping import Listener as StopListener
    from snowpea_core.commands.registry import Command
    from snowpea_core.prompts.sections import PromptSection
    from snowpea_core.server.app_server import Core
    from snowpea_core.tools.registry import Tool
    from snowpea_core.tools.result_hooks import Listener as PostListener

log = logging.getLogger("snowpea.extensions")

#: The major version of this API (see "Versioning" above).
API_VERSION = 1

#: Undoes one registration.  ``ctx.on_dispose`` also accepts an async one.
Disposer = Callable[[], None]
AnyDisposer = Callable[[], "None | Awaitable[None]"]

#: Seconds one disposer may take before it is abandoned (logged).
DISPOSE_TIMEOUT = 10.0

#: MCP servers registered by extensions: ``{server name: (entry, extension name)}``.
PLUGIN_MCP: dict[str, tuple[dict[str, Any], str]] = {}

#: Tool names: what the providers accept.
TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
#: Command, hook and MCP server names.
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
#: Permission tags an extension tool may declare.
TOOL_PERMISSIONS = ("read", "write", "exec", "network", "send")


class ExtensionError(Exception):
    """A registration the extension asked for cannot be made."""


def where(exc: BaseException, root: Path | None) -> str:
    """``file:line`` inside ``root`` where ``exc`` happened, or ``""``.

    A syntax error names its own position; otherwise the innermost frame of the
    traceback that lies in the extension's folder wins, so the location points
    at the extension's code even when the exception came from the core.
    """
    if root is None:
        return ""
    base = root.resolve()

    def rel(filename: str) -> str | None:
        try:
            return str(Path(filename).resolve().relative_to(base))
        except (ValueError, OSError):
            return None

    if isinstance(exc, SyntaxError) and exc.filename:
        inside = rel(exc.filename)
        if inside is not None:
            return f"{inside}:{exc.lineno}"
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        inside = rel(frame.filename)
        if inside is not None:
            return f"{inside}:{frame.lineno}"
    return ""


def describe(exc: BaseException, root: Path | None) -> str:
    """``Type: message (at file:line)`` for a report a developer can act on."""
    text = f"{type(exc).__name__}: {exc}".rstrip(": ")
    location = where(exc, root)
    return f"{text} (at {location})" if location else text


@dataclass
class _Effect:
    label: str
    disposer: AnyDisposer


@dataclass
class _Effects:
    entries: list[_Effect] = field(default_factory=list)

    def add(self, label: str, disposer: AnyDisposer) -> Any:
        self.entries.append(_Effect(label, disposer))
        return disposer

    def labels(self, kind: str | None = None) -> list[str]:
        return [
            entry.label
            for entry in self.entries
            if kind is None or entry.label.startswith(f"{kind}:")
        ]

    async def undo(self, timeout: float = DISPOSE_TIMEOUT) -> list[str]:
        """Run every disposer, newest first; returns the errors, if any."""
        errors: list[str] = []
        while self.entries:
            entry = self.entries.pop()
            try:
                outcome = entry.disposer()
                if inspect.isawaitable(outcome):
                    await asyncio.wait_for(outcome, timeout)
            except TimeoutError:
                errors.append(f"{entry.label}: did not finish within {timeout:g}s")
            except Exception as exc:  # noqa: BLE001 - keep undoing the rest
                errors.append(f"{entry.label}: {type(exc).__name__}: {exc}")
        return errors


class _Part:
    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx


class ToolsApi(_Part):
    """``ctx.tools``."""

    def register(self, tool: Tool, *, timeout: float | None = None) -> Disposer:
        """Offer ``tool`` to the model; returns a disposer that withdraws it.

        ``tool.permission`` is checked against the session's mode exactly as a
        built-in's is (``read``, ``write``, ``exec``, ``network`` or ``send``).
        ``tool.run`` must be ``async def run(tool_ctx, args) -> ToolResult``.
        It is guarded: an exception becomes that call's error result (logged
        with the extension's name), a plain ``str`` is taken as a successful
        output, and with ``timeout`` (seconds) a call that runs longer fails
        with a timeout error.  The tool's ``source`` becomes
        ``extension:<name>``.

        Raises :class:`ExtensionError` for a malformed tool or a name already
        in use (a built-in, an MCP tool or another extension's).
        """
        from snowpea_core.tools.registry import Tool

        ctx = self._ctx
        ctx._check_open()
        if not isinstance(tool, Tool):
            raise ExtensionError(
                f"tools.register needs a snowpea_core.extensions.Tool, got {type(tool).__name__}"
            )
        if not TOOL_NAME_RE.match(tool.name or ""):
            raise ExtensionError(
                f"tool name {tool.name!r} must match [A-Za-z][A-Za-z0-9_-]{{0,63}}"
            )
        if tool.permission not in TOOL_PERMISSIONS:
            raise ExtensionError(
                f"tool {tool.name}: permission {tool.permission!r} must be one of "
                + ", ".join(TOOL_PERMISSIONS)
            )
        if tool.parallel_safe and tool.permission not in ("read", "network"):
            raise ExtensionError(
                f"tool {tool.name}: parallel_safe is only for read or network tools"
            )
        if not isinstance(tool.input_schema, dict) or tool.input_schema.get("type") != "object":
            raise ExtensionError(
                f'tool {tool.name}: input_schema must be a JSON Schema with "type": "object"'
            )
        if not inspect.iscoroutinefunction(tool.run):
            raise ExtensionError(f"tool {tool.name}: run must be an async function")
        if timeout is not None and timeout <= 0:
            raise ExtensionError(f"tool {tool.name}: timeout must be positive")
        core = ctx.core
        existing = core.tools.get(tool.name)
        if existing is not None:
            raise ExtensionError(
                f"tool {tool.name} is already registered by {existing.source}"
            )
        registered = replace(
            tool,
            source=f"extension:{ctx.name}",
            run=_guarded_run(ctx, tool.name, tool.run, timeout),
        )
        core.tools.register(registered)
        ctx._changed = True

        def dispose() -> None:
            if core.tools.get(registered.name) is registered:
                core.tools.unregister(registered.name)

        return ctx._effects.add(f"tool:{tool.name}", dispose)  # type: ignore[no-any-return]


def _guarded_run(ctx: PluginContext, name: str, run: Any, timeout: float | None) -> Any:
    from snowpea_core.tools.registry import ToolResult

    extension = ctx.name
    root = ctx.path

    async def guarded(tool_ctx: Any, args: dict[str, Any]) -> ToolResult:
        try:
            if timeout is None:
                result = await run(tool_ctx, args)
            else:
                result = await asyncio.wait_for(run(tool_ctx, args), timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            log.warning("extension %s: tool %s timed out after %gs", extension, name, timeout)
            return ToolResult(
                ok=False, error=f"{name} (extension {extension}) timed out after {timeout:g}s"
            )
        except Exception as exc:  # noqa: BLE001 - an extension's bug is that call's error
            detail = describe(exc, root)
            log.warning("extension %s: tool %s failed: %s", extension, name, detail, exc_info=True)
            return ToolResult(ok=False, error=f"{name} (extension {extension}) failed: {detail}")
        if isinstance(result, str):
            return ToolResult(ok=True, output=result)
        if not isinstance(result, ToolResult):
            log.warning(
                "extension %s: tool %s returned %s, not a ToolResult",
                extension,
                name,
                type(result).__name__,
            )
            return ToolResult(
                ok=False,
                error=(
                    f"{name} (extension {extension}) returned {type(result).__name__}, "
                    "not a ToolResult"
                ),
            )
        return result

    return guarded


def _guarded_render(ctx: PluginContext, kind: str, name: str, render: Any) -> Any:
    """A render function that logs and drops out (``""``) instead of raising."""
    extension = ctx.name
    root = ctx.path
    failures = 0

    def guarded(*args: Any) -> str:
        nonlocal failures
        try:
            text = render(*args)
        except Exception as exc:  # noqa: BLE001 - one broken extension must not cost the turn
            failures += 1
            # Once loudly, then quietly: a render runs every model call.
            level = logging.WARNING if failures == 1 else logging.DEBUG
            log.log(
                level,
                "extension %s: %s %s failed and was skipped: %s",
                extension,
                kind,
                name,
                describe(exc, root),
                exc_info=failures == 1,
            )
            return ""
        return text if isinstance(text, str) else ("" if text is None else str(text))

    return guarded


class PromptApi(_Part):
    """``ctx.prompt``."""

    def section(self, section: PromptSection) -> Disposer:
        """Add a system-prompt section; its ``order`` decides where it sits.

        ``section.render(inputs)`` runs every model call and returns the text,
        or ``""`` to drop out.  An exception is logged and the section skipped
        for that call.  Raises :class:`ExtensionError` when the name is taken.
        """
        from snowpea_core.prompts import sections
        from snowpea_core.prompts.sections import PromptSection

        ctx = self._ctx
        ctx._check_open()
        if not isinstance(section, PromptSection):
            raise ExtensionError(
                "prompt.section needs a snowpea_core.extensions.PromptSection, got "
                f"{type(section).__name__}"
            )
        if not callable(section.render) or not isinstance(section.order, int):
            raise ExtensionError(f"section {section.name}: needs an int order and a render")
        for current in sections.REGISTRY.sections():
            if current.name == section.name:
                raise ExtensionError(
                    f"prompt section {section.name} is already registered by {current.source}"
                )
        registered = replace(
            section,
            source=f"extension:{ctx.name}",
            render=_guarded_render(ctx, "prompt section", section.name, section.render),
        )
        ctx._changed = True
        return ctx._effects.add(  # type: ignore[no-any-return]
            f"section:{section.name}", sections.REGISTRY.register(registered)
        )

    def context(self, provider: ContextProvider) -> Disposer:
        """Add a runtime-context provider: a fact re-sent only when its text changes.

        ``provider.render(core, session)`` runs before every model call; an
        exception is logged and the provider skipped for that call.  Raises
        :class:`ExtensionError` when the name is taken.
        """
        from snowpea_core.agent import runtime_context
        from snowpea_core.agent.runtime_context import ContextProvider

        ctx = self._ctx
        ctx._check_open()
        if not isinstance(provider, ContextProvider):
            raise ExtensionError(
                "prompt.context needs a snowpea_core.extensions.ContextProvider, got "
                f"{type(provider).__name__}"
            )
        if not callable(provider.render) or not isinstance(provider.order, int):
            raise ExtensionError(f"context {provider.name}: needs an int order and a render")
        for current in runtime_context.REGISTRY.providers():
            if current.name == provider.name:
                raise ExtensionError(
                    f"runtime context {provider.name} is already registered by {current.source}"
                )
        registered = replace(
            provider,
            source=f"extension:{ctx.name}",
            render=_guarded_render(ctx, "runtime context", provider.name, provider.render),
        )
        return ctx._effects.add(  # type: ignore[no-any-return]
            f"context:{provider.name}", runtime_context.REGISTRY.register(registered)
        )


class HooksApi(_Part):
    """``ctx.hooks``."""

    def _label(self, kind: str, name: str, listener: Any, order: int) -> str:
        ctx = self._ctx
        ctx._check_open()
        if not SLUG_RE.match(name or ""):
            raise ExtensionError(f"{kind} hook name {name!r} must be a short slug")
        if not callable(listener):
            raise ExtensionError(f"{kind} hook {name}: the listener is not callable")
        if isinstance(order, bool) or not isinstance(order, int):
            raise ExtensionError(f"{kind} hook {name}: order must be an int")
        if f"{kind}:{ctx.name}:{name}" in ctx._effects.labels(kind):
            raise ExtensionError(f"{kind} hook {name} is already registered by this extension")
        return f"{ctx.name}:{name}"

    def post_execute(self, name: str, listener: PostListener, *, order: int = 0) -> Disposer:
        """Run ``listener(tool_ctx, tool_name, args, result)`` after every tool call.

        Return ``None`` (no change), ``PostDecision.accept(output, *notes)`` or
        ``PostDecision.block(feedback)``; it may be ``async``.  Listeners run by
        ``order`` then registration; one that raises is logged and skipped.
        """
        from snowpea_core.tools import result_hooks

        label = self._label("post_execute", name, listener, order)
        return self._ctx._effects.add(  # type: ignore[no-any-return]
            f"post_execute:{label}",
            result_hooks.POST_EXECUTE.register(label, listener, order=order),
        )

    def turn_stopping(self, name: str, listener: StopListener, *, order: int = 150) -> Disposer:
        """Run ``listener(core, session, stop_ctx)`` when a turn is about to end.

        Return ``None`` to let it end or a ``StopDecision(text, producer)`` to
        send ``text`` as the next instruction; the first decision in ``order``
        wins.  Built-ins: verify-gate 100, unfinished-work 150, stop-hooks 200.
        """
        from snowpea_core.agent import turn_stopping

        label = self._label("turn_stopping", name, listener, order)
        return self._ctx._effects.add(  # type: ignore[no-any-return]
            f"turn_stopping:{label}",
            turn_stopping.CHAIN.register(label, listener, order=order),
        )


class CommandsApi(_Part):
    """``ctx.commands``."""

    def register(self, command: Command) -> Disposer:
        """Add a slash command ``/<command.name>``.

        ``command.run`` must be ``async def run(cmd_ctx, args: str)``.  Raises
        :class:`ExtensionError` for a built-in name or one another skill or
        extension already holds.
        """
        from snowpea_core.commands.registry import Command

        ctx = self._ctx
        ctx._check_open()
        if not isinstance(command, Command):
            raise ExtensionError(
                "commands.register needs a snowpea_core.extensions.Command, got "
                f"{type(command).__name__}"
            )
        if command.name.startswith("/") or not SLUG_RE.match(command.name or ""):
            raise ExtensionError(f"command name {command.name!r} must be a slug without '/'")
        if not inspect.iscoroutinefunction(command.run):
            raise ExtensionError(f"command /{command.name}: run must be an async function")
        registry = ctx.core.commands
        if command.name in getattr(registry, "protected", frozenset()):
            raise ExtensionError(f"/{command.name} is a built-in command and cannot be replaced")
        existing = registry.get(command.name)
        if existing is not None:
            raise ExtensionError(
                f"/{command.name} is already registered by {existing.source}"
            )
        registered = replace(command, source=f"extension:{ctx.name}")
        registry.register(registered)
        ctx._commands_changed = True

        def dispose() -> None:
            if registry.get(registered.name) is registered:
                registry.unregister(registered.name)

        return ctx._effects.add(f"command:{command.name}", dispose)  # type: ignore[no-any-return]


class McpApi(_Part):
    """``ctx.mcp``."""

    def register(self, name: str, entry: dict[str, Any]) -> Disposer:
        """Start an MCP server, ``entry`` shaped like one ``.mcp.json`` entry.

        The server starts in the background; its tools appear as
        ``mcp__<name>__<tool>`` once it is up and go when the extension does.
        Raises :class:`ExtensionError` for a bad entry or a name another
        source already declares.
        """
        from snowpea_core.tools import mcp_client

        ctx = self._ctx
        ctx._check_open()
        if not SLUG_RE.match(str(name or "")):
            raise ExtensionError(f"MCP server name {name!r} must be a short slug")
        if not isinstance(entry, dict):
            raise ExtensionError(f"MCP server {name}: the entry must be a dict")
        config = mcp_client.McpServerConfig.parse(str(name), entry, "plugin")
        if config is None:
            raise ExtensionError(f"MCP server {name}: the entry needs a command or a url")
        owner = PLUGIN_MCP.get(name)
        if owner is not None:
            raise ExtensionError(f"MCP server {name} is already registered by extension:{owner[1]}")
        try:
            from snowpea_core.server.mcp_handlers import declared

            taken = declared(ctx.core, None).get(name)
        except Exception:  # noqa: BLE001 - a core without the MCP layer (tests)
            taken = None
        if taken is not None:
            raise ExtensionError(f"MCP server {name} is already declared ({taken.scope})")
        PLUGIN_MCP[name] = (dict(entry), ctx.name)
        core = ctx.core
        task: asyncio.Task[Any] | None = None

        async def start() -> None:
            try:
                names = await mcp_client.register_config(core, config)
            except Exception as exc:  # noqa: BLE001 - logged; the extension stays loaded
                log.warning("extension %s: MCP server %s did not start: %s", ctx.name, name, exc)
                return
            if names:
                mcp_client._invalidate_tool_prompt()
            log.info("extension %s: MCP server %s registered %d tools", ctx.name, name, len(names))

        try:
            task = asyncio.get_running_loop().create_task(start())
        except RuntimeError:  # no loop: listed only, started by `snowpea mcp reload`
            task = None

        async def dispose() -> None:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            current = PLUGIN_MCP.get(name)
            if current is not None and current[1] == ctx.name:
                del PLUGIN_MCP[name]
            mcp_client.drop_tools(core, name)
            await mcp_client.MANAGER.remove(name)

        return ctx._effects.add(f"mcp:{name}", dispose)  # type: ignore[no-any-return]


class PluginContext:
    """What ``apply(ctx)`` receives; every registration is undone when the extension goes.

    Attributes:
        name: the extension's name (its folder name).
        path: its folder.
        data_dir: a folder for its own files that survives updates.
        config: ``settings.extensions.<name>.config``, validated against the
            ``configSchema`` of ``plugin.json`` and completed with the
            schema's top-level ``default`` values.  A copy: changing it
            changes nothing; a new config arrives by reloading.
        log: a logger named ``snowpea.extensions.<name>``; it writes to the
            daemon log (``$SNOWPEA_HOME/logs/daemon.log``).
        core: the daemon core.  Not part of the stable API: read from it if
            you must, but expect its shape to change between releases.
        tools, prompt, hooks, commands, mcp: the registration namespaces.
    """

    def __init__(
        self,
        core: Core,
        name: str,
        config: dict[str, Any] | None = None,
        *,
        path: Path | None = None,
    ) -> None:
        self.core = core
        self.name = name
        self.path = path
        self.config: dict[str, Any] = dict(config or {})
        self.log = logging.getLogger(f"snowpea.extensions.{name}")
        self._effects = _Effects()
        self._closed = False
        #: A tool or a section came or went: cached prompt pieces are stale.
        self._changed = False
        #: A command came or went: clients re-read the command table.
        self._commands_changed = False
        self.tools = ToolsApi(self)
        self.prompt = PromptApi(self)
        self.hooks = HooksApi(self)
        self.commands = CommandsApi(self)
        self.mcp = McpApi(self)

    def _check_open(self) -> None:
        if self._closed:
            raise ExtensionError(f"extension {self.name} is unloaded; it cannot register more")

    def on_dispose(self, fn: AnyDisposer) -> AnyDisposer:
        """Run ``fn()`` when the extension is unloaded, in reverse registration order.

        ``fn`` may be ``async``; it is awaited for at most
        :data:`DISPOSE_TIMEOUT` seconds.  Use it to close what ``apply``
        opened: a client session, a background task, a file.
        """
        self._check_open()
        if not callable(fn):
            raise ExtensionError("on_dispose needs a callable")
        label = getattr(fn, "__qualname__", None) or type(fn).__name__
        return self._effects.add(f"dispose:{label}", fn)  # type: ignore[no-any-return]

    @property
    def data_dir(self) -> Path:
        """``$SNOWPEA_HOME/extension-data/<name>/``, created on first use.

        The place for files the extension keeps between runs.  It is outside
        the extension's folder, so replacing that folder with a new version
        (a ``git pull``, a new zip) keeps the data.
        """
        folder = Path(self.core.paths.home) / "extension-data" / self.name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @property
    def effects(self) -> int:
        """How many registrations the extension holds."""
        return len(self._effects.entries)

    @property
    def registrations(self) -> list[str]:
        """``kind:name`` of every registration, oldest first (``tool:ticket_lookup`` …)."""
        return self._effects.labels()

    async def dispose(self, timeout: float = DISPOSE_TIMEOUT) -> list[str]:
        """Undo every registration, newest first; returns the errors, if any."""
        self._closed = True
        return await self._effects.undo(timeout)


__all__ = [
    "API_VERSION",
    "DISPOSE_TIMEOUT",
    "PLUGIN_MCP",
    "TOOL_PERMISSIONS",
    "CommandsApi",
    "Disposer",
    "ExtensionError",
    "HooksApi",
    "McpApi",
    "PluginContext",
    "PromptApi",
    "ToolsApi",
    "describe",
    "where",
]
