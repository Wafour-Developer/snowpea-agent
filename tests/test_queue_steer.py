"""Busy-turn steering: queued follow-ups can steer the running turn."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, fake_provider, make_daemon

from snowpea_core.providers import fake as fake_provider_mod
from snowpea_core.server.app_server import Daemon

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "steer_queue.json"
TIMEOUT = 15.0
FIRST_PROMPT = "start steer"
FOLLOWUP_PROMPT = "insert this mid-turn"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
    return str(content)


def _user_texts(messages: list[Any]) -> list[str]:
    return [_content_text(message.content) for message in messages if message.role == "user"]


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            await instance.stop()


async def _start_session(client: Any, workdir: Path, mode: str = "auto") -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": mode})
    return str(result["sessionId"])


async def _prompt(client: Any, session_id: str, text: str) -> str:
    result = await client.ok("session.prompt", {"sessionId": session_id, "text": text})
    return str(result["turnId"])


async def test_busy_steer_merges_queued_prompt_into_the_running_turn(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_calls: list[list[Any]] = []
    original_stream = fake_provider_mod.FakeProvider.stream

    async def spying_stream(self: Any, messages: list[Any], tools: list[Any], **kwargs: Any) -> Any:
        captured_calls.append(list(messages))
        async for event in original_stream(self, messages, tools, **kwargs):
            yield event

    monkeypatch.setattr(fake_provider_mod.FakeProvider, "stream", spying_stream)

    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await _start_session(client, workdir, mode="auto")

    running_turn = await _prompt(client, session_id, FIRST_PROMPT)
    await client.wait(
        lambda e: e["kind"] == "tool.call" and e["payload"].get("name") == "shell", timeout=TIMEOUT
    )
    queued_turn = await _prompt(client, session_id, FOLLOWUP_PROMPT)
    await client.wait(
        lambda e: e["kind"] == "turn.queued" and e["payload"].get("turnId") == queued_turn,
        timeout=TIMEOUT,
    )

    assert await client.wait_turn(running_turn, timeout=TIMEOUT) == "complete"

    dequeued = [
        event["payload"]
        for event in client.of_kind("turn.dequeued")
        if event["payload"]["turnId"] == queued_turn
    ]
    assert dequeued and dequeued[0]["reason"] == "steered"
    started_ids = [event["payload"]["turnId"] for event in client.of_kind("turn.started")]
    assert queued_turn not in started_ids

    steered_user = [
        event["payload"]
        for event in client.of_kind("message.user")
        if event["payload"]["text"] == FOLLOWUP_PROMPT
    ]
    assert steered_user and steered_user[0]["steered"] is True

    # The next model call in the same running turn sees the follow-up prompt.
    assert len(captured_calls) >= 2
    assert FOLLOWUP_PROMPT in _user_texts(captured_calls[1])
    assert client.of_kind("message.done")[-1]["payload"]["text"] == "steered instruction observed"
    await client.stop()


async def test_busy_queue_keeps_the_legacy_separate_turn_behavior(
    http: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_calls: list[list[Any]] = []
    original_stream = fake_provider_mod.FakeProvider.stream

    async def spying_stream(self: Any, messages: list[Any], tools: list[Any], **kwargs: Any) -> Any:
        captured_calls.append(list(messages))
        async for event in original_stream(self, messages, tools, **kwargs):
            yield event

    monkeypatch.setattr(fake_provider_mod.FakeProvider, "stream", spying_stream)

    home = tmp_path / "home"
    with fake_provider(FIXTURE):
        daemon = await make_daemon(home, settings={"agent": {"busy": "queue"}})
        try:
            workdir = tmp_path / "project"
            workdir.mkdir()
            client = await connect(http, daemon, timeout=TIMEOUT)
            session_id = await _start_session(client, workdir, mode="auto")

            first_turn = await _prompt(client, session_id, FIRST_PROMPT)
            await client.wait(
                lambda e: e["kind"] == "tool.call" and e["payload"].get("name") == "shell",
                timeout=TIMEOUT,
            )
            queued_turn = await _prompt(client, session_id, FOLLOWUP_PROMPT)
            await client.wait(
                lambda e: e["kind"] == "turn.queued" and e["payload"].get("turnId") == queued_turn,
                timeout=TIMEOUT,
            )

            assert await client.wait_turn(first_turn, timeout=TIMEOUT) == "complete"
            assert await client.wait_turn(queued_turn, timeout=TIMEOUT) == "complete"

            dequeued = [
                event["payload"]
                for event in client.of_kind("turn.dequeued")
                if event["payload"]["turnId"] == queued_turn
            ]
            assert dequeued and dequeued[0]["reason"] == "started"
            started_ids = [event["payload"]["turnId"] for event in client.of_kind("turn.started")]
            assert queued_turn in started_ids

            followup_user = [
                event["payload"]
                for event in client.of_kind("message.user")
                if event["payload"]["text"] == FOLLOWUP_PROMPT
            ]
            assert followup_user and followup_user[0]["steered"] is False

            # While queue mode is on, the running turn's next call does not see it.
            assert len(captured_calls) >= 3
            assert FOLLOWUP_PROMPT not in _user_texts(captured_calls[1])
            assert FOLLOWUP_PROMPT in _user_texts(captured_calls[2])
            await client.stop()
        finally:
            await daemon.stop()
