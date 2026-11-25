"""M19 WP5: the ``external.*`` RPCs on a real daemon."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _external_adapter import FAKE_CLAUDE, FakeClaudeAdapter
from _support import RpcClient, connect, env_vars, make_daemon, pinned_cli

from snowpea_core.server.app_server import Daemon
from snowpea_core.server.external_handlers import ensure_manager
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")

SETTINGS = {"jarvis": {"external": {"clis": {"claude": {**pinned_cli(FAKE_CLAUDE)}}}}}


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with env_vars(FAKE_CLI_SCENARIO=None, FAKE_CLI_LOG=str(tmp_path / "fake.log")):
        started = await make_daemon(tmp_path / "home", SETTINGS)
        try:
            yield started
        finally:
            await started.stop()


async def _client(http: aiohttp.ClientSession, daemon: Daemon, kind: str | None) -> RpcClient:
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    client = RpcClient(ws)
    client.start()
    hello: dict[str, Any] = {"token": daemon.token, "clientVersion": "test",
                             "protocolVersion": PROTOCOL_VERSION}
    if kind is not None:
        hello["clientKind"] = kind
    result = await client.ok("system.hello", hello)
    assert "externalSessions" in result["capabilities"]
    return client


def _code(frame: dict[str, Any]) -> str:
    return str(frame["error"]["data"]["code"])


async def _wait(predicate: Any, timeout: float = 10.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def test_with_no_adapters(daemon: Daemon, http: aiohttp.ClientSession,
                                tmp_path: Path) -> None:
    assert "externalSessions" in CAPABILITIES
    assert daemon.core is not None
    registry = ensure_manager(daemon.core).registry
    for cli in registry.ids():  # the shipped adapters would find CLIs installed here
        registry.unregister(cli)
    client = await connect(http, daemon)
    try:
        clis = (await client.ok("external.clis"))["clis"]
        assert [c["id"] for c in clis] == ["claude", "codex", "agy", "cursor"]
        assert not any(c["installed"] for c in clis)
        ref = {"cli": "claude", "id": "abc"}
        for method, params in (
            ("external.list", {"allProjects": True}),
            ("external.list", {"cwd": str(tmp_path)}),
            ("external.open", {"ref": ref}),
            ("external.read", {"ref": ref}),
            ("external.send", {"ref": ref, "text": "hi"}),
            ("external.interrupt", {"ref": ref}),
            ("external.setMode", {"ref": ref, "mode": "readonly"}),
            ("external.create", {"cli": "codex", "project": str(tmp_path)}),
            ("external.close", {"ref": ref}),
        ):
            frame = await client.call(method, params)
            assert _code(frame) == "external_unavailable", (method, frame)
        bad = await client.call("external.open", {"ref": {"cli": "gemini", "id": "x"}})
        assert _code(bad) == "invalid_params"
    finally:
        await client.stop()


async def test_end_to_end_with_an_adapter(daemon: Daemon, http: aiohttp.ClientSession,
                                          tmp_path: Path) -> None:
    assert daemon.core is not None and daemon.core.external is not None
    registry = daemon.core.external.registry
    for cli in registry.ids():  # never the real CLIs installed here (models are listed)
        registry.unregister(cli)
    registry.register(FakeClaudeAdapter())
    project = tmp_path / "proj"
    project.mkdir()
    ide = await _client(http, daemon, "ide")
    other = await _client(http, daemon, "tui")
    agent = await _client(http, daemon, "connector")  # an agent connector: not a person
    try:
        await ide.ok("workspace.add", {"path": str(project)})
        row = {c["id"]: c for c in (await ide.ok("external.clis"))["clis"]}["claude"]
        assert row["installed"] and row["version"].startswith("2.1.325")
        # unrestricted needs a person.
        refused = await agent.call("external.create", {"cli": "claude", "project": str(project),
                                                       "mode": "unrestricted"})
        assert _code(refused) == "jarvis_person_only"
        (tmp_path / "elsewhere").mkdir()
        outside = await ide.call("external.create", {"cli": "claude",
                                                     "project": str(tmp_path / "elsewhere")})
        assert _code(outside) == "outside_root"
        # snowpea's own home is never a project (security review 7, H-2).
        home = await ide.call("external.create", {"cli": "claude",
                                                  "project": str(daemon.paths.home)})
        assert _code(home) == "invalid_params"
        with env_vars(FAKE_CLI_SCENARIO="approval"):
            session = (await ide.ok("external.create",
                                    {"cli": "claude", "project": str(project)}))["session"]
            ref = session["ref"]
            assert session["host"] == "snowpea" and session["approvals"] == "bridged"
            state = await other.wait_notification("external.state")
            assert state["ref"] == ref and state["host"] == "snowpea"
            turn = (await ide.ok("external.send", {"ref": ref, "text": "write"}))["turnId"]
        # The CLI's prompt is a shared snowpea approval any surface may answer.
        pending = await other.wait_notification("approval.pending")
        request = pending["request"]
        assert request["external"] == ref and request["sessionId"] == f"ext:claude:{ref['id']}"
        await other.ok("approval.respond", {"requestId": request["requestId"],
                                            "decision": "allow"})
        events = await _wait(lambda: [p for p in ide.of_method("external.event")
                                      if p["kind"] == "turn.done" and p["turnId"] == turn])
        assert events[0]["payload"]["reason"] == "complete" and events[0]["ref"] == ref
        assert (project / "note.txt").read_text() == "hi\n"
        assert not other.of_method("external.event")  # never opened it
        page = await other.ok("external.open", {"ref": ref, "limit": 5})
        assert len(page["events"]) == 5 and page["hasMore"] and not page["readOnly"]
        older = await other.ok("external.read", {"ref": ref, "beforeSeq":
                                                 page["events"][0]["seq"]})
        assert older["events"][0]["seq"] == 1
        listed = await ide.ok("external.list", {"cwd": str(project)})
        assert [s["ref"] for s in listed["sessions"]] == [ref]
        mode = await agent.call("external.setMode", {"ref": ref, "mode": "unrestricted"})
        # A connector changes nothing at all (security review 5, M1; before that the
        # session did not exist for it, security review L6).
        assert _code(mode) == "jarvis_person_only"
        switched = await ide.ok("external.setMode", {"ref": ref, "mode": "unrestricted"})
        assert switched["session"]["controlled"] is False
        ledger = Path(daemon.paths.home) / "jarvis" / "ledger.jsonl"
        assert json.loads(ledger.read_text().splitlines()[-1])["decision"] == "setMode"
        await ide.ok("external.interrupt", {"ref": ref})  # no turn running: fine
        await ide.ok("external.close", {"ref": ref})
        closed = await _wait(lambda: [p for p in other.of_method("external.state")
                                      if p.get("previousHost") == "snowpea"])
        assert closed[-1]["host"] == "none"
        assert not daemon.core.external.supervisor.live()
    finally:
        for client in (ide, other, agent):
            await client.stop()


async def test_native_modes_over_rpc(daemon: Daemon, http: aiohttp.ClientSession,
                                     tmp_path: Path) -> None:
    """1.56.0: nativeModes on external.clis, nativeMode on create/setMode and the row.

    The test adapter has no table of its own: its native modes are the three
    buckets (modes.GENERIC), so the wire works the same for any adapter.
    """
    assert "externalNativeModes" in CAPABILITIES
    assert daemon.core is not None and daemon.core.external is not None
    registry = daemon.core.external.registry
    for cli in registry.ids():
        registry.unregister(cli)
    registry.register(FakeClaudeAdapter())
    project = tmp_path / "proj"
    project.mkdir()
    ide = await _client(http, daemon, "ide")
    agent = await _client(http, daemon, "connector")
    try:
        await ide.ok("workspace.add", {"path": str(project)})
        row = {c["id"]: c for c in (await ide.ok("external.clis"))["clis"]}["claude"]
        assert [m["id"] for m in row["nativeModes"]] == ["default", "readonly", "unrestricted"]
        assert row["nativeModes"][2]["personOnly"] is True
        assert row["defaultNativeMode"] == "default"
        bad = await ide.call("external.create", {"cli": "claude", "project": str(project),
                                                 "nativeMode": "nonsense"})
        assert _code(bad) == "invalid_params"
        refused = await agent.call("external.create", {"cli": "claude", "project": str(project),
                                                       "nativeMode": "unrestricted"})
        assert _code(refused) == "jarvis_person_only"
        session = (await ide.ok("external.create", {"cli": "claude", "project": str(project),
                                                    "nativeMode": "readonly"}))["session"]
        assert (session["mode"], session["nativeMode"]) == ("readonly", "readonly")
        ref = session["ref"]
        refused = await agent.call("external.setMode", {"ref": ref,
                                                        "nativeMode": "unrestricted"})
        assert _code(refused) == "jarvis_person_only"
        switched = (await ide.ok("external.setMode", {"ref": ref, "nativeMode": "default",
                                                      "mode": "unrestricted"}))
        assert (switched["session"]["mode"], switched["session"]["nativeMode"]) == (
            "default", "default")
        neither = await ide.call("external.setMode", {"ref": ref})
        assert _code(neither) == "invalid_params"
        legacy = (await ide.ok("external.setMode", {"ref": ref, "mode": "readonly"}))
        assert legacy["session"]["nativeMode"] == "readonly"
        await ide.ok("external.close", {"ref": ref})
    finally:
        for client in (ide, agent):
            await client.stop()


async def test_set_model_over_rpc(daemon: Daemon, http: aiohttp.ClientSession,
                                  tmp_path: Path) -> None:
    """1.67.0 (externalModelSwitch): external.setModel, the row's model and external.state."""
    assert "externalModelSwitch" in CAPABILITIES and PROTOCOL_VERSION == "1.79.0"
    assert daemon.core is not None and daemon.core.external is not None
    registry = daemon.core.external.registry
    for cli in registry.ids():
        registry.unregister(cli)
    registry.register(FakeClaudeAdapter())
    project = tmp_path / "proj"
    project.mkdir()
    ide = await _client(http, daemon, "ide")
    other = await _client(http, daemon, "tui")
    try:
        await ide.ok("workspace.add", {"path": str(project)})
        row = {c["id"]: c for c in (await ide.ok("external.clis"))["clis"]}["claude"]
        assert "defaultModel" in row and "models" in row
        ref = (await ide.ok("external.create",
                            {"cli": "claude", "project": str(project)}))["session"]["ref"]
        result = await ide.ok("external.setModel", {"ref": ref, "model": "opus"})
        assert result["ref"] == ref and result["model"] == "opus"
        assert not result.get("appliesNextTurn")
        states = await _wait(lambda: [p for p in other.of_method("external.state")
                                      if p.get("model") == "opus"])
        assert states[0]["ref"] == ref
        got = (await ide.ok("external.get", {"ref": ref}))["session"]
        assert got["model"] == "opus"
        bad = await ide.call("external.setModel", {"ref": ref, "model": "--yolo"})
        assert _code(bad) == "invalid_params"
        missing = await ide.call("external.setModel", {"ref": ref})
        assert _code(missing) == "invalid_params"  # model is required (null allowed)
        back = await ide.ok("external.setModel", {"ref": ref, "model": None})
        assert back["model"] is None
        await ide.ok("external.close", {"ref": ref})
    finally:
        for client in (ide, other):
            await client.stop()


async def test_send_subscribes_the_sender(daemon: Daemon, http: aiohttp.ClientSession,
                                          tmp_path: Path) -> None:
    """external.list -> external.send on a host none session streams to the sender."""
    assert daemon.core is not None and daemon.core.external is not None
    registry = daemon.core.external.registry
    for cli in registry.ids():
        registry.unregister(cli)
    registry.register(FakeClaudeAdapter())
    project = tmp_path / "proj"
    project.mkdir()
    ide = await _client(http, daemon, "ide")
    try:
        await ide.ok("workspace.add", {"path": str(project)})
        ref = (await ide.ok("external.create",
                            {"cli": "claude", "project": str(project)}))["session"]["ref"]
        first = (await ide.ok("external.send", {"ref": ref, "text": "start"}))["turnId"]
        await _wait(lambda: [p for p in ide.of_method("external.event")
                             if p["kind"] == "turn.done" and p["turnId"] == first])
        await ide.ok("external.close", {"ref": ref})
        await _wait(lambda: not daemon.core.external.supervisor.live())
    finally:
        await ide.stop()
    tui = await _client(http, daemon, "tui")
    watcher = await _client(http, daemon, "ide")
    try:
        listed = await tui.ok("external.list", {"cwd": str(project)})
        row = next(s for s in listed["sessions"] if s["ref"] == ref)
        assert row["host"] == "none"
        turn = (await tui.ok("external.send", {"ref": ref, "text": "hello"}))["turnId"]
        done = await _wait(lambda: [p for p in tui.of_method("external.event")
                                    if p["kind"] == "turn.done" and p["turnId"] == turn])
        assert done[0]["ref"] == ref
        mine = [p for p in tui.of_method("external.event") if p.get("turnId") == turn]
        assert any(p["kind"] == "message.user" for p in mine)
        assert not watcher.of_method("external.event")  # only the sender is subscribed
        # A second send does not subscribe twice (each event arrives once).
        second = (await tui.ok("external.send", {"ref": ref, "text": "again"}))["turnId"]
        await _wait(lambda: [p for p in tui.of_method("external.event")
                             if p["kind"] == "turn.done" and p["turnId"] == second])
        seqs = [p["seq"] for p in tui.of_method("external.event")]
        assert len(seqs) == len(set(seqs))
        await tui.ok("external.close", {"ref": ref})
    finally:
        await tui.stop()
        await watcher.stop()


async def test_daemon_stop_stops_external_children(tmp_path: Path,
                                                   http: aiohttp.ClientSession) -> None:
    with env_vars(FAKE_CLI_SCENARIO=None):
        daemon = await make_daemon(tmp_path / "home", SETTINGS)
        assert daemon.core is not None
        daemon.core.external.registry.register(FakeClaudeAdapter())
        project = tmp_path / "proj"
        project.mkdir()
        client = await _client(http, daemon, "ide")
        try:
            await client.ok("workspace.add", {"path": str(project)})
            await client.ok("external.create", {"cli": "claude", "project": str(project)})
            child = daemon.core.external.supervisor.live()[0]
            assert (Path(daemon.paths.home) / "external" / f"{child.pid}.json").exists()
        finally:
            await client.stop()
            await daemon.stop()
        assert not child.alive
        assert not list((Path(tmp_path / "home") / "external").glob("*.json"))
