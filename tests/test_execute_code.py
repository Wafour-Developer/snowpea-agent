"""Tests for the temporary Python execution tool."""

from __future__ import annotations

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
