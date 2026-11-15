"""Keep-awake while a turn runs (server/keep_awake.py)."""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.server import keep_awake as ka
from snowpea_core.server.keep_awake import KeepAwake, command_for, running_turns


class FakeProc:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> int | None:
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


class Spawner:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.procs: list[FakeProc] = []
        self.fail = fail

    def __call__(self, argv: list[str]) -> FakeProc:
        self.calls.append(argv)
        if self.fail:
            raise PermissionError("not allowed")
        proc = FakeProc()
        self.procs.append(proc)
        return proc


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _which(name: str) -> str:
    return f"/usr/bin/{name}"


def make(spawner: Spawner, clock: Clock, **kwargs: Any) -> KeepAwake:
    return KeepAwake(
        platform="darwin", pid=4242, spawner=spawner, which=_which, clock=clock, **kwargs
    )


def test_holds_while_turns_run_and_lingers_after() -> None:
    spawner, clock = Spawner(), Clock()
    awake = make(spawner, clock, linger_sec=30)
    awake.update(0)
    assert spawner.calls == []
    awake.update(1)
    awake.update(2)
    assert spawner.calls == [["/usr/bin/caffeinate", "-i", "-w", "4242"]]
    assert awake.held and awake.summary() == "on (2 turns)"
    awake.update(0)
    clock.now += 20
    awake.update(0)
    assert awake.held, "still inside the linger"
    awake.update(1)  # a back-to-back turn reuses the same process
    awake.update(0)
    clock.now += 29
    awake.update(0)
    assert awake.held and len(spawner.calls) == 1
    clock.now += 2
    awake.update(0)
    assert not awake.held and spawner.procs[0].terminated
    assert awake.summary() == "off"


def test_failure_is_a_quiet_noop() -> None:
    spawner, clock = Spawner(fail=True), Clock()
    awake = make(spawner, clock)
    awake.update(1)
    awake.update(0)
    awake.update(1)
    assert len(spawner.calls) == 1, "one try, then no-op"
    assert not awake.held


def test_missing_binary_is_a_noop() -> None:
    spawner, clock = Spawner(), Clock()
    awake = KeepAwake(platform="linux", spawner=spawner, which=lambda _n: None, clock=clock)
    awake.update(1)
    assert spawner.calls == [] and not awake.held


def test_disabled_never_spawns() -> None:
    spawner, clock = Spawner(), Clock()
    awake = make(spawner, clock, enabled=False)
    awake.update(3)
    assert spawner.calls == [] and awake.summary() == "disabled"


def test_release_on_stop() -> None:
    spawner, clock = Spawner(), Clock()
    awake = make(spawner, clock)
    awake.update(1)
    awake.release()
    assert spawner.procs[0].terminated and not awake.held


def test_platform_commands() -> None:
    assert command_for("darwin", pid=7, which=_which) == [
        "/usr/bin/caffeinate",
        "-i",
        "-w",
        "7",
    ]
    linux = command_for("linux", which=_which)
    assert linux is not None
    assert linux[0] == "/usr/bin/systemd-inhibit"
    assert "--what=idle" in linux and "--mode=block" in linux
    assert linux[-2:] == ["sleep", "infinity"]
    assert command_for("linux", which=lambda _n: None) is None
    assert command_for("win32", which=_which) is None
    assert command_for("freebsd", which=_which) is None


def test_windows_uses_one_thread() -> None:
    calls: list[tuple[int, int]] = []
    import threading

    def call(flags: int) -> int:
        calls.append((flags, threading.get_ident()))
        return 1

    spawner = Spawner()
    awake = KeepAwake(platform="win32", spawner=spawner, win_call=call, clock=Clock())
    awake.update(1)
    awake.release()
    assert spawner.calls == []
    assert [flags for flags, _ in calls] == [
        ka.ES_CONTINUOUS | ka.ES_SYSTEM_REQUIRED,
        ka.ES_CONTINUOUS,
    ]
    assert calls[0][1] == calls[1][1] != threading.get_ident()


def test_windows_failure_is_a_noop() -> None:
    awake = KeepAwake(platform="win32", win_call=lambda _f: 0, clock=Clock())
    awake.update(1)
    assert not awake.held


def test_setting_defaults_on() -> None:
    assert Settings().daemon.keepAwake is True


def _core(*turns: str | None) -> Any:
    sessions = [
        types.SimpleNamespace(id=f"s{i}", current_turn=turn) for i, turn in enumerate(turns)
    ]
    return types.SimpleNamespace(sessions=types.SimpleNamespace(all=lambda: sessions))


class _Waits:
    """An approval queue or question broker: pending requests per session."""

    def __init__(self) -> None:
        self.pending: set[str] = set()

    def count(self, session_id: str) -> int:
        return int(session_id in self.pending)


def test_running_turns_counts_every_session() -> None:
    assert running_turns(_core(None, "t1", "t2", None)) == 2


async def test_watcher_follows_turns_and_setting() -> None:
    spawner, clock = Spawner(), Clock()
    awake = make(spawner, clock, linger_sec=0)
    session = types.SimpleNamespace(id="s1", current_turn="t1")
    core = types.SimpleNamespace(sessions=types.SimpleNamespace(all=lambda: [session]))
    enabled = {"on": True}
    awake.start(lambda: running_turns(core), enabled=lambda: enabled["on"], tick=0.01)
    try:
        await asyncio.sleep(0.05)
        assert awake.held, "a running turn holds it"
        session.current_turn = None
        await asyncio.sleep(0.05)
        assert not awake.held, "a finished turn releases it"
        enabled["on"] = False
        session.current_turn = "t2"
        await asyncio.sleep(0.05)
        assert not awake.held and len(spawner.calls) == 1
    finally:
        await awake.stop()
    assert not awake.held


@pytest.mark.parametrize("queue", ["approvals", "questions"])
async def test_a_turn_waiting_on_the_person_releases_and_answering_reacquires(
    queue: str,
) -> None:
    spawner, clock = Spawner(), Clock()
    awake = make(spawner, clock, linger_sec=30)
    core = _core("t1")
    waits = {"approvals": _Waits(), "questions": _Waits()}
    core.approvals, core.questions = waits["approvals"], waits["questions"]
    awake.start(lambda: running_turns(core), tick=0.01)
    try:
        await asyncio.sleep(0.05)
        assert awake.held, "a working turn holds it"
        waits[queue].pending.add("s0")
        assert running_turns(core) == 0
        await asyncio.sleep(0.05)
        assert awake.held, "it lingers first"
        clock.now += 31
        await asyncio.sleep(0.05)
        assert not awake.held, "a turn waiting on the person releases it after the linger"
        waits[queue].pending.clear()
        await asyncio.sleep(0.05)
        assert awake.held and len(spawner.calls) == 2, "answering re-acquires it"
    finally:
        await awake.stop()


def test_running_turns_skips_only_the_waiting_session() -> None:
    core = _core("t1", "t2")
    core.approvals, core.questions = _Waits(), _Waits()
    core.questions.pending.add("s1")
    assert running_turns(core) == 1
