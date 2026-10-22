"""The daemon's children get no console window on Windows (addendum 26)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from snowpea_core.util import no_window


def test_the_flag_is_added_unless_the_caller_decided_the_console() -> None:
    assert no_window.with_no_window(0) == no_window.CREATE_NO_WINDOW
    assert no_window.with_no_window(0x200) == 0x200 | no_window.CREATE_NO_WINDOW
    assert no_window.with_no_window(no_window.DETACHED_PROCESS) == no_window.DETACHED_PROCESS
    assert no_window.with_no_window(0x10) == 0x10


def test_only_windows_gets_the_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    before = subprocess.Popen.__init__
    assert no_window.hide_child_consoles(platform="linux") is False
    assert subprocess.Popen.__init__ is before


def test_every_popen_gets_create_no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def fake_init(self: Any, *args: Any, **kwargs: Any) -> None:
        seen.append(kwargs.get("creationflags"))

    monkeypatch.setattr(subprocess.Popen, "__init__", fake_init)
    assert no_window.hide_child_consoles(platform="win32") is True
    # Installing twice does not wrap twice.
    assert no_window.hide_child_consoles(platform="win32") is True
    subprocess.Popen(["x"])
    subprocess.Popen(["x"], creationflags=no_window.DETACHED_PROCESS)
    subprocess.Popen(["x"], creationflags=0x200)
    assert seen == [
        no_window.CREATE_NO_WINDOW,
        no_window.DETACHED_PROCESS,
        0x200 | no_window.CREATE_NO_WINDOW,
    ]
