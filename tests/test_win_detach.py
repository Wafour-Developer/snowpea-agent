"""Windows: a browser-spawned uv core moves its daemon out of uv's job (mocked on Linux).

uv's trampoline keeps the daemon in a kill-on-close job and the browser ends
the trampoline (its child, never the advert's pid), so the launcher re-spawns
the daemon detached, breaking away from the job; see
:mod:`snowpea_core.win_detach`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from snowpea_core import __main__ as entry
from snowpea_core import win_detach
from snowpea_core.win_detach import GUARD_ENV, HOME_LOCKED_EXIT

CORE = Path(__file__).resolve().parents[1] / "core"
BROWSER = {"SNOWPEA_SPAWNED_BY": "browser"}


# -- the decision ------------------------------------------------------------


def test_detaches_on_windows_unfrozen_browser_spawn() -> None:
    assert win_detach.should_detach(BROWSER, "win32", False)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_never_off_windows(platform: str) -> None:
    assert not win_detach.should_detach(BROWSER, platform, False)


def test_never_when_frozen() -> None:
    # The bundled (PyInstaller) core is the browser's own child, pid and all.
    assert not win_detach.should_detach(BROWSER, "win32", True)


@pytest.mark.parametrize("env", [{}, {"SNOWPEA_SPAWNED_BY": "ide"}, {"SNOWPEA_SPAWNED_BY": ""}])
def test_never_without_the_browser_marker(env: dict[str, str]) -> None:
    assert not win_detach.should_detach(env, "win32", False)


def test_the_detached_daemon_never_detaches_again() -> None:
    assert not win_detach.should_detach({**BROWSER, GUARD_ENV: "1"}, "win32", False)


def test_reads_the_real_process_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("SNOWPEA_SPAWNED_BY", "browser")
    monkeypatch.delenv(GUARD_ENV, raising=False)
    assert win_detach.should_detach()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not win_detach.should_detach()


# -- the spawn ---------------------------------------------------------------


def test_creation_flags_break_away_detached_without_a_window() -> None:
    assert win_detach.DETACH_FLAGS == 0x01000000 | 0x00000008 | 0x08000000 | 0x00000200
    for name in (
        "CREATE_BREAKAWAY_FROM_JOB",
        "DETACHED_PROCESS",
        "CREATE_NO_WINDOW",
        "CREATE_NEW_PROCESS_GROUP",
    ):
        if hasattr(subprocess, name):  # Windows: the module's values are subprocess's
            assert getattr(win_detach, name) == getattr(subprocess, name)


def test_spawn_runs_the_same_arguments_with_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kwargs: Any) -> str:
        seen["cmd"] = cmd
        seen.update(kwargs)
        return "child"

    monkeypatch.setenv("SNOWPEA_SPAWNED_BY", "browser")
    monkeypatch.setattr(win_detach.subprocess, "Popen", fake_popen)
    argv = ["--home", "C:\\h", "--port", "0"]
    assert win_detach.spawn_detached(argv) == "child"
    assert seen["cmd"] == [sys.executable, "-m", "snowpea_core", *argv]
    assert seen["creationflags"] == win_detach.DETACH_FLAGS
    assert seen["env"][GUARD_ENV] == "1"
    assert seen["env"]["SNOWPEA_SPAWNED_BY"] == "browser"
    assert seen["stdin"] is subprocess.DEVNULL
    # stdout/stderr are inherited: the browser's launch log.
    assert "stdout" not in seen and "stderr" not in seen
    assert GUARD_ENV not in os.environ


def test_breakaway_refused_falls_back_in_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refused(*_a: Any, **_k: Any) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(win_detach.subprocess, "Popen", refused)
    assert win_detach.spawn_detached(["--port", "0"]) is None
    assert win_detach.run_detached(["--port", "0"], None) is None
    assert "running it in this process" in capsys.readouterr().err


def test_main_runs_in_process_when_breakaway_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[Any] = []
    monkeypatch.setattr(win_detach, "should_detach", lambda: True)
    monkeypatch.setattr(win_detach, "run_detached", lambda argv, home: ran.append(argv))
    monkeypatch.setattr(entry, "extend_macos_path", lambda: None)

    async def fake_daemon(**kwargs: Any) -> None:
        ran.append("daemon")

    from snowpea_core.server import app_server

    monkeypatch.setattr(app_server, "run_daemon", fake_daemon)
    monkeypatch.delenv("SNOWPEA_HOME", raising=False)
    assert entry.main(["--port", "0"]) == 0
    assert ran == [["--port", "0"], "daemon"]


def test_main_exits_with_the_launchers_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win_detach, "should_detach", lambda: True)
    monkeypatch.setattr(win_detach, "run_detached", lambda argv, home: HOME_LOCKED_EXIT)
    monkeypatch.setattr(entry, "extend_macos_path", lambda: None)
    assert entry.main(["--port", "0"]) == HOME_LOCKED_EXIT


# -- waiting for the detached daemon ------------------------------------------


class FakeChild:
    def __init__(self, codes: list[int | None]) -> None:
        self.codes = codes

    def poll(self) -> int | None:
        return self.codes.pop(0) if len(self.codes) > 1 else self.codes[0]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _advert(path: Path, **fields: Any) -> None:
    path.write_text(json.dumps({"pid": 4242, "port": 51234, "ready": True, **fields}), "utf-8")


def _wait(child: FakeChild, path: Path, since: float, clock: Clock, healthy: bool = True) -> int:
    return win_detach.wait_for_daemon(
        child,  # type: ignore[arg-type]
        path,
        since,
        timeout=10.0,
        is_healthy=lambda port: healthy,
        sleep=clock.sleep,
        clock=clock,
    )


def test_waits_for_the_healthy_advert_then_lingers_and_exits_73(tmp_path: Path) -> None:
    path = tmp_path / "daemon.json"
    clock = Clock()
    _advert(path)
    assert _wait(FakeChild([None]), path, path.stat().st_mtime, clock) == HOME_LOCKED_EXIT
    assert clock.sleeps == [win_detach.LINGER_S]


@pytest.mark.parametrize(
    "fields",
    [{"ready": False}, {"restarting": True}, {"port": 0}, {"pid": os.getpid()}],
)
def test_not_ready_adverts_are_waited_out(tmp_path: Path, fields: dict[str, Any]) -> None:
    path = tmp_path / "daemon.json"
    clock = Clock()
    _advert(path, **fields)
    assert _wait(FakeChild([None]), path, path.stat().st_mtime, clock) == HOME_LOCKED_EXIT
    assert win_detach.LINGER_S not in clock.sleeps
    assert clock.now >= 10.0


def test_a_stale_advert_is_not_the_new_daemon(tmp_path: Path) -> None:
    path = tmp_path / "daemon.json"
    clock = Clock()
    _advert(path)
    assert _wait(FakeChild([None]), path, path.stat().st_mtime + 60, clock) == HOME_LOCKED_EXIT
    assert win_detach.LINGER_S not in clock.sleeps


def test_unhealthy_advert_is_waited_out(tmp_path: Path) -> None:
    path = tmp_path / "daemon.json"
    clock = Clock()
    _advert(path)
    _wait(FakeChild([None]), path, path.stat().st_mtime, clock, healthy=False)
    assert win_detach.LINGER_S not in clock.sleeps


@pytest.mark.parametrize("code", [HOME_LOCKED_EXIT, 1])
def test_a_daemon_that_dies_passes_its_code_on(tmp_path: Path, code: int) -> None:
    clock = Clock()
    assert _wait(FakeChild([None, code]), tmp_path / "daemon.json", 0.0, clock) == code


# -- --version stays light -----------------------------------------------------


@pytest.mark.parametrize(
    "entry_code",
    [
        "import sys; from snowpea_core.__main__ import main; sys.exit(main())",
        "import runpy; runpy.run_module('snowpea_core', run_name='__main__')",
    ],
)
def test_version_does_not_import_the_daemon(entry_code: str) -> None:
    probe = (
        "import atexit, sys\n"
        "def report():\n"
        "    heavy = ('snowpea_core.server.app_server', 'snowpea_core.server.protocol',\n"
        "             'pydantic')\n"
        "    loaded = ','.join(m for m in heavy if m in sys.modules)\n"
        "    sys.stderr.write('LOADED=' + (loaded or 'clean'))\n"
        "atexit.register(report)\n"
        "sys.argv = ['snowpea-core', '--version']\n"
        f"{entry_code}\n"
    )
    env = {**os.environ, "PYTHONPATH": str(CORE)}
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    from snowpea_core.server.protocol import PROTOCOL_VERSION

    assert done.stdout.strip().endswith(f"(protocol {PROTOCOL_VERSION})")
    assert "LOADED=clean" in done.stderr
