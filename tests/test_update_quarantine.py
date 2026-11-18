"""macOS: the engine ``uv tool install`` wrote is released from Gatekeeper's quarantine."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from snowpea_core import update as updates
from snowpea_core.config.paths import Paths


@pytest.fixture
def layout(tmp_path, monkeypatch):
    tools = tmp_path / "uvtools"
    bindir = tmp_path / "bin"
    tool = tools / updates.PACKAGE
    tool.mkdir(parents=True)
    bindir.mkdir()
    (bindir / "snowpea").write_text("x")
    (bindir / "other").write_text("x")
    monkeypatch.setenv("UV_TOOL_DIR", str(tools))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bindir))
    calls: list[list[str]] = []

    def fake_run(argv, **_kw):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    paths = Paths.create(tmp_path / "home")
    return SimpleNamespace(paths=paths, tool=tool, bindir=bindir, calls=calls)


def test_darwin_clears_exact_tool_dir_and_entry_point(layout):
    updates.clear_quarantine(layout.paths, "darwin")
    assert layout.calls == [
        ["xattr", "-dr", "com.apple.quarantine", str(layout.tool)],
        ["xattr", "-dr", "com.apple.quarantine", str(layout.bindir / "snowpea")],
    ]
    assert "com.apple.quarantine" in layout.paths.update_log.read_text()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_platforms_do_not_call_xattr(layout, platform):
    updates.clear_quarantine(layout.paths, platform)
    assert layout.calls == []


def test_missing_paths_are_skipped(layout, monkeypatch, tmp_path):
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path / "nobin"))
    updates.clear_quarantine(layout.paths, "darwin")
    assert layout.calls == []


def test_xattr_failure_is_swallowed(layout, monkeypatch):
    def boom(argv, **_kw):
        raise OSError("no xattr")

    def slow(argv, **_kw):
        raise subprocess.TimeoutExpired("xattr", 1)

    for failing in (boom, slow):
        monkeypatch.setattr(updates.subprocess, "run", failing)
        updates.clear_quarantine(layout.paths, "darwin")


async def test_successful_update_on_darwin_still_succeeds_when_xattr_fails(layout, monkeypatch):
    monkeypatch.setattr(updates.sys, "platform", "darwin")

    def boom(*_a, **_k):
        raise OSError("x")

    monkeypatch.setattr(updates.subprocess, "run", boom)
    monkeypatch.setattr(updates, "notify_progress", AsyncMock())
    monkeypatch.setattr(updates, "installed_cli_version", lambda: "0.2.93")
    core = SimpleNamespace(paths=layout.paths, restart_required=False)
    await updates.watch_update(core, SimpleNamespace(poll=lambda: 0), "0.2.93")
    assert core.restart_required is True
    updates.notify_progress.assert_awaited_with(core, "done", "updated to v0.2.93")
