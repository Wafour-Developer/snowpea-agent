"""Holder drain: keep the PTY open while session children still run after the CLI exits."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")

from snowpea_core.terminal.manager import TerminalManager, TerminalSpec  # noqa: E402

FAKE_CODEX = Path(__file__).resolve().parent / "fixtures" / "fake_clis" / "fake_codex.py"


class Conn:
    def __init__(self) -> None:
        self.closed = False
        self.data = bytearray()
        self.notes: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notes.append((method, params))
        if method == "terminal.data":
            import base64

            self.data += base64.b64decode(params["data"])


def _env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}


def _spec(tmp_path: Path, argv: list[str], **kwargs: Any) -> TerminalSpec:
    env = kwargs.pop("env", None) or _env()
    return TerminalSpec(argv=argv, env=env, cwd=str(tmp_path), **kwargs)


def _make(home: Path, events: list, **kwargs: Any) -> TerminalManager:
    def emit(method: str, params: dict[str, Any]) -> None:
        events.append((method, params))

    async def send(conn: Conn, method: str, params: dict[str, Any]) -> None:
        await conn.notify(method, params)

    return TerminalManager(home, emit=emit, send=send, **kwargs)


@pytest.fixture
async def env(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    events: list = []
    managers: list[TerminalManager] = []
    holders: set[int] = set()

    def new(**kwargs: Any) -> TerminalManager:
        manager = _make(home, events, **kwargs)
        managers.append(manager)
        return manager

    box = {"new": new, "home": home, "events": events, "holders": holders, "tmp": tmp_path}
    try:
        yield box
    finally:
        for manager in managers:
            for term in manager.terminals():
                if term.holder_pid:
                    holders.add(term.holder_pid)
            await manager.close_all()
            await manager.shutdown()
        for pid in holders:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            os.kill(pid, signal.SIGKILL)


async def _until(predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_background_updater_output_and_marker_survive_parent_exit(env) -> None:
    """Simulates Codex self-update: parent exits; the holder must wait for the child."""
    manager = env["new"]()
    await manager.start()
    marker = env["tmp"] / "updater.done"
    conn = Conn()
    argv = [
        sys.executable,
        str(FAKE_CODEX),
        "--background-updater",
        "--background-updater-marker",
        str(marker),
    ]
    term_env = {**_env(), "FAKE_CLI_BG_DELAY": "0.8", "FAKE_CLI_BG_MSG": "post-exit output\n"}
    info = await manager.create(_spec(env["tmp"], argv, env=term_env, profile="codex"))
    tid = info["terminalId"]
    await manager.attach(tid, conn, "observe")
    await _until(lambda: b"updater started" in conn.data)
    assert manager.get(tid).alive
    result = await manager.wait(tid, timeout=12)
    assert result["reason"] == "exit"
    assert marker.is_file() and marker.read_text() == "done"
    text = conn.data.decode("utf-8", "replace")
    assert "post-exit output" in text
    start = text.index("updater started")
    after = text.index("post-exit output")
    assert after > start
    exit_idx = next(
        i for i, (method, _) in enumerate(env["events"]) if method == "terminal.exit"
    )
    assert exit_idx > 0


async def test_drain_cap_ends_a_stuck_session_child(env) -> None:
    manager = env["new"]()
    manager.holder_drain_max = 0.25
    await manager.start()
    marker = env["tmp"] / "never.done"
    argv = [
        sys.executable,
        str(FAKE_CODEX),
        "--background-updater",
        "--background-updater-marker",
        str(marker),
    ]
    term_env = {**_env(), "FAKE_CLI_BG_DELAY": "60"}
    info = await manager.create(_spec(env["tmp"], argv, env=term_env, profile="codex"))
    tid = info["terminalId"]
    await _until(lambda: "updater started" in manager.read(tid)["text"])
    result = await manager.wait(tid, timeout=5)
    assert result["reason"] == "exit"
    assert not marker.exists()


async def test_normal_exit_stays_fast(env) -> None:
    manager = env["new"]()
    await manager.start()
    started = time.monotonic()
    info = await manager.create(_spec(env["tmp"], ["sh", "-c", "echo quick; exit 0"]))
    tid = info["terminalId"]
    await manager.wait(tid, timeout=3)
    assert time.monotonic() - started < 0.8


async def test_terminal_close_still_ends_promptly(env) -> None:
    manager = env["new"]()
    await manager.start()
    info = await manager.create(_spec(
        env["tmp"],
        ["sh", "-c", "trap '' HUP TERM; echo stubborn; while :; do sleep 0.05; done"],
    ))
    tid = info["terminalId"]
    await _until(lambda: "stubborn" in manager.read(tid)["text"])
    started = time.monotonic()
    result = await manager.close(tid)
    assert time.monotonic() - started < 4.0
    assert result["exitCode"] == 128 + signal.SIGKILL