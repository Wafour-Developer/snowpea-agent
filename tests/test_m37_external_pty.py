"""M37 T3 §2.8: external sessions as terminal sessions (``transport: "pty"``, externalPty).

The CLI in the terminal is a fake (a Python script written from this file's
literal) that records its argv, reports OSC 7501 and shows a prompt; session
storage and liveness are the test adapter's (``FakeClaudeAdapter``).  No real
CLI is started.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _external_adapter import FAKE_CLAUDE, FakeClaudeAdapter, encoded
from _support import RpcClient, env_vars, make_daemon, pinned_cli

from snowpea_core.external import claude, proc
from snowpea_core.server.protocol import CAPABILITIES, PENDING_CAPABILITIES, PROTOCOL_VERSION
from snowpea_core.terminal import profiles

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")

SETTINGS = {"jarvis": {"external": {"clis": {"claude": {**pinned_cli(FAKE_CLAUDE)}}}},
            "terminals": {"keepShell": False}}

TUI = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:],
                          "secret": any(k.startswith("SNOWPEA_") and k not in (
                              "SNOWPEA_TERMINAL_ID", "SNOWPEA_TERMINAL_TOKEN",
                              "SNOWPEA_TERMINAL_REPORT", "SNOWPEA_CORE_BIN")
                              for k in os.environ)}) + "\n")
def report(state):
    sys.stdout.write("\x1b]7501;state=" + state + "\x1b\\")
    sys.stdout.flush()
report("idle")
sys.stdout.write("tui> ")
sys.stdout.flush()
for line in sys.stdin:
    report("working")
    sys.stdout.write("got:" + line.strip() + "\r\ntui> ")
    sys.stdout.flush()
    report("idle")
'''


async def _wait(predicate: Any, timeout: float = 10.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


def _transcript(config: Path, project: Path, session_id: str, text: str) -> None:
    folder = config / "projects" / encoded(str(project))
    folder.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "user", "cwd": str(project), "uuid": f"{session_id}-u",
         "timestamp": "2026-11-24T00:00:00Z", "message": {"role": "user", "content": text}},
        {"type": "assistant", "cwd": str(project), "uuid": f"{session_id}-a",
         "timestamp": "2026-11-24T00:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
    ]
    (folder / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


@pytest_asyncio.fixture
async def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    config = tmp_path / "claude-config"
    argv_log = tmp_path / "argv.log"
    tui = tmp_path / "bin" / "claude-tui"
    tui.parent.mkdir()
    tui.write_text(TUI, encoding="utf-8")
    tui.chmod(0o755)

    async def state(_core: Any, cli: str) -> profiles.CliState:
        return profiles.CliState(str(tui), claude.NATIVE_MODES, [],
                                 claude.ClaudeAdapter.forbidden_args,
                                 claude.ClaudeAdapter.forbidden_pairs)

    monkeypatch.setattr(profiles, "cli_state", state)
    with env_vars(CLAUDE_CONFIG_DIR=str(config), FAKE_ARGV_LOG=str(argv_log), SHELL="/bin/sh",
                  FAKE_CLI_LOG=str(tmp_path / "fake.log"), FAKE_CLI_SCENARIO=None):
        daemon = await make_daemon(tmp_path / "home", SETTINGS)
        try:
            registry = daemon.core.external.registry
            for cli in registry.ids():
                registry.unregister(cli)
            registry.register(FakeClaudeAdapter())
            project = tmp_path / "proj"
            project.mkdir()
            yield {"daemon": daemon, "config": config, "argv": argv_log, "project": project,
                   "tmp": tmp_path}
        finally:
            if daemon.core is not None and getattr(daemon.core, "terminals", None) is not None:
                await daemon.core.terminals.close_all()
            await daemon.stop()


async def _client(http: aiohttp.ClientSession, daemon: Any, kind: str | None = "ide",
                  version: str = PROTOCOL_VERSION) -> RpcClient:
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    client = RpcClient(ws)
    client.start()
    hello: dict[str, Any] = {"token": daemon.token, "clientVersion": "test",
                             "protocolVersion": version}
    if kind is not None:
        hello["clientKind"] = kind
    await client.ok("system.hello", hello)
    return client


def _argvs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_capability_advertised() -> None:
    assert "externalPty" in CAPABILITIES and "externalPty" not in PENDING_CAPABILITIES


async def test_open_as_terminal_resume_reuse_and_end(world: dict[str, Any],
                                                     http: aiohttp.ClientSession) -> None:
    daemon, project = world["daemon"], world["project"]
    _transcript(world["config"], project.resolve(), "sess-b", "hello from b")
    ide = await _client(http, daemon)
    browser = await _client(http, daemon, "browser")
    try:
        await ide.ok("workspace.add", {"path": str(project)})
        ref = {"cli": "claude", "id": "sess-b"}
        refused = await browser.call("external.open", {"ref": ref, "transport": "pty"})
        assert refused["error"]["data"]["code"] == "jarvis_person_only"
        opened = await ide.ok("external.open", {"ref": ref, "transport": "pty"})
        row = opened["session"]
        assert row["host"] == "terminal" and opened["readOnly"]
        assert row["terminal"]["kind"] == "pty" and row["controllable"] is None
        tid = row["terminal"]["terminalId"]
        assert [e["kind"] for e in opened["events"]][:1] == ["message.user"]
        logged = await _wait(lambda: _argvs(world["argv"]))
        assert logged[0]["argv"] == ["--resume", "sess-b"] and not logged[0]["secret"]
        term = daemon.core.terminals.get(tid)
        assert term.spec.external == {"cli": "claude", "sessionId": "sess-b"}
        # The structured calls stay view only while the terminal runs it.
        sent = await ide.call("external.send", {"ref": ref, "text": "hi"})
        assert sent["error"]["data"]["code"] == "external_view_only"
        # Opening it again shows the same terminal; no second CLI.
        again = await ide.ok("external.open", {"ref": ref, "transport": "pty"})
        assert again["session"]["terminal"]["terminalId"] == tid
        assert len(daemon.core.terminals.terminals()) == 1
        listed = await ide.ok("external.list", {"cwd": str(project)})
        assert listed["sessions"][0]["terminal"]["terminalId"] == tid
        # Typing into it goes through the terminal.
        await ide.ok("terminal.send", {"terminalId": tid, "text": "ping"})
        await _wait(lambda: "got:ping" in daemon.core.terminals.read(tid)["text"])
        # Its terminal closes: the session is free again; pty resumes it anew.
        await ide.ok("terminal.close", {"terminalId": tid})
        await _wait(lambda: [p for p in ide.of_method("external.state")
                             if p["ref"] == ref and p["host"] == "none"])
        reopened = await ide.ok("external.open", {"ref": ref, "transport": "pty"})
        assert reopened["session"]["terminal"]["terminalId"] != tid
        await _wait(lambda: len(_argvs(world["argv"])) == 2)
    finally:
        await ide.stop()
        await browser.stop()


async def test_old_client_keeps_structured_and_live_elsewhere_stays_view_only(
    world: dict[str, Any], http: aiohttp.ClientSession
) -> None:
    daemon, project, config = world["daemon"], world["project"], world["config"]
    _transcript(config, project.resolve(), "sess-a", "hello from a")
    _transcript(config, project.resolve(), "sess-c", "hello from c")
    # sess-c runs in the person's own terminal (this test process stands in for it).
    (config / "sessions").mkdir(parents=True, exist_ok=True)
    (config / "sessions" / "c.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": "sess-c",
        "procStart": proc.process_start_time(os.getpid()), "status": "idle"}))
    old = await _client(http, daemon, version="1.70.0")
    try:
        await old.ok("workspace.add", {"path": str(project)})
        opened = await old.ok("external.open", {"ref": {"cli": "claude", "id": "sess-a"}})
        assert opened["session"]["host"] == "snowpea"
        assert opened["session"]["terminal"] is None
        assert not getattr(daemon.core, "terminals", None) or not daemon.core.terminals.terminals()
        await old.ok("external.close", {"ref": {"cli": "claude", "id": "sess-a"}})
        elsewhere = await old.ok("external.open", {"ref": {"cli": "claude", "id": "sess-c"},
                                                   "transport": "pty"})
        assert elsewhere["session"]["host"] == "terminal" and elsewhere["readOnly"]
        assert elsewhere["session"]["terminal"] is None
        assert not daemon.core.terminals.terminals()
    finally:
        await old.stop()


async def test_create_as_terminal(world: dict[str, Any], http: aiohttp.ClientSession) -> None:
    daemon, project = world["daemon"], world["project"]
    ide = await _client(http, daemon)
    try:
        await ide.ok("workspace.add", {"path": str(project)})
        refused = await ide.call("external.create", {
            "cli": "claude", "project": str(project), "transport": "pty",
            "options": ["bypassPermissions"]})
        assert refused["error"]["data"]["details"]["reason"] == "confirm_dangerous"
        created = await ide.ok("external.create", {
            "cli": "claude", "project": str(project), "transport": "pty", "options": ["plan"]})
        row = created["session"]
        assert row["host"] == "terminal" and row["terminal"]["kind"] == "pty"
        logged = await _wait(lambda: _argvs(world["argv"]))
        argv = logged[0]["argv"]
        assert argv[:1] == ["--permission-mode=plan"]
        assert argv[1:] == ["--session-id", row["ref"]["id"]]
        info = daemon.core.terminals.get(row["terminal"]["terminalId"]).info()
        assert info["external"] == {"cli": "claude", "sessionId": row["ref"]["id"]}
        assert info["options"] == ["plan"] and not info["dangerous"]
    finally:
        await ide.stop()
