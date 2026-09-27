"""One daemon per home: the lock, the locked exit, and ensure_daemon not orphaning a slow daemon."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from snowpea_core.cli import daemon_client
from snowpea_core.server.app_server import (
    HOME_LOCKED_EXIT,
    HomeLocked,
    acquire_home_lock,
    release_home_lock,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")


def test_second_lock_on_the_same_home_is_refused(tmp_path: Path) -> None:
    first = acquire_home_lock(tmp_path)
    try:
        # A different process must be refused; flock is per open file
        # description, so a child process is the honest check.
        script = textwrap.dedent(
            f"""
            import sys
            from snowpea_core.server.app_server import HomeLocked, acquire_home_lock
            try:
                acquire_home_lock({str(tmp_path)!r})
            except HomeLocked as exc:
                print(exc)
                sys.exit(3)
            sys.exit(0)
            """
        )
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        assert done.returncode == 3, done.stderr
        assert "another snowpea daemon" in done.stdout
    finally:
        release_home_lock(first)
    # Released: the next daemon can take it.
    again = acquire_home_lock(tmp_path)
    release_home_lock(again)


def test_locked_daemon_exits_with_the_locked_status(tmp_path: Path) -> None:
    held = acquire_home_lock(tmp_path)
    try:
        done = subprocess.run(
            [sys.executable, "-m", "snowpea_core", "--home", str(tmp_path), "--port", "0"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        release_home_lock(held)
    assert done.returncode == HOME_LOCKED_EXIT
    assert "another snowpea daemon" in done.stderr


def test_home_locked_is_a_runtime_error() -> None:
    assert issubclass(HomeLocked, RuntimeError)
    assert daemon_client.HOME_LOCKED_EXIT == HOME_LOCKED_EXIT


async def test_ensure_daemon_retries_a_slow_daemon_instead_of_starting_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = daemon_client.DaemonInfo(port=1, pid=4242, token="t")
    answers = iter([False, False, True])

    async def health(_port: int) -> bool:
        return next(answers)

    monkeypatch.setattr(daemon_client, "read_daemon_json", lambda _home: info)
    monkeypatch.setattr(daemon_client, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_client, "health_ok", health)
    monkeypatch.setattr(daemon_client, "POLL_INTERVAL_SEC", 0)
    stopped: list[int] = []
    monkeypatch.setattr(daemon_client, "_stop_unresponsive", stopped.append)

    def no_spawn(*_a: object, **_k: object) -> None:
        raise AssertionError("must not spawn a second daemon")

    monkeypatch.setattr(daemon_client, "_spawn_daemon", no_spawn)
    assert await daemon_client.ensure_daemon(tmp_path) is info
    assert stopped == []


async def test_ensure_daemon_stops_a_dead_to_the_world_daemon_before_replacing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = daemon_client.DaemonInfo(port=1, pid=4242, token="t")
    order: list[str] = []

    async def health(_port: int) -> bool:
        return False

    monkeypatch.setattr(daemon_client, "read_daemon_json", lambda _home: info)
    monkeypatch.setattr(daemon_client, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_client, "health_ok", health)
    monkeypatch.setattr(daemon_client, "POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(
        daemon_client, "_stop_unresponsive", lambda pid: order.append(f"stop {pid}")
    )

    class Spawned(Exception):
        pass

    def spawn(*_a: object, **_k: object) -> None:
        order.append("spawn")
        raise Spawned

    monkeypatch.setattr(daemon_client, "_spawn_daemon", spawn)
    with pytest.raises(Spawned):
        await daemon_client.ensure_daemon(tmp_path)
    assert order == ["stop 4242", "spawn"]


async def test_stopping_daemon_leaves_a_successors_daemon_json_alone(tmp_path: Path) -> None:
    import json

    from snowpea_core.server.app_server import Daemon

    daemon = Daemon(port=0, home=tmp_path)
    await daemon.start()
    try:
        advert = tmp_path / "daemon.json"
        successor = json.loads(advert.read_text()) | {"pid": 999_999, "port": 1}
        advert.write_text(json.dumps(successor))
    finally:
        await daemon.stop()
    assert json.loads(advert.read_text())["pid"] == 999_999

    own = Daemon(port=0, home=tmp_path)
    await own.start()
    await own.stop()
    assert not advert.exists()
