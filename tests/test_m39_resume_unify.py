"""M39: unified external resume (transport pty; tmux bridge removed)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _external_adapter import FAKE_CLAUDE, FakeClaudeAdapter, encoded
from _support import RpcClient, env_vars, make_daemon, pinned_cli

from snowpea_core.external import claude, proc
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION
from snowpea_core.terminal import profiles

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")

SETTINGS = {"jarvis": {"external": {"clis": {"claude": {**pinned_cli(FAKE_CLAUDE)}}}},
            "terminals": {"keepShell": False}}

TUI = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
sys.stdout.write("ready\n")
sys.stdout.flush()
for line in sys.stdin:
    sys.stdout.write("got:" + line.strip() + "\n")
    sys.stdout.flush()
'''


@pytest_asyncio.fixture
async def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    config = tmp_path / "claude-config"
    argv_log = tmp_path / "argv.log"
    tui = tmp_path / "bin" / "claude-tui"
    tui.parent.mkdir()
    tui.write_text(TUI, encoding="utf-8")
    tui.chmod(0o755)
    async def state(_core: Any, cli: str) -> profiles.CliState:
        return profiles.CliState(
            str(tui), claude.NATIVE_MODES, [], claude.ClaudeAdapter.forbidden_args,
            claude.ClaudeAdapter.forbidden_pairs,
        )

    monkeypatch.setattr(profiles, "cli_state", state)
    with env_vars(CLAUDE_CONFIG_DIR=str(config), FAKE_ARGV_LOG=str(argv_log), SHELL="/bin/sh",
                  FAKE_CLI_LOG=str(tmp_path / "fake.log")):
        daemon = await make_daemon(tmp_path / "home", SETTINGS)
        registry = daemon.core.external.registry
        for cli in registry.ids():
            registry.unregister(cli)
        registry.register(FakeClaudeAdapter())
        project = tmp_path / "proj"
        project.mkdir()
        try:
            yield {"daemon": daemon, "config": config, "argv": argv_log, "project": project}
        finally:
            if getattr(daemon.core, "terminals", None) is not None:
                await daemon.core.terminals.close_all()
            await daemon.stop()


async def _wait(predicate: Any, timeout: float = 10.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def _client(
    http: aiohttp.ClientSession, daemon: Any, *, version: str = PROTOCOL_VERSION,
) -> RpcClient:
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    client = RpcClient(ws)
    client.start()
    await client.ok("system.hello", {
        "token": daemon.token, "clientVersion": "test", "protocolVersion": version,
        "clientKind": "ide",
    })
    return client


def _transcript(config: Path, project: Path, session_id: str) -> None:
    folder = config / "projects" / encoded(str(project))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{session_id}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(project), "uuid": "u",
                    "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )


def test_protocol_pins_and_capabilities() -> None:
    assert PROTOCOL_VERSION == "1.75.0"
    assert "externalResumeUnified" in CAPABILITIES
    assert "externalTmux" not in CAPABILITIES


async def test_transport_tmux_refused(world: dict[str, Any], http: aiohttp.ClientSession) -> None:
    daemon, project = world["daemon"], world["project"]
    client = await _client(http, daemon)
    try:
        await client.ok("workspace.add", {"path": str(project)})
        err = await client.call("external.create", {"cli": "claude", "project": str(project),
                                                    "transport": "tmux"})
        assert err["error"]["data"]["code"] == "external_unavailable"
    finally:
        await client.stop()


async def test_terminal_snapshot_refused(
    world: dict[str, Any], http: aiohttp.ClientSession,
) -> None:
    daemon, project, config = world["daemon"], world["project"], world["config"]
    _transcript(config, project.resolve(), "snap-1")
    client = await _client(http, daemon)
    try:
        await client.ok("workspace.add", {"path": str(project)})
        err = await client.call(
            "external.terminalSnapshot", {"ref": {"cli": "claude", "id": "snap-1"}},
        )
        assert err["error"]["data"]["code"] == "external_unavailable"
    finally:
        await client.stop()


async def test_list_rows_never_tmux(world: dict[str, Any], http: aiohttp.ClientSession) -> None:
    daemon, project, config = world["daemon"], world["project"], world["config"]
    _transcript(config, project.resolve(), "list-1")
    (config / "sessions").mkdir(parents=True, exist_ok=True)
    (config / "sessions" / "live.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": "list-1",
        "procStart": proc.process_start_time(os.getpid()), "status": "idle"}))
    client = await _client(http, daemon)
    try:
        await client.ok("workspace.add", {"path": str(project)})
        listed = await client.ok("external.list", {"cwd": str(project)})
        row = listed["sessions"][0]
        assert row["host"] in ("none", "terminal", "snowpea")
        assert row["controllable"] is None
        assert row["tmux"] is None
        assert row.get("terminal") is None or row["terminal"]["kind"] == "pty"
    finally:
        await client.stop()


async def test_default_transport_new_vs_old_client(
    world: dict[str, Any], http: aiohttp.ClientSession,
) -> None:
    daemon, project, config = world["daemon"], world["project"], world["config"]
    _transcript(config, project.resolve(), "def-a")
    _transcript(config, project.resolve(), "def-b")
    new_client = await _client(http, daemon, version=PROTOCOL_VERSION)
    old_client = await _client(http, daemon, version="1.70.0")
    try:
        await new_client.ok("workspace.add", {"path": str(project)})
        opened_new = await new_client.ok("external.open", {"ref": {"cli": "claude", "id": "def-a"}})
        assert opened_new["session"]["terminal"]["kind"] == "pty"
        opened_old = await old_client.ok("external.open", {"ref": {"cli": "claude", "id": "def-b"}})
        assert opened_old["session"]["host"] == "snowpea"
        assert opened_old["session"]["terminal"] is None
    finally:
        await new_client.stop()
        await old_client.stop()


async def test_live_elsewhere_still_resumes_pty(
    world: dict[str, Any], http: aiohttp.ClientSession,
) -> None:
    daemon, project, config = world["daemon"], world["project"], world["config"]
    _transcript(config, project.resolve(), "live-c")
    (config / "sessions").mkdir(parents=True, exist_ok=True)
    (config / "sessions" / "c.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": "live-c",
        "procStart": proc.process_start_time(os.getpid()), "status": "idle"}))
    client = await _client(http, daemon)
    try:
        await client.ok("workspace.add", {"path": str(project)})
        opened = await client.ok("external.open", {"ref": {"cli": "claude", "id": "live-c"},
                                                   "transport": "pty"})
        assert opened["session"]["terminal"]["kind"] == "pty"
        entry = await _wait(lambda: world["argv"].exists() and world["argv"].read_text().strip())
        argv = json.loads(entry.strip().splitlines()[0])
        assert argv["argv"] == ["--resume", "live-c"]
    finally:
        await client.stop()