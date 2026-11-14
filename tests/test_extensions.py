"""Python plugins (M16 Phase 3): everything a plugin registers leaves with it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _support import make_daemon

from snowpea_core.agent import runtime_context, turn_stopping
from snowpea_core.extensions.api import PLUGIN_MCP
from snowpea_core.extensions.manager import ExtensionManager
from snowpea_core.prompts import sections
from snowpea_core.tools import result_hooks

PLUGIN = '''
from snowpea_core.agent.runtime_context import ContextProvider
from snowpea_core.commands.registry import Command
from snowpea_core.prompts.sections import PromptSection
from snowpea_core.tools.registry import Tool, ToolResult


async def _ticket(ctx, args):
    return ToolResult(ok=True, output="TICKET-42")


async def _cmd(ctx, args):
    await ctx.say("hello from the plugin")


def apply(ctx):
    ctx.tools.register(Tool(name="ticket_lookup", category="ext", description="look up",
                            input_schema={"type": "object"}, permission="read", run=_ticket))
    ctx.prompt.section(PromptSection("ext:style", 650, lambda i: "House style: terse."))
    ticket = ctx.config.get("ticket", "?")
    ctx.prompt.context(ContextProvider("ext:ticket", 500, lambda c, s: "Ticket: " + ticket))
    ctx.hooks.post_execute("tag", lambda c, n, a, r: None)
    ctx.hooks.turn_stopping("never", lambda c, s, x: None)
    ctx.commands.register(Command(name="ticket", summary="show the ticket", run=_cmd))
    ctx.mcp.register("ext-docs", {"command": "true"})
'''


def _install(home: Path, name: str, body: str, manifest: dict | None = None) -> Path:
    folder = home / "extensions" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.py").write_text(body, encoding="utf-8")
    if manifest is not None:
        (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def _state() -> dict[str, Any]:
    return {
        "sections": {s.name for s in sections.REGISTRY.sections()},
        "contexts": {p.name for p in runtime_context.REGISTRY.providers()},
        "post": {e.name for e in result_hooks.POST_EXECUTE.listeners()},
        "stop": {e.name for e in turn_stopping.CHAIN.listeners()},
        "mcp": set(PLUGIN_MCP),
    }


async def test_a_plugin_registers_everything_and_unloading_removes_it(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        _install(core.paths.home, "tickets", PLUGIN, {"name": "tickets", "version": "1.0.0"})
        before = _state()
        manager = ExtensionManager(core)
        [info] = manager.load_all()
        assert info["state"] == "loaded" and info["effects"] == 7, info
        after = _state()
        assert "ext:style" in after["sections"] and "ext:ticket" in after["contexts"]
        assert "tickets:tag" in after["post"] and "tickets:never" in after["stop"]
        assert "ext-docs" in after["mcp"]
        tool = core.tools.get("ticket_lookup")
        assert tool is not None and tool.source == "extension:tickets"
        assert core.commands.get("ticket") is not None
        text = "\n".join(t for _s, t in sections.REGISTRY.render(sections.PromptInputs()))
        assert "House style: terse." in text

        manager.unload("tickets")
        assert _state() == before
        assert core.tools.get("ticket_lookup") is None and core.commands.get("ticket") is None
    finally:
        await daemon.stop()


async def test_a_plugin_that_fails_leaves_nothing_behind(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        body = PLUGIN + "\n    raise RuntimeError('half way')\n"
        _install(core.paths.home, "broken", body)
        before = _state()
        manager = ExtensionManager(core)
        [info] = manager.load_all()
        assert info["state"] == "error" and "half way" in info["error"]
        assert _state() == before and core.tools.get("ticket_lookup") is None
    finally:
        await daemon.stop()


async def test_disable_enable_and_reload_after_an_edit(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        folder = _install(core.paths.home, "tickets", PLUGIN)
        manager = ExtensionManager(core)
        manager.load_all()
        manager.set_enabled("tickets", False)
        assert core.tools.get("ticket_lookup") is None
        manager.set_enabled("tickets", True)
        assert core.tools.get("ticket_lookup") is not None
        (folder / "plugin.py").write_text(
            PLUGIN.replace("House style: terse.", "House style: warm."), encoding="utf-8"
        )
        ext = manager.reload("tickets")
        assert ext.state == "loaded"
        text = "\n".join(t for _s, t in sections.REGISTRY.render(sections.PromptInputs()))
        assert "House style: warm." in text and "terse" not in text
        manager.unload("tickets")
    finally:
        await daemon.stop()


async def test_a_plugin_for_a_newer_api_is_not_loaded(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        _install(core.paths.home, "future", PLUGIN, {"engines": {"snowpea": "^9.0.0"}})
        [info] = ExtensionManager(core).load_all()
        assert info["state"] == "incompatible" and core.tools.get("ticket_lookup") is None
    finally:
        await daemon.stop()


async def test_a_built_in_command_cannot_be_replaced(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        body = PLUGIN.replace('Command(name="ticket"', 'Command(name="goal"')
        _install(core.paths.home, "grabby", body)
        [info] = ExtensionManager(core).load_all()
        assert info["state"] == "error" and "built-in" in info["error"]
        assert core.commands.get("goal").run.__module__ == "snowpea_core.commands.goal_cmd"
    finally:
        await daemon.stop()


# ---- over the protocol ----------------------------------------------------------


async def test_extension_rpcs_list_configure_and_toggle(tmp_path: Any) -> None:
    import pytest

    from snowpea_core.server.errors import RpcError
    from snowpea_core.server.extension_handlers import (
        extension_configure_handler,
        extension_disable_handler,
        extension_enable_handler,
        extension_list_handler,
    )
    from snowpea_core.server.protocol import (
        Empty,
        ExtensionConfigureParams,
        ExtensionNameParams,
    )

    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        schema = {
            "type": "object",
            "properties": {"ticket": {"type": "string"}},
            "additionalProperties": False,
        }
        _install(core.paths.home, "tickets", PLUGIN, {"name": "tickets", "configSchema": schema})
        listing = await extension_list_handler(None, Empty(), core)
        [ext] = listing.extensions
        assert ext.name == "tickets" and ext.configSchema == schema
        with pytest.raises(RpcError):
            await extension_configure_handler(
                None, ExtensionConfigureParams(name="tickets", config={"nope": 1}), core
            )
        result = await extension_configure_handler(
            None, ExtensionConfigureParams(name="tickets", config={"ticket": "SP-7"}), core
        )
        assert result.extension.config == {"ticket": "SP-7"} and result.extension.state == "loaded"
        provider = next(
            p for p in runtime_context.REGISTRY.providers() if p.name == "ext:ticket"
        )
        assert provider.render(core, None) == "Ticket: SP-7"
        off = await extension_disable_handler(None, ExtensionNameParams(name="tickets"), core)
        assert off.extension.state == "disabled" and core.tools.get("ticket_lookup") is None
        assert core.settings.extensions["tickets"]["enabled"] is False
        on = await extension_enable_handler(None, ExtensionNameParams(name="tickets"), core)
        assert on.extension.state == "loaded"
        # The plugin's MCP server shows up among the declared servers, owned by it.
        from snowpea_core.server.mcp_handlers import declared

        rows = declared(core, tmp_path)
        assert rows["ext-docs"].plugin == "tickets"
        core.extensions.unload("tickets")
    finally:
        await daemon.stop()
