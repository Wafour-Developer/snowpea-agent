"""The M13 LSP contract (``docs/design/m13-lsp-contract.md``), AC-43…AC-47.

Nothing here needs a real language server: ``tests/fixtures/lsp/fake_server.py``
is a stdio server that speaks the same protocol over a toy language, so the
client, the manager, the seven tools and the ``Diagnostics`` block are all
exercised on every machine.  The tests that do want a real server (pyright,
typescript-language-server) are skip-marked on whether it is on PATH.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from snowpea_core import lsp
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import LspSettings, Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.lsp import diagnostic, language, servers
from snowpea_core.lsp import tools as lsp_tools
from snowpea_core.lsp.manager import LspManager, manager_for
from snowpea_core.server.app_server import Core
from snowpea_core.server.lsp_handlers import lsp_catalog_handler, lsp_status_handler
from snowpea_core.server.protocol import Empty
from snowpea_core.session import events
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import ToolContext, ToolRegistry, register_builtin_tools

FAKE_SERVER = Path(__file__).parent / "fixtures" / "lsp" / "fake_server.py"

#: A ``lsp.servers`` entry pointing at the fixture server.
FAKE_SPEC: dict[str, Any] = {
    "command": [sys.executable, str(FAKE_SERVER)],
    "extensions": [".fake"],
}


async def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the LSP layer must never spawn a shell")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    target = tmp_path / "work"
    target.mkdir()
    return target


def build_core(tmp_path: Path, *, lsp_settings: LspSettings | None = None) -> Core:
    settings = Settings()
    if lsp_settings is not None:
        settings.lsp = lsp_settings
    core = Core(settings=settings, paths=Paths.create(tmp_path / "home"), token="test-token")
    register_builtin_tools(core.tools)
    lsp_tools.refresh_state(core)
    return core


@pytest.fixture
def core(tmp_path: Path) -> Core:
    """A core whose only language server is the fixture one."""
    return build_core(tmp_path, lsp_settings=LspSettings(servers={"fake": FAKE_SPEC}))


@pytest.fixture
def ctx(core: Core, workdir: Path) -> ToolContext:
    session = Session(id="s-lsp", workdir=workdir)
    return ToolContext(session=session, core=core, backend=LocalBackend(workdir))


async def shutdown(core: Core) -> None:
    manager = getattr(core, "lsp", None)
    if manager is not None:
        await manager.shutdown()


async def run(ctx: ToolContext, name: str, **args: Any) -> Any:
    tool = ctx.core.tools.get(name)
    assert tool is not None, f"{name} is not registered"
    return await tool.run(ctx, args)


# ---------------------------------------------------------------------------
# §2 units: language, diagnostic, servers
# ---------------------------------------------------------------------------


def test_language_id_maps_extensions_and_bare_names() -> None:
    assert language.language_id("a/b/c.py") == "python"
    assert language.language_id("x.tsx") == "typescriptreact"
    assert language.language_id("Makefile") == "makefile"
    assert language.language_id("mystery.qqq") == language.PLAINTEXT
    assert language.extension_of("Dockerfile") == "Dockerfile"
    assert language.extension_of("a/b.rs") == ".rs"


def test_diagnostic_report_renders_severity_position_and_code() -> None:
    block = diagnostic.report(
        "app.py",
        [
            {
                "range": {"start": {"line": 11, "character": 4}},
                "severity": 1,
                "code": "reportUndefinedVariable",
                "message": "foo is not defined",
            }
        ],
    )
    assert 'file="app.py"' in block
    assert "ERROR [12:5] foo is not defined (reportUndefinedVariable)" in block


def test_diagnostic_report_caps_at_twenty_with_a_tail() -> None:
    many = [
        {"range": {"start": {"line": n, "character": 0}}, "severity": 1, "message": f"e{n}"}
        for n in range(25)
    ]
    block = diagnostic.report("a.py", many)
    assert block.count("ERROR") == diagnostic.MAX_PER_FILE
    assert "… 5 more" in block


def test_diagnostic_report_is_empty_for_hints_only() -> None:
    assert diagnostic.report("a.py", [{"range": {}, "severity": 3, "message": "note"}]) == ""


def test_diagnostic_report_puts_errors_before_warnings() -> None:
    block = diagnostic.report(
        "a.py",
        [
            {"range": {"start": {"line": 1, "character": 0}}, "severity": 2, "message": "w"},
            {"range": {"start": {"line": 9, "character": 0}}, "severity": 1, "message": "e"},
        ],
    )
    lines = block.splitlines()
    assert lines[1].startswith("ERROR")
    assert lines[2].startswith("WARN")


def test_find_root_uses_the_nearest_marker(tmp_path: Path) -> None:
    project = tmp_path / "repo" / "pkg"
    project.mkdir(parents=True)
    (tmp_path / "repo" / "pyproject.toml").write_text("[project]\n")
    server = servers.ServerInfo(id="x", extensions=(".py",), root_markers=("pyproject.toml",))
    assert servers.find_root(server, project / "a.py", tmp_path) == tmp_path / "repo"


def test_find_root_falls_back_to_the_workdir_unless_strict(tmp_path: Path) -> None:
    loose = servers.ServerInfo(id="x", extensions=(".py",), root_markers=("nothing.toml",))
    strict = servers.ServerInfo(
        id="y", extensions=(".py",), root_markers=("nothing.toml",), strict_root=True
    )
    assert servers.find_root(loose, tmp_path / "a.py", tmp_path) == tmp_path
    assert servers.find_root(strict, tmp_path / "a.py", tmp_path) is None


def test_find_root_declines_when_an_exclude_marker_is_present(tmp_path: Path) -> None:
    (tmp_path / "deno.json").write_text("{}")
    typescript = next(item for item in servers.BUILTIN if item.id == "typescript")
    assert servers.find_root(typescript, tmp_path / "a.ts", tmp_path) is None


def test_catalog_honours_disabled_and_user_defined_servers() -> None:
    catalog = servers.catalog(disabled=["pyright"], overrides={"fake": FAKE_SPEC})
    assert "pyright" not in catalog
    assert catalog["fake"].candidates[0][0] == sys.executable
    assert catalog["fake"].serves(".fake")
    assert not catalog["fake"].serves(".py")


def test_catalog_ignores_a_settings_entry_without_a_command() -> None:
    assert "broken" not in servers.catalog(overrides={"broken": {"extensions": [".x"]}})


def test_server_registry_never_builds_a_shell_string() -> None:
    """AC-46: every builtin server is an argv list, never a command line."""
    for server in servers.BUILTIN:
        assert server.candidates, f"{server.id} declares no binary"
        for binary, args in server.candidates:
            assert " " not in binary or binary.endswith(".sh"), binary
            assert all(isinstance(arg, str) for arg in args)


# ---------------------------------------------------------------------------
# §3 the tool catalog
# ---------------------------------------------------------------------------


def test_the_seven_lsp_tools_are_registered_with_the_contract_tags(core: Core) -> None:
    listed = {info.name: info for info in core.tools.list()}
    for name in lsp_tools.REGISTERED:
        assert name in listed, f"{name} is missing from tool.list"
        assert listed[name].category == "lsp"
    assert len(lsp_tools.REGISTERED) == 7
    writes = {name for name in lsp_tools.REGISTERED if listed[name].permissionTag == "write"}
    assert writes == {"lsp_rename"}


def test_tool_list_marks_lsp_inactive_with_a_reason_when_disabled(tmp_path: Path) -> None:
    """Contract §4: ``state: inactive`` plus a reason, not a silent absence."""
    core = build_core(tmp_path, lsp_settings=LspSettings(enabled=False))
    listed = {info.name: info for info in core.tools.list()}
    for name in lsp_tools.REGISTERED:
        assert listed[name].state == "inactive"
        assert listed[name].reason == lsp_tools.INACTIVE_REASON
    assert lsp_tools.REGISTERED[0] not in {tool.name for tool in core.tools.active()}


async def test_an_lsp_tool_called_while_disabled_says_tool_inactive(tmp_path: Path) -> None:
    core = build_core(tmp_path, lsp_settings=LspSettings(enabled=False))
    session = Session(id="s", workdir=tmp_path)
    ctx = ToolContext(session=session, core=core, backend=LocalBackend(tmp_path))
    result = await run(ctx, "lsp_hover", path="a.fake", line=0, character=0)
    assert not result.ok
    assert "tool_inactive" in (result.error or "")


def test_hot_reloading_lsp_enabled_flips_the_tool_state(tmp_path: Path) -> None:
    from snowpea_core.config import hot_reload

    core = build_core(tmp_path)
    assert core.tools.get("lsp_hover").state == "active"
    off = Settings()
    off.lsp = LspSettings(enabled=False)
    hot_reload.rebind(core, off)
    assert core.tools.get("lsp_hover").state == "inactive"


def test_lsp_settings_defaults_match_the_contract() -> None:
    block = LspSettings()
    assert block.enabled is True
    assert block.autoInstall is False
    assert block.disabled == []
    assert block.servers == {}
    assert block.idleTimeoutSec == 600


def test_registry_registers_lsp_tools_with_the_builtins() -> None:
    registry = register_builtin_tools(ToolRegistry())
    assert {tool.name for tool in registry.list()} >= set(lsp_tools.REGISTERED)


# ---------------------------------------------------------------------------
# AC-43: the Diagnostics block on an edit
# ---------------------------------------------------------------------------


async def test_ac43_patch_appends_a_diagnostics_block(ctx: ToolContext) -> None:
    target = Path(ctx.session.workdir) / "a.fake"
    target.write_text("def alpha\nok\n")
    # The read-before-write guard (M15 §A3) refuses an edit to a file this
    # session has not read; the diagnostics block is what this test is about.
    await run(ctx, "read_file", path="a.fake")
    result = await run(ctx, "patch", path="a.fake", old_string="ok", new_string="ERROR broken thing")
    try:
        assert result.ok, result.error
        assert "replaced 1 occurrence(s)" in result.output
        assert '<diagnostics file="a.fake">' in result.output
        assert "ERROR [2:1] broken thing (fake-error)" in result.output
    finally:
        await shutdown(ctx.core)


async def test_ac43_write_file_appends_a_diagnostics_block(ctx: ToolContext) -> None:
    result = await run(ctx, "write_file", path="b.fake", content="ERROR nope\nWARN meh\n")
    try:
        assert result.ok, result.error
        assert "ERROR [1:1] nope (fake-error)" in result.output
        assert "WARN [2:1] meh (fake-warn)" in result.output
    finally:
        await shutdown(ctx.core)


async def test_ac43_a_clean_file_appends_nothing(ctx: ToolContext) -> None:
    result = await run(ctx, "write_file", path="c.fake", content="def beta\n")
    try:
        assert result.ok
        assert "diagnostics" not in result.output
    finally:
        await shutdown(ctx.core)


async def test_ac43_the_result_is_unchanged_when_lsp_is_disabled(tmp_path: Path) -> None:
    core = build_core(
        tmp_path, lsp_settings=LspSettings(enabled=False, servers={"fake": FAKE_SPEC})
    )
    workdir = tmp_path / "w"
    workdir.mkdir()
    ctx = ToolContext(
        session=Session(id="s", workdir=workdir), core=core, backend=LocalBackend(workdir)
    )
    result = await run(ctx, "write_file", path="d.fake", content="ERROR nope\n")
    assert result.ok
    assert result.output == f"wrote {len('ERROR nope' + chr(10))} characters to d.fake"
    assert "diagnostics" not in result.output


async def test_ac43_an_edit_still_succeeds_when_no_server_is_installed(
    tmp_path: Path,
) -> None:
    """A missing binary is not an error; the edit lands without diagnostics."""
    core = build_core(
        tmp_path,
        lsp_settings=LspSettings(
            servers={
                "absent": {
                    "command": ["definitely-not-a-real-binary"],
                    "extensions": [".fake"],
                }
            }
        ),
    )
    workdir = tmp_path / "w"
    workdir.mkdir()
    ctx = ToolContext(
        session=Session(id="s", workdir=workdir), core=core, backend=LocalBackend(workdir)
    )
    result = await run(ctx, "write_file", path="e.fake", content="ERROR nope\n")
    try:
        assert result.ok
        assert "diagnostics" not in result.output
    finally:
        await shutdown(core)


async def test_ac43_a_slow_server_does_not_hold_the_edit_past_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LSP_SLOW_INITIALIZE", "30")
    core = build_core(tmp_path, lsp_settings=LspSettings(servers={"fake": FAKE_SPEC}))
    workdir = tmp_path / "w"
    workdir.mkdir()
    ctx = ToolContext(
        session=Session(id="s", workdir=workdir), core=core, backend=LocalBackend(workdir)
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await run(ctx, "write_file", path="f.fake", content="ERROR nope\n")
    elapsed = loop.time() - started
    try:
        assert result.ok
        assert elapsed < lsp.START_BUDGET_SEC + 3
        assert "diagnostics" not in result.output
    finally:
        await shutdown(core)


# ---------------------------------------------------------------------------
# AC-44: definition, references, symbols, hover, rename
# ---------------------------------------------------------------------------


def _seed(workdir: Path) -> None:
    (workdir / "one.fake").write_text("def alpha\nalpha\n")
    (workdir / "two.fake").write_text("call alpha\nalpha again\n")


async def test_ac44_references_lists_every_use_across_files(ctx: ToolContext) -> None:
    workdir = Path(ctx.session.workdir)
    _seed(workdir)
    try:
        # Both files must be known to the server, which happens when they are
        # touched; ``one.fake`` is touched by the request itself.
        await manager_for(ctx.core).touch_file(workdir / "two.fake", workdir)
        result = await run(ctx, "lsp_references", path="one.fake", line=0, character=4)
        assert result.ok, result.error
        lines = set(result.output.splitlines())
        assert "one.fake:1:5" in lines
        assert "one.fake:2:1" in lines
        assert "two.fake:1:6" in lines
        assert "two.fake:2:1" in lines
    finally:
        await shutdown(ctx.core)


async def test_ac44_definition_points_at_the_right_file_and_line(ctx: ToolContext) -> None:
    workdir = Path(ctx.session.workdir)
    _seed(workdir)
    try:
        await manager_for(ctx.core).touch_file(workdir / "one.fake", workdir)
        result = await run(ctx, "lsp_definition", path="two.fake", line=0, character=6)
        assert result.ok, result.error
        assert result.output == "one.fake:1:5"
    finally:
        await shutdown(ctx.core)


async def test_ac44_document_symbols_outline_a_file(ctx: ToolContext) -> None:
    workdir = Path(ctx.session.workdir)
    _seed(workdir)
    try:
        result = await run(ctx, "lsp_symbols", path="one.fake")
        assert result.ok, result.error
        assert result.output.startswith("function alpha")
    finally:
        await shutdown(ctx.core)


async def test_ac44_workspace_symbols_search_every_live_server(ctx: ToolContext) -> None:
    workdir = Path(ctx.session.workdir)
    _seed(workdir)
    try:
        await manager_for(ctx.core).touch_file(workdir / "one.fake", workdir)
        result = await run(ctx, "lsp_workspace_symbols", query="alph")
        assert result.ok, result.error
        assert "alpha" in result.output
    finally:
        await shutdown(ctx.core)


async def test_ac44_hover_returns_the_servers_text(ctx: ToolContext) -> None:
    workdir = Path(ctx.session.workdir)
    _seed(workdir)
    try:
        result = await run(ctx, "lsp_hover", path="one.fake", line=0, character=4)
        assert result.ok, result.error
        assert "alpha" in result.output
    finally:
        await shutdown(ctx.core)


async def test_ac44_diagnostics_tool_reports_a_touched_file(ctx: ToolContext) -> None:
    workdir = Path(ctx.session.workdir)
    (workdir / "bad.fake").write_text("ERROR still broken\n")
    try:
        result = await run(ctx, "lsp_diagnostics", path="bad.fake")
        assert result.ok, result.error
        assert "ERROR [1:1] still broken (fake-error)" in result.output
    finally:
        await shutdown(ctx.core)


async def test_ac44_diagnostics_tool_is_clean_for_a_clean_file(ctx: ToolContext) -> None:
    (Path(ctx.session.workdir) / "fine.fake").write_text("def gamma\n")
    try:
        result = await run(ctx, "lsp_diagnostics", path="fine.fake")
        assert result.ok
        assert result.output == "No errors or warnings."
    finally:
        await shutdown(ctx.core)


async def test_ac44_rename_rewrites_every_file_the_server_named(ctx: ToolContext) -> None:
    workdir = Path(ctx.session.workdir)
    _seed(workdir)
    try:
        await manager_for(ctx.core).touch_file(workdir / "two.fake", workdir)
        result = await run(
            ctx, "lsp_rename", path="one.fake", line=0, character=4, newName="omega"
        )
        assert result.ok, result.error
        assert "one.fake" in result.output and "two.fake" in result.output
        assert (workdir / "one.fake").read_text() == "def omega\nomega\n"
        assert (workdir / "two.fake").read_text() == "call omega\nomega again\n"
    finally:
        await shutdown(ctx.core)


def test_apply_workspace_edit_applies_edits_back_to_front(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("aaa bbb ccc\n")
    edit = {
        "changes": {
            f"file://{target}": [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 3},
                    },
                    "newText": "A",
                },
                {
                    "range": {
                        "start": {"line": 0, "character": 8},
                        "end": {"line": 0, "character": 11},
                    },
                    "newText": "CCCCC",
                },
            ]
        }
    }
    assert lsp_tools.apply_workspace_edit(edit) == {target: "A bbb CCCCC\n"}


async def test_a_missing_file_is_a_tool_error_not_a_crash(ctx: ToolContext) -> None:
    result = await run(ctx, "lsp_hover", path="nope.fake", line=0, character=0)
    assert not result.ok
    assert "no such file" in (result.error or "")


async def test_negative_positions_are_refused(ctx: ToolContext) -> None:
    (Path(ctx.session.workdir) / "one.fake").write_text("def alpha\n")
    result = await run(ctx, "lsp_definition", path="one.fake", line=-1, character=0)
    assert not result.ok
    assert "zero-based" in (result.error or "")


# ---------------------------------------------------------------------------
# AC-45: crash recovery
# ---------------------------------------------------------------------------


async def test_ac45_a_crashing_server_restarts_once_then_reports_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LSP_CRASH_AFTER", "1")
    core = build_core(tmp_path, lsp_settings=LspSettings(servers={"fake": FAKE_SPEC}))
    workdir = tmp_path / "w"
    workdir.mkdir()
    ctx = ToolContext(
        session=Session(id="s", workdir=workdir), core=core, backend=LocalBackend(workdir)
    )
    manager = manager_for(core)
    try:
        for attempt in range(3):
            result = await run(
                ctx, "write_file", path="g.fake", content=f"ERROR crash {attempt}\n"
            )
            # The edit lands every time, crash or no crash.
            assert result.ok, result.error
            await asyncio.sleep(0.1)
        assert [row["state"] for row in manager.status()] == ["broken"]
        # And it stays broken rather than being restarted for ever.
        assert await manager.clients_for(workdir / "g.fake", workdir) == []
    finally:
        await shutdown(core)


# ---------------------------------------------------------------------------
# AC-46: argv only, and no downloads unless asked
# ---------------------------------------------------------------------------


async def test_ac46_spawn_uses_argv_and_never_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = asyncio.create_subprocess_exec

    async def record(*argv: Any, **kwargs: Any) -> Any:
        seen["argv"] = argv
        return await real(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_shell",
        _forbidden,
        raising=True,
    )
    server = servers.catalog(overrides={"fake": FAKE_SPEC})["fake"]
    process = await servers.spawn(server, tmp_path, home=tmp_path, allow_install=False)
    assert process is not None
    try:
        assert seen["argv"] == (sys.executable, str(FAKE_SERVER))
    finally:
        process.kill()
        await process.wait()


async def test_ac46_autoinstall_off_never_installs_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("auto_install ran with lsp.autoInstall false")

    monkeypatch.setattr(servers, "auto_install", forbidden)
    server = servers.ServerInfo(
        id="ghost",
        extensions=(".ghost",),
        candidates=(("definitely-not-a-real-binary", ()),),
        install=servers.Install("npm", "ghost-ls", "ghost-ls"),
    )
    assert await servers.resolve_command(server, home=tmp_path, allow_install=False) is None


async def test_ac46_autoinstall_on_is_the_only_path_that_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_install(spec: servers.Install, home: Path) -> str | None:
        calls.append(spec.package)
        return None

    monkeypatch.setattr(servers, "auto_install", fake_install)
    server = servers.ServerInfo(
        id="ghost",
        extensions=(".ghost",),
        candidates=(("definitely-not-a-real-binary", ()),),
        install=servers.Install("npm", "ghost-ls", "ghost-ls"),
    )
    assert await servers.resolve_command(server, home=tmp_path, allow_install=True) is None
    assert calls == ["ghost-ls"]


def test_ac46_the_managed_install_prefix_is_under_snowpea_home(tmp_path: Path) -> None:
    assert servers.lsp_home(tmp_path) == tmp_path / "lsp"


# ---------------------------------------------------------------------------
# §4 protocol: lsp.status and the lsp.diagnostics session event
# ---------------------------------------------------------------------------


async def test_lsp_status_rpc_reports_every_server(ctx: ToolContext) -> None:
    workdir = Path(ctx.session.workdir)
    (workdir / "h.fake").write_text("def delta\n")
    try:
        await manager_for(ctx.core).touch_file(workdir / "h.fake", workdir)
        result = await lsp_status_handler(None, Empty(), ctx.core)  # type: ignore[arg-type]
        assert [row.id for row in result.servers] == ["fake"]
        assert result.servers[0].state == "ready"
        assert result.servers[0].root == str(workdir.resolve())
        assert result.servers[0].pid is not None
    finally:
        await shutdown(ctx.core)


async def test_lsp_status_is_empty_before_anything_is_touched(core: Core) -> None:
    result = await lsp_status_handler(None, Empty(), core)  # type: ignore[arg-type]
    assert result.servers == []


async def test_lsp_catalog_lists_every_builtin_server_regardless_of_state(
    core: Core,
) -> None:
    result = await lsp_catalog_handler(None, Empty(), core)  # type: ignore[arg-type]
    assert {row.id for row in result.servers} == {s.id for s in servers.BUILTIN}
    pyright = next(row for row in result.servers if row.id == "pyright")
    assert ".py" in pyright.extensions
    assert "python" in pyright.languageIds
    assert pyright.disabled is False


async def test_lsp_catalog_marks_default_off_servers_disabled(core: Core) -> None:
    result = await lsp_catalog_handler(None, Empty(), core)  # type: ignore[arg-type]
    by_id = {row.id for row in result.servers if row.disabled}
    assert servers.DISABLED_BY_DEFAULT <= by_id


async def test_lsp_catalog_disabled_reflects_settings_lsp_disabled(
    tmp_path: Path,
) -> None:
    core = build_core(tmp_path, lsp_settings=LspSettings(disabled=["gopls"]))
    result = await lsp_catalog_handler(None, Empty(), core)  # type: ignore[arg-type]
    gopls = next(row for row in result.servers if row.id == "gopls")
    assert gopls.disabled is True
    rust = next(row for row in result.servers if row.id == "rust")
    assert rust.disabled is False


async def test_lsp_catalog_installable_matches_whether_install_is_declared(
    core: Core,
) -> None:
    result = await lsp_catalog_handler(None, Empty(), core)  # type: ignore[arg-type]
    by_id = {row.id: row for row in result.servers}
    assert by_id["pyright"].installable is True
    assert by_id["pyright"].installHint == "npm install -g pyright"
    assert by_id["ty"].installHint == "pip install ty"
    gopls = next(s for s in servers.BUILTIN if s.id == "gopls")
    assert gopls.install is not None and gopls.install.kind == "go"
    assert by_id["gopls"].installHint == "go install golang.org/x/tools/gopls@latest"
    assert by_id["rust"].installable is False
    assert by_id["rust"].installHint is None


def test_the_lsp_diagnostics_event_validates_against_the_protocol() -> None:
    kind, payload = events.lsp_diagnostics("a.py", count=3, errors=2, warnings=1)
    assert kind == "lsp.diagnostics"
    assert events.validate(kind, payload) == payload
    assert payload == {"path": "a.py", "count": 3, "errors": 2, "warnings": 1}


def test_the_protocol_declares_lsp_status_and_the_event() -> None:
    from snowpea_core.server.protocol import (
        IMPLEMENTED_METHODS,
        METHODS,
        PROTOCOL_VERSION,
        SESSION_EVENT_KINDS,
    )

    assert PROTOCOL_VERSION == "1.5.0"
    assert "lsp.status" in METHODS
    assert "lsp.status" in IMPLEMENTED_METHODS
    assert "lsp.catalog" in METHODS
    assert "lsp.catalog" in IMPLEMENTED_METHODS
    assert "lsp.diagnostics" in SESSION_EVENT_KINDS


# ---------------------------------------------------------------------------
# §2 manager behaviour
# ---------------------------------------------------------------------------


async def test_one_client_is_started_per_server_and_root(ctx: ToolContext) -> None:
    workdir = Path(ctx.session.workdir)
    (workdir / "i.fake").write_text("def one\n")
    (workdir / "j.fake").write_text("def two\n")
    manager = manager_for(ctx.core)
    try:
        await asyncio.gather(
            manager.touch_file(workdir / "i.fake", workdir),
            manager.touch_file(workdir / "j.fake", workdir),
        )
        assert len(manager.entries) == 1
    finally:
        await shutdown(ctx.core)


async def test_an_idle_server_is_shut_down_after_the_timeout(tmp_path: Path) -> None:
    core = build_core(
        tmp_path, lsp_settings=LspSettings(servers={"fake": FAKE_SPEC}, idleTimeoutSec=1)
    )
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "k.fake").write_text("def kilo\n")
    manager = manager_for(core)
    try:
        await manager.touch_file(workdir / "k.fake", workdir)
        assert any(row["state"] == "ready" for row in manager.status())
        for entry in manager.entries.values():
            entry.last_used -= 10
        assert await manager.sweep_idle() is False
        assert [row["state"] for row in manager.status()] == ["stopped"]
    finally:
        await shutdown(core)


async def test_the_manager_answers_nothing_when_lsp_is_disabled(tmp_path: Path) -> None:
    core = build_core(
        tmp_path, lsp_settings=LspSettings(enabled=False, servers={"fake": FAKE_SPEC})
    )
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "l.fake").write_text("ERROR nope\n")
    manager = LspManager(core)
    assert await manager.clients_for(workdir / "l.fake", workdir) == []
    assert manager.diagnostics() == {}


async def test_pull_diagnostics_cover_a_server_that_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LSP_NO_PUBLISH", "1")
    core = build_core(tmp_path, lsp_settings=LspSettings(servers={"fake": FAKE_SPEC}))
    workdir = tmp_path / "w"
    workdir.mkdir()
    ctx = ToolContext(
        session=Session(id="s", workdir=workdir), core=core, backend=LocalBackend(workdir)
    )
    try:
        result = await run(ctx, "write_file", path="m.fake", content="ERROR pulled\n")
        assert result.ok
        assert "ERROR [1:1] pulled (fake-error)" in result.output
    finally:
        await shutdown(core)


# ---------------------------------------------------------------------------
# AC-43/AC-44/AC-47 against real servers, when they happen to be installed
# ---------------------------------------------------------------------------


def _typescript_project(tmp_path: Path) -> Path:
    """A workdir the TypeScript server will actually serve.

    typescript-language-server exits during ``initialize`` unless the workspace
    has its own ``typescript``, which is why ``ServerInfo.precondition`` exists;
    the test links the one this test run installed rather than downloading.
    """
    workdir = tmp_path / "ts"
    workdir.mkdir()
    (workdir / "package-lock.json").write_text('{"lockfileVersion": 3}')
    (workdir / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}')
    (workdir / "warm.ts").write_text("export const warm = 1;\n")
    tsserver = servers.find_tsserver(
        Path(shutil.which("typescript-language-server") or "").resolve().parent
    )
    if tsserver is None:
        pytest.skip("no workspace typescript to point tsserver at")
    modules = workdir / "node_modules"
    modules.mkdir()
    (modules / "typescript").symlink_to(tsserver.parent.parent, target_is_directory=True)
    return workdir


def test_typescript_declines_a_workspace_without_its_own_typescript(tmp_path: Path) -> None:
    typescript = next(item for item in servers.BUILTIN if item.id == "typescript")
    assert typescript.precondition is not None
    assert typescript.precondition(tmp_path) is False


@pytest.mark.skipif(
    shutil.which("pyright-langserver") is None, reason="pyright is not on PATH"
)
async def test_ac43_real_pyright_reports_a_type_error(tmp_path: Path) -> None:
    """AC-43 against pyright itself.

    Pyright's *cold* start is well past the three-second budget an edit gives
    it, so the first edit in a workdir legitimately carries no block (contract
    §3: "when a server is running or can start within 3 s").  The server is
    warmed here the way a real session warms it — by having touched a file
    already — and the assertion is on the steady state.
    """
    core = build_core(tmp_path, lsp_settings=LspSettings(disabled=["ruff", "ty"]))
    workdir = tmp_path / "py"
    workdir.mkdir()
    (workdir / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n')
    (workdir / "warm.py").write_text("value = 1\n")
    ctx = ToolContext(
        session=Session(id="s", workdir=workdir), core=core, backend=LocalBackend(workdir)
    )
    try:
        assert await manager_for(core).clients_for(
            workdir / "warm.py", workdir, budget=60
        ), "pyright never came up"
        started = asyncio.get_running_loop().time()
        result = await run(
            ctx, "write_file", path="broken.py", content="x: int = 'not an int'\n"
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert result.ok
        assert "<diagnostics" in result.output, result.output
        assert "ERROR" in result.output
        assert elapsed < 5, f"took {elapsed:.1f}s"
    finally:
        await shutdown(core)


@pytest.mark.skipif(
    shutil.which("pyright-langserver") is None, reason="pyright is not on PATH"
)
async def test_ac44_real_pyright_finds_a_definition(tmp_path: Path) -> None:
    core = build_core(tmp_path, lsp_settings=LspSettings(disabled=["ruff", "ty"]))
    workdir = tmp_path / "py"
    workdir.mkdir()
    (workdir / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\n')
    (workdir / "mod.py").write_text("def alpha() -> int:\n    return 1\n\n\nalpha()\n")
    ctx = ToolContext(
        session=Session(id="s", workdir=workdir), core=core, backend=LocalBackend(workdir)
    )
    try:
        result = await run(ctx, "lsp_definition", path="mod.py", line=4, character=0)
        assert result.ok, result.error
        assert result.output.startswith("mod.py:1:")
    finally:
        await shutdown(core)


@pytest.mark.skipif(
    shutil.which("typescript-language-server") is None,
    reason="typescript-language-server is not on PATH",
)
async def test_ac47_real_typescript_reports_a_type_error(tmp_path: Path) -> None:
    """AC-47: the TypeScript fixture passes the same check as AC-43."""
    workdir = _typescript_project(tmp_path)
    core = build_core(tmp_path, lsp_settings=LspSettings(disabled=["eslint", "biome", "oxlint"]))
    ctx = ToolContext(
        session=Session(id="s", workdir=workdir), core=core, backend=LocalBackend(workdir)
    )
    try:
        assert await manager_for(core).clients_for(
            workdir / "warm.ts", workdir, budget=60
        ), "typescript-language-server never came up"
        result = await run(ctx, "write_file", path="a.ts", content="const x: number = 'no';\n")
        assert result.ok
        assert "<diagnostics" in result.output, result.output
        assert "ERROR" in result.output
    finally:
        await shutdown(core)


@pytest.mark.skipif(
    shutil.which("typescript-language-server") is None,
    reason="typescript-language-server is not on PATH",
)
async def test_ac47_real_typescript_lists_references(tmp_path: Path) -> None:
    """AC-47: the TypeScript fixture passes the same check as AC-44."""
    workdir = _typescript_project(tmp_path)
    (workdir / "b.ts").write_text("export function alpha() {}\nalpha();\n")
    core = build_core(tmp_path, lsp_settings=LspSettings(disabled=["eslint", "biome", "oxlint"]))
    ctx = ToolContext(
        session=Session(id="s", workdir=workdir), core=core, backend=LocalBackend(workdir)
    )
    try:
        assert await manager_for(core).clients_for(workdir / "b.ts", workdir, budget=60)
        result = await run(ctx, "lsp_references", path="b.ts", line=0, character=16)
        assert result.ok, result.error
        assert "b.ts:2:1" in result.output
    finally:
        await shutdown(core)


def test_the_fixture_server_exists_and_is_executable() -> None:
    assert FAKE_SERVER.is_file()
    assert os.access(FAKE_SERVER, os.R_OK)
