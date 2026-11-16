"""Background processes end with their session or the daemon — and nothing else does.

Real processes on POSIX.  Every process here is one the test started (through
the tool or ``subprocess``), tracked by pid, and ended by pid at teardown; an
unrelated sleeper the test also started must survive every cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _support import env_vars

from snowpea_core.exec.local import LocalBackend
from snowpea_core.server.app_server import Daemon
from snowpea_core.tools import process as process_tools
from snowpea_core.tools.registry import ToolContext
from snowpea_core.tools.shell import shell

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX processes")


def _alive(pid: int) -> bool:
    """True while ``pid`` runs (a zombie waiting to be reaped counts as gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return not stat[stat.rfind(")") + 2 :].startswith("Z")


async def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


#: Pids this test module started; teardown ends any still running, by pid.
_STARTED: list[int] = []


@pytest.fixture(autouse=True)
def _end_what_we_started() -> Iterator[None]:
    yield
    for pid in _STARTED:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    _STARTED.clear()
    process_tools.REGISTRY.clear()


@pytest.fixture
def bystander() -> Iterator[subprocess.Popen[bytes]]:
    """A sleeper unrelated to any session: no cleanup may touch it."""
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    yield proc
    proc.kill()
    proc.wait()


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    with env_vars(SNOWPEA_PROVIDER=None):
        instance = Daemon(port=0, home=home)
        await instance.start()
        try:
            yield instance
        finally:
            await instance.stop()


async def _session(daemon: Daemon, tmp_path: Path, name: str = "w", **kwargs: Any) -> Any:
    core = daemon.core
    assert core is not None
    work = tmp_path / name
    work.mkdir(exist_ok=True)
    session = await core.sessions.create(work, mode="auto", **kwargs)
    session.backend = LocalBackend(work)
    return session


def _ctx(daemon: Daemon, session: Any) -> ToolContext:
    assert daemon.core is not None
    return ToolContext(session=session, core=daemon.core, backend=session.backend)


async def _background(daemon: Daemon, session: Any, command: str = "sleep 300") -> int:
    result = await shell(_ctx(daemon, session), {"command": command, "background": True})
    assert result.ok, result.error
    [entry] = [e for e in process_tools.REGISTRY.list(session.id) if e.command == command]
    _STARTED.append(entry.pid)
    assert entry.pgid is not None and entry.pgid != os.getpgrp()
    assert _alive(entry.pid)
    return entry.pid


async def test_closing_a_session_ends_its_background_process(
    daemon: Daemon, tmp_path: Path, bystander: subprocess.Popen[bytes]
) -> None:
    assert daemon.core is not None
    session = await _session(daemon, tmp_path)
    other = await _session(daemon, tmp_path, "other")
    pid = await _background(daemon, session)
    kept = await _background(daemon, other)

    await daemon.core.sessions.close(session.id)

    assert await _gone(pid)
    assert _alive(kept)  # another session's server is not touched
    assert bystander.poll() is None
    assert process_tools.REGISTRY.list(session.id) == []


async def test_deleting_a_session_ends_its_background_process(
    daemon: Daemon, tmp_path: Path, bystander: subprocess.Popen[bytes]
) -> None:
    from snowpea_core.server.protocol import SessionDeleteParams
    from snowpea_core.server.session_handlers import session_delete_saved_handler

    assert daemon.core is not None
    session = await _session(daemon, tmp_path)
    pid = await _background(daemon, session)

    await session_delete_saved_handler(
        None, SessionDeleteParams(sessionId=session.id), daemon.core  # type: ignore[arg-type]
    )

    assert await _gone(pid)
    assert bystander.poll() is None


async def test_daemon_stop_ends_every_background_process(
    tmp_path: Path, bystander: subprocess.Popen[bytes]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with env_vars(SNOWPEA_PROVIDER=None):
        daemon = Daemon(port=0, home=home)
        await daemon.start()
        try:
            first = await _background(daemon, await _session(daemon, tmp_path, "a"))
            second = await _background(daemon, await _session(daemon, tmp_path, "b"))
        finally:
            await daemon.stop()
    assert await _gone(first)
    assert await _gone(second)
    assert bystander.poll() is None


async def test_a_command_moved_to_the_background_ends_with_its_session(
    daemon: Daemon, tmp_path: Path, bystander: subprocess.Popen[bytes]
) -> None:
    assert daemon.core is not None
    session = await _session(daemon, tmp_path)
    ctx = _ctx(daemon, session)
    running = asyncio.ensure_future(
        shell(ctx, {"command": "echo $$; sleep 300", "timeout": 600})
    )
    await asyncio.sleep(1.3)
    session.user_waiting.set()
    result = await asyncio.wait_for(running, 10)
    assert "still running" in result.output
    pid = int(result.output.split()[0])
    _STARTED.append(pid)
    assert _alive(pid)

    await daemon.core.sessions.close(session.id)

    assert await _gone(pid)
    assert bystander.poll() is None


async def test_a_subagent_server_passes_to_its_parent(
    daemon: Daemon, tmp_path: Path
) -> None:
    assert daemon.core is not None
    parent = await _session(daemon, tmp_path, "p")
    child = await _session(daemon, tmp_path, "c")
    child.parent_session_id = parent.id
    pid = await _background(daemon, child)

    await daemon.core.sessions.close(child.id)
    assert _alive(pid)  # the parent may still need the dev server it asked for
    assert [e.pid for e in process_tools.REGISTRY.list(parent.id)] == [pid]

    await daemon.core.sessions.close(parent.id)
    assert await _gone(pid)


async def test_a_reused_pid_outside_the_group_is_not_signalled(
    bystander: subprocess.Popen[bytes],
) -> None:
    """An entry whose pid now belongs to another process group is left alone."""
    entry = process_tools.REGISTRY.add(
        session_id="s-x", pid=bystander.pid, command="x", log_path="", backend_kind="local"
    )
    entry.pgid = bystander.pid + 1_000_000  # not the bystander's group
    await process_tools.stop_owned("s-x")
    assert bystander.poll() is None


async def test_a_grandchild_holding_the_output_does_not_hang_the_call(tmp_path: Path) -> None:
    """``adb start-server``-style: the command exits, a process it started keeps stdout."""
    backend = LocalBackend(tmp_path)
    started = time.monotonic()
    result = await backend.run("sleep 60 & echo $!; echo done", timeout=50)
    elapsed = time.monotonic() - started
    grandchild = int(result.stdout.split()[0])
    _STARTED.append(grandchild)
    assert elapsed < 10, elapsed
    assert result.exit_code == 0 and "done" in result.stdout
    assert "kept its output open" in result.stderr
    assert _alive(grandchild)  # as before: what a command leaves behind keeps running


async def test_a_normal_command_has_no_note(tmp_path: Path) -> None:
    result = await LocalBackend(tmp_path).run("echo hi")
    assert result.stdout == "hi\n" and result.stderr == ""
