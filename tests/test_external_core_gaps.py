"""M19 core integration: the external layer's contract and base-layer gaps.

``external.get`` / ``external.unwatch``, models in ``external.clis`` (filled in
the background), the one person rule (desktop and TUI without a clientKind,
remote web logins), the messenger confirmation seam, the ledger hook, helper
processes under the supervisor, tree kill of a pid that leads no group,
adapter shutdown, transcript copies of snowpea's own prompts, observers, and
person-only approvals.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _external_adapter import FAKE_CLAUDE, FakeClaudeAdapter
from _support import env_vars
from test_external_manager import Conn, Env, until

from snowpea_core.config.settings import Settings
from snowpea_core.external import proc
from snowpea_core.external.base import ApprovalAsk, Discovery
from snowpea_core.external.proc import ExternalError, ProcessSupervisor
from snowpea_core.external.registry import AdapterRegistry
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")


class SlowModels(FakeClaudeAdapter):
    """Lists its models only once the test lets it; counts shutdowns."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.listed = 0
        self.shut = 0

    async def list_models(self, discovery: Discovery | None = None) -> list[str]:
        await self.gate.wait()
        self.listed += 1
        return ["fake-large", "--dangerously-skip-permissions", "fake-small"]

    async def shutdown(self) -> None:
        self.shut += 1


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    setup = Env(tmp_path)
    setup.adapter = SlowModels()  # type: ignore[attr-defined]
    setup.manager.registry = AdapterRegistry([setup.adapter])  # type: ignore[attr-defined]
    with env_vars(FAKE_CLI_LOG=str(setup.log), FAKE_CLI_SCENARIO=None,
                  FAKE_CLI_SCENARIO_FILE=None):
        await setup.manager.start()
        try:
            yield setup
        finally:
            setup.adapter.gate.set()  # type: ignore[attr-defined]
            await setup.manager.shutdown()


def _remote_web() -> Conn:
    conn = Conn("web")
    conn.remote_login = True  # type: ignore[attr-defined]
    return conn


# -- models and modes in external.clis --------------------------------------------------


async def test_models_arrive_in_the_background(env: Env) -> None:
    adapter: SlowModels = env.adapter  # type: ignore[attr-defined]
    rows = await asyncio.wait_for(env.manager.clis(), 5)  # never waits for the listing
    claude = rows[0]
    assert claude["models"] == [] and claude["modes"] == ["default", "readonly", "unrestricted"]
    assert rows[2]["modes"] == [] and rows[2]["models"] == []  # no agy adapter here
    adapter.gate.set()
    changed = await until(lambda: [p for m, p in env.hub.notes if m == "external.clis.changed"])
    # Bypass-like names never get through, whatever the CLI lists.
    assert changed[0]["clis"][0]["models"] == ["fake-large", "fake-small"]
    assert (await env.manager.clis())[0]["models"] == ["fake-large", "fake-small"]
    assert adapter.listed == 1  # cached
    assert await env.manager.models("claude") == ["fake-large", "fake-small"]


async def test_adapter_shutdown_hook_runs_at_daemon_stop(env: Env) -> None:
    adapter: SlowModels = env.adapter  # type: ignore[attr-defined]
    await env.manager.create("claude", str(env.project), conn=Conn())
    await env.manager.shutdown()
    assert adapter.shut == 1
    assert not env.manager.supervisor.live()


# -- external.get / external.unwatch ------------------------------------------------------


async def test_get_and_unwatch(env: Env) -> None:
    watcher = Conn("ide")
    created = (await env.manager.create("claude", str(env.project), conn=watcher))["session"]
    ref = created["ref"]
    record = env.manager._records[("claude", ref["id"])]
    assert record.subscribers == [watcher]  # external.create subscribes its caller
    await env.manager.send("claude", ref["id"], "hello", conn=watcher)
    await until(lambda: "turn.done" in watcher.kinds())
    row = (await env.manager.get("claude", ref["id"]))["session"]
    assert row["host"] == "snowpea" and row["ref"] == ref
    await env.manager.unwatch("claude", ref["id"], conn=watcher)
    assert record.subscribers == [] and record.channel is not None and record.channel.alive
    assert env.manager._records.get(("claude", ref["id"])) is record  # kept: it runs
    await env.manager.unwatch("claude", "never-opened", conn=watcher)  # fine
    with pytest.raises(ExternalError) as caught:
        await env.manager.get("claude", "no-such-session")
    assert caught.value.code == errors.NOT_FOUND
    # The idle sweep stops it, as for every other idle process.
    record.last_activity -= 10_000
    assert await env.manager.sweep_idle() == 1
    assert not env.manager.supervisor.live()
    # A stored session not open anywhere comes back as a listed row.
    row = (await env.manager.get("claude", ref["id"]))["session"]
    assert row["host"] == "none" and row["canSend"]


# -- the person rule, the gateway seam and the ledger -------------------------------------


async def test_unrestricted_follows_the_delegation_engines_person_rule(env: Env) -> None:
    # The desktop app and the TUI send no clientKind: a person.
    row = (await env.manager.create("claude", str(env.project), mode="unrestricted",
                                    conn=Conn(None)))["session"]
    assert row["mode"] == "unrestricted" and row["controlled"] is False
    for conn in (_remote_web(), Conn("browser"), None):
        with pytest.raises(ExternalError) as caught:
            await env.manager.create("claude", str(env.project), mode="unrestricted", conn=conn)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    # A remote web login is still a person for everything else (its prompt reads human).
    ref = row["ref"]
    turn = (await env.manager.send("claude", ref["id"], "hi", conn=_remote_web()))["turnId"]
    record = env.manager._records[("claude", ref["id"])]
    user = next(e for e in record.events if e["kind"] == "message.user" and e["turnId"] == turn)
    assert user["payload"]["origin"]["kind"] == "human"


async def test_the_gateway_seam_and_the_ledger_hook(env: Env) -> None:
    entries: list[dict[str, Any]] = []

    def ledger(entry: dict[str, Any]) -> str:
        entries.append(entry)
        return f"jl-{len(entries)}"

    env.manager.ledger = ledger
    settings = env.settings
    confirmation = proc.person_confirmation_from_gateway(
        "telegram", bound_person=True, settings=settings
    )
    assert confirmation.surface == "messenger:telegram"
    row = (await env.manager.create("claude", str(env.project), mode="unrestricted",
                                    confirmation=confirmation))["session"]
    assert row["mode"] == "unrestricted"
    assert entries[-1]["kind"] == "unrestricted" and entries[-1]["by"] == "messenger:telegram"
    assert not (env.home / "jarvis" / "ledger.jsonl").exists()  # the hook got it, not a file
    with pytest.raises(ExternalError) as caught:
        proc.person_confirmation_from_gateway("telegram", bound_person=False, settings=settings)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    off = Settings.model_validate({"jarvis": {"messenger": {"allowGrants": False}}})
    with pytest.raises(ExternalError) as caught:
        proc.person_confirmation_from_gateway("slack", bound_person=True, settings=off)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY

    def broken(_entry: dict[str, Any]) -> None:
        raise OSError("disk full")

    env.manager.ledger = broken
    with pytest.raises(ExternalError) as caught:
        await env.manager.create("claude", str(env.project), mode="unrestricted", conn=Conn())
    assert caught.value.code == errors.INTERNAL  # no ledger entry, no grant


async def test_send_continues_in_the_default_mode_never_unrestricted_for_jarvis(
    env: Env,
) -> None:
    person = Conn("ide")
    ref = (await env.manager.create("claude", str(env.project), conn=person))["session"]["ref"]
    await env.manager.send("claude", ref["id"], "hello", conn=person)
    await until(lambda: "turn.done" in person.kinds())
    await env.manager.close("claude", ref["id"], conn=person)
    env.settings = Settings.model_validate(
        {"jarvis": {"external": {"clis": {"claude": {"path": str(FAKE_CLAUDE),
                                                     "defaultMode": "unrestricted"}}}}}
    )
    adapter = env.manager.registry.get("claude")
    assert adapter is not None
    assert env.manager.default_mode(adapter) == "unrestricted"
    assert env.manager.controlled_default_mode(adapter) == "default"
    # host none + defaultMode unrestricted: an in-process caller cannot open it so.
    with pytest.raises(ExternalError) as caught:
        await env.manager.send("claude", ref["id"], "go on", origin={"kind": "jarvis",
                                                                    "surface": "jarvis"})
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert not env.manager.supervisor.live()
    # A person's send opens it (their choice, a ledger entry) and runs the turn.
    turn = (await env.manager.send("claude", ref["id"], "go on", conn=person))["turnId"]
    record = env.manager._records[("claude", ref["id"])]
    await until(lambda: any(e["kind"] == "turn.done" and e["turnId"] == turn
                            for e in record.events))
    assert env.manager.opened()[0]["mode"] == "unrestricted"
    assert env.manager.opened()[0]["controlled"] is False


# -- transcript copies and observers ---------------------------------------------------


async def test_observers_hear_live_events_and_who_typed(env: Env) -> None:
    seen: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def observer(row: dict[str, Any], event: dict[str, Any]) -> None:
        seen.append((row, event))

    env.manager.observers.append(observer)
    person = Conn("ide")
    ref = (await env.manager.create("claude", str(env.project), conn=person))["session"]["ref"]
    turn = (await env.manager.send("claude", ref["id"], "from the person", conn=person))["turnId"]
    await until(lambda: any(e["kind"] == "turn.done" for _r, e in seen))
    users = [e for _r, e in seen if e["kind"] == "message.user"]
    assert users[0]["payload"]["origin"]["kind"] == "human" and users[0]["turnId"] == turn
    assert not any(e["kind"] == "message.delta" for _r, e in seen)
    assert seen[0][0]["ref"] == ref
    seen.clear()
    await env.manager.send("claude", ref["id"], "from jarvis",
                           origin={"kind": "jarvis", "surface": "jarvis"})
    await until(lambda: any(e["kind"] == "turn.done" for _r, e in seen))
    users = [e for _r, e in seen if e["kind"] == "message.user"]
    assert [u["payload"]["origin"]["kind"] for u in users] == ["jarvis"]
    # The CLI's own copies of both prompts, read back later: not shown, not observed.
    record = env.manager._records[("claude", ref["id"])]
    seen.clear()
    fresh = await env.adapter.get_session(ref["id"])  # type: ignore[attr-defined]
    await env.manager._publish(record, await env.adapter.read(fresh))  # type: ignore[attr-defined]
    assert not [e for _r, e in seen if e["kind"] == "message.user"]
    # Something the person typed in the CLI itself (not a copy) is theirs.
    from snowpea_core.external.timeline import TimelineBuilder

    builder = TimelineBuilder("claude", ref["id"])
    builder.user("typed in the terminal", key="terminal-1")
    await env.manager._publish(record, builder.drain())
    typed = [e for _r, e in seen if e["kind"] == "message.user"]
    assert typed[0]["payload"]["origin"] == {"kind": "human", "surface": "claude"}


async def test_loading_history_is_not_observed(env: Env) -> None:
    person = Conn("ide")
    ref = (await env.manager.create("claude", str(env.project), conn=person))["session"]["ref"]
    await env.manager.send("claude", ref["id"], "hello", conn=person)
    await until(lambda: "turn.done" in person.kinds())
    await env.manager.close("claude", ref["id"], conn=person)
    assert ("claude", ref["id"]) not in env.manager._records
    seen: list[dict[str, Any]] = []

    async def observer(_row: dict[str, Any], event: dict[str, Any]) -> None:
        seen.append(event)

    env.manager.observers.append(observer)
    page = await env.manager.read("claude", ref["id"])
    assert any(e["kind"] == "message.user" for e in page["events"])
    assert seen == []


# -- person-only approvals --------------------------------------------------------------


async def test_a_person_only_ask_is_never_cached_and_only_a_person_allows(env: Env) -> None:
    person = Conn("ide")
    ref = (await env.manager.create("claude", str(env.project), conn=person))["session"]["ref"]
    record = env.manager._records[("claude", ref["id"])]
    record.approval_cache.add(("tmux.adopt", ""))  # as if allowed "for the session" before
    ask = ApprovalAsk(tool="tmux.adopt", args={"session": "mine", "pane": "%1"}, risk="high",
                      person_only=True)
    task = asyncio.ensure_future(env.manager._ask_approval(record, ask))
    request = (await until(env.hub.pending))[-1]
    assert request["personOnly"] is True and request["external"] == ref
    with pytest.raises(RpcError) as caught:
        await env.approvals.respond(request["requestId"], "allow", by="jarvis")
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    await env.approvals.respond(request["requestId"], "allow", conn=person)
    answer = await task
    assert answer.allow and answer.scope == "once"


# -- helper processes and tree kill -------------------------------------------------------


async def test_helpers_are_counted_and_reaped(tmp_path: Path) -> None:
    supervisor = ProcessSupervisor(tmp_path, max_processes=1)
    code, out = await supervisor.run_helper(["/bin/sh", "-c", "echo hi"], cwd=str(tmp_path),
                                            env=dict(os.environ), cli="test", timeout=10)
    assert (code, out) == (0, b"hi\n") and supervisor.helpers_started == 1
    assert not supervisor.helpers() and not list((tmp_path / "external").glob("*.json"))
    # One that overruns its time is stopped with its tree.
    code, out = await supervisor.run_helper(["/bin/sh", "-c", "sleep 30 & wait"],
                                            cwd=str(tmp_path), env=dict(os.environ), cli="test",
                                            timeout=0.3)
    assert code is None and not supervisor.helpers()
    # Helpers do not count against maxProcesses...
    first = await supervisor.spawn(["/bin/sleep", "30"], cwd=str(tmp_path), env={}, cli="test")
    helper = await supervisor.spawn_helper(["/bin/sleep", "30"], cwd=str(tmp_path), env={},
                                           cli="test")
    assert supervisor.pids() == {first.pid, helper.pid}
    assert (tmp_path / "external" / f"{helper.pid}.json").exists()
    # ...and daemon stop stops them as well.
    await supervisor.shutdown(grace=0.5)
    assert not first.alive and not helper.alive
    assert not list((tmp_path / "external").glob("*.json"))
    with pytest.raises(ExternalError):
        await supervisor.spawn_helper(["/bin/true"], cwd=str(tmp_path), env={}, cli="test")


async def test_kill_tree_of_a_pid_that_leads_no_group(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    # Same process group as this test run (no new session), with a grandchild.
    child = subprocess.Popen(
        ["/bin/sh", "-c", f"sleep 60 & echo $! > {marker}; wait"],
    )
    try:
        await until(lambda: marker.exists() and marker.read_text().strip())
        grandchild = int(marker.read_text().strip())
        assert os.getpgid(child.pid) == os.getpgrp()
        await proc._kill_tree(child.pid, grace=1.0)
        child.wait(timeout=5)
        await until(lambda: not proc._running(grandchild))
        # Asked to signal this process's own group: refused, the test survives.
        another = subprocess.Popen(["/bin/sleep", "60"])
        await proc._kill_tree(another.pid, pgid=os.getpgrp(), grace=1.0)
        assert another.wait(timeout=5) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
