"""Process liveness probes must be non-destructive on every platform."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from snowpea_core.cli import daemon_client


class FakeKernel32:
    def __init__(self) -> None:
        self.handle = 2**40 + 123
        self.wait_result = daemon_client.WAIT_TIMEOUT
        self.closed: list[int] = []
        self.open_calls: list[tuple[int, bool, int]] = []
        self.OpenProcess = Mock(side_effect=self._open_process)
        self.WaitForSingleObject = Mock(side_effect=self._wait_for_single_object)
        self.CloseHandle = Mock(side_effect=self._close_handle)

    def _open_process(self, access: int, inherit: bool, pid: int) -> int:
        self.open_calls.append((access, inherit, pid))
        return self.handle

    def _wait_for_single_object(self, handle: int, timeout_ms: int) -> int:
        assert handle == self.handle
        assert timeout_ms == 0
        return self.wait_result

    def _close_handle(self, handle: int) -> bool:
        self.closed.append(handle)
        return True

    def assert_winapi_prototypes(self) -> None:
        assert self.OpenProcess.argtypes == [
            daemon_client.ctypes.wintypes.DWORD,
            daemon_client.ctypes.wintypes.BOOL,
            daemon_client.ctypes.wintypes.DWORD,
        ]
        assert self.OpenProcess.restype is daemon_client.ctypes.wintypes.HANDLE
        assert self.WaitForSingleObject.argtypes == [
            daemon_client.ctypes.wintypes.HANDLE,
            daemon_client.ctypes.wintypes.DWORD,
        ]
        assert self.WaitForSingleObject.restype is daemon_client.ctypes.wintypes.DWORD
        assert self.CloseHandle.argtypes == [daemon_client.ctypes.wintypes.HANDLE]
        assert self.CloseHandle.restype is daemon_client.ctypes.wintypes.BOOL

@pytest.fixture
def windows_pid_alive(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeKernel32, list[str]]:
    kernel32 = FakeKernel32()
    calls: list[str] = []

    def windll(name: str, *, use_last_error: bool) -> FakeKernel32:
        assert name == "kernel32"
        assert use_last_error is True
        calls.append(name)
        return kernel32

    monkeypatch.setattr(daemon_client.sys, "platform", "win32")
    monkeypatch.setattr(daemon_client.ctypes, "WinDLL", windll, raising=False)
    monkeypatch.setattr(daemon_client.ctypes, "get_last_error", lambda: 0, raising=False)

    def forbidden_kill(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Windows pid_alive must not call os.kill(pid, 0)")

    monkeypatch.setattr(daemon_client.os, "kill", forbidden_kill)
    return kernel32, calls


def test_pid_alive_windows_active_uses_query_handle(
    windows_pid_alive: tuple[FakeKernel32, list[str]],
) -> None:
    kernel32, calls = windows_pid_alive

    assert daemon_client.pid_alive(4242) is True

    assert calls == ["kernel32"]
    assert kernel32.handle > 2**32
    assert kernel32.open_calls == [(daemon_client.SYNCHRONIZE, False, 4242)]
    assert kernel32.closed == [kernel32.handle]
    kernel32.assert_winapi_prototypes()


def test_pid_alive_windows_exited_process_is_not_alive(
    windows_pid_alive: tuple[FakeKernel32, list[str]],
) -> None:
    kernel32, _calls = windows_pid_alive
    kernel32.wait_result = daemon_client.WAIT_OBJECT_0

    assert daemon_client.pid_alive(4242) is False
    assert kernel32.closed == [kernel32.handle]


def test_pid_alive_windows_still_active_exit_code_is_not_treated_as_alive(
    windows_pid_alive: tuple[FakeKernel32, list[str]],
) -> None:
    kernel32, _calls = windows_pid_alive
    kernel32.wait_result = daemon_client.STILL_ACTIVE

    assert daemon_client.pid_alive(4242) is False
    assert kernel32.closed == [kernel32.handle]


def test_pid_alive_windows_missing_process_is_not_alive(
    windows_pid_alive: tuple[FakeKernel32, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel32, _calls = windows_pid_alive
    kernel32.handle = 0
    monkeypatch.setattr(daemon_client.ctypes, "get_last_error", lambda: 87, raising=False)

    assert daemon_client.pid_alive(4242) is False
    assert kernel32.closed == []


def test_pid_alive_windows_access_denied_is_conservatively_alive(
    windows_pid_alive: tuple[FakeKernel32, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel32, _calls = windows_pid_alive
    kernel32.handle = 0
    monkeypatch.setattr(
        daemon_client.ctypes,
        "get_last_error",
        lambda: daemon_client.ERROR_ACCESS_DENIED,
        raising=False,
    )

    assert daemon_client.pid_alive(4242) is True
    assert kernel32.closed == []


def test_pid_alive_windows_query_failure_closes_handle_and_returns_false(
    windows_pid_alive: tuple[FakeKernel32, list[str]],
) -> None:
    kernel32, _calls = windows_pid_alive
    kernel32.wait_result = 0xFFFFFFFF

    assert daemon_client.pid_alive(4242) is False
    assert kernel32.closed == [kernel32.handle]


def test_pid_alive_rejects_non_positive_pid_without_platform_calls(
    windows_pid_alive: tuple[FakeKernel32, list[str]],
) -> None:
    kernel32, calls = windows_pid_alive

    assert daemon_client.pid_alive(0) is False
    assert daemon_client.pid_alive(-1) is False
    assert calls == []
    assert kernel32.open_calls == []


def test_pid_alive_posix_uses_kill_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_client.sys, "platform", "linux")
    monkeypatch.setattr(daemon_client.os, "kill", lambda pid, sig: calls.append((pid, sig)))

    assert daemon_client.pid_alive(1234) is True
    assert calls == [(1234, 0)]


def test_pid_alive_posix_permission_error_is_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(_pid: int, _sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(daemon_client.sys, "platform", "linux")
    monkeypatch.setattr(daemon_client.os, "kill", deny)

    assert daemon_client.pid_alive(1234) is True


def test_pid_alive_current_child_survives_repeated_probes() -> None:
    if sys.platform == "win32":
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    else:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        for _ in range(5):
            assert daemon_client.pid_alive(process.pid) is True
            assert process.poll() is None
            time.sleep(0.05)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                process.kill()
            else:
                os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
