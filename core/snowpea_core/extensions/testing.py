"""A pytest harness for extensions: load one into a real, private daemon.

    from snowpea_core.extensions.testing import load_extension

    async def test_greets(tmp_path):
        async with load_extension(Path(__file__).parent, home=tmp_path) as ext:
            result = await ext.call_tool("hello_greet", {"name": "Ada"})
            assert result.output == "Hello, Ada!"

:func:`load_extension` starts a daemon on an ephemeral loopback port in a
throwaway ``SNOWPEA_HOME``, copies the extension folder into it, loads it
through the same :class:`~snowpea_core.extensions.manager.ExtensionManager` the
real daemon uses, opens one session, and on exit unloads it and checks that it
left nothing registered.  Nothing reaches the network: no model is called.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.session.mode_guard import ModeCaller

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.agent.turn_stopping import StopDecision
    from snowpea_core.extensions.manager import Extension
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session
    from snowpea_core.tools.registry import ToolResult

#: Keeps a test daemon off the network and the machine's sleep settings.
_QUIET_ENV = {
    "SNOWPEA_UPDATE_CHECK": "0",
    "SNOWPEA_MODELS_DEV": "0",
    "SNOWPEA_KEEP_AWAKE": "0",
    "SNOWPEA_EXTENSIONS": "0",  # only the extension under test is loaded
}


class ExtensionLoadError(AssertionError):
    """The extension under test did not load; the message is its error."""


@contextlib.contextmanager
def _env(values: dict[str, str]) -> Iterator[None]:
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def snapshot(core: Core) -> dict[str, frozenset[str]]:
    """Every name each registry holds, to compare before and after."""
    from snowpea_core.agent import runtime_context, turn_stopping
    from snowpea_core.extensions.api import PLUGIN_MCP
    from snowpea_core.prompts import sections
    from snowpea_core.tools import result_hooks

    return {
        "tools": frozenset(core.tools.names()),
        "commands": frozenset(command.name for command in core.commands.commands()),
        "sections": frozenset(section.name for section in sections.REGISTRY.sections()),
        "contexts": frozenset(p.name for p in runtime_context.REGISTRY.providers()),
        "post_execute": frozenset(e.name for e in result_hooks.POST_EXECUTE.listeners()),
        "turn_stopping": frozenset(e.name for e in turn_stopping.CHAIN.listeners()),
        "mcp": frozenset(PLUGIN_MCP),
        "modules": frozenset(name for name in sys.modules if name.startswith("snowpea_ext_")),
    }


def leftovers(before: dict[str, frozenset[str]], after: dict[str, frozenset[str]]) -> list[str]:
    """``kind:name`` of what ``after`` holds that ``before`` did not."""
    return sorted(
        f"{kind}:{name}" for kind in after for name in after[kind] - before.get(kind, frozenset())
    )


@dataclass
class LoadedExtension:
    """The extension under test, with ways to exercise what it registered."""

    core: Core
    session: Session
    name: str
    _before: dict[str, frozenset[str]]

    @property
    def extension(self) -> Extension:
        """The manager's record: ``state``, ``error``, ``info()``."""
        return self.core.extensions.extensions[self.name]  # type: ignore[no-any-return]

    @property
    def registrations(self) -> list[str]:
        """``kind:name`` of everything it registered (``tool:hello_greet`` …)."""
        context = self.extension.context
        return list(context.registrations) if context is not None else []

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        """Call a tool the way the agent loop does, then the post_execute hooks."""
        from snowpea_core.agent.loop import backend_for
        from snowpea_core.tools import result_hooks
        from snowpea_core.tools.registry import ToolContext

        tool = self.core.tools.get(name, self.session)
        if tool is None:
            raise KeyError(f"no tool {name!r}; registered: {self.registrations}")
        ctx = ToolContext(
            session=self.session, core=self.core, backend=backend_for(self.core, self.session)
        )
        result = await tool.run(ctx, dict(args or {}))
        return await result_hooks.POST_EXECUTE.run(ctx, name, dict(args or {}), result)

    def system_prompt(self) -> str:
        """The system prompt the session's next model call would get."""
        from snowpea_core.agent import agent

        return agent.build_system_prompt(
            self.session, self.core.tools.specs(self.session), core=self.core
        )

    def runtime_context(self) -> dict[str, str]:
        """``{provider name: text}`` of the runtime context right now."""
        from snowpea_core.agent import runtime_context

        return dict(runtime_context.REGISTRY.collect(self.core, self.session))

    async def stop_turn(self, last_message: str) -> StopDecision | None:
        """What the ``turn_stopping`` chain decides for an answer ``last_message``."""
        from snowpea_core.agent import turn_stopping

        return await turn_stopping.CHAIN.run(
            self.core, self.session, turn_stopping.StopContext(last_message=last_message)
        )

    async def run_command(self, name: str, args: str = "") -> list[str]:
        """Run ``/name args``; returns what it said (``cmd_ctx.say``)."""
        from snowpea_core.commands.registry import CommandContext

        command = self.core.commands.get(name)
        if command is None:
            raise KeyError(f"no command /{name}")
        ctx = CommandContext(
            core=self.core,
            session=self.session,
            turn_id="t-extension-test",
            command_name=name,
            command_args=args,
        )
        await command.run(ctx, args)
        return list(ctx.output_texts)

    async def reload(self, config: dict[str, Any] | None = None) -> Extension:
        """Reload from disk, optionally with a new config."""
        if config is not None:
            self.core.settings.extensions.setdefault(self.name, {})["config"] = dict(config)
        return await self.core.extensions.reload(self.name)  # type: ignore[no-any-return]

    async def unload(self) -> list[str]:
        """Unload it; returns what it left registered (empty when it cleaned up)."""
        await self.core.extensions.unload(self.name)
        return leftovers(self._before, snapshot(self.core))


@contextlib.asynccontextmanager
async def load_extension(
    folder: Path | str,
    *,
    config: dict[str, Any] | None = None,
    home: Path | str | None = None,
    check: bool = True,
) -> AsyncIterator[LoadedExtension]:
    """Load the extension in ``folder`` into a private daemon for one test.

    ``config`` becomes ``settings.extensions.<name>.config``.  ``home`` is the
    daemon's ``SNOWPEA_HOME`` (a fresh temporary folder when omitted; pass
    pytest's ``tmp_path``).  With ``check`` (the default) a load failure raises
    :class:`ExtensionLoadError` with the extension's error, and leaving
    anything registered after unload fails the test.
    """
    from snowpea_core.server.app_server import Daemon

    source = Path(folder).resolve()
    name = source.name
    with contextlib.ExitStack() as stack:
        if home is None:
            home = stack.enter_context(tempfile.TemporaryDirectory(prefix="snowpea-ext-"))
        root = Path(home)
        root.mkdir(parents=True, exist_ok=True)
        with _env(_QUIET_ENV):
            daemon = Daemon(port=0, home=root)
            await daemon.start()
        try:
            core = daemon.core
            assert core is not None
            workdir = root / "project"
            workdir.mkdir(exist_ok=True)
            session = await core.sessions.create(
                workdir, mode="accept", caller=ModeCaller.internal("extension-test-harness")
            )
            before = snapshot(core)
            shutil.copytree(
                source,
                root / "extensions" / name,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests"),
                dirs_exist_ok=True,
            )
            if config is not None:
                core.settings.extensions.setdefault(name, {})["config"] = dict(config)
            await core.extensions.rescan()
            await core.extensions.load(name)
            loaded = LoadedExtension(core=core, session=session, name=name, _before=before)
            if check and loaded.extension.state != "loaded":
                raise ExtensionLoadError(f"{name}: {loaded.extension.error}")
            yield loaded
            left = await loaded.unload()
            if check and left:
                raise AssertionError(f"{name} left registered after unload: {left}")
        finally:
            await daemon.stop()


__all__ = ["ExtensionLoadError", "LoadedExtension", "leftovers", "load_extension", "snapshot"]
