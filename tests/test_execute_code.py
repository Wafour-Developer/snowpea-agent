"""Tests for the temporary Python execution tool."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from snowpea_core.agent.loop import _run_one_call, _spill_long_result
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.base import ToolCall
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools.execute_code import TOOLS, execute_code
from snowpea_core.tools.registry import ToolContext


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    settings = Settings()
    core = Core(settings=settings, paths=Paths.create(tmp_path / "home"), token="test-token")
    session = Session(id="execute-code", workdir=tmp_path)
    return ToolContext(session=session, core=core, backend=LocalBackend(tmp_path))


async def test_prints_stdout(ctx: ToolContext) -> None:
    result = await execute_code(ctx, {"code": "print('hello')"})
    assert result.ok is True
    assert result.output == "hello\n[exit 0]"


async def test_nonzero_exit_reports_code_and_stderr(ctx: ToolContext) -> None:
    result = await execute_code(
        ctx, {"code": "import sys; print('bad', file=sys.stderr); raise SystemExit(7)"}
    )
    assert result.ok is False
    assert result.error == "command exited with 7"
    assert "--- stderr ---\nbad" in result.output
    assert result.output.endswith("[exit 7]")


async def test_timeout_kills_infinite_loop_promptly(ctx: ToolContext) -> None:
    started = time.monotonic()
    result = await execute_code(ctx, {"code": "while True: pass", "timeout": 1})
    assert time.monotonic() - started < 3
    assert result.ok is False
    assert result.error is not None and "timed out" in result.error


async def test_temp_file_is_deleted_even_on_error(ctx: ToolContext) -> None:
    result = await execute_code(ctx, {"code": "raise RuntimeError('boom')"})
    assert result.ok is False
    assert list((ctx.session.workdir / ".snowpea" / "tmp").glob("execute-code-*.py")) == []


async def test_relative_paths_resolve_from_requested_workdir(ctx: ToolContext) -> None:
    nested = ctx.session.workdir / "nested"
    nested.mkdir()
    (nested / "value.txt").write_text("relative", encoding="utf-8")
    result = await execute_code(
        ctx, {"code": "print(open('value.txt').read())", "cwd": "nested"}
    )
    assert result.ok is True
    assert result.output.startswith("relative\n")


async def test_project_virtualenv_python_is_preferred(ctx: ToolContext) -> None:
    python = ctx.session.workdir / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\necho project-python\n", encoding="utf-8")
    python.chmod(0o700)
    result = await execute_code(ctx, {"code": "print('system-python')"})
    assert result.ok is True
    assert result.output.startswith("project-python\n")


def test_execute_code_requires_approval_in_plan_mode(ctx: ToolContext) -> None:
    tool = TOOLS[0]
    assert tool.permission == "exec"
    assert PermissionPolicy().decide("plan", tool.permission, tool, {"code": "print(1)"}) == "ask"


async def test_deny_exec_session_refuses_execute_code(ctx: ToolContext) -> None:
    ctx.core.tools.register(TOOLS[0])
    ctx.session.mode = "auto"
    ctx.session.deny_exec = True
    result = await _run_one_call(
        ctx.core,
        ctx.session,
        ctx.backend,
        PermissionPolicy(),
        ToolCall(id="call-1", name="execute_code", arguments={"code": "print(1)"}),
        "turn-1",
        False,
    )
    assert result is None
    assert ctx.session.history.messages[-1].name == "execute_code"
    assert "exec tools are disabled for this session" in ctx.session.history.messages[-1].content


async def test_large_output_is_capped_and_uses_shell_spill_path(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await execute_code(ctx, {"code": "print('x' * (5 * 1024 * 1024))"})
    assert result.ok is True
    assert "[truncated," in result.output
    monkeypatch.setenv("SNOWPEA_HOME", str(ctx.core.paths.home))
    lines = "\n".join(str(index) for index in range(500))
    spilled = _spill_long_result(ctx.core, "execute_code", type(result)(ok=True, output=lines))
    assert "lines omitted" in spilled.output


def test_a_frozen_core_runs_code_with_a_python_not_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A desktop (PyInstaller) core's sys.executable is the core: it took the
    script for an unknown argument, so execute_code never ran there."""
    import sys

    from snowpea_core.tools import execute_code as module

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/python3")
    assert module.interpreter(tmp_path) == "/usr/bin/python3"
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    assert module.interpreter(tmp_path).endswith(" --run-hook")
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("")
    assert module.interpreter(tmp_path) == str(venv / "python")


class _WindowsLikeBackend:
    """Mimics Windows: a script still open here cannot be run by the child,
    and it must still exist while the child runs (deleted only after)."""

    def __init__(self) -> None:
        self.script: Path | None = None

    async def run(self, command: str, **_: object) -> object:
        from snowpea_core.exec.backend import ExecResult

        script = Path(command.rsplit(" ", 1)[1].strip("'\""))
        self.script = script
        open_files = set()
        for link in Path("/proc/self/fd").iterdir():
            try:
                open_files.add(os.readlink(link))
            except OSError:
                continue
        assert str(script) not in open_files, "script still open when the child starts"
        assert script.read_text(encoding="utf-8") == "print(1)"
        return ExecResult(exit_code=0, stdout="1\n")


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc")
async def test_script_is_closed_before_spawn_and_deleted_after_wait(
    ctx: ToolContext,
) -> None:
    backend = _WindowsLikeBackend()
    ctx.backend = backend  # type: ignore[assignment]
    result = await execute_code(ctx, {"code": "print(1)"})
    assert result.ok is True
    assert backend.script is not None and not backend.script.exists()


async def test_a_locked_script_is_retried_then_left_for_the_sweep(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WinError 32 on delete must not fail a call whose program ran."""
    from snowpea_core.tools import execute_code as module

    monkeypatch.setattr(module, "UNLINK_PAUSE", 0.0)
    real_unlink = Path.unlink
    attempts: list[Path] = []

    def locked(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith("execute-code-"):
            attempts.append(self)
            raise PermissionError(32, "The process cannot access the file", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)
    result = await execute_code(ctx, {"code": "print('ran')"})
    assert result.ok is True and result.output.startswith("ran\n")
    assert len(attempts) == module.UNLINK_ATTEMPTS
    left = attempts[0]
    assert left.exists()

    # A later call sweeps it once it is old enough.
    monkeypatch.setattr(Path, "unlink", real_unlink)
    old = time.time() - module.STALE_AFTER - 10
    os.utime(left, (old, old))
    result = await execute_code(ctx, {"code": "print('again')"})
    assert result.ok is True
    assert not left.exists()


async def test_a_lock_that_clears_is_retried_until_deleted(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.tools import execute_code as module

    monkeypatch.setattr(module, "UNLINK_PAUSE", 0.0)
    real_unlink = Path.unlink
    failures = {"left": 2}

    def flaky(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith("execute-code-") and failures["left"]:
            failures["left"] -= 1
            raise PermissionError(32, "The process cannot access the file", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky)
    result = await execute_code(ctx, {"code": "print(1)"})
    assert result.ok is True
    assert list((ctx.session.workdir / ".snowpea" / "tmp").glob("execute-code-*.py")) == []


def test_windows_quotes_paths_for_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd.exe does not strip POSIX single quotes."""
    import sys

    from snowpea_core.tools import execute_code as module

    monkeypatch.setattr(sys, "platform", "win32")
    assert module._quote(r"C:\Users\a b\x.py") == r'"C:\Users\a b\x.py"'
    assert module._quote(r"C:\x.py") == r"C:\x.py"
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")
    assert module.interpreter(tmp_path) == str(scripts / "python.exe")
