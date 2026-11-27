"""macOS libproc session scan (fakes only; no Mac or real libproc required)."""

from __future__ import annotations

import ctypes
import logging
import os

import pytest

from snowpea_core import platform_procs
from snowpea_core.terminal import pty_unix


@pytest.fixture(autouse=True)
def _reset_libproc() -> None:
    platform_procs.reset_for_tests()
    yield
    platform_procs.reset_for_tests()


def test_macos_session_match_uses_injected_pids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pty_unix.sys, "platform", "darwin")

    pids = [1, 42, 99, 0, -1]

    def fake_listallpids(_buf: ctypes.c_void_p, size: int) -> int:
        if size == 0:
            return len(pids)
        arr = ctypes.cast(_buf, ctypes.POINTER(ctypes.c_int * len(pids))).contents
        for i, pid in enumerate(pids):
            arr[i] = pid
        return len(pids)

    platform_procs._proc_listallpids = fake_listallpids
    platform_procs._lib = ctypes.CDLL(None)

    def getsid(pid: int) -> int:
        return 7 if pid == 42 else 1

    monkeypatch.setattr(pty_unix.os, "getsid", getsid)
    assert pty_unix.session_has_processes(7) is True
    assert pty_unix.session_has_processes(99) is False


def test_macos_libproc_missing_falls_back_to_eof_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(pty_unix.sys, "platform", "darwin")
    platform_procs._lib = False
    caplog.set_level(logging.WARNING, logger="snowpea.platform_procs")
    assert platform_procs.list_all_pids() is None
    assert pty_unix.session_has_processes(123) is False
    assert "libproc unavailable" in caplog.text
    caplog.clear()
    assert pty_unix.session_has_processes(123) is False
    assert "libproc unavailable" not in caplog.text


@pytest.mark.skipif(pty_unix.sys.platform != "linux", reason="Linux /proc scan")
def test_linux_session_scan_unchanged() -> None:
    sid = os.getsid(os.getpid())
    assert pty_unix.session_has_processes(sid) is True
    assert pty_unix.session_has_processes(sid + 1_000_000) is False