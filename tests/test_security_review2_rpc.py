"""Security review, part 2: who may call what, on a real daemon.

One table over every ``jarvis.*``, ``external.*``, ``mcp.preset.*`` and
``job.*`` method × connection kind {person, remote_login, browser,
connector}: ``refused`` means ``jarvis_person_only`` (or, for another
connection's external session, ``not_found``); ``passes`` means the call got
past the person rule (any other answer, an error included).

Plus the settings RPC (H2), worker sessions (M6), event delivery (L6) and
the external trust and path contracts (M4, H2).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _external_adapter import FAKE_CLAUDE, FakeClaudeAdapter
from _support import RpcClient, env_vars, make_daemon

from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import METHODS, PROTOCOL_VERSION

pytestmark = [pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs"),
              pytest.mark.real_trust]

KINDS = ("person", "remote_login", "browser", "connector")
P = "passes"
R = "refused"
N = "not_found"  # someone else's external session does not exist for them

#: method -> expected outcome per kind (person, remote_login, browser, connector).
TABLE: dict[str, tuple[str, str, str, str]] = {
    "jarvis.status": (P, P, R, R),
    "jarvis.enable": (P, R, R, R),
    "jarvis.disable": (P, P, R, R),
    "jarvis.overview": (P, P, R, R),
    "jarvis.project.list": (P, P, R, R),
    "jarvis.project.rename": (P, P, R, R),
    "jarvis.project.alias": (P, P, R, R),
    "jarvis.escalations.list": (P, P, R, R),
    "jarvis.escalations.dismiss": (P, P, R, R),
    "jarvis.ledger": (P, P, R, R),
    "jarvis.delegation.list": (P, P, R, R),
    "jarvis.delegation.set": (P, R, R, R),  # security review 8, H8-2 (was P for remote)
    "jarvis.delegation.revoke": (P, P, P, P),  # an unknown id: not_found for anyone
    "jarvis.takeover": (P, P, P, P),  # anyone may take authority away
    "jarvis.handback": (P, P, R, R),
    "jarvis.briefing.get": (P, P, R, R),
    "jarvis.briefing.set": (P, P, R, R),
    "mcp.preset.list": (P, P, P, P),
    "mcp.preset.check": (P, P, R, R),
    "mcp.preset.apply": (P, R, R, R),
    "mcp.preset.remove": (P, R, R, R),
    "external.clis": (P, P, P, P),
    "external.clis.confirmPath": (P, R, R, R),
    "external.list": (P, P, P, P),  # non-person: only its own rows
    "external.get": (P, P, N, N),
    "external.read": (P, P, N, N),
    # Security review 5, M1: a connector is deny-by-default (reads and streams only).
    "external.open": (P, P, P, R),  # trust decides (test below)
    "external.send": (P, P, N, R),
    "external.setMode": (P, P, N, R),
    "external.interrupt": (P, P, N, R),
    "external.create": (P, P, P, R),  # trust decides (test below)
    "external.close": (P, P, N, R),
    "external.unwatch": (P, P, P, P),
    "external.terminalSnapshot": (P, P, R, R),  # people only (a remote login too)
    "job.schedule": (P, P, P, R),
    "job.preview": (P, P, P, P),
    "job.list": (P, P, P, P),
    "job.cancel": (P, P, P, R),
    "job.runNow": (P, P, R, R),  # on a jarvis-task job
    "job.delete": (P, P, P, R),
    "job.nextRunForClient": (P, P, P, P),
    # On a jarvis-task job. A remote login changes only the jobs it created
    # (review 6 part B, M-4): this one was made locally, so it is refused.
    "job.update": (P, R, R, R),
    "job.runs": (P, P, P, P),
}

SETTINGS = {
    "jarvis": {"external": {"clis": {"claude": {
        "path": str(FAKE_CLAUDE), "confirmedPath": str(FAKE_CLAUDE.resolve())}}}},
}


def test_the_table_covers_every_method() -> None:
    families = ("jarvis.", "external.", "mcp.preset.", "job.")
    declared = {name for name in METHODS if name.startswith(families)}
    assert declared == set(TABLE), sorted(declared ^ set(TABLE))


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with env_vars(FAKE_CLI_SCENARIO=None, FAKE_CLI_LOG=str(tmp_path / "fake.log")):
        settings = {**SETTINGS, "jarvis": {**SETTINGS["jarvis"],
                                           "folder": str(tmp_path / "JarvisFolder")}}
        started = await make_daemon(tmp_path / "home", settings)
        try:
            registry = started.core.external.registry if started.core.external else None
            if registry is None:
                from snowpea_core.server.external_handlers import ensure_manager

                registry = ensure_manager(started.core).registry
            for cli in registry.ids():
                registry.unregister(cli)
            registry.register(FakeClaudeAdapter())
            yield started
        finally:
            await started.stop()


async def _client(http: aiohttp.ClientSession, daemon: Daemon, kind: str) -> RpcClient:
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    client = RpcClient(ws)
    client.start()
    hello: dict[str, Any] = {"token": daemon.token, "clientVersion": "test",
                             "protocolVersion": PROTOCOL_VERSION}
    hello["clientKind"] = {"person": "ide", "remote_login": "web", "browser": "browser",
                           "connector": "connector"}[kind]
    await client.ok("system.hello", hello)
    if kind == "remote_login":
        # What the remote listener's password login marks on the connection.
        for conn in daemon.core.hub.clients():
            if getattr(conn, "client_kind", None) == "web":
                conn.remote_login = True
    return client


def _code(frame: dict[str, Any]) -> str | None:
    error = frame.get("error")
    if not error:
        return None
    return str((error.get("data") or {}).get("code"))


async def _params(daemon: Daemon, ide: RpcClient, tmp_path: Path) -> dict[str, dict[str, Any]]:
    """Minimal params per method (what the call does past the gate does not matter)."""
    from snowpea_core.scheduler import services

    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    await ide.ok("workspace.add", {"path": str(project)})
    session = await ide.ok("session.create", {"workdir": str(project)})
    job = await services(daemon.core).schedule(
        "every day at 9am", "summarise", mode="accept", workdir=str(project), title="t",
        created_via="rpc", target="jarvis-task", target_data={"kind": "task"},
    )
    ref = {"cli": "claude", "id": "0000aaaa-0000-0000-0000-000000000000"}
    sid = session["sessionId"]
    return {
        "jarvis.status": {}, "jarvis.disable": {}, "jarvis.overview": {},
        "jarvis.enable": {"folder": "/"},
        "jarvis.project.list": {},
        "jarvis.project.rename": {"root": str(project), "name": "P"},
        "jarvis.project.alias": {"root": str(project), "add": ["p"], "remove": []},
        "jarvis.escalations.list": {}, "jarvis.escalations.dismiss": {"all": True},
        "jarvis.ledger": {}, "jarvis.delegation.list": {},
        "jarvis.delegation.set": {"scope": "session", "target": sid, "level": "staged",
                                  "allow": []},
        "jarvis.delegation.revoke": {"id": "g-nope"},
        "jarvis.takeover": {"sessionId": sid},
        "jarvis.handback": {"sessionId": sid},
        "jarvis.briefing.get": {}, "jarvis.briefing.set": {"enabled": False},
        "mcp.preset.list": {}, "mcp.preset.check": {"id": "nope"},
        "mcp.preset.apply": {"id": "nope"}, "mcp.preset.remove": {"id": "nope"},
        "external.clis": {},
        "external.clis.confirmPath": {"cli": "claude", "path": "/nope"},
        "external.list": {"cwd": str(project)},
        "external.get": {"ref": ref}, "external.read": {"ref": ref},
        "external.open": {"ref": ref}, "external.send": {"ref": ref, "text": "hi"},
        "external.setMode": {"ref": ref, "mode": "readonly"},
        "external.interrupt": {"ref": ref},
        "external.create": {"cli": "claude", "project": str(project)},
        "external.close": {"ref": ref}, "external.unwatch": {"ref": ref},
        "external.terminalSnapshot": {"ref": ref},
        "job.schedule": {"spec": "every day at 9am", "task": "x", "workdir": str(project)},
        "job.preview": {"spec": "every day at 9am"}, "job.list": {},
        "job.cancel": {"jobId": "nope"}, "job.runNow": {"jobId": job.id},
        "job.delete": {"jobId": "nope"}, "job.nextRunForClient": {"clientId": "x"},
        "job.update": {"jobId": job.id, "title": "renamed"}, "job.runs": {"jobId": job.id},
    }


async def test_every_method_by_connection_kind(daemon: Daemon, http: aiohttp.ClientSession,
                                               tmp_path: Path) -> None:
    clients = {kind: await _client(http, daemon, kind) for kind in KINDS}
    try:
        params = await _params(daemon, clients["person"], tmp_path)
        wrong: list[str] = []
        for method, expected in TABLE.items():
            for kind, want in zip(KINDS, expected, strict=True):
                if method == "job.runNow" and want == P:
                    continue  # firing the task is not this test's business
                frame = await clients[kind].call(method, params[method])
                code = _code(frame)
                if want == R:
                    ok = code == "jarvis_person_only"
                elif want == N:
                    ok = code == "not_found"
                else:
                    ok = code != "jarvis_person_only"
                if not ok:
                    wrong.append(f"{method} as {kind}: wanted {want}, got {code} {frame}")
        assert not wrong, "\n".join(wrong)
    finally:
        for client in clients.values():
            await client.stop()


# -- H2: settings.set ----------------------------------------------------------


async def test_settings_set_guards_jarvis_and_mcp_tags(daemon: Daemon,
                                                       http: aiohttp.ClientSession,
                                                       tmp_path: Path) -> None:
    clients = {kind: await _client(http, daemon, kind) for kind in KINDS}
    try:
        patches = [
            {"jarvis": {"exposeBrowser": False}},
            {"jarvis": {"messenger": {"allowGrants": False}}},
            {"jarvis": {"external": {"clis": {"codex": {"path": "/bin/true"}}}}},
            {"mcp": {"permissions": {"x": "read"}}},
            {"mcp": {"allowVars": ["X"]}},
        ]
        for kind in ("remote_login", "browser", "connector"):
            for patch in patches:
                frame = await clients[kind].call("settings.set",
                                                 {"scope": "global", "patch": patch})
                assert _code(frame) == "jarvis_person_only", (kind, patch, frame)
            frame = await clients[kind].call("settings.set", {
                "scope": "project", "workdir": str(tmp_path),
                "patch": {"jarvis": {"expose": True}}})
            assert _code(frame) == "jarvis_person_only", (kind, frame)
            # Security review 3 (N7): only the keys the browser's own pages write are
            # open to them; everything else is a local person's.
            frame = await clients[kind].call("settings.set",
                                             {"scope": "global", "patch": {"ralph": {}}})
            assert _code(frame) == "jarvis_person_only", frame
            frame = await clients[kind].call(
                "settings.set", {"scope": "global", "patch": {"browser": {"siteMemory": False}}})
            # The browser host's runtime switch; the connector has no settings of its own.
            assert _code(frame) == (None if kind != "connector" else "jarvis_person_only"), frame
        person = clients["person"]
        for patch in (
            {"jarvis": {"delegation": {"default": "full"}}},
            {"jarvis": {"external": {"clis": {"claude": {"defaultMode": "unrestricted"}}}}},
            {"jarvis": {"external": {"clis": {"claude": {"confirmedPath": "/bin/true"}}}}},
        ):
            frame = await person.call("settings.set", {"scope": "global", "patch": patch})
            assert _code(frame) == "invalid_params", (patch, frame)
        ok = await person.ok("settings.set", {"scope": "global", "patch": {
            "jarvis": {"external": {"clis": {"codex": {"path": "/bin/true"}}}}}})
        assert ok["settings"]["jarvis"]["external"]["clis"]["codex"]["path"] == "/bin/true"
        entries = [json.loads(line) for line in
                   (daemon.core.paths.home / "jarvis" / "ledger.jsonl").read_text().splitlines()]
        assert any("clis.codex.path" in str(entry.get("summary")) for entry in entries)
    finally:
        for client in clients.values():
            await client.stop()


async def test_a_changed_cli_path_is_not_run_until_the_person_confirms_it(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    other = tmp_path / "other-claude"
    shutil.copy2(FAKE_CLAUDE, other)  # a new file: a path nobody confirmed yet
    person = await _client(http, daemon, "person")
    browser = await _client(http, daemon, "browser")
    try:
        await person.ok("settings.set", {"scope": "global", "patch": {
            "jarvis": {"external": {"clis": {"claude": {"path": str(other)}}}}}})
        manager = daemon.core.external
        probed: list[str | None] = []
        adapter = manager.registry.get("claude")
        real = adapter.discover

        async def watch(override: str | None = None) -> Any:
            probed.append(override)
            return await real(override)

        adapter.discover = watch  # type: ignore[method-assign]
        rows = {row["id"]: row for row in (await person.ok("external.clis"))["clis"]}
        resolved = str(other.resolve())
        assert rows["claude"]["pendingPath"] == resolved
        assert not rows["claude"]["installed"] and probed == []
        refused = await browser.call("external.clis.confirmPath",
                                     {"cli": "claude", "path": resolved})
        assert _code(refused) == "jarvis_person_only"
        stale = await person.call("external.clis.confirmPath", {"cli": "claude", "path": "/x"})
        assert _code(stale) == "invalid_params"
        confirmed = await person.ok("external.clis.confirmPath",
                                    {"cli": "claude", "path": resolved})
        row = next(r for r in confirmed["clis"] if r["id"] == "claude")
        assert row["pendingPath"] is None and row["installed"]
        assert probed == [str(other)]
        assert daemon.core.settings.jarvis.external.clis["claude"].confirmedPath == resolved
    finally:
        await person.stop()
        await browser.stop()


# -- M4: trusted projects ---------------------------------------------------------


async def test_external_cli_needs_the_person_to_trust_the_project(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    clients = {kind: await _client(http, daemon, kind) for kind in KINDS}
    try:
        project = tmp_path / "proj"
        project.mkdir()
        person = clients["person"]
        await person.ok("workspace.add", {"path": str(project)})
        params = {"cli": "claude", "project": str(project)}
        for kind in ("remote_login", "browser"):
            frame = await clients[kind].call("external.create", {**params, "trust": True})
            assert _code(frame) == "external_unavailable"
            assert frame["error"]["data"]["details"]["reason"] == "untrusted_project_person_only"
        # Security review 5, M1: a connector starts nothing at all.
        frame = await clients["connector"].call("external.create", {**params, "trust": True})
        assert _code(frame) == "jarvis_person_only"
        frame = await person.call("external.create", params)
        assert _code(frame) == "external_unavailable"
        assert frame["error"]["data"]["details"]["reason"] == "untrusted_project"
        created = await person.ok("external.create", {**params, "trust": True})
        ref = created["session"]["ref"]
        # Trusted now: a browser host may start one there too, and sees only its own.
        mine = await clients["browser"].ok("external.create", params)
        listed = await clients["browser"].ok("external.list", {"cwd": str(project)})
        refs = [row["ref"] for row in listed["sessions"]]
        assert ref not in refs
        assert _code(await clients["browser"].call("external.read", {"ref": ref})) == "not_found"
        assert await clients["browser"].ok("external.read", {"ref": mine["session"]["ref"]})
        # One trusted-projects record (security review 5, C1): the externalCli scope.
        trust = json.loads((daemon.core.paths.home / "trust" / "projects.json").read_text())
        assert "externalCli" in trust["projects"][str(project.resolve())]["scopes"]
        # The repository cannot trust itself: its own settings file is not read for this.
        other = tmp_path / "other"
        (other / ".snowpea").mkdir(parents=True)
        (other / ".snowpea" / "settings.json").write_text(
            json.dumps({"jarvis": {"external": {"trusted": True}}}), encoding="utf-8")
        await person.ok("workspace.add", {"path": str(other)})
        frame = await clients["browser"].call("external.create",
                                               {"cli": "claude", "project": str(other)})
        assert frame["error"]["data"]["details"]["reason"] == "untrusted_project_person_only"
    finally:
        for client in clients.values():
            await client.stop()


# -- L6: events -----------------------------------------------------------------------


async def test_jarvis_and_external_events_reach_people_only(
    daemon: Daemon, http: aiohttp.ClientSession
) -> None:
    clients = {kind: await _client(http, daemon, kind) for kind in KINDS}
    try:
        hub = daemon.core.hub
        await hub.notify("jarvis.escalation", {"id": "e1"})
        await hub.notify("external.state", {"ref": {"cli": "claude", "id": "x"}})
        await hub.notify("external.clis.changed", {"clis": []})
        await hub.notify("approval.pending", {"request": {"requestId": "r"}})
        await asyncio.sleep(0.3)

        def methods(kind: str) -> set[str]:
            return {n.get("method") for n in clients[kind].notifications}

        for kind in ("person", "remote_login"):
            assert {"jarvis.escalation", "external.state", "external.clis.changed"} <= methods(kind)
        for kind in ("browser", "connector"):
            got = methods(kind)
            assert not got & {"jarvis.escalation", "external.state", "external.clis.changed"}
            assert "approval.pending" in got  # unchanged for browser hosts
    finally:
        for client in clients.values():
            await client.stop()


# -- M6: jarvis-task workers --------------------------------------------------------------


async def test_task_workers_are_the_persons_only(daemon: Daemon, http: aiohttp.ClientSession,
                                                 tmp_path: Path) -> None:
    from snowpea_core.jarvis.ports import TASK_WORKER_SURFACE

    folder = tmp_path / "w"
    folder.mkdir()
    worker = await daemon.core.sessions.create(
        workdir=folder, origin_surface=TASK_WORKER_SURFACE, origin_conn=None, kind="scheduled")
    clients = {kind: await _client(http, daemon, kind) for kind in KINDS}
    try:
        for kind in ("browser", "connector"):
            client = clients[kind]
            listed = await client.ok("session.list", {})
            assert worker.id not in [row["sessionId"] for row in listed["sessions"]]
            for method in ("session.resume", "session.events", "session.trajectory"):
                frame = await client.call(method, {"sessionId": worker.id})
                assert _code(frame) == "not_found", (kind, method, frame)
        listed = await clients["person"].ok("session.list", {})
        assert worker.id in [row["sessionId"] for row in listed["sessions"]]
        assert _code(await clients["person"].call("session.events",
                                                  {"sessionId": worker.id})) is None
    finally:
        for client in clients.values():
            await client.stop()
