"""Slash commands while a turn runs (1.55.0, ``commands/kinds.py``).

A command sent mid-turn used to start at once and take over ``current_turn``
and ``turn_task``: the model turn could no longer be stopped and the chat
looked idle.  Now an ``instant`` command runs beside the turn and answers with
one ``command.result``; a ``turn`` command waits like a prompt.  Real daemon,
fake model ("slow reply" sleeps two seconds).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.commands import kinds
from snowpea_core.commands.persists import COMMAND_PERSISTS
from snowpea_core.commands.registry import CommandRegistry, register_builtin_commands
from snowpea_core.server.app_server import Daemon

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "shutdown_race.json"
TIMEOUT = 20.0


def test_every_builtin_is_classified() -> None:
    registry = register_builtin_commands(CommandRegistry())
    names = {command.name for command in registry.commands()}
    assert names <= set(kinds.COMMAND_KINDS), sorted(names - set(kinds.COMMAND_KINDS))
    assert set(kinds.COMMAND_KINDS) <= names | set(COMMAND_PERSISTS)
    for name, judge in kinds.COMMAND_KINDS.items():
        assert judge("") in (kinds.INSTANT, kinds.TURN), name
    assert kinds.command_kind("plan") == kinds.INSTANT
    assert kinds.command_kind("plan", "write the parser") == kinds.TURN
    assert kinds.command_kind("mcp", "list") == kinds.INSTANT
    assert kinds.command_kind("mcp", "add x -- y") == kinds.TURN
    assert kinds.command_kind("nonexistent") == kinds.TURN
    assert kinds.command_kind("model", source="skill") == kinds.TURN
    assert kinds.applies_from("model") is None  # a read
    assert kinds.applies_from("model", "gpt") == kinds.NEXT_TURN
    assert kinds.applies_from("auto") == kinds.NEXT_TOOL_CALL


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            if instance.core is not None:
                await instance.stop()


async def _running(client: RpcClient, tmp_path: Path) -> tuple[str, str]:
    workdir = tmp_path / "project"
    workdir.mkdir(parents=True, exist_ok=True)
    created = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept"})
    session_id = str(created["sessionId"])
    turn = await client.ok("session.prompt", {"sessionId": session_id, "text": "slow reply"})
    turn_id = str(turn["turnId"])
    await client.wait(
        lambda e: e["kind"] == "turn.started" and e["payload"]["turnId"] == turn_id,
        timeout=TIMEOUT,
    )
    return session_id, turn_id


def _kinds_after(client: RpcClient, start: int) -> list[str]:
    return [event["kind"] for event in client.events[start:]]


@pytest.mark.parametrize("via", ["prompt", "command.run"])
async def test_an_instant_command_runs_beside_the_turn(
    tmp_path: Path, http: aiohttp.ClientSession, daemon: Daemon, via: str
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id, turn_id = await _running(client, tmp_path)
    session = daemon.core.sessions.get(session_id)
    task = session.turn_task
    mark = len(client.events)
    if via == "prompt":
        result = await client.ok("session.prompt", {"sessionId": session_id, "text": "/help"})
    else:
        result = await client.ok("command.run", {"sessionId": session_id, "name": "help"})
    assert result["besideTurn"] is True and result["turnId"].startswith("c-")
    done = await client.wait(
        lambda e: e["kind"] == "command.result" and e["payload"]["commandId"] == result["turnId"],
        timeout=TIMEOUT,
    )
    payload = done["payload"]
    assert payload["ok"] is True and payload["command"] == "/help"
    assert payload["runningTurnId"] == turn_id and payload["appliesFrom"] is None
    assert "/model" in payload["text"]
    # The running turn's bookkeeping is untouched.
    assert session.current_turn == turn_id and session.turn_task is task and not task.done()
    listed = await client.ok("session.list", {})
    row = next(r for r in listed["sessions"] if r["sessionId"] == session_id)
    assert row["running"] is True
    # Nothing that looks like a turn of its own (the model turn's own events,
    # message.user and step.started, go on around it).
    after = _kinds_after(client, mark)
    assert after.count("command.result") == 1
    assert "turn.started" not in after and "turn.done" not in after
    assert not any(
        e["kind"] == "message.done" and "/model" in str(e["payload"].get("text"))
        for e in client.events[mark:]
    )
    # The model turn can still be stopped.
    await client.ok("session.interrupt", {"sessionId": session_id})
    end = await client.wait(
        lambda e: e["kind"] == "turn.done" and e["payload"]["turnId"] == turn_id,
        timeout=TIMEOUT,
    )
    assert end["payload"]["reason"] == "interrupted"
    history = [str(m.content) for m in session.history.snapshot()]
    assert not any(line.startswith("/help") for line in history)
    await client.stop()


async def test_a_setting_change_says_when_it_applies(
    tmp_path: Path, http: aiohttp.ClientSession, daemon: Daemon
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id, turn_id = await _running(client, tmp_path)
    result = await client.ok("session.prompt", {"sessionId": session_id, "text": "/plan"})
    done = await client.wait(
        lambda e: e["kind"] == "command.result" and e["payload"]["commandId"] == result["turnId"],
        timeout=TIMEOUT,
    )
    assert done["payload"]["appliesFrom"] == "next_tool_call"
    assert daemon.core.sessions.get(session_id).mode == "plan"
    assert daemon.core.sessions.get(session_id).current_turn == turn_id
    await client.ok("session.interrupt", {"sessionId": session_id})
    await client.stop()


@pytest.mark.parametrize(
    ("via", "when_busy"),
    [("prompt", "queue"), ("prompt", "steer"), ("command.run", "queue"),
     ("command.run", "steer")],
)
async def test_a_turn_command_waits_for_the_running_turn(
    tmp_path: Path, http: aiohttp.ClientSession, daemon: Daemon, via: str, when_busy: str
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id, turn_id = await _running(client, tmp_path)
    session = daemon.core.sessions.get(session_id)
    task = session.turn_task
    if via == "prompt":
        result = await client.ok("session.prompt", {
            "sessionId": session_id, "text": "/compact", "whenBusy": when_busy})
    else:
        result = await client.ok("command.run", {
            "sessionId": session_id, "name": "compact", "whenBusy": when_busy})
    command_turn = result["turnId"]
    assert command_turn.startswith("t-") and "besideTurn" not in {
        k for k, v in result.items() if v is not None}
    queued = await client.wait(
        lambda e: e["kind"] == "turn.queued" and e["payload"]["turnId"] == command_turn,
        timeout=TIMEOUT,
    )
    assert queued["payload"]["position"] == 1
    # Still the model turn's session: nothing started, nothing replaced.
    assert session.current_turn == turn_id and session.turn_task is task
    started = [e for e in client.of_kind("turn.started")
               if e["payload"]["turnId"] == command_turn]
    assert started == []
    first = await client.wait(
        lambda e: e["kind"] == "turn.done" and e["payload"]["turnId"] == turn_id,
        timeout=TIMEOUT,
    )
    assert first["payload"]["reason"] == "complete"  # it was not folded into it
    await client.wait(
        lambda e: e["kind"] == "turn.started" and e["payload"]["turnId"] == command_turn,
        timeout=TIMEOUT,
    )
    order = [
        (e["kind"], e["payload"].get("turnId"))
        for e in client.events
        if e["kind"] in ("turn.started", "turn.done", "turn.queued", "turn.dequeued")
    ]
    assert order.index(("turn.done", turn_id)) < order.index(("turn.started", command_turn))
    await client.ok("session.interrupt", {"sessionId": session_id})
    await client.stop()


async def test_an_idle_command_is_still_a_turn(
    tmp_path: Path, http: aiohttp.ClientSession, daemon: Daemon
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    workdir = tmp_path / "w"
    workdir.mkdir()
    created = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept"})
    session_id = created["sessionId"]
    result = await client.ok("session.prompt", {"sessionId": session_id, "text": "/help"})
    assert result["turnId"].startswith("t-")
    reason = await client.wait_turn(result["turnId"], timeout=TIMEOUT)
    assert reason == "complete"
    assert client.of_kind("command.result") == []
    assert daemon.core.sessions.get(session_id).current_turn is None
    await client.stop()
