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


async def test_daemon_json_says_ready_only_after_boot(tmp_path: Path) -> None:
    """A client can tell a booting daemon (skills, MCP) from a hung one."""
    import json

    from snowpea_core.server.app_server import Daemon

    seen: list[bool] = []
    daemon = Daemon(port=0, home=tmp_path)
    original = daemon._write_daemon_json

    def spy(*, ready: bool = True) -> None:
        original(ready=ready)
        seen.append(json.loads((tmp_path / "daemon.json").read_text())["ready"])

    daemon._write_daemon_json = spy  # type: ignore[method-assign]
    await daemon.start()
    try:
        assert seen == [False, True]
    finally:
        await daemon.stop()


def test_the_lock_file_names_the_holder(tmp_path: Path) -> None:
    import os

    held = acquire_home_lock(tmp_path)
    try:
        assert (tmp_path / "daemon.lock").read_text().strip() == str(os.getpid())
    finally:
        release_home_lock(held)


async def test_ensure_daemon_waits_for_a_self_restarting_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows update helper owns daemon.json while it reinstalls: the CLI
    must neither kill it (pid alive, port closed) nor spawn beside it."""
    helper = daemon_client.DaemonInfo(port=5, pid=7777, token="t", restarting=True)
    back = daemon_client.DaemonInfo(port=5, pid=8888, token="t")
    adverts = iter([helper, helper, helper, back])

    async def health(_port: int) -> bool:
        return True

    monkeypatch.setattr(daemon_client, "read_daemon_json", lambda _home: next(adverts))
    monkeypatch.setattr(daemon_client, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_client, "health_ok", health)
    monkeypatch.setattr(daemon_client, "POLL_INTERVAL_SEC", 0)

    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("must not stop the helper or spawn a daemon")

    monkeypatch.setattr(daemon_client, "_stop_unresponsive", never)
    monkeypatch.setattr(daemon_client, "_spawn_daemon", never)
    assert await daemon_client.ensure_daemon(tmp_path) is back


async def test_a_dead_helper_leaves_the_home_to_a_new_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = daemon_client.DaemonInfo(port=5, pid=7777, token="t", restarting=True)
    alive = iter([True, False])

    async def health(_port: int) -> bool:
        return False

    monkeypatch.setattr(daemon_client, "read_daemon_json", lambda _home: helper)
    monkeypatch.setattr(daemon_client, "pid_alive", lambda _pid: next(alive, False))
    monkeypatch.setattr(daemon_client, "health_ok", health)
    monkeypatch.setattr(daemon_client, "POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(daemon_client, "RESTART_GRACE_SEC", 0)

    class Spawned(Exception):
        pass

    def spawn(*_a: object, **_k: object) -> None:
        raise Spawned

    monkeypatch.setattr(daemon_client, "_spawn_daemon", spawn)
    with pytest.raises(Spawned):
        await daemon_client.ensure_daemon(tmp_path)


def test_daemon_info_reads_the_markers() -> None:
    info = daemon_client.DaemonInfo.from_dict(
        {"port": 1, "pid": 2, "token": "t", "ready": False, "restarting": True}
    )
    assert info.ready is False and info.restarting is True
    old = daemon_client.DaemonInfo.from_dict({"port": 1, "pid": 2, "token": "t"})
    assert old.ready is True and old.restarting is False


async def test_daemon_json_names_the_app_that_spawned_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from snowpea_core.server.app_server import Daemon

    monkeypatch.setenv("SNOWPEA_SPAWNED_BY", "ide")
    daemon = Daemon(port=0, home=tmp_path)
    await daemon.start()
    try:
        advert = json.loads((tmp_path / "daemon.json").read_text())
        assert advert["spawnedBy"] == "ide" and advert["ready"] is True
    finally:
        await daemon.stop()


async def test_the_gap_after_the_helper_exits_is_waited_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper ends once it starts the new core; until that core writes its
    advert, the restarting advert names a dead pid. That is not a failure."""
    helper = daemon_client.DaemonInfo(port=5, pid=7777, token="t", restarting=True)
    back = daemon_client.DaemonInfo(port=5, pid=8888, token="t")
    adverts = iter([helper, helper, helper, helper, back])

    async def health(_port: int) -> bool:
        return False

    async def back_healthy(port: int) -> bool:
        return True

    reads = {"n": 0}

    def read(_home: object) -> daemon_client.DaemonInfo:
        reads["n"] += 1
        info = next(adverts, back)
        if info is back:
            monkeypatch.setattr(daemon_client, "health_ok", back_healthy)
        return info

    monkeypatch.setattr(daemon_client, "read_daemon_json", read)
    monkeypatch.setattr(daemon_client, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(daemon_client, "health_ok", health)
    monkeypatch.setattr(daemon_client, "POLL_INTERVAL_SEC", 0)

    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("must not spawn while the new core is coming up")

    monkeypatch.setattr(daemon_client, "_spawn_daemon", never)
    assert await daemon_client.ensure_daemon(tmp_path) is back


async def test_a_long_running_update_is_reported_not_raced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the wait, a live helper still installing means "try later": a core
    spawned now would lock the files uv is replacing."""
    from datetime import UTC, datetime

    helper = daemon_client.DaemonInfo(
        port=5, pid=7777, token="t", restarting=True,
        restartingSince=datetime.now(UTC).isoformat(),
    )

    async def health(_port: int) -> bool:
        return False

    monkeypatch.setattr(daemon_client, "read_daemon_json", lambda _home: helper)
    monkeypatch.setattr(daemon_client, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(daemon_client, "health_ok", health)
    monkeypatch.setattr(daemon_client, "POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(daemon_client, "RESTART_WAIT_SEC", 0.05)

    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("must not spawn during an update")

    monkeypatch.setattr(daemon_client, "_spawn_daemon", never)
    monkeypatch.setattr(daemon_client, "_stop_unresponsive", never)
    with pytest.raises(daemon_client.DaemonError, match="still installing"):
        await daemon_client.ensure_daemon(tmp_path)


async def test_a_boot_time_update_hand_off_is_not_overwritten(tmp_path: Path) -> None:
    import json

    from snowpea_core.server.app_server import Daemon

    daemon = Daemon(port=0, home=tmp_path)
    original = daemon._write_daemon_json

    def hand_off_after_first_write(*, ready: bool = True) -> None:
        original(ready=ready)
        if not ready:
            advert = tmp_path / "daemon.json"
            payload = json.loads(advert.read_text()) | {"pid": 1, "restarting": True}
            advert.write_text(json.dumps(payload))

    daemon._write_daemon_json = hand_off_after_first_write  # type: ignore[method-assign]
    await daemon.start()
    try:
        assert json.loads((tmp_path / "daemon.json").read_text())["restarting"] is True
    finally:
        await daemon.stop()


def test_core_version_names_the_protocol() -> None:
    """An app can skip an installed core of another protocol major unspawned."""
    from snowpea_core import __version__
    from snowpea_core.server.protocol import PROTOCOL_VERSION

    done = subprocess.run(
        [sys.executable, "-m", "snowpea_core", "--version"], capture_output=True, text=True
    )
    assert done.stdout.strip() == f"snowpea-core {__version__} (protocol {PROTOCOL_VERSION})"
