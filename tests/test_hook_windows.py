"""Plugin hooks on Windows, and hooks that fail anywhere.

A user's session opened 43 console windows: a plugin's PreToolUse hook said
``python3 hooks/browser_guard.py``, which on Windows is the Microsoft Store
alias stub — it exits 9009 and leaves its own console open — and the hook ran
on every matching call.  Now hooks get no window and a job of their own, a
bare ``python3`` resolves to a usable interpreter, a failing hook is warned
about once per session and shown to the person, a fail-closed guard that
cannot run blocks, and a hook timing out can never end the turn.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.exec import winproc
from snowpea_core.skills import hooks

STORE = r"C:\Users\kim\AppData\Local\Microsoft\WindowsApps\python3.exe"
REAL = r"C:\Python312\python.exe"
ENV = {"LOCALAPPDATA": r"C:\Users\kim\AppData\Local", "PATH": "x"}


def _which(found: dict[str, str | None]) -> Any:
    return lambda name, path=None: found.get(name)


@pytest.mark.parametrize(
    ("command", "found", "substituted"),
    [
        ('python3 "C:/p/hooks/guard.py"', {"python3": STORE}, True),  # Store stub
        ("python3 hooks/guard.py", {"python3": None}, True),  # missing
        ("python hooks/guard.py", {"python": None}, True),
        ("PYTHON3.EXE hooks/guard.py", {"PYTHON3.EXE": None}, True),
        ('"python3" hooks/guard.py', {"python3": None}, True),
        ("python3 hooks/guard.py", {"python3": REAL}, False),  # a real one is kept
        ("py -3 hooks/guard.py", {"py": None}, False),  # the launcher is not touched
        ("node hooks/guard.js", {"node": None}, False),
        (r"C:\Python312\python.exe hooks/guard.py", {}, False),  # a path is not bare
        ("python3-config --help", {}, False),
    ],
)
def test_bare_python_resolution_table(
    command: str, found: dict[str, str | None], substituted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks.sys, "executable", r"C:\snowpea\python.exe")
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    out = hooks.windows_python(command, platform="win32", which=_which(found), environ=ENV)
    if substituted:
        assert out.startswith(r"C:\snowpea\python.exe ")
        assert out.endswith("hooks/guard.py") or out.endswith('"C:/p/hooks/guard.py"')
    else:
        assert out == command


def test_posix_hooks_are_never_rewritten() -> None:
    assert hooks.windows_python("python3 x.py", platform="linux") == "python3 x.py"


def test_store_alias_is_judged_by_folder() -> None:
    assert winproc.is_store_alias(STORE, ENV)
    assert winproc.is_store_alias(r"D:\Other\Microsoft\WindowsApps\python.exe", {})
    assert not winproc.is_store_alias(REAL, ENV)


def test_substitution_is_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    hooks._SUBSTITUTED.clear()
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    caplog.set_level(logging.INFO, logger="snowpea.skills.hooks")
    for _ in range(3):
        hooks.windows_python("python3 a.py", which=_which({}), environ=ENV)
    assert sum("not a usable Python" in r.message for r in caplog.records) == 1


def test_mcp_stdio_program_follows_the_same_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winproc, "python_substitute", lambda program: program == "python3")
    assert winproc.python_program("python3", platform="win32") == sys.executable
    assert winproc.python_program("npx", platform="win32") == "npx"
    assert winproc.python_program("python3", platform="linux") == "python3"


# ---------------------------------------------------------------------------
# spawning
# ---------------------------------------------------------------------------


class _Hung:
    """A hook process that never answers until it is ended."""

    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.ended = asyncio.Event()

    async def communicate(self, _body: bytes) -> tuple[bytes, bytes]:
        await self.ended.wait()
        return b"", b""

    def kill(self) -> None:
        # What asyncio does when the process is already gone.
        raise ProcessLookupError()


def _core(tmp_path: Path, registry: hooks.HookRegistry, hub: Any = None) -> Any:
    return SimpleNamespace(
        paths=SimpleNamespace(home=tmp_path), skills=SimpleNamespace(hooks=registry), hub=hub,
        tools=None,
    )


def _session(tmp_path: Path, sid: str = "s-hook") -> Any:
    return SimpleNamespace(id=sid, workdir=tmp_path)


async def test_hooks_spawn_without_a_window_in_a_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    process = _Hung()
    terminated: list[int] = []

    async def create(command: str, **kwargs: Any) -> _Hung:
        seen["command"], seen["kwargs"] = command, kwargs
        return process

    class Tree:
        pid = 4242
        closed = False

        def terminate(self) -> None:
            terminated.append(self.pid)
            process.ended.set()

        def release(self, keep_leftovers: bool = False) -> None:
            self.closed = True

    monkeypatch.setattr(hooks.asyncio, "create_subprocess_shell", create)
    monkeypatch.setattr(winproc, "enabled", lambda: True)
    monkeypatch.setattr(winproc, "contain", lambda pid: Tree())
    monkeypatch.setattr(winproc, "tree_of", lambda p: Tree() if p is process else None)
    hook = hooks.Hook(event="PreToolUse", matcher="*", command="guard", timeout=0.2)
    code, _out, err = await hooks.run_hook_output(hook, {}, home=tmp_path)
    assert seen["kwargs"]["creationflags"] == winproc.SPAWN_FLAGS
    assert code == hooks.HOOK_FAILED and "timed out" in err
    assert terminated == [4242]  # the whole tree, not just cmd.exe


async def test_a_hook_timeout_never_raises_process_lookup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``internal: ProcessLookupError`` turn error, 30 s into a shell call."""
    process = _Hung()

    async def create(command: str, **kwargs: Any) -> _Hung:
        return process

    monkeypatch.setattr(hooks.asyncio, "create_subprocess_shell", create)
    hook = hooks.Hook(event="PreToolUse", matcher="*", command="guard", timeout=0.1)
    code, _out, err = await hooks.run_hook_output(hook, {}, home=tmp_path)
    assert code == hooks.HOOK_FAILED and "timed out" in err


async def test_an_unexpected_error_in_the_hook_machinery_is_a_failed_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def create(command: str, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks.asyncio, "create_subprocess_shell", create)
    hook = hooks.Hook(event="PreToolUse", matcher="*", command="guard")
    code, _out, err = await hooks.run_hook_output(hook, {}, home=tmp_path)
    assert code == hooks.HOOK_FAILED and "boom" in err


# ---------------------------------------------------------------------------
# failing hooks
# ---------------------------------------------------------------------------


class _Hub:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def emit_event(self, session_id: str, event: Any) -> None:
        self.events.append((session_id, event))


def _registry(**kwargs: Any) -> hooks.HookRegistry:
    registry = hooks.HookRegistry()
    registry.add(
        hooks.Hook(
            event="PreToolUse", matcher="read_file", command="exit 9", plugin="guardian", **kwargs
        )
    )
    return registry


async def test_a_failing_hook_warns_once_per_session_and_is_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    hooks.HEALTH.clear()
    hooks._WARNED.clear()

    async def failing(hook: Any, payload: Any, **_: Any) -> tuple[int, str]:
        return 9009, "'python3' is not recognized as an internal or external command\r\nx"

    monkeypatch.setattr(hooks, "run_hook", failing)
    caplog.set_level(logging.WARNING, logger="snowpea.skills.hooks")
    hub = _Hub()
    core = _core(tmp_path, _registry(), hub)
    for _ in range(3):
        assert await hooks.pre_tool_use(core, _session(tmp_path), "read_file", {}) is None
    await hooks.pre_tool_use(core, _session(tmp_path, "s-other"), "read_file", {})
    warnings = [r for r in caplog.records if "is not running" in r.message]
    assert len(warnings) == 2  # once per session, not once per call
    assert "exit 9009" in warnings[0].message and "not recognized" in warnings[0].message
    assert [sid for sid, _ in hub.events] == ["s-hook", "s-other"]
    payload = hub.events[0][1]
    text = str(payload)
    assert "hook_failed" in text and "guardian" in text and "guard hook is not running" in text
    [line] = hooks.health_lines()
    assert "guardian" in line and "4 failures" in line and "command not found" in line


async def test_a_hook_that_recovers_leaves_the_health_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks.HEALTH.clear()
    hooks._WARNED.clear()
    answers = iter([(1, "bad"), (0, "")])

    async def flaky(hook: Any, payload: Any, **_: Any) -> tuple[int, str]:
        return next(answers)

    monkeypatch.setattr(hooks, "run_hook", flaky)
    core = _core(tmp_path, _registry())
    await hooks.pre_tool_use(core, _session(tmp_path), "read_file", {})
    assert hooks.health_lines()
    await hooks.pre_tool_use(core, _session(tmp_path), "read_file", {})
    assert hooks.health_lines() == []


async def test_a_fail_closed_guard_that_cannot_start_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks.HEALTH.clear()
    hooks._WARNED.clear()

    async def cannot_start(command: str, **kwargs: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(hooks.asyncio, "create_subprocess_shell", cannot_start)
    core = _core(tmp_path, _registry(fail_closed=True), _Hub())
    blocked = await hooks.pre_tool_use(core, _session(tmp_path), "read_file", {})
    assert blocked is not None and "guardian guard hook failed" in blocked
    # Windows "not found" (9009 from cmd.exe) blocks the same way.
    async def not_found(hook: Any, payload: Any, **_: Any) -> tuple[int, str]:
        return 9009, "'python3' is not recognized"

    monkeypatch.setattr(hooks, "run_hook", not_found)
    blocked = await hooks.pre_tool_use(core, _session(tmp_path), "read_file", {})
    assert blocked is not None and "blocked" in blocked


async def test_without_fail_closed_a_broken_guard_lets_the_call_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cannot_start(command: str, **kwargs: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(hooks.asyncio, "create_subprocess_shell", cannot_start)
    core = _core(tmp_path, _registry(), _Hub())
    assert await hooks.pre_tool_use(core, _session(tmp_path), "read_file", {}) is None


async def test_a_crash_inside_run_event_cannot_end_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_a: Any, **_k: Any) -> Any:
        raise ProcessLookupError()

    monkeypatch.setattr(hooks, "run_event", broken)
    core = _core(tmp_path, _registry())
    assert await hooks.pre_tool_use(core, _session(tmp_path), "read_file", {}) is None
    await hooks.post_tool_use(_core(tmp_path, _registry()), _session(tmp_path), "read_file", {})
    closed = _core(tmp_path, _registry(fail_closed=True))
    blocked = await hooks.pre_tool_use(closed, _session(tmp_path), "read_file", {})
    assert blocked is not None and "fail-closed" in blocked


async def test_skill_hooks_command_lists_failing_hooks(tmp_path: Path) -> None:
    from snowpea_core.commands import skill_cmd

    hooks.HEALTH.clear()
    said: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    ctx = SimpleNamespace(core=_core(tmp_path, _registry()), say=say)
    await skill_cmd.hooks_status(ctx)  # type: ignore[arg-type]
    assert said[-1] == "1 plugin hooks loaded; none has failed."
    await hooks.note_failure(None, _session(tmp_path), _registry().hooks["PreToolUse"][0], 1, "x")
    await skill_cmd.hooks_status(ctx)  # type: ignore[arg-type]
    assert "1 failing" in said[-1] and "guardian" in said[-1]
    hooks.HEALTH.clear()
    hooks._WARNED.clear()
