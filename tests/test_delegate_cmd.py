"""``/delegate <agent> <task>`` and ``$<agent> <task>`` (CORE-us020).

Before this, both paths reached ``agent.spawn`` directly: a subagent started
in the background and, once it finished, nobody turned that into a reply on
the parent session — the child's own transcript was the only place the
answer showed up. Both now rewrite into an ordinary main-agent turn whose
text tells the model to call ``delegate_task`` and report back, so the
model's own reply (with the child's report folded in) is what the parent
session sees. This runs against a real in-process daemon with the
deterministic scripted fake provider (``tests/_support.py``), so the
assertions describe what any client — TUI, SDK, a scheduled job — would
actually see.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect

from snowpea_core.server.app_server import Daemon

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "delegate.json"
TIMEOUT = 20.0


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    instance = Daemon(port=0, home=home)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project


async def start_session(client: RpcClient, workdir: Path) -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept"})
    return str(result["sessionId"])


async def prompt(client: RpcClient, session_id: str, text: str) -> str:
    result = await client.ok("session.prompt", {"sessionId": session_id, "text": text})
    turn_id = str(result["turnId"])
    await client.wait_turn(turn_id, TIMEOUT)
    return turn_id


def _payloads(client: RpcClient, kind: str) -> list[dict]:
    return [event["payload"] for event in client.of_kind(kind)]


# ---------------------------------------------------------------------------
# /delegate <agent> <task>
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_is_listed_in_command_list(
    daemon: Daemon, http: aiohttp.ClientSession
) -> None:
    """``/help`` and completion read ``command.list``; ``delegate`` must be in it."""
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        result = await client.ok("command.list", {})
        names = {c["name"] for c in result["commands"]}
        assert "delegate" in names
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_command_run_delegate_matches_slash_delegate(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """``command.run {name: "delegate", args: ...}`` is the same path as ``/delegate``."""
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, workdir)
        result = await client.ok(
            "command.run",
            {"sessionId": session_id, "name": "delegate", "args": "executor do the thing"},
        )
        await client.wait_turn(str(result["turnId"]), TIMEOUT)

        calls = [p for p in _payloads(client, "tool.call") if p["name"] == "delegate_task"]
        assert calls, f"delegate_task was never called; saw {client.kinds()}"
        assert _payloads(client, "subagent.spawn"), "the child never actually spawned"
        done = _payloads(client, "message.done")
        assert done and "task complete" in done[-1]["text"]
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_delegate_command_calls_delegate_task_and_the_parent_answers(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, workdir)
        await prompt(client, session_id, "/delegate executor do the thing")

        calls = [p for p in _payloads(client, "tool.call") if p["name"] == "delegate_task"]
        assert calls, f"delegate_task was never called; saw {client.kinds()}"
        assert calls[0]["args"]["agent"] == "executor"

        results = [p for p in _payloads(client, "tool.result") if p["name"] == "delegate_task"]
        assert results and results[0]["ok"] is True

        # The bug this fixes: the MAIN session gets a real reply, not silence.
        done = _payloads(client, "message.done")
        assert done, "the parent session never answered the user"
        assert "task complete" in done[-1]["text"]

        assert _payloads(client, "subagent.done"), "the child still runs and finishes as usual"
        assert _payloads(client, "turn.done")[-1]["reason"] == "complete"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_delegate_command_unknown_agent_lists_available_names(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, workdir)
        await prompt(client, session_id, "/delegate nobody-defined-this do the thing")

        # Refused before any model call — no tool call, no subagent spawned.
        assert not [p for p in _payloads(client, "tool.call") if p["name"] == "delegate_task"]
        assert not _payloads(client, "subagent.spawn")

        done = _payloads(client, "message.done")
        assert done, "an unknown agent must still answer, not crash the turn"
        text = done[-1]["text"]
        assert "unknown agent" in text
        assert "nobody-defined-this" in text
        assert "executor" in text  # a builtin name is offered as an alternative

        assert _payloads(client, "turn.done")[-1]["reason"] == "complete"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_delegate_command_usage_without_a_task(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, workdir)
        await prompt(client, session_id, "/delegate executor")
        done = _payloads(client, "message.done")
        assert done and "Usage" in done[-1]["text"]
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# $<agent> <task>
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dollar_shorthand_takes_the_same_path_as_slash_delegate(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, workdir)
        await prompt(client, session_id, "$executor do the thing")

        calls = [p for p in _payloads(client, "tool.call") if p["name"] == "delegate_task"]
        assert calls, f"delegate_task was never called; saw {client.kinds()}"
        assert calls[0]["args"]["agent"] == "executor"
        assert _payloads(client, "subagent.spawn"), "the child must actually spawn, not just chat"

        done = _payloads(client, "message.done")
        assert done, "the parent session never answered the user"
        assert "task complete" in done[-1]["text"]
        assert _payloads(client, "turn.done")[-1]["reason"] == "complete"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_dollar_without_a_space_is_plain_text_not_a_delegation(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """``$5`` in ordinary chat must not be mistaken for a delegation prefix."""
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, workdir)
        await prompt(client, session_id, "$5")
        assert not [p for p in _payloads(client, "tool.call") if p["name"] == "delegate_task"]
        assert _payloads(client, "turn.done")[-1]["reason"] == "complete"
    finally:
        await client.stop()
