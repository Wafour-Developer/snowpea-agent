"""M37 T1 fixes from the IDE run: observe-only writes, resize reflow, holder cleanup,
and the workspace migration race."""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import connect, env_vars, make_daemon

from snowpea_core.terminal.manager import TerminalManager, TerminalSpec, close_all_holders

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")


def _alive(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _until(predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def _spec(cwd: Path, argv: list[str]) -> TerminalSpec:
    return TerminalSpec(argv=argv, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                        cwd=str(cwd))


class Conn:
    closed = False

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        pass


@POSIX
async def test_observer_write_gets_a_clear_error(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    with env_vars(SHELL="/bin/sh"):
        daemon = await make_daemon(tmp_path / "home")
        try:
            client = await connect(http, daemon)
            created = await client.ok("terminal.create", {"profile": "shell", "noProject": True})
            tid = created["terminal"]["terminalId"]
            await client.ok("terminal.attach", {"terminalId": tid, "mode": "observe"})
            data = base64.b64encode(b"x").decode()
            answer = await client.call("terminal.write", {"terminalId": tid, "data": data},
                                       timeout=5)
            error = answer["error"]["data"]
            assert error["code"] == "unavailable"
            assert error.get("details", {}).get("reason") == "observe_only"
            answer = await client.call("terminal.resize", {"terminalId": tid, "cols": 80,
                                                           "rows": 20}, timeout=5)
            assert answer["error"]["data"].get("details", {}).get("reason") == "observe_only"
            await client.stop()
        finally:
            if daemon.core is not None and daemon.core.terminals is not None:
                await daemon.core.terminals.close_all()
            await daemon.stop()


@POSIX
async def test_long_wrapped_line_survives_shrink_and_grow(tmp_path: Path) -> None:
    line = "A" * 100 + "B" * 100 + "C" * 100
    manager = TerminalManager(tmp_path / "home")
    await manager.start()
    try:
        info = await manager.create(_spec(tmp_path, [
            sys.executable, "-c", f"print('{line}'); print('END'); import time; time.sleep(30)"]))
        tid = info["terminalId"]
        await _until(lambda: "END" in manager.read(tid)["text"])
        conn = Conn()
        for cols, rows in ((40, 20), (120, 30)):
            attached = await manager.attach(tid, conn, "interactive", cols, rows)
            assert (attached["cols"], attached["rows"]) == (cols, rows)
            await manager.settle(tid)
            joined = manager.read(tid, 60)["text"].replace("\n", "")
            assert line in joined, (cols, manager.read(tid, 60)["text"])
            painted = base64.b64decode(attached["repaint"]).decode()
            assert line in "".join(ch for ch in painted.replace("\r\n", "") if ch in "ABC")
    finally:
        await manager.close_all()
        await manager.shutdown()


@POSIX
async def test_holder_ends_when_its_meta_file_disappears(tmp_path: Path) -> None:
    home = tmp_path / "home"
    manager = TerminalManager(home)
    manager.holder_watch_interval = 0.2
    await manager.start()
    info = await manager.create(_spec(tmp_path, [
        "sh", "-c", "trap '' HUP; echo up; while :; do sleep 0.05; done"]))
    term = manager.get(info["terminalId"])
    holder, child = term.holder_pid, term.pid
    await manager.shutdown()  # a daemon stop: the terminal keeps running
    assert _alive(holder) and _alive(child)
    (home / "terminals" / f"{info['terminalId']}.json").unlink()
    await _until(lambda: not _alive(holder) and not _alive(child))
    assert not (home / "terminals" / f"{info['terminalId']}.exit.json").exists()


@POSIX
async def test_holder_ends_when_its_socket_is_removed(tmp_path: Path) -> None:
    manager = TerminalManager(tmp_path / "home")
    manager.holder_watch_interval = 0.2
    await manager.start()
    info = await manager.create(_spec(tmp_path, ["sleep", "30"]))
    term = manager.get(info["terminalId"])
    holder = term.holder_pid
    await manager.shutdown()
    os.unlink(term.socket)
    await _until(lambda: not _alive(holder))


@POSIX
async def test_close_all_holders_without_a_daemon(tmp_path: Path) -> None:
    home = tmp_path / "home"
    manager = TerminalManager(home)
    await manager.start()
    pids = []
    for argv in (["sleep", "30"], ["sh", "-c", "trap '' HUP TERM; while :; do sleep 0.05; done"]):
        info = await manager.create(_spec(tmp_path, argv))
        term = manager.get(info["terminalId"])
        pids += [term.holder_pid, term.pid]
    await manager.shutdown()
    assert all(_alive(pid) for pid in pids)
    ended = await asyncio.to_thread(close_all_holders, home)
    assert ended == 2
    await _until(lambda: not any(_alive(pid) for pid in pids))
    assert not list((home / "terminals").glob("t*"))


async def test_concurrent_first_opens_migrate_once(tmp_path: Path) -> None:
    """Two first calls at once used to race the ALTER: "duplicate column name: archived"."""
    from snowpea_core.session import workspaces
    from snowpea_core.session.store import Store

    for attempt in range(5):
        store = Store(tmp_path / f"state-{attempt}.db")
        try:
            await asyncio.gather(*(workspaces.ensure(store) for _ in range(8)))
            names = {str(r["name"]) for r in store._query("PRAGMA table_info(sessions)")}
            assert {"pinned", "archived"} <= names
        finally:
            store.close()
    # A column some other connection added first is not an error either.
    store = Store(tmp_path / "state-x.db")
    try:
        workspaces._ensure_sync(store)
        workspaces._add_column(store, "archived", "INTEGER")
    finally:
        store.close()


@POSIX
async def test_daemon_stop_and_terminals(tmp_path: Path, http: aiohttp.ClientSession,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    from snowpea_core.cli.commands import daemon_stop

    home = tmp_path / "home"
    with env_vars(SHELL="/bin/sh"):
        daemon = await make_daemon(home)
        client = await connect(http, daemon)
        created = await client.ok("terminal.create", {"profile": "shell", "noProject": True})
        term = daemon.core.terminals.get(created["terminal"]["terminalId"])
        holder, child = term.holder_pid, term.pid
        await client.stop()
        await daemon.stop()  # a plain stop keeps the terminal
        assert _alive(holder) and _alive(child)
        assert await daemon_stop(home, and_terminals=True) == 0
    assert "ended 1 terminal session" in capsys.readouterr().out
    await _until(lambda: not _alive(holder) and not _alive(child))
