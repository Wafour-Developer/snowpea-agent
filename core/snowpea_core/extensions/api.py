"""The Python plugin API (M16 Phase 3).

A plugin is a folder ``$SNOWPEA_HOME/extensions/<name>/`` holding ``plugin.py``
and, optionally, ``plugin.json`` (``name``, ``description``, ``version``,
``engines.snowpea``, ``configSchema``).  ``plugin.py`` defines
``apply(ctx: PluginContext)``; everything it registers through ``ctx`` is
recorded, and disabling, reloading or removing the plugin undoes exactly that,
in reverse order, without a daemon restart (dsh Cordis effects).

What a plugin can add, each returning a disposer:

* ``ctx.tools.register(tool)`` — a tool the model can call;
* ``ctx.prompt.section(section)`` — a system-prompt section (``prompts.sections``);
* ``ctx.prompt.context(provider)`` — a runtime-context provider (``agent.runtime_context``);
* ``ctx.hooks.post_execute(name, fn, order=)`` — see or change a tool result
  (``tools.result_hooks``);
* ``ctx.hooks.turn_stopping(name, fn, order=)`` — keep a stopping turn going
  (``agent.turn_stopping``);
* ``ctx.commands.register(command)`` — a slash command;
* ``ctx.mcp.register(name, entry)`` — an MCP server that lives as long as the plugin;
* ``ctx.on_dispose(fn)`` — anything else to undo.

``ctx.config`` is the plugin's settings (``settings.extensions.<name>.config``),
validated against ``configSchema`` by surfaces that render the form.

Plugins run in the daemon's process with its permissions: install only code you
trust.  Only the home folder is scanned, never a project's, so opening a cloned
repository never runs its code.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.extensions")

#: Bumped when the context's shape changes incompatibly; a plugin naming a
#: higher ``engines.snowpea`` major is refused.
API_VERSION = 1

Disposer = Callable[[], None]

#: MCP servers registered by plugins: ``{server name: (entry, plugin name)}``.
PLUGIN_MCP: dict[str, tuple[dict[str, Any], str]] = {}


@dataclass
class _Effects:
    disposers: list[Disposer] = field(default_factory=list)

    def add(self, disposer: Disposer) -> Disposer:
        self.disposers.append(disposer)
        return disposer

    def undo(self) -> list[str]:
        """Run every disposer, newest first; returns the errors, if any."""
        errors: list[str] = []
        while self.disposers:
            disposer = self.disposers.pop()
            try:
                disposer()
            except Exception as exc:  # noqa: BLE001 - keep undoing the rest
                errors.append(f"{type(exc).__name__}: {exc}")
        return errors


class _Tools:
    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    def register(self, tool: Any) -> Disposer:
        core = self._ctx.core
        if not str(getattr(tool, "source", "") or "").startswith("extension:"):
            tool.source = f"extension:{self._ctx.name}"
        core.tools.register(tool)

        def dispose() -> None:
            current = core.tools.get(tool.name)
            if current is tool:
                core.tools.unregister(tool.name)

        return self._ctx._effects.add(dispose)


class _Prompt:
    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    def section(self, section: Any) -> Disposer:
        from dataclasses import replace

        from snowpea_core.prompts import sections

        if getattr(section, "source", "builtin") == "builtin":
            section = replace(section, source=f"extension:{self._ctx.name}")
        return self._ctx._effects.add(sections.REGISTRY.register(section))

    def context(self, provider: Any) -> Disposer:
        from dataclasses import replace

        from snowpea_core.agent import runtime_context

        if getattr(provider, "source", "builtin") == "builtin":
            provider = replace(provider, source=f"extension:{self._ctx.name}")
        return self._ctx._effects.add(runtime_context.REGISTRY.register(provider))


class _Hooks:
    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    def post_execute(self, name: str, listener: Any, *, order: int = 0) -> Disposer:
        from snowpea_core.tools import result_hooks

        label = f"{self._ctx.name}:{name}"
        return self._ctx._effects.add(
            result_hooks.POST_EXECUTE.register(label, listener, order=order)
        )

    def turn_stopping(self, name: str, listener: Any, *, order: int = 150) -> Disposer:
        from snowpea_core.agent import turn_stopping

        label = f"{self._ctx.name}:{name}"
        return self._ctx._effects.add(turn_stopping.CHAIN.register(label, listener, order=order))


class _Commands:
    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    def register(self, command: Any) -> Disposer:
        registry = self._ctx.core.commands
        if command.name in getattr(registry, "protected", frozenset()):
            raise ValueError(f"/{command.name} is a built-in command and cannot be replaced")
        previous = registry.get(command.name) if hasattr(registry, "get") else None
        registry.register(command)

        def dispose() -> None:
            current = registry.get(command.name) if hasattr(registry, "get") else command
            if current is command:
                registry.unregister(command.name)
                if previous is not None:
                    registry.register(previous)

        return self._ctx._effects.add(dispose)


class _Mcp:
    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    def register(self, name: str, entry: dict[str, Any]) -> Disposer:
        PLUGIN_MCP[str(name)] = (dict(entry), self._ctx.name)

        def dispose() -> None:
            current = PLUGIN_MCP.get(str(name))
            if current is not None and current[1] == self._ctx.name:
                del PLUGIN_MCP[str(name)]

        return self._ctx._effects.add(dispose)


class PluginContext:
    """What ``apply(ctx)`` receives; every registration is undone when the plugin goes."""

    def __init__(self, core: Core, name: str, config: dict[str, Any] | None = None) -> None:
        self.core = core
        self.name = name
        self.config: dict[str, Any] = dict(config or {})
        self.log = logging.getLogger(f"snowpea.extensions.{name}")
        self._effects = _Effects()
        self.tools = _Tools(self)
        self.prompt = _Prompt(self)
        self.hooks = _Hooks(self)
        self.commands = _Commands(self)
        self.mcp = _Mcp(self)

    def on_dispose(self, fn: Disposer) -> Disposer:
        return self._effects.add(fn)

    @property
    def effects(self) -> int:
        return len(self._effects.disposers)

    def dispose(self) -> list[str]:
        return self._effects.undo()


__all__ = ["API_VERSION", "PLUGIN_MCP", "Disposer", "PluginContext"]
