"""macOS voice fixes: native arch for the audio runtime, a load check after
install, and the Homebrew PATH for a core started from the Dock."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core import __main__ as core_main
from snowpea_core.audio import install, runtime


@pytest.fixture(autouse=True)
def _fresh_prefix() -> Any:
    runtime.native_prefix.cache_clear()
    yield
    runtime.native_prefix.cache_clear()


def test_apple_silicon_runs_the_runtime_natively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="1\n"))
    monkeypatch.setattr(
        runtime.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "arch" else None
    )
    assert runtime.native_prefix() == ("arch", "-arm64")
    assert runtime.runtime_command(tmp_path)[:2] == ["arch", "-arm64"]
    assert runtime.runtime_install_argv(tmp_path, "sherpa-onnx")[:2] == ["arch", "-arm64"]
    assert all(argv[:2] == ["arch", "-arm64"] for argv in runtime.create_argv(tmp_path))


def test_intel_and_linux_add_no_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="0\n"))
    assert runtime.native_prefix() == ()
    runtime.native_prefix.cache_clear()
    monkeypatch.setattr(sys, "platform", "linux")
    assert runtime.native_prefix() == ()


def test_a_frozen_core_does_not_try_to_make_a_venv_of_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    assert all(sys.executable not in argv for argv in runtime.create_argv(tmp_path))


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    async def say(self, line: str) -> None:
        self.lines.append(line)


async def test_a_module_that_will_not_load_is_reinstalled_once(tmp_path: Path) -> None:
    codes = iter([1, 0, 0])  # import fails, reinstall ok, import ok
    seen: list[list[str]] = []

    async def execute(argv: list[str], _log: Any) -> int:
        seen.append(list(argv))
        return next(codes)

    log = _Log()
    ok = await install.verify_runtime_import(
        tmp_path, "sherpa-onnx-zipformer-ko", "sherpa-onnx", log, execute
    )
    assert ok is True
    assert any("--force-reinstall" in argv or "--reinstall" in argv for argv in seen)
    assert log.lines[-1] == "sherpa_onnx loads"


async def test_an_engine_that_still_will_not_load_fails_the_install(tmp_path: Path) -> None:
    codes = iter([1, 0, 1])

    async def execute(argv: list[str], _log: Any) -> int:
        return next(codes)

    log = _Log()
    assert not await install.verify_runtime_import(
        tmp_path, "sherpa-onnx-zipformer-ko", "sherpa-onnx", log, execute
    )
    assert "still cannot be loaded" in log.lines[-1]


def test_a_dock_started_core_finds_homebrew_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    brew = tmp_path / "brew-bin"
    brew.mkdir()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(core_main, "MACOS_TOOL_DIRS", (str(brew), "/nonexistent/bin"))
    env = {"PATH": "/usr/bin:/bin"}
    core_main.extend_macos_path(env)
    assert env["PATH"] == f"/usr/bin:/bin:{brew}"
    core_main.extend_macos_path(env)  # idempotent
    assert env["PATH"].count(str(brew)) == 1
