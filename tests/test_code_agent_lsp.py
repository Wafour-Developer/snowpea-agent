"""snowpea-browser addendum 28: code agent parity and language servers (C1–C9)."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent import verify_gate
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import LspSettings, Settings
from snowpea_core.lsp import servers
from snowpea_core.lsp import tools as lsp_tools
from snowpea_core.lsp.manager import IDLE, manager_for
from snowpea_core.server import errors
from snowpea_core.server.app_server import Core
from snowpea_core.server.lsp_handlers import (
    lsp_catalog_handler,
    lsp_install_handler,
    lsp_restart_handler,
)
from snowpea_core.server.protocol import (
    CAPABILITIES,
    EVENTS,
    METHODS,
    Empty,
    LspInstallParams,
    LspRestartParams,
)
from snowpea_core.server.rpc import RpcError
from snowpea_core.session.session import Session
from snowpea_core.tools import host_tools as host_tools_mod
from snowpea_core.tools import set_mode as set_mode_mod
from snowpea_core.tools.registry import ToolResult, register_builtin_tools

FAKE_SERVER = Path(__file__).parent / "fixtures" / "lsp" / "fake_server.py"
FAKE_SPEC: dict[str, Any] = {
    "command": [sys.executable, str(FAKE_SERVER)],
    "extensions": [".fake"],
}


class _Hub:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, payload: dict[str, Any]) -> None:
        self.sent.append((method, payload))

    def of(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.sent if name == method]


def _core(tmp_path: Path, lsp: LspSettings | None = None) -> Core:
    settings = Settings()
    if lsp is not None:
        settings.lsp = lsp
    core = Core(settings=settings, paths=Paths.create(tmp_path / "home"), token="t")
    register_builtin_tools(core.tools)
    lsp_tools.refresh_state(core)
    core.hub = _Hub()
    return core


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


posix_only = pytest.mark.skipif(os.name == "nt", reason="shell-script fakes")


# ---------------------------------------------------------------------------
# C1: verify-on-stop counts a look through the browser host
# ---------------------------------------------------------------------------


def _gate_session(tmp_path: Path) -> Session:
    session = Session(id="s-gate", workdir=tmp_path)
    verify_gate.begin_turn(session)
    verify_gate.observe(
        session, "patch", {"path": "src/App.vue"}, ToolResult(ok=True, path="src/App.vue")
    )
    return session


def _gate_core() -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(agent=SimpleNamespace(verifyOnStop="on")))


def _host(monkeypatch: pytest.MonkeyPatch, **tools: Any) -> None:
    monkeypatch.setattr(host_tools_mod.HOST_TOOLS, "for_session", lambda _session: tools)


def test_repl_code_that_snapshots_the_page_is_a_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, repl=SimpleNamespace(look=False))
    session = _gate_session(tmp_path)
    verify_gate.observe(
        session, "repl", {"code": "await page.goto('http://localhost:5173')"}, ToolResult(ok=True)
    )
    nudge = verify_gate.nudge(_gate_core(), session) or ""
    assert "snapshot(page)" in nudge and "in repl" in nudge, "navigating is not a look"
    verify_gate.observe(session, "repl", {"code": "snapshot(page)"}, ToolResult(ok=True))
    assert verify_gate.nudge(_gate_core(), session) is None


@pytest.mark.parametrize(
    "code",
    [
        "const s = await snapshot(page)",
        "await annotatedScreenshot(page)",
        "await page.screenshot({fullPage: true})",
        "display(await page.screenshot())",
        "await snapshotTabs()",
    ],
)
def test_each_repl_look_counts(tmp_path: Path, code: str) -> None:
    session = _gate_session(tmp_path)
    verify_gate.observe(session, "repl", {"code": code}, ToolResult(ok=True))
    assert verify_gate.nudge(_gate_core(), session) is None


def test_a_failed_repl_or_a_lookalike_name_is_not_a_look(tmp_path: Path) -> None:
    session = _gate_session(tmp_path)
    verify_gate.observe(session, "repl", {"code": "snapshot(page)"}, ToolResult(ok=False))
    verify_gate.observe(session, "repl", {"code": "mysnapshot(page)"}, ToolResult(ok=True))
    assert verify_gate.nudge(_gate_core(), session)


def test_a_host_look_flag_or_result_meta_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, peek=SimpleNamespace(look=True))
    session = _gate_session(tmp_path)
    verify_gate.observe(session, "peek", {}, ToolResult(ok=True))
    assert verify_gate.nudge(_gate_core(), session) is None

    _host(monkeypatch)
    session = _gate_session(tmp_path)
    meta = {host_tools_mod.HOST_META_KEY: {"look": True}}
    verify_gate.observe(session, "other", {}, ToolResult(ok=True, meta=meta))
    assert verify_gate.nudge(_gate_core(), session) is None


def test_without_a_repl_host_the_nudge_names_browser_screenshot(tmp_path: Path) -> None:
    session = _gate_session(tmp_path)
    assert "browser_screenshot" in (verify_gate.nudge(_gate_core(), session) or "")


def test_tool_register_keeps_the_look_flag(tmp_path: Path) -> None:
    core = _core(tmp_path)
    conn = SimpleNamespace(surface_id="browser:1", client_id="c1", on_close=[])
    registry = host_tools_mod.HostTools()
    registry.register(
        core,
        conn,
        [{"name": "peek", "description": "d", "permission": "read", "look": True}],
    )
    assert registry._hosts["browser:1"].tools["peek"].look is True


# ---------------------------------------------------------------------------
# C2: set_mode words a coding agent's picker as coding
# ---------------------------------------------------------------------------


def _mode_ctx(agent: str | None, allowed: set[str] | None) -> Any:
    session = SimpleNamespace(
        origin_surface="browser", origin_conn=None, agent=agent, allowed_tools=allowed
    )
    return SimpleNamespace(session=session)


def test_the_browser_code_agent_gets_the_coding_wording() -> None:
    assert set_mode_mod._is_browser(_mode_ctx("browser-code", None)) is False
    assert set_mode_mod._is_browser(_mode_ctx("browser", {"repl", "read_tab"})) is True
    assert set_mode_mod._is_browser(_mode_ctx(None, None)) is True


# ---------------------------------------------------------------------------
# C3: lsp.catalog says what is there
# ---------------------------------------------------------------------------


@posix_only
async def test_catalog_reports_a_managed_binary_and_what_a_missing_one_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    core = _core(tmp_path)
    _script(core.paths.home / "lsp" / "bin" / "gopls", "exit 0\n")
    result = await lsp_catalog_handler(None, Empty(), core)  # type: ignore[arg-type]
    rows = {row.id: row for row in result.servers}
    assert rows["gopls"].available and rows["gopls"].managed
    assert rows["gopls"].path == str(core.paths.home / "lsp" / "bin" / "gopls")
    assert rows["gopls"].needs is None
    assert not rows["vue"].available and rows["vue"].needs == "npm"
    assert rows["zls"].needs == "zls" and rows["zls"].path is None
    assert rows["rust"].needs == "rustup"


@posix_only
async def test_a_rustup_proxy_without_the_component_is_not_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "cargo-bin"
    rustup = _script(bin_dir / "rustup", 'echo "error: Unknown binary" >&2; exit 1\n')
    (bin_dir / "rust-analyzer").symlink_to(rustup)
    monkeypatch.setenv("PATH", str(bin_dir))
    core = _core(tmp_path)
    rows = {
        row.id: row
        for row in (await lsp_catalog_handler(None, Empty(), core)).servers  # type: ignore[arg-type]
    }
    assert rows["rust"].available is False
    assert rows["rust"].needs is None  # rustup is there: lsp.install can add it

    real = _script(tmp_path / "toolchain" / "rust-analyzer", "exit 0\n")
    _script(bin_dir / "rustup", f'[ "$1" = which ] && echo {real} && exit 0\nexit 1\n')
    rows = {
        row.id: row
        for row in (await lsp_catalog_handler(None, Empty(), core)).servers  # type: ignore[arg-type]
    }
    assert rows["rust"].available is True


# ---------------------------------------------------------------------------
# C4: lsp.install, even with autoInstall off
# ---------------------------------------------------------------------------


async def test_lsp_install_runs_with_auto_install_off_and_reports_each_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = _core(tmp_path, LspSettings(autoInstall=False))
    calls: list[str] = []

    async def fake_locate(server: servers.ServerInfo, home: Path, cwd: Any = None) -> Any:
        return None

    async def fake_install(spec: servers.Install, home: Path, *, on_line: Any = None) -> Any:
        calls.append(spec.package)
        await on_line("added 1 package")
        if spec.package == "pyright":
            return str(home / "lsp" / "node_modules" / ".bin" / "pyright-langserver"), None
        return None, "needs npm on PATH"

    monkeypatch.setattr(servers, "locate", fake_locate)
    monkeypatch.setattr(servers, "install_server", fake_install)
    conn = SimpleNamespace(surface_id="browser:1")
    result = await lsp_install_handler(
        conn,  # type: ignore[arg-type]
        LspInstallParams(ids=["pyright", "vue", "zls", "nope"]),
        core,
    )
    by_id = {item.id: item for item in result.results}
    assert calls == ["pyright", "@vue/language-server"]
    assert by_id["pyright"].ok and by_id["pyright"].installed
    assert by_id["vue"].ok is False and by_id["vue"].error == "needs npm on PATH"
    assert by_id["zls"].ok is False and "needs zls" in (by_id["zls"].error or "")
    assert by_id["nope"].error == "no such language server"
    await asyncio.sleep(0)
    hub: _Hub = core.hub  # type: ignore[assignment]
    stages = [(p["id"], p["stage"]) for p in hub.of("lsp.install.progress")]
    assert stages[:3] == [("pyright", "start"), ("pyright", "output"), ("pyright", "done")]
    assert hub.of("lsp.changed") == [{"id": None, "root": None, "state": None, "reason": None}]


async def test_lsp_install_and_restart_refuse_a_chat_gateway(tmp_path: Path) -> None:
    core = _core(tmp_path)
    gateway = SimpleNamespace(surface_id="gateway:telegram:1")
    with pytest.raises(RpcError) as raised:
        await lsp_install_handler(gateway, LspInstallParams(ids=["pyright"]), core)  # type: ignore[arg-type]
    assert raised.value.code == errors.UNAUTHORIZED
    with pytest.raises(RpcError):
        await lsp_restart_handler(gateway, LspRestartParams(), core)  # type: ignore[arg-type]


@posix_only
async def test_npm_install_resolves_npm_and_installs_typescript_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = tmp_path / "tools"
    log = tmp_path / "npm.log"
    _script(
        tools / "npm",
        f'echo "$@" > {log}\n'
        'prefix="$3"; /bin/mkdir -p "$prefix/node_modules/.bin"\n'
        'printf "#!/bin/sh\\n" > "$prefix/node_modules/.bin/typescript-language-server"\n'
        '/bin/chmod +x "$prefix/node_modules/.bin/typescript-language-server"\n',
    )
    monkeypatch.setenv("PATH", str(tools))
    home = tmp_path / "home"
    spec = servers.builtin("typescript").install  # type: ignore[union-attr]
    path, error = await servers.install_server(spec, home)  # type: ignore[arg-type]
    assert error is None
    assert path == str(home / "lsp" / "node_modules" / ".bin" / "typescript-language-server")
    assert log.read_text().split() == [
        "install", "--prefix", str(home / "lsp"), "typescript-language-server", "typescript",
    ]
    # Saved to a manifest of its own, so the next npm install does not prune it.
    assert (home / "lsp" / "package.json").is_file()


@posix_only
async def test_two_npm_servers_installed_in_a_row_both_stay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil as shutil_mod

    if shutil_mod.which("npm") is None:
        pytest.skip("npm is not installed")
    packages = tmp_path / "packages"
    tarballs = []
    for name in ("pa", "pb"):
        folder = packages / name
        _script(folder / "bin.js", "exit 0\n")
        (folder / "package.json").write_text(
            f'{{"name": "{name}", "version": "1.0.0", "bin": {{"{name}": "bin.js"}}}}'
        )
        tarballs.append(str(folder))
    home = tmp_path / "home"
    monkeypatch.setenv("npm_config_offline", "true")
    monkeypatch.setenv("npm_config_audit", "false")
    monkeypatch.setenv("npm_config_fund", "false")
    for name, folder in zip(("pa", "pb"), tarballs, strict=True):
        path, error = await servers.install_server(servers.Install("npm", folder, name), home)
        assert error is None, error
    bins = home / "lsp" / "node_modules" / ".bin"
    assert (bins / "pa").exists() and (bins / "pb").exists()


async def test_the_edit_budget_does_not_cancel_a_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = _core(tmp_path, LspSettings(servers={"fake": FAKE_SPEC}))
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "a.fake").write_text("x\n")
    manager = manager_for(core)
    release = asyncio.Event()
    real = servers.start_server
    starts: list[str] = []

    async def slow(server: servers.ServerInfo, root: Path, **kwargs: Any) -> Any:
        starts.append(server.id)
        await release.wait()
        return await real(server, root, **kwargs)

    monkeypatch.setattr(servers, "start_server", slow)
    try:
        assert await manager.clients_for(workdir / "a.fake", workdir, budget=0.05) == []
        assert await manager.clients_for(workdir / "a.fake", workdir, budget=0.05) == []
        assert starts == ["fake"], "a second edit must join the start, not begin another"
        release.set()
        for _ in range(100):
            if manager.status() and manager.status()[0]["state"] == "ready":
                break
            await asyncio.sleep(0.05)
        assert manager.status()[0]["state"] == "ready"
    finally:
        await manager.shutdown()


async def test_a_restart_during_a_start_does_not_leak_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = _core(tmp_path, LspSettings(servers={"fake": FAKE_SPEC}))
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "a.fake").write_text("x\n")
    manager = manager_for(core)
    release = asyncio.Event()
    real = servers.start_server
    processes: list[Any] = []

    async def slow(server: servers.ServerInfo, root: Path, **kwargs: Any) -> Any:
        await release.wait()
        process, reason = await real(server, root, **kwargs)
        processes.append(process)
        return process, reason

    monkeypatch.setattr(servers, "start_server", slow)
    try:
        touch = asyncio.ensure_future(manager.clients_for(workdir / "a.fake", workdir))
        await asyncio.sleep(0.05)
        assert await manager.restart() == ["fake"]
        release.set()
        assert await touch == []
        assert manager.entries == {}
        await asyncio.wait_for(processes[0].wait(), 5)
    finally:
        await manager.shutdown()


async def test_auto_install_leaves_a_linter_alone_without_its_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_install(spec: servers.Install, home: Path) -> str | None:
        calls.append(spec.package)
        return None

    async def nothing(*_args: Any) -> Any:
        return None

    monkeypatch.setattr(servers, "auto_install", fake_install)
    monkeypatch.setattr(servers, "locate", nothing)
    project = tmp_path / "p"
    project.mkdir()
    biome = servers.builtin("biome")
    assert biome is not None
    await servers.resolve_command(biome, home=tmp_path, allow_install=True, root=project)
    assert calls == []
    (project / "biome.json").write_text("{}")
    await servers.resolve_command(biome, home=tmp_path, allow_install=True, root=project)
    assert calls == ["@biomejs/biome"]


@posix_only
async def test_a_frozen_core_makes_the_pip_venv_with_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = tmp_path / "tools"
    log = tmp_path / "uv.log"
    _script(
        tools / "uv",
        f'echo "$@" >> {log}\n'
        'if [ "$1" = venv ]; then /bin/mkdir -p "$2/bin"; printf "#!/bin/sh\\n" > "$2/bin/python"; '
        '/bin/chmod +x "$2/bin/python"; exit 0; fi\n'
        'venv=$(/usr/bin/dirname $(/usr/bin/dirname "$4"))\n'
        'printf "#!/bin/sh\\n" > "$venv/bin/ruff"; '
        '/bin/chmod +x "$venv/bin/ruff"\n',
    )
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    home = tmp_path / "home"
    path, error = await servers.install_server(servers.builtin("ruff").install, home)  # type: ignore[arg-type]
    assert error is None, error
    assert path == str(home / "lsp" / "py" / "bin" / "ruff")
    venv = home / "lsp" / "py"
    assert log.read_text().splitlines() == [
        f"venv {venv}",
        f"pip install --python {venv / 'bin' / 'python'} ruff",
    ]


@posix_only
async def test_dotnet_install_falls_back_to_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = tmp_path / "tools"
    log = tmp_path / "dotnet.log"
    _script(
        tools / "dotnet",
        '[ "$1" = --version ] && echo 8.0.132 && exit 0\n'
        f'echo "$@" >> {log}\n'
        '[ "$2" = install ] && echo "already installed" && exit 1\n'
        'printf "#!/bin/sh\\n" > "$4/csharp-ls"; /bin/chmod +x "$4/csharp-ls"\n',
    )
    monkeypatch.setenv("PATH", str(tools))
    home = tmp_path / "home"
    (home / "lsp" / "bin").mkdir(parents=True)
    path, error = await servers.install_server(servers.builtin("csharp").install, home)  # type: ignore[arg-type]
    assert error is None and path == str(home / "lsp" / "bin" / "csharp-ls")
    lines = log.read_text().splitlines()
    assert [line.split()[1] for line in lines] == ["install", "update"]
    # .NET 8 gets the last csharp-ls that installs there.
    assert all(line.endswith("csharp-ls --version 0.16.0") for line in lines)
    # The installed csharp-ls gets its own arguments, not Roslyn's.
    assert servers.install_args(servers.builtin("csharp")) == ()  # type: ignore[arg-type]


async def test_a_missing_installer_is_an_error_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    path, error = await servers.install_server(servers.builtin("gopls").install, tmp_path)  # type: ignore[arg-type]
    assert path is None and error == "needs go on PATH"


# ---------------------------------------------------------------------------
# C5 / C9: re-probe, lsp.restart, reasons, lsp.changed
# ---------------------------------------------------------------------------


async def test_a_missing_server_is_reprobed_after_install_or_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = {"command": ["definitely-not-a-real-binary"], "extensions": [".fake"]}
    core = _core(tmp_path, LspSettings(servers={"fake": missing}))
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "a.fake").write_text("x\n")
    manager = manager_for(core)
    starts: list[str] = []
    real = servers.start_server

    async def counting(server: servers.ServerInfo, root: Path, **kwargs: Any) -> Any:
        starts.append(server.id)
        return await real(server, root, **kwargs)

    monkeypatch.setattr(servers, "start_server", counting)
    try:
        await manager.touch_file(workdir / "a.fake", workdir)
        await manager.touch_file(workdir / "a.fake", workdir)
        assert starts == ["fake"]
        [row] = manager.status()
        assert row["state"] == "stopped" and row["reason"] == "not installed"
        manager.invalidate()
        await manager.touch_file(workdir / "a.fake", workdir)
        assert starts == ["fake", "fake"]
        # A settings change counts as a reason to look again.
        core.settings.lsp.idleTimeoutSec = 5
        await manager.touch_file(workdir / "a.fake", workdir)
        assert starts == ["fake", "fake", "fake"]
        # And so does lsp.restart, which forgets the entry.
        result = await lsp_restart_handler(
            SimpleNamespace(surface_id="browser:1"),  # type: ignore[arg-type]
            LspRestartParams(id="fake"),
            core,
        )
        assert result.restarted == ["fake"] and manager.entries == {}
        await asyncio.sleep(0)
        hub: _Hub = core.hub  # type: ignore[assignment]
        changed = hub.of("lsp.changed")
        assert {"id": "fake", "root": str(workdir.resolve()), "state": "stopped",
                "reason": "not installed"} in changed
        assert changed[-1]["id"] is None
    finally:
        await manager.shutdown()


async def test_an_idle_stopped_server_starts_again_on_the_next_touch(tmp_path: Path) -> None:
    core = _core(tmp_path, LspSettings(servers={"fake": FAKE_SPEC}, idleTimeoutSec=1))
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "k.fake").write_text("def kilo\n")
    manager = manager_for(core)
    try:
        await manager.touch_file(workdir / "k.fake", workdir)
        for entry in manager.entries.values():
            entry.last_used -= 10
        await manager.sweep_idle()
        assert [(r["state"], r["reason"]) for r in manager.status()] == [("stopped", IDLE)]
        assert await manager.touch_file(workdir / "k.fake", workdir)
        assert [(r["state"], r["reason"]) for r in manager.status()] == [("ready", None)]
    finally:
        await manager.shutdown()


async def test_a_broken_server_gets_another_go_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LSP_CRASH_AFTER", "1")
    core = _core(tmp_path, LspSettings(servers={"fake": FAKE_SPEC}))
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "g.fake").write_text("x\n")
    manager = manager_for(core)
    try:
        for _ in range(3):
            await manager.touch_file(workdir / "g.fake", workdir)
            await asyncio.sleep(0.1)
        [row] = manager.status()
        assert row["state"] == "broken" and str(row["reason"]).startswith("exited")
        monkeypatch.delenv("FAKE_LSP_CRASH_AFTER")
        assert await manager.restart() == ["fake"]
        assert await manager.touch_file(workdir / "g.fake", workdir)
        assert manager.status()[0]["state"] == "ready"
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------
# C6: TypeScript without a project TypeScript; C7: enable keeps the install
# ---------------------------------------------------------------------------


def test_tsserver_falls_back_to_the_managed_typescript(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    assert servers.find_tsserver(project, home) is None
    tsserver = home / "lsp" / "node_modules" / "typescript" / "lib" / "tsserver.js"
    tsserver.parent.mkdir(parents=True)
    tsserver.write_text("")
    typescript = servers.builtin("typescript")
    assert typescript is not None
    assert servers.precondition_holds(typescript, project, home)
    assert not servers.precondition_holds(typescript, project)
    assert servers.initialization_for(typescript, project, home) == {
        "tsserver": {"path": str(tsserver)}
    }


def test_enable_turns_on_a_default_off_server_with_its_install() -> None:
    assert "ruff" not in servers.catalog()
    enabled = servers.catalog(enable=["ruff"])
    assert enabled["ruff"].install is not None
    overridden = servers.catalog(overrides={"ty": {}})
    assert overridden["ty"].install is not None
    assert overridden["ty"].candidates == servers.builtin("ty").candidates  # type: ignore[union-attr]
    extended = servers.catalog(overrides={"ruff": {"extensions": [".py"]}})
    assert extended["ruff"].extensions == (".py",) and extended["ruff"].install is not None
    # A command still replaces the builtin, without its install.
    replaced = servers.catalog(overrides={"ruff": {"command": ["my-ruff", "server"]}})
    assert replaced["ruff"].install is None
    assert "ruff" not in servers.catalog(enable=["ruff"], disabled=["ruff"])


async def test_lsp_catalog_honours_lsp_enable(tmp_path: Path) -> None:
    core = _core(tmp_path, LspSettings(enable=["ty"]))
    rows = {
        row.id: row
        for row in (await lsp_catalog_handler(None, Empty(), core)).servers  # type: ignore[arg-type]
    }
    assert rows["ty"].disabled is False and rows["ruff"].disabled is True


def test_the_protocol_declares_the_new_methods_events_and_flags() -> None:
    assert {"lsp.install", "lsp.restart"} <= set(METHODS)
    assert {"lsp.install.progress", "lsp.changed"} <= set(EVENTS)
    assert {"lspInstall", "lspRestart", "lspChanged"} <= set(CAPABILITIES)
