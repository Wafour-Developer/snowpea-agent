"""Extensions from a developer's side: errors, guards, lifecycle, isolation, CLI.

Each test writes a small extension into a temporary ``SNOWPEA_HOME`` and loads
it through the daemon's real :class:`ExtensionManager`.  No network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from _support import make_daemon

from snowpea_core.agent import runtime_context, turn_stopping
from snowpea_core.extensions.api import PLUGIN_MCP, ExtensionError, PluginContext
from snowpea_core.extensions.manager import (
    compatible,
    package_name,
    render_template,
    scaffold,
)
from snowpea_core.prompts import compose, sections
from snowpea_core.tools import result_hooks

FIXTURE_ECHO = Path(__file__).parent / "fixtures" / "mcp" / "echo_server.py"

HEADER = """
from snowpea_core.extensions import (
    Command, ContextProvider, PromptSection, StopDecision, Tool, ToolResult,
)
"""


def _install(
    home: Path,
    name: str,
    body: str,
    manifest: dict[str, Any] | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    folder = home / "extensions" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.py").write_text(HEADER + textwrap.dedent(body), encoding="utf-8")
    if manifest is not None:
        (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    for file_name, text in (files or {}).items():
        target = folder / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text), encoding="utf-8")
    return folder


def _tool(name: str = "dev_tool", body: str = 'return ToolResult(ok=True, output="ok")') -> str:
    return f"""
async def _run(tool_ctx, args):
    {body}

def apply(ctx):
    ctx.tools.register(Tool(name="{name}", category="dev", description="d",
                            input_schema={{"type": "object"}}, permission="read", run=_run))
"""


def _state() -> dict[str, Any]:
    return {
        "sections": {s.name for s in sections.REGISTRY.sections()},
        "contexts": {p.name for p in runtime_context.REGISTRY.providers()},
        "post": {e.name for e in result_hooks.POST_EXECUTE.listeners()},
        "stop": {e.name for e in turn_stopping.CHAIN.listeners()},
        "mcp": set(PLUGIN_MCP),
        "modules": {m for m in sys.modules if m.startswith("snowpea_ext_")},
    }


@pytest.fixture
async def core(tmp_path: Path) -> Any:
    daemon = await make_daemon(tmp_path / "home")
    try:
        yield daemon.core
    finally:
        await daemon.stop()


async def _load(core: Any, name: str) -> Any:
    manager = core.extensions
    await manager.rescan()
    return await manager.load(name)


# ---- load-time errors ----------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "kind", "line"),
    [
        ("def apply(ctx)\n    pass\n", "SyntaxError", "plugin.py:"),
        ("import not_a_module_anywhere\ndef apply(ctx):\n    pass\n", "ModuleNotFoundError",
         "plugin.py:"),
        ("def apply(ctx):\n    raise RuntimeError('half way')\n", "RuntimeError: half way",
         "plugin.py:"),
        ("x = 1\n", "TypeError: plugin.py defines no apply(ctx)", ""),
    ],
)
async def test_load_errors_name_the_type_message_and_line(
    core: Any, body: str, kind: str, line: str, caplog: Any
) -> None:
    before = _state()
    _install(core.paths.home, "broken", body)
    caplog.set_level(logging.WARNING, logger="snowpea.extensions")
    ext = await _load(core, "broken")
    assert ext.state == "error" and kind in ext.error, ext.error
    assert line in ext.error
    if line:
        # The line number is the one in the extension's own file.
        assert ext.error.rstrip(")").rsplit(":", 1)[1].isdigit()
    assert _state() == before
    assert any("broken" in record.getMessage() for record in caplog.records)


async def test_a_failure_part_way_through_apply_undoes_what_was_registered(core: Any) -> None:
    before = _state()
    _install(
        core.paths.home,
        "half",
        """
        def apply(ctx):
            ctx.prompt.section(PromptSection("half:a", 650, lambda i: "A"))
            ctx.hooks.post_execute("watch", lambda *a: None)
            ctx.tools.register(Tool(name="half_tool", category="x", description="d",
                                    input_schema={"type": "object"}, permission="read",
                                    run=_run))
            raise ValueError("boom")

        async def _run(tool_ctx, args):
            return ToolResult(ok=True)
        """,
    )
    ext = await _load(core, "half")
    assert ext.state == "error" and "ValueError: boom (at plugin.py:" in ext.error
    assert _state() == before and core.tools.get("half_tool") is None


@pytest.mark.parametrize(
    ("manifest_text", "needle"),
    [
        ('{"name": "bad",', "plugin.json:1: not valid JSON"),
        ('["x"]', "must be a JSON object"),
        ('{"version": 3}', "version must be a string"),
        ('{"name": "other"}', "differs from the folder name"),
        ('{"configSchema": {"type": "nope"}}', "configSchema is not a valid JSON Schema"),
    ],
)
async def test_a_bad_plugin_json_is_reported(core: Any, manifest_text: str, needle: str) -> None:
    folder = _install(core.paths.home, "bad", _tool("bad_tool"))
    (folder / "plugin.json").write_text(manifest_text, encoding="utf-8")
    ext = await _load(core, "bad")
    assert ext.state == "error" and needle in ext.error, ext.error
    assert core.tools.get("bad_tool") is None


def test_engines_ranges() -> None:
    def ok(value: str) -> bool:
        return compatible({"engines": {"snowpea": value}}, api=1)[0]

    assert ok("^1") and ok("1") and ok("1.x") and ok("~1.2") and ok(">=1") and ok(">=0")
    assert not ok("^0") and not ok("^2") and not ok(">=2") and not ok("latest")
    assert "not understood" in compatible({"engines": {"snowpea": "latest"}})[1]
    assert compatible({})[0]


async def test_an_incompatible_extension_says_which_api(core: Any) -> None:
    _install(core.paths.home, "old", _tool("old_tool"), {"engines": {"snowpea": "^0"}})
    ext = await _load(core, "old")
    assert ext.state == "incompatible" and "provides API 1" in ext.error
    assert core.tools.get("old_tool") is None


# ---- collisions --------------------------------------------------------------------


async def test_a_built_in_tool_cannot_be_replaced(core: Any) -> None:
    builtin = core.tools.get("read_file")
    _install(core.paths.home, "grab", _tool("read_file"))
    ext = await _load(core, "grab")
    assert ext.state == "error" and "already registered by builtin" in ext.error
    assert core.tools.get("read_file") is builtin


async def test_two_extensions_cannot_take_the_same_names(core: Any) -> None:
    body = """
    async def _run(tool_ctx, args):
        return ToolResult(ok=True)

    async def _cmd(cmd_ctx, args):
        pass

    def apply(ctx):
        ctx.tools.register(Tool(name="shared", category="x", description="d",
                                input_schema={"type": "object"}, permission="read", run=_run))
    """
    _install(core.paths.home, "first", body)
    _install(core.paths.home, "second", body)
    assert (await _load(core, "first")).state == "loaded"
    second = await _load(core, "second")
    assert second.state == "error" and "already registered by extension:first" in second.error
    assert core.tools.get("shared").source == "extension:first"


@pytest.mark.parametrize(
    ("call", "needle"),
    [
        ('ctx.prompt.section(PromptSection("identity", 1, lambda i: "x"))',
         "prompt section identity is already registered by builtin"),
        ('ctx.prompt.context(ContextProvider("plan", 1, lambda c, s: "x"))',
         "runtime context plan is already registered by builtin"),
        ('ctx.commands.register(Command(name="mode", summary="s", run=_cmd))', "built-in"),
        ('ctx.commands.register(Command(name="/x", summary="s", run=_cmd))', "without '/'"),
        ('ctx.tools.register(Tool(name="t", category="c", description="d", '
         'input_schema={"type": "object"}, permission="admin", run=_cmd))',
         "permission 'admin' must be one of"),
        ('ctx.tools.register(Tool(name="t", category="c", description="d", '
         'input_schema={"type": "object"}, permission="read", run=lambda c, a: None))',
         "run must be an async function"),
        ('ctx.tools.register(Tool(name="t", category="c", description="d", '
         'input_schema={"type": "string"}, permission="read", run=_cmd))',
         '"type": "object"'),
        ('ctx.tools.register(Tool(name="t", category="c", description="d", '
         'input_schema={"type": "object"}, permission="write", run=_cmd, parallel_safe=True))',
         "parallel_safe"),
        ('ctx.tools.register("t")', "needs a snowpea_core.extensions.Tool"),
        ('ctx.hooks.post_execute("a b", lambda *a: None)', "short slug"),
        ('ctx.mcp.register("srv", {"args": []})', "needs a command or a url"),
    ],
)
async def test_malformed_registrations_fail_apply_with_a_clear_error(
    core: Any, call: str, needle: str
) -> None:
    before = _state()
    _install(
        core.paths.home,
        "malformed",
        f"""
        async def _cmd(*args):
            return None

        def apply(ctx):
            {call}
        """,
    )
    ext = await _load(core, "malformed")
    assert ext.state == "error" and needle in ext.error, ext.error
    assert "ExtensionError" in ext.error and "(at plugin.py:" in ext.error
    assert _state() == before


# ---- run-time guards --------------------------------------------------------------


async def test_a_tool_that_raises_is_that_calls_error_not_the_daemons(
    core: Any, caplog: Any
) -> None:
    _install(
        core.paths.home,
        "flaky",
        """
        import asyncio

        async def _boom(tool_ctx, args):
            return {}["missing"]

        async def _text(tool_ctx, args):
            return "plain text"

        async def _wrong(tool_ctx, args):
            return 42

        async def _slow(tool_ctx, args):
            await asyncio.sleep(5)

        def _t(name, run):
            return Tool(name=name, category="x", description="d",
                        input_schema={"type": "object"}, permission="read", run=run)

        def apply(ctx):
            ctx.tools.register(_t("flaky_boom", _boom))
            ctx.tools.register(_t("flaky_text", _text))
            ctx.tools.register(_t("flaky_wrong", _wrong))
            ctx.tools.register(_t("flaky_slow", _slow), timeout=0.05)
        """,
    )
    assert (await _load(core, "flaky")).state == "loaded"
    caplog.set_level(logging.WARNING, logger="snowpea.extensions")
    from snowpea_core.extensions import ToolContext

    call = ToolContext(session=None, core=core, backend=None)  # type: ignore[arg-type]
    boom = await core.tools.get("flaky_boom").run(call, {})
    assert not boom.ok and "flaky_boom (extension flaky) failed: KeyError" in boom.error
    assert "(at plugin.py:" in boom.error
    assert any("extension flaky: tool flaky_boom failed" in r.getMessage() for r in caplog.records)
    text = await core.tools.get("flaky_text").run(call, {})
    assert text.ok and text.output == "plain text"
    wrong = await core.tools.get("flaky_wrong").run(call, {})
    assert not wrong.ok and "returned int, not a ToolResult" in wrong.error
    slow = await core.tools.get("flaky_slow").run(call, {})
    assert not slow.ok and "timed out after 0.05s" in slow.error


async def test_a_section_or_provider_that_raises_is_skipped(core: Any) -> None:
    _install(
        core.paths.home,
        "noisy",
        """
        def _bad(*args):
            raise RuntimeError("render broke")

        def apply(ctx):
            ctx.prompt.section(PromptSection("noisy:bad", 650, _bad))
            ctx.prompt.section(PromptSection("noisy:good", 651, lambda i: "GOOD SECTION"))
            ctx.prompt.context(ContextProvider("noisy:bad", 500, _bad))
            ctx.prompt.context(ContextProvider("noisy:good", 501, lambda c, s: "GOOD FACT"))
        """,
    )
    assert (await _load(core, "noisy")).state == "loaded"
    prompt = compose.build_system_prompt()
    assert "GOOD SECTION" in prompt
    items = runtime_context.REGISTRY.collect(core, object())
    assert ("noisy:good", "GOOD FACT") in items
    assert all(name != "noisy:bad" for name, _ in items)


async def test_hooks_that_raise_are_skipped_and_decisions_apply(core: Any) -> None:
    _install(
        core.paths.home,
        "hooks",
        """
        from snowpea_core.extensions import PostDecision

        def _bad(*args):
            raise RuntimeError("hook broke")

        def _redact(tool_ctx, name, args, result):
            return PostDecision.accept(result.output.replace("secret", "[redacted]"))

        def apply(ctx):
            ctx.hooks.post_execute("bad", _bad, order=-1)
            ctx.hooks.post_execute("redact", _redact)
            ctx.hooks.turn_stopping("bad", _bad)
        """,
    )
    assert (await _load(core, "hooks")).state == "loaded"
    from snowpea_core.extensions import ToolResult

    plain = ToolResult(ok=True, output="a secret")
    result = await result_hooks.POST_EXECUTE.run(None, "x", {}, plain)
    assert result.output == "a [redacted]"


# ---- config -------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "ticket": {"type": "string", "pattern": "^[A-Z]+-[0-9]+$"},
        "limit": {"type": "integer", "minimum": 1, "default": 5},
    },
    "required": ["ticket"],
    "additionalProperties": False,
}

CONFIG_BODY = """
def apply(ctx):
    text = f"{ctx.config['ticket']} x{ctx.config['limit']}"
    ctx.prompt.section(PromptSection("cfg:line", 650, lambda i: text))
"""


async def test_invalid_config_names_the_failing_path(core: Any) -> None:
    _install(core.paths.home, "cfg", CONFIG_BODY, {"configSchema": SCHEMA})
    core.settings.extensions["cfg"] = {"config": {"ticket": "nope"}}
    ext = await _load(core, "cfg")
    assert ext.state == "error"
    assert ext.error.startswith("settings.extensions.cfg.config.ticket: 'nope' does not match")
    core.settings.extensions["cfg"] = {"config": {}}
    ext = await core.extensions.reload("cfg")
    assert "'ticket' is a required property" in ext.error
    core.settings.extensions["cfg"] = {"config": {"ticket": "SP-1"}}
    ext = await core.extensions.reload("cfg")
    assert ext.state == "loaded" and ext.context.config == {"ticket": "SP-1", "limit": 5}
    assert "SP-1 x5" in compose.build_system_prompt()


async def test_an_edited_settings_file_reconfigures_without_a_restart(core: Any) -> None:
    _install(core.paths.home, "cfg", CONFIG_BODY, {"configSchema": SCHEMA})
    settings = core.paths.home / "settings.json"

    def write(config: dict[str, Any], enabled: bool = True) -> None:
        data = json.loads(settings.read_text()) if settings.exists() else {}
        data["extensions"] = {"cfg": {"enabled": enabled, "config": config}}
        settings.write_text(json.dumps(data), encoding="utf-8")

    async def reload() -> None:
        # Security review 8, M8-5: ``extensions`` is the daemon's own code, so an
        # edit of the file waits for a local person (changed expectation: it used
        # to apply by itself); accepting it reconfigures without a restart.
        await core.reload_settings(force=True)
        assert any(key.startswith("extensions") for key in core.settings_waiting)
        await core.accept_disk_settings(True)

    write({"ticket": "SP-1"})
    await reload()
    assert "SP-1 x5" in compose.build_system_prompt()
    write({"ticket": "SP-2", "limit": 9})
    await reload()
    assert "SP-2 x9" in compose.build_system_prompt()
    write({"ticket": "SP-2", "limit": 9}, enabled=False)
    await reload()
    assert core.extensions.extensions["cfg"].state == "disabled"
    assert "SP-2" not in compose.build_system_prompt()


# ---- lifecycle ----------------------------------------------------------------------


async def test_async_apply_and_async_disposers_run_in_reverse_order(core: Any) -> None:
    order: list[str] = []
    core.test_order = order  # read by the extension
    _install(
        core.paths.home,
        "resources",
        """
        import asyncio

        async def apply(ctx):
            order = ctx.core.test_order
            await asyncio.sleep(0)
            ctx.on_dispose(lambda: order.append("first"))

            async def close_second():
                await asyncio.sleep(0)
                order.append("second")

            ctx.on_dispose(close_second)

            async def hangs():
                await asyncio.sleep(60)

            ctx.on_dispose(hangs)
            ctx.on_dispose(lambda: order.append("last"))
        """,
    )
    import snowpea_core.extensions.api as api

    api_timeout = api.DISPOSE_TIMEOUT
    ext = await _load(core, "resources")
    assert ext.state == "loaded" and ext.context.effects == 4
    ctx = ext.context
    problems = await ctx.dispose(timeout=0.05)
    assert order == ["last", "second", "first"]
    assert problems and "did not finish within 0.05s" in problems[0]
    assert api.DISPOSE_TIMEOUT == api_timeout
    with pytest.raises(ExtensionError, match="is unloaded"):
        ctx.on_dispose(lambda: None)
    ext.context = None


async def test_reload_picks_up_edited_submodules_and_plugin_json(core: Any) -> None:
    body = """
    from . import helpers
    from .lib.words import WORD

    def apply(ctx):
        ctx.prompt.section(PromptSection("sub:text", 650,
                                         lambda i: helpers.TEXT + " " + WORD))
    """
    folder = _install(
        core.paths.home,
        "sub",
        body,
        {"version": "1.0.0"},
        {"helpers.py": "TEXT = 'alpha'\n", "lib/__init__.py": "", "lib/words.py": "WORD = 'one'\n"},
    )
    assert (await _load(core, "sub")).state == "loaded"
    assert "alpha one" in compose.build_system_prompt()
    # Same size, same second: a cached .pyc must not win.
    (folder / "helpers.py").write_text("TEXT = 'gamma'\n", encoding="utf-8")
    (folder / "lib" / "words.py").write_text("WORD = 'two'\n", encoding="utf-8")
    (folder / "plugin.json").write_text(json.dumps({"version": "1.1.0"}), encoding="utf-8")
    ext = await core.extensions.reload("sub")
    assert ext.state == "loaded" and ext.info()["version"] == "1.1.0"
    prompt = compose.build_system_prompt()
    assert "gamma two" in prompt and "alpha" not in prompt


async def test_two_extensions_never_share_a_module(core: Any) -> None:
    for name, word in (("left", "LEFT"), ("right", "RIGHT")):
        folder = _install(
            core.paths.home,
            name,
            f"""
            from . import helpers

            def apply(ctx):
                ctx.prompt.section(PromptSection("{name}:w", 650, lambda i: helpers.WORD))
            """,
        )
        (folder / "helpers.py").write_text(f"WORD = '{word}'\n", encoding="utf-8")
        assert (await _load(core, name)).state == "loaded"
    prompt = compose.build_system_prompt()
    assert "LEFT" in prompt and "RIGHT" in prompt
    assert package_name("left") != package_name("right")
    assert package_name("a-b") != package_name("a_b")
    await core.extensions.unload("left")
    assert not [m for m in sys.modules if m.startswith(package_name("left"))]
    assert [m for m in sys.modules if m.startswith(package_name("right"))]


async def test_a_removed_folder_is_unloaded_by_the_next_rescan(core: Any) -> None:
    import shutil

    folder = _install(core.paths.home, "gone", _tool("gone_tool"))
    assert (await _load(core, "gone")).state == "loaded"
    shutil.rmtree(folder)
    await core.extensions.rescan()
    assert "gone" not in core.extensions.extensions and core.tools.get("gone_tool") is None


async def test_a_disabled_extension_stays_off_on_reload(core: Any) -> None:
    _install(core.paths.home, "off", _tool("off_tool"))
    await _load(core, "off")
    await core.extensions.set_enabled("off", False)
    ext = await core.extensions.reload("off")
    assert ext.state == "disabled" and core.tools.get("off_tool") is None


async def test_running_sessions_see_a_change_at_their_next_model_call(core: Any) -> None:
    """Tools, sections and providers are read live: no cache keeps the old set."""
    _install(
        core.paths.home,
        "live",
        """
        async def _run(tool_ctx, args):
            return ToolResult(ok=True)

        def apply(ctx):
            ctx.tools.register(Tool(name="live_tool", category="x", description="d",
                                    input_schema={"type": "object"}, permission="read",
                                    run=_run))
            ctx.prompt.section(PromptSection("live:s", 650, lambda i: "LIVE SECTION"))
        """,
    )
    names = {spec.name for spec in core.tools.all_specs()}
    assert "live_tool" not in names
    await _load(core, "live")
    assert "live_tool" in {spec.name for spec in core.tools.all_specs()}
    assert "LIVE SECTION" in compose.build_system_prompt()
    await core.extensions.unload("live")
    assert "live_tool" not in {spec.name for spec in core.tools.all_specs()}
    assert "LIVE SECTION" not in compose.build_system_prompt()


async def test_the_daemon_loads_extensions_at_start_and_disposes_them_at_stop(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    marker = tmp_path / "disposed.txt"
    _install(
        home,
        "boot",
        f"""
        async def _run(tool_ctx, args):
            return ToolResult(ok=True)

        def apply(ctx):
            ctx.tools.register(Tool(name="boot_tool", category="x", description="d",
                                    input_schema={{"type": "object"}}, permission="read",
                                    run=_run))

            async def closing():
                open({str(marker)!r}, "w").write("closed")

            ctx.on_dispose(closing)
        """,
    )
    daemon = await make_daemon(home)
    try:
        assert daemon.core.tools.get("boot_tool").source == "extension:boot"
    finally:
        await daemon.stop()
    assert marker.read_text() == "closed"


# ---- isolation hygiene ------------------------------------------------------------


async def test_extension_tools_are_tagged_and_the_approval_names_them(core: Any) -> None:
    from snowpea_core.agent.loop import _source_note

    _install(core.paths.home, "tagged", _tool("tagged_tool"))
    await _load(core, "tagged")
    tool = core.tools.get("tagged_tool")
    [info] = [row for row in core.tools.list() if row.name == "tagged_tool"]
    assert info.source == "extension:tagged" and info.permissionTag == "read"
    assert _source_note(tool, "") == "from extension tagged"
    assert _source_note(tool, "careful") == "from extension tagged; careful"
    assert _source_note(core.tools.get("read_file"), "careful") == "careful"


async def test_an_extension_command_is_tagged(core: Any) -> None:
    _install(
        core.paths.home,
        "cmds",
        """
        async def _cmd(cmd_ctx, args):
            await cmd_ctx.say("hi " + args)

        def apply(ctx):
            ctx.commands.register(Command(name="hi", summary="say hi", run=_cmd))
        """,
    )
    await _load(core, "cmds")
    assert core.commands.get("hi").source == "extension:cmds"
    await core.extensions.unload("cmds")
    assert core.commands.get("hi") is None


def test_a_context_rejects_registration_after_dispose() -> None:
    ctx = PluginContext(core=None, name="x")  # type: ignore[arg-type]
    asyncio.run(ctx.dispose())
    with pytest.raises(ExtensionError):
        ctx.hooks.post_execute("late", lambda *a: None)


# ---- MCP ------------------------------------------------------------------------------


async def test_an_mcp_server_starts_with_the_extension_and_stops_with_it(core: Any) -> None:
    _install(
        core.paths.home,
        "mcpext",
        f"""
        import sys

        def apply(ctx):
            ctx.mcp.register("ext-echo", {{"command": sys.executable,
                                          "args": [{str(FIXTURE_ECHO)!r}]}})
        """,
    )
    assert (await _load(core, "mcpext")).state == "loaded"
    for _ in range(200):
        if core.tools.get("mcp__ext-echo__echo") is not None:
            break
        await asyncio.sleep(0.05)
    tool = core.tools.get("mcp__ext-echo__echo")
    assert tool is not None and tool.source == "mcp:ext-echo"
    from snowpea_core.server.mcp_handlers import declared
    from snowpea_core.tools import mcp_client

    assert declared(core, None)["ext-echo"].plugin == "mcpext"
    await core.extensions.unload("mcpext")
    assert core.tools.get("mcp__ext-echo__echo") is None
    assert mcp_client.MANAGER.get("ext-echo") is None and "ext-echo" not in PLUGIN_MCP


# ---- scaffold and CLI -------------------------------------------------------------


def test_the_template_renders_a_valid_name(tmp_path: Path) -> None:
    files = render_template("my-ext")
    assert set(files) == {"plugin.py", "plugin.json"}
    assert 'name="my_ext_greet"' in files["plugin.py"]
    assert json.loads(files["plugin.json"])["name"] == "my-ext"
    assert 'name="x9lives_greet"' in render_template("9lives")["plugin.py"]
    folder = scaffold(tmp_path, "my-ext")
    assert (folder / "plugin.py").is_file()
    with pytest.raises(FileExistsError):
        scaffold(tmp_path, "my-ext")
    with pytest.raises(ValueError):
        scaffold(tmp_path, "../escape")


async def test_cli_new_reload_list_info_disable(tmp_path: Path, capsys: Any) -> None:
    import argparse

    from snowpea_core.cli.extension_cmd import extension_command

    home = tmp_path / "home"
    daemon = await make_daemon(home)
    try:

        async def run(action: str, name: str = "", as_json: bool = True) -> Any:
            code = await extension_command(
                action, argparse.Namespace(name=name), home, as_json=as_json
            )
            out = capsys.readouterr().out
            return code, (json.loads(out) if as_json and out.strip() else out)

        code, made = await run("new", "hello")
        assert code == 0 and made["files"] == ["plugin.json", "plugin.py"]
        code, again = await run("new", "hello")
        assert code == 2
        code, rows = await run("list")
        assert code == 0 and rows[0]["name"] == "hello" and rows[0]["state"] == "unloaded"
        code, row = await run("reload", "hello")
        assert code == 0 and row["state"] == "loaded" and row["effects"] == 1
        code, detail = await run("info", "hello")
        assert code == 0 and [t["name"] for t in detail["tools"]] == ["hello_greet"]
        assert detail["tools"][0]["source"] == "extension:hello"
        code, text = await run("list", as_json=False)
        assert "hello" in text and "loaded" in text
        # A broken edit: reload exits 1 and says where.
        plugin = home / "extensions" / "hello" / "plugin.py"
        plugin.write_text(plugin.read_text() + "\ndef broken(:\n", encoding="utf-8")
        code, rows = await run("reload")
        assert code == 1 and "SyntaxError" in rows[0]["error"]
        assert daemon.core.tools.get("hello_greet") is None
        code, row = await run("disable", "hello")
        assert code == 0 and row["state"] == "disabled" and row["enabled"] is False
        code, _ = await run("info", "nope")
        assert code == 2
    finally:
        await daemon.stop()
