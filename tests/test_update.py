"""CORE-update: ``system.checkUpdate``, ``system.update`` and the restart path.

Nothing here touches the network: :func:`snowpea_core.update._new_client` is
replaced with a scripted stub, and the upgrade subprocess is a fake script that
writes to the log and exits.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, env_vars, make_daemon

from snowpea_core import __version__
from snowpea_core import update as update_mod
from snowpea_core.cli import commands as cli_commands
from snowpea_core.cli import main as cli_main
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server.app_server import Daemon

# asyncio_mode = "auto" (pyproject) runs the async tests; this module mixes in
# plenty of synchronous ones, so there is no module-level asyncio mark.
NEWER = "9.9.9"
SAME = __version__


# ---------------------------------------------------------------------------
# a scripted httpx stand-in
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    @property
    def text(self) -> str:
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)


class FakeClient:
    """Answers ``get`` from a routing table; records what was asked for."""

    def __init__(self, routes: dict[str, FakeResponse | Exception]) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.closed = False

    async def __aenter__(self) -> FakeClient:
        self.closed = False
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True

    async def get(self, url: str, **_kwargs: Any) -> FakeResponse:
        if self.closed:
            # httpx refuses a request on a client whose ``async with`` has ended.
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        self.calls.append(url)
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return FakeResponse(404, {})


def scripted(
    monkeypatch: pytest.MonkeyPatch, routes: dict[str, FakeResponse | Exception]
) -> FakeClient:
    """Point :func:`update._new_client` at one :class:`FakeClient`."""
    client = FakeClient(routes)
    monkeypatch.setattr(update_mod, "_new_client", lambda: client)
    return client


def pypi(version: str) -> FakeResponse:
    return FakeResponse(200, {"info": {"version": version}})


def tags(*names: str) -> FakeResponse:
    return FakeResponse(200, [{"name": name} for name in names])


NO_PYPI = FakeResponse(404, {"message": "Not Found"})


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths.create(tmp_path / "home")


@pytest.fixture
def settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# version arithmetic
# ---------------------------------------------------------------------------


def test_parse_version_accepts_a_leading_v() -> None:
    assert update_mod.parse_version("v1.2.3") == (1, 2, 3)
    assert update_mod.parse_version("1.2.3") == (1, 2, 3)
    assert update_mod.parse_version("nightly") is None


def test_is_newer_is_a_strict_comparison() -> None:
    assert update_mod.is_newer("0.1.1", "0.1.0")
    assert update_mod.is_newer("v0.2.0", "0.1.9")
    assert not update_mod.is_newer("0.1.0", "0.1.0")
    assert not update_mod.is_newer("0.0.9", "0.1.0")
    assert not update_mod.is_newer("garbage", "0.1.0")


# ---------------------------------------------------------------------------
# check_update
# ---------------------------------------------------------------------------


async def test_a_newer_pypi_release_is_available(
    paths: Paths, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, {update_mod.PYPI_URL: pypi(NEWER)})
    answer = await update_mod.check_update(paths, settings)
    assert answer["available"] is True
    assert answer["latest"] == NEWER
    assert answer["channel"] == "pypi"
    assert answer["source"] == update_mod.PACKAGE
    assert answer["error"] is None


async def test_the_same_version_is_not_available(
    paths: Paths, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, {update_mod.PYPI_URL: pypi(SAME)})
    answer = await update_mod.check_update(paths, settings)
    assert answer["available"] is False
    assert answer["latest"] == SAME


async def test_git_tags_answer_when_the_package_is_not_on_pypi(
    paths: Paths, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = scripted(
        monkeypatch,
        {update_mod.PYPI_URL: NO_PYPI, update_mod.TAGS_URL: tags("v0.1.0", "v9.9.9", "nightly")},
    )
    answer = await update_mod.check_update(paths, settings)
    assert answer["channel"] == "git"
    assert answer["latest"] == NEWER
    assert answer["available"] is True
    assert answer["source"] == f"git+{update_mod.REPO_URL}@v9.9.9"
    assert answer["releaseUrl"] == f"{update_mod.REPO_URL}/releases/tag/v9.9.9"
    assert update_mod.PYPI_URL in client.calls


async def test_the_git_channel_never_asks_pypi(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings.model_validate({"updates": {"channel": "git"}})
    client = scripted(monkeypatch, {update_mod.TAGS_URL: tags("v9.9.9")})
    answer = await update_mod.check_update(paths, settings)
    assert answer["channel"] == "git"
    assert client.calls == [update_mod.TAGS_URL]


async def test_a_network_failure_reports_an_error_and_never_raises(
    paths: Paths, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, {update_mod.PYPI_URL: RuntimeError("connection refused")})
    answer = await update_mod.check_update(paths, settings)
    assert answer["available"] is False
    assert "connection refused" in str(answer["error"])
    assert answer["latest"] == __version__
    # A failure is never cached, so the next call tries again.
    assert not paths.update_check_json.exists()


async def test_a_successful_check_is_cached_and_reused(
    paths: Paths, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = scripted(monkeypatch, {update_mod.PYPI_URL: pypi(NEWER)})
    await update_mod.check_update(paths, settings)
    assert paths.update_check_json.exists()

    second = await update_mod.check_update(paths, settings)
    assert second["cached"] is True
    assert second["latest"] == NEWER
    assert len(client.calls) == 1, "the cache should have answered the second call"


async def test_force_ignores_the_cache(
    paths: Paths, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = scripted(monkeypatch, {update_mod.PYPI_URL: pypi(NEWER)})
    await update_mod.check_update(paths, settings)
    forced = await update_mod.check_update(paths, settings, force=True)
    assert forced["cached"] is False
    assert len(client.calls) == 2


async def test_a_stale_cache_is_ignored(
    paths: Paths, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.update_check_json.write_text(
        json.dumps({"latest": "1.0.0", "checkedAt": "2000-01-01T00:00:00Z", "available": True}),
        encoding="utf-8",
    )
    client = scripted(monkeypatch, {update_mod.PYPI_URL: pypi(NEWER)})
    answer = await update_mod.check_update(paths, settings)
    assert answer["cached"] is False
    assert client.calls == [update_mod.PYPI_URL]


def test_check_enabled_follows_the_setting_and_the_env(settings: Settings) -> None:
    with env_vars(SNOWPEA_UPDATE_CHECK=None):
        assert update_mod.check_enabled(settings) is True
        off = Settings.model_validate({"updates": {"check": False}})
        assert update_mod.check_enabled(off) is False
    with env_vars(SNOWPEA_UPDATE_CHECK="0"):
        assert update_mod.check_enabled(settings) is False


def test_version_suffix_reads_only_the_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = Paths.create(home)
    assert update_mod.version_suffix(home) == ""
    update_mod.write_cache(
        paths,
        {"current": __version__, "latest": NEWER, "available": True, "checkedAt": "2999-01-01"},
    )
    assert update_mod.version_suffix(home) == f" (update available: v{NEWER})"


# ---------------------------------------------------------------------------
# the upgrade command
# ---------------------------------------------------------------------------


def test_uv_is_the_command_whenever_uv_is_on_path(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_mod.shutil, "which", lambda name: "/usr/bin/uv")
    assert update_mod.update_command(paths, "snowpea-agent") == [
        "uv",
        "tool",
        "install",
        "--force",
        "--reinstall",
        # requires-python, so a Mac's system 3.9 is never picked.
        "--python",
        ">=3.11",
        # The images extra rides along, or an upgrade would silently drop the
        # downscaling the installers put there (CORE-multimodal).
        "snowpea-agent[images]",
    ]


@pytest.fixture
def no_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No uv, git or pipx anywhere: not on PATH, not in a home of its own."""
    home = tmp_path / "user-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("UV_INSTALL_DIR", raising=False)
    monkeypatch.delenv("XDG_BIN_HOME", raising=False)
    monkeypatch.setattr(update_mod.shutil, "which", lambda name: None)
    return home


def test_without_uv_the_recorded_install_method_decides(paths: Paths, no_uv: Path) -> None:
    update_mod.write_install_json(paths, "pip", "snowpea-agent")
    assert update_mod.update_command(paths, "snowpea-agent") == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "snowpea-agent[images]",
    ]


def test_without_uv_and_without_a_record_uv_is_installed_first(
    paths: Paths, no_uv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine with only the desktop app: the update button still works."""
    monkeypatch.setattr(update_mod, "_on_windows", lambda: False)
    command = update_mod.update_command(paths, "snowpea-agent")
    assert command is not None and command[:2] == ["sh", "-c"]
    assert update_mod.UV_INSTALL_SH in command
    assert str(no_uv / ".local" / "bin") in command
    assert command[-1] == "snowpea-agent[images]"
    # The profile files are left alone; the app finds ~/.local/bin by itself.
    assert "UV_NO_MODIFY_PATH=1" in command[2]


def test_the_windows_bootstrap_runs_the_powershell_installer(
    no_uv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_mod, "_on_windows", lambda: True)
    command = update_mod.bootstrap_command("snowpea-agent[images]")
    assert command[0] == "powershell" and update_mod.UV_INSTALL_PS1 in command[-1]
    assert "tool install --force --reinstall --python '>=3.11' 'snowpea-agent[images]'" in (
        command[-1]
    )


def test_a_recorded_pipx_install_without_pipx_has_no_command(paths: Paths, no_uv: Path) -> None:
    update_mod.write_install_json(paths, "pipx", "snowpea-agent")
    assert update_mod.update_command(paths, "snowpea-agent") is None
    assert update_mod.manual_command("snowpea-agent").startswith("uv tool install")
    assert "images" in update_mod.manual_command("snowpea-agent")


def test_uv_in_its_install_folder_is_found_off_path(paths: Paths, no_uv: Path) -> None:
    """An app opened from Finder gives the daemon a PATH without ~/.local/bin."""
    folder = no_uv / ".local" / "bin"
    folder.mkdir(parents=True)
    exe = folder / ("uv.exe" if update_mod._on_windows() else "uv")
    exe.write_text("")
    command = update_mod.update_command(paths, "snowpea-agent")
    assert command is not None and command[0] == str(exe)


def test_a_release_tag_installs_the_wheel_when_git_is_missing(no_uv: Path) -> None:
    source = f"git+{update_mod.REPO_URL}@v0.2.34"
    assert update_mod.installable_source(source) == (
        f"{update_mod.REPO_URL}/releases/download/v0.2.34/snowpea_agent-0.2.34-py3-none-any.whl"
    )
    # A branch or a commit has no wheel; it stays a git source.
    assert update_mod.installable_source(f"git+{update_mod.REPO_URL}@main").endswith("@main")
    assert update_mod.installable_source("snowpea-agent") == "snowpea-agent"


def test_with_git_the_tag_is_installed_from_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_mod, "git_usable", lambda: True)
    source = f"git+{update_mod.REPO_URL}@v0.2.34"
    assert update_mod.installable_source(source) == source


def test_the_mac_git_stub_without_the_xcode_tools_is_not_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(update_mod.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(update_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        update_mod.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=2)
    )
    assert update_mod.git_usable() is False
    monkeypatch.setattr(
        update_mod.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0)
    )
    assert update_mod.git_usable() is True


def test_install_json_written_with_a_byte_order_mark_is_read(paths: Paths) -> None:
    """Windows PowerShell's Set-Content -Encoding utf8 writes a BOM."""
    paths.ensure()
    paths.install_json.write_text('\ufeff{"method": "uv", "source": "x"}', encoding="utf-8")
    assert update_mod.read_install_json(paths)["method"] == "uv"


def test_the_images_extra_is_spelled_per_source_kind() -> None:
    """A URL needs the PEP 508 form; a path or a name takes the extra inline."""
    assert update_mod.with_images("snowpea-agent") == "snowpea-agent[images]"
    assert update_mod.with_images("/opt/snowpea") == "/opt/snowpea[images]"
    assert (
        update_mod.with_images("git+https://example.com/snowpea")
        == "snowpea-agent[images] @ git+https://example.com/snowpea"
    )
    # Already asking for an extra: left alone.
    assert update_mod.with_images("snowpea-agent[images]") == "snowpea-agent[images]"


def _fake_installer(tmp_path: Path, *, exit_code: int = 0) -> Path:
    """A script that stands in for ``uv``: prints a line, exits ``exit_code``."""
    script = tmp_path / "fake-uv"
    script.write_text(
        f'#!/bin/sh\necho "installing $*"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_start_update_writes_the_installer_output_to_the_log(
    paths: Paths, tmp_path: Path
) -> None:
    script = _fake_installer(tmp_path)
    process = update_mod.start_update(paths, [str(script), "tool", "install"])
    assert process.wait(timeout=30) == 0
    text = paths.update_log.read_text(encoding="utf-8")
    assert "installing tool install" in text


async def test_wait_for_exit_returns_the_status(paths: Paths, tmp_path: Path) -> None:
    process = update_mod.start_update(paths, [str(_fake_installer(tmp_path, exit_code=2))])
    assert await update_mod.wait_for_exit(process, timeout=30) == 2


async def test_wait_for_exit_gives_up_instead_of_blocking(
    paths: Paths, tmp_path: Path
) -> None:
    """A stuck installer must not pin the watcher: the wait is bounded."""
    script = tmp_path / "hanging-installer"
    script.write_text("#!/bin/sh\nsleep 120\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    process = update_mod.start_update(paths, [str(script)])
    try:
        assert await update_mod.wait_for_exit(process, timeout=0.3, poll_interval=0.05) is None
    finally:
        process.kill()
        process.wait(timeout=30)


async def test_a_cancelled_watcher_stops_at_once(paths: Paths, tmp_path: Path) -> None:
    """`Daemon.stop` cancels the watcher; that must return immediately."""
    script = tmp_path / "hanging-installer-2"
    script.write_text("#!/bin/sh\nsleep 120\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    process = update_mod.start_update(paths, [str(script)])
    try:
        task = asyncio.ensure_future(update_mod.wait_for_exit(process, timeout=600))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        process.kill()
        process.wait(timeout=30)


def test_start_update_does_not_hold_the_log_open(paths: Paths, tmp_path: Path) -> None:
    """The parent closes its handle; only the child keeps a dup."""
    process = update_mod.start_update(paths, [str(_fake_installer(tmp_path))])
    process.wait(timeout=30)
    assert paths.update_log.exists()


# ---------------------------------------------------------------------------
# the RPC surface
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = await make_daemon(tmp_path / "daemon-home")
    try:
        yield instance
    finally:
        await instance.stop()


async def test_check_update_over_rpc(daemon: Daemon, monkeypatch: pytest.MonkeyPatch) -> None:
    scripted(monkeypatch, {update_mod.PYPI_URL: pypi(NEWER)})
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            result = await client.ok("system.checkUpdate", {"force": True})
            assert result["available"] is True
            assert result["latest"] == NEWER
            assert result["current"] == __version__
        finally:
            await client.stop()


async def test_check_update_over_rpc_survives_a_network_failure(
    daemon: Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, {update_mod.PYPI_URL: OSError("dns is down")})
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            result = await client.ok("system.checkUpdate", {"force": True})
            assert result["available"] is False
            assert "dns is down" in result["error"]
        finally:
            await client.stop()


async def test_hello_advertises_the_update_capability(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            info = await client.ok("system.info")
            assert info["restartRequired"] is False
            # The desktop picks macOS/Windows microphone guidance from this.
            assert info["platform"] == sys.platform
        finally:
            await client.stop()


async def test_update_runs_the_command_and_emits_progress(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, {update_mod.PYPI_URL: pypi(NEWER)})
    script = _fake_installer(tmp_path)
    monkeypatch.setattr(
        update_mod, "update_command", lambda paths, source: [str(script), source]
    )
    # The fake installer does not replace the executable on PATH, so the real
    # reader would report whatever ``snowpea`` this machine has installed.
    monkeypatch.setattr(update_mod, "installed_cli_version", lambda: NEWER)

    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            result = await client.ok("system.update")
            assert result["started"] is True
            assert result["error"] is None
            assert str(script) in result["command"]

            started = await client.wait_notification("system.updateProgress")
            assert started["phase"] == "started"

            done = await _wait_for_phase(client, "done")
            assert NEWER in done["message"]

            # The upgrade rewrote files on disk; the running daemon says so.
            info = await client.ok("system.info")
            assert info["restartRequired"] is True

            assert "installing snowpea-agent" in daemon.paths.update_log.read_text(
                encoding="utf-8"
            )
        finally:
            await client.stop()


async def test_a_failing_installer_reports_failed(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, {update_mod.PYPI_URL: pypi(NEWER)})
    script = _fake_installer(tmp_path, exit_code=3)
    monkeypatch.setattr(update_mod, "update_command", lambda paths, source: [str(script)])

    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            await client.ok("system.update")
            failed = await _wait_for_phase(client, "failed")
            assert "status 3" in failed["message"]

            info = await client.ok("system.info")
            assert info["restartRequired"] is False
        finally:
            await client.stop()


async def test_update_without_a_runnable_command_explains_the_manual_one(
    daemon: Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, {update_mod.PYPI_URL: pypi(NEWER)})
    monkeypatch.setattr(update_mod, "update_command", lambda paths, source: None)
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            result = await client.ok("system.update")
            assert result["started"] is False
            assert "uv tool install" in result["command"]
            assert "run this by hand" in str(result["error"])
        finally:
            await client.stop()


async def test_restart_shuts_the_daemon_down(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            assert (await client.ok("system.restart"))["ok"] is True
        finally:
            await client.stop()
    await asyncio.wait_for(daemon.wait_closed(), timeout=5.0)
    assert daemon.shutdown_reason == "restart"


async def _wait_for_phase(client: Any, phase: str, timeout: float = 20.0) -> dict[str, Any]:
    """Wait for a ``system.updateProgress`` notification with ``phase``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for frame in client.notifications:
            if frame["method"] == "system.updateProgress" and frame["params"]["phase"] == phase:
                return dict(frame["params"])
        await asyncio.sleep(0.05)
    seen = [
        f["params"]
        for f in client.notifications
        if f["method"] == "system.updateProgress"
    ]
    raise AssertionError(f"no updateProgress {phase}; saw {seen}")


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


async def test_snowpea_update_check_prints_the_versions(
    daemon: Daemon, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted(monkeypatch, {update_mod.PYPI_URL: pypi(NEWER)})
    code = await cli_commands.update_cli(daemon.paths.home, check_only=True)
    assert code == 0
    out = capsys.readouterr().out
    assert f"update available: v{NEWER}" in out
    assert __version__ in out


async def test_snowpea_update_check_when_up_to_date(
    daemon: Daemon, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted(monkeypatch, {update_mod.PYPI_URL: pypi(SAME)})
    assert await cli_commands.update_cli(daemon.paths.home, check_only=True) == 0
    assert "is up to date" in capsys.readouterr().out


async def test_await_update_gives_up_instead_of_blocking() -> None:
    """A daemon that never reports back must not hang `snowpea update`."""

    class _Silent:
        async def notifications(self) -> Any:
            while True:
                await asyncio.sleep(0.05)
            yield {}  # pragma: no cover - unreachable, makes this a generator

    phase, message = await cli_commands._await_update(_Silent(), timeout=0.2)
    assert phase == "failed"
    assert "did not report back" in message


async def test_await_update_returns_the_terminal_phase() -> None:
    frames = [
        {"method": "session.event", "params": {}},
        {"method": "system.updateProgress", "params": {"phase": "started", "message": "go"}},
        {"method": "system.updateProgress", "params": {"phase": "done", "message": "updated"}},
    ]

    class _Scripted:
        async def notifications(self) -> Any:
            for frame in frames:
                yield frame

    assert await cli_commands._await_update(_Scripted(), timeout=5) == ("done", "updated")


def test_format_update_line_reports_a_failed_check() -> None:
    line = cli_commands.format_update_line(
        {"current": "0.1.0", "latest": "0.1.0", "available": False, "error": "offline"}
    )
    assert "could not check for updates: offline" in line


def test_version_prints_the_cached_nudge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "version-home"
    paths = Paths.create(home)
    update_mod.write_cache(
        paths,
        {"current": __version__, "latest": NEWER, "available": True, "checkedAt": "2999-01-01"},
    )
    assert cli_main.main(["--version", "--home", str(home)]) == 0
    assert f"update available: v{NEWER}" in capsys.readouterr().out


def test_version_says_nothing_without_a_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main.main(["--version", "--home", str(tmp_path / "empty")]) == 0
    out = capsys.readouterr().out
    assert "update available" not in out
    assert __version__ in out


def test_update_is_a_documented_subcommand() -> None:
    parser = cli_main.build_parser()
    args = parser.parse_args(["update", "--check"])
    assert args.subcommand == "update"
    assert args.check_only is True


# ---------------------------------------------------------------------------
# the exit-75 restart path
# ---------------------------------------------------------------------------


def test_exit_75_from_the_tui_re_execs_snowpea(monkeypatch: pytest.MonkeyPatch) -> None:
    execs: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        cli_main, "wait_for_daemon_exit", lambda home, timeout=0.0: None
    )
    monkeypatch.setattr(cli_main.shutil, "which", lambda name: "/usr/local/bin/snowpea")
    monkeypatch.setattr(cli_main.os, "execv", lambda path, argv: execs.append((path, argv)))
    monkeypatch.setattr(sys, "argv", ["snowpea", "--mode", "auto"])

    assert cli_main.relaunch(None) == 0
    assert execs == [("/usr/local/bin/snowpea", ["/usr/local/bin/snowpea", "--mode", "auto"])]


def test_launch_tui_relaunches_on_exit_75(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli_main, "resolve_tui_command", lambda: ["node", "tui.js"])

    class _Info:
        port = 1234
        token = "t"

    async def _ensure(_home: Any) -> _Info:
        return _Info()

    monkeypatch.setattr(cli_main, "ensure_daemon", _ensure)
    monkeypatch.setattr(cli_main.subprocess, "call", lambda command: cli_main.TUI_RESTART_EXIT)
    monkeypatch.setattr(cli_main, "relaunch", lambda home: calls.append("relaunch") or 0)

    args = cli_main.build_parser().parse_args(["--cwd", str(tmp_path)])
    assert cli_main.launch_tui(args, None) == 0
    assert calls == ["relaunch"]


def test_a_normal_tui_exit_code_passes_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli_main, "resolve_tui_command", lambda: ["node", "tui.js"])

    class _Info:
        port = 1234
        token = "t"

    async def _ensure(_home: Any) -> _Info:
        return _Info()

    monkeypatch.setattr(cli_main, "ensure_daemon", _ensure)
    monkeypatch.setattr(cli_main.subprocess, "call", lambda command: 4)
    args = cli_main.build_parser().parse_args(["--cwd", str(tmp_path)])
    assert cli_main.launch_tui(args, None) == 4


# ---------------------------------------------------------------------------
# the installers record how snowpea got here
# ---------------------------------------------------------------------------


def test_install_sh_records_the_install_method() -> None:
    text = (Path(__file__).resolve().parents[1] / "installer" / "install.sh").read_text(
        encoding="utf-8"
    )
    assert "record_install" in text
    assert "install.json" in text


def test_install_ps1_records_the_install_method() -> None:
    text = (Path(__file__).resolve().parents[1] / "installer" / "install.ps1").read_text(
        encoding="utf-8"
    )
    assert "Record-Install" in text
    assert "install.json" in text


def test_install_sh_dry_run_writes_nothing(tmp_path: Path) -> None:
    """``--dry-run`` plans the record instead of writing it (AC: idempotent)."""
    import subprocess

    home = tmp_path / "home"
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path),
            "SNOWPEA_HOME": str(home),
            "SNOWPEA_SKIP_NODE": "1",
            "SNOWPEA_SKIP_PATH": "1",
        }
    )
    script = Path(__file__).resolve().parents[1] / "installer" / "install.sh"
    result = subprocess.run(  # noqa: S603 - fixed argv, test-local
        ["sh", str(script), "--dry-run"], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "install.json" in result.stdout
    assert not (home / "install.json").exists()


def test_write_install_json_round_trips(paths: Paths) -> None:
    update_mod.write_install_json(paths, "uv", "snowpea-agent")
    record = update_mod.read_install_json(paths)
    assert record["method"] == "uv"
    assert record["source"] == "snowpea-agent"
    assert record["time"].endswith("Z")


def test_a_corrupt_install_json_reads_as_empty(paths: Paths) -> None:
    paths.install_json.write_text("{not json", encoding="utf-8")
    assert update_mod.read_install_json(paths) == {}


# ---------------------------------------------------------------------------
# the /update builtin command
# ---------------------------------------------------------------------------


def test_update_is_a_builtin_slash_command() -> None:
    from snowpea_core.commands import builtin

    names = [command.name for command in builtin.COMMANDS]
    assert "update" in names


def test_the_registry_carries_update() -> None:
    from snowpea_core.commands.registry import CommandRegistry, register_builtin_commands

    registry = register_builtin_commands(CommandRegistry())
    assert registry.get("update") is not None


def test_the_restart_handoff_stops_a_daemon_that_outlives_the_drain(monkeypatch) -> None:
    """A relaunch that found the old daemon still draining talked to the old
    version ("Update available v0.2.15 (current v0.2.14)" right after updating).
    Past the drain the old pid is terminated, and only that pid."""
    import subprocess
    import sys
    from types import SimpleNamespace

    from snowpea_core.cli import main as cli_main

    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setattr(cli_main, "read_daemon_json", lambda home: SimpleNamespace(pid=old.pid))
        cli_main.wait_for_daemon_exit(None, timeout=0.3, term_timeout=3.0)
        assert old.wait(timeout=5) is not None
    finally:
        if old.poll() is None:
            old.kill()


def test_the_restart_handoff_returns_at_once_when_the_daemon_is_gone(monkeypatch) -> None:
    import time

    from snowpea_core.cli import main as cli_main

    monkeypatch.setattr(cli_main, "read_daemon_json", lambda home: None)
    started = time.monotonic()
    cli_main.wait_for_daemon_exit(None, timeout=5.0)
    assert time.monotonic() - started < 1.0


def test_only_a_windows_uv_tool_core_replaces_its_own_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uv removes the tool's Scripts folder, which the running core holds open
    on Windows ("os error 5"); only that case is handed to the helper."""
    tool = r"C:\Users\u\AppData\Roaming\uv\tools\snowpea-agent\Scripts\python.exe"
    monkeypatch.setattr(update_mod, "_on_windows", lambda: True)
    monkeypatch.setattr(update_mod.sys, "executable", tool.replace("\\", "/"))
    assert update_mod.replaces_running_core()
    monkeypatch.setattr(update_mod.sys, "frozen", True, raising=False)
    assert not update_mod.replaces_running_core()  # the bundled core
    monkeypatch.setattr(update_mod.sys, "frozen", False, raising=False)
    monkeypatch.setattr(update_mod, "_on_windows", lambda: False)
    assert not update_mod.replaces_running_core()


def test_the_new_core_comes_back_on_the_same_port() -> None:
    argv = ["--port", "0", "--state-dir", r"C:\Users\u\snowpea desktop", "--token", "t"]
    assert update_mod.relaunch_argv(argv, 51234) == [
        "--state-dir",
        r"C:\Users\u\snowpea desktop",
        "--token",
        "t",
        "--port",
        "51234",
    ]
    assert update_mod.relaunch_argv(["--port=7"], 8) == ["--port", "8"]


def test_the_windows_helper_gets_its_plan_in_a_file(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> Any:
        started["argv"] = argv
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(update_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(update_mod.sys, "argv", ["snowpea-core", "--port", "0", "--token", "t"])
    command = ["uv", "tool", "install", "--force", "snowpea-agent @ https://x/y.whl"]
    helper = update_mod.start_windows_update(paths, command, port=40000, pid=99)
    assert helper.pid == 4321
    assert started["argv"][0] == "powershell" and started["argv"][-2] == "-Plan"
    plan = json.loads(Path(started["argv"][-1]).read_text(encoding="utf-8"))
    assert plan["pid"] == 99 and plan["command"] == command
    assert plan["args"] == ["--token", "t", "--port", "40000"]
    assert (paths.home / "update-helper.ps1").read_text(encoding="utf-8").startswith("\nparam")


async def test_on_windows_the_core_hands_the_update_over_and_steps_aside(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daemon.json names the helper (so a reconnecting client waits for the
    port instead of starting the old core), clients hear "done", and the core
    asks to stop so its files can be replaced."""
    from snowpea_core.server import update_handlers

    async def available(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"available": True, "latest": "0.2.99", "source": "git+https://x@v0.2.99"}

    monkeypatch.setattr(update_mod, "check_update", available)
    monkeypatch.setattr(update_mod, "update_command", lambda *_a: ["uv", "tool", "install"])
    monkeypatch.setattr(update_mod, "replaces_running_core", lambda: True)
    handed: dict[str, Any] = {}

    def fake_start(p: Paths, command: list[str], *, port: int, pid: int) -> Any:
        handed.update(port=port, pid=pid)
        return SimpleNamespace(pid=7777)

    monkeypatch.setattr(update_mod, "start_windows_update", fake_start)
    monkeypatch.setattr(update_handlers, "EXIT_DELAY_SEC", 0)
    paths.ensure()
    paths.daemon_json.write_text(json.dumps({"pid": os.getpid(), "port": 40001}))
    notes: list[tuple[str, dict[str, Any]]] = []
    stops: list[str] = []

    async def notify(method: str, params: dict[str, Any]) -> None:
        notes.append((method, params))

    core = SimpleNamespace(
        paths=paths,
        settings=Settings(),
        port=40001,
        hub=SimpleNamespace(notify=notify),
        request_shutdown=stops.append,
        update_task=None,
    )
    result = await update_handlers.update_handler(None, None, core)  # type: ignore[arg-type]
    await core.update_task
    assert result.started and handed == {"port": 40001, "pid": os.getpid()}
    assert json.loads(paths.daemon_json.read_text())["pid"] == 7777
    assert notes[-1][1]["phase"] == "done" and stops == ["update"]
