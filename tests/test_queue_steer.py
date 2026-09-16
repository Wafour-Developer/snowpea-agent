"""Busy-turn steering: queued follow-ups can steer the running turn."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import Recorder, connect, fake_provider, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers import fake as fake_provider_mod
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall
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


def _has_tool_result(messages: list[Any], name: str) -> bool:
    return any(
        message.role == "tool" and getattr(message, "name", "") == name
        for message in messages
    )


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


async def test_busy_steer_propagates_to_running_child_next_provider_request(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    class DelegateProvider:
        vendor = "test-steer-child"

        def __init__(self) -> None:
            self.child_calls: list[list[ChatMessage]] = []

        async def stream(
            self, messages: list[ChatMessage], tools: list[Any], **kwargs: Any
        ) -> Any:
            del tools, kwargs
            users = "\n".join(_user_texts(messages))
            if "slow child" in users:
                self.child_calls.append(list(messages))
                if not _has_tool_result(messages, "shell"):
                    yield StreamEvent(
                        kind="tool_call",
                        tool_call=ToolCall(
                            id="child_sleep",
                            name="shell",
                            arguments={"command": "sleep 0.3"},
                        ),
                    )
                    yield StreamEvent(kind="done", stop_reason="tool_use")
                    return
                yield StreamEvent(kind="text_delta", text="child done")
                yield StreamEvent(kind="done", stop_reason="end_turn")
                return
            if not _has_tool_result(messages, "delegate_task"):
                yield StreamEvent(
                    kind="tool_call",
                    tool_call=ToolCall(
                        id="delegate_1",
                        name="delegate_task",
                        arguments={"task": "slow child", "agent": "executor"},
                    ),
                )
                yield StreamEvent(kind="done", stop_reason="tool_use")
                return
            yield StreamEvent(kind="text_delta", text="parent done")
            yield StreamEvent(kind="done", stop_reason="end_turn")

    home = tmp_path / "home"
    daemon = await make_daemon(home)
    try:
        core = daemon.core
        assert core is not None
        provider = DelegateProvider()
        core.providers.get = lambda _provider, _model: provider  # type: ignore[assignment]
        workdir = tmp_path / "project"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        recorder = Recorder()
        core.hub.subscribe(recorder, None)

        running_turn = agent_loop.start_turn(core, session, FIRST_PROMPT)
        deadline = asyncio.get_running_loop().time() + TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            child_shell = [
                event
                for event in recorder.of_kind("tool.call")
                if event["sessionId"] != session.id and event["payload"].get("name") == "shell"
            ]
            if child_shell:
                break
            await asyncio.sleep(0.01)
        assert child_shell

        queued_turn = agent_loop.start_turn(core, session, FOLLOWUP_PROMPT)
        assert await asyncio.wait_for(session.turn_task, timeout=TIMEOUT) is None

        child_messages = "\n".join(
            "\n---\n".join(_user_texts(call)) for call in provider.child_calls[1:]
        )
        assert f"[from the user, mid-task] {FOLLOWUP_PROMPT}" in child_messages
        child_user_events = [
            event["payload"]
            for event in recorder.of_kind("message.user")
            if event["sessionId"] != session.id
        ]
        assert any(
            payload.get("steered") is True
            and payload.get("text") == f"[from the user, mid-task] {FOLLOWUP_PROMPT}"
            for payload in child_user_events
        )
        assert any(
            event["payload"].get("turnId") == queued_turn
            and event["payload"].get("reason") == "steered"
            for event in recorder.of_kind("turn.dequeued")
            if event["sessionId"] == session.id
        )
        assert any(
            event["payload"].get("turnId") == running_turn
            and event["payload"].get("reason") == "complete"
            for event in recorder.of_kind("turn.done")
            if event["sessionId"] == session.id
        )
    finally:
        await daemon.stop()


async def test_busy_queue_does_not_propagate_steer_to_running_child(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    class DelegateProvider:
        vendor = "test-steer-child-queue"

        def __init__(self) -> None:
            self.child_calls: list[list[ChatMessage]] = []

        async def stream(
            self, messages: list[ChatMessage], tools: list[Any], **kwargs: Any
        ) -> Any:
            del tools, kwargs
            users = "\n".join(_user_texts(messages))
            if "slow child" in users:
                self.child_calls.append(list(messages))
                if not _has_tool_result(messages, "shell"):
                    yield StreamEvent(
                        kind="tool_call",
                        tool_call=ToolCall(
                            id="child_sleep",
                            name="shell",
                            arguments={"command": "sleep 0.3"},
                        ),
                    )
                    yield StreamEvent(kind="done", stop_reason="tool_use")
                    return
                yield StreamEvent(kind="text_delta", text="child done")
                yield StreamEvent(kind="done", stop_reason="end_turn")
                return
            if not _has_tool_result(messages, "delegate_task"):
                yield StreamEvent(
                    kind="tool_call",
                    tool_call=ToolCall(
                        id="delegate_1",
                        name="delegate_task",
                        arguments={"task": "slow child", "agent": "executor"},
                    ),
                )
                yield StreamEvent(kind="done", stop_reason="tool_use")
                return
            yield StreamEvent(kind="text_delta", text="parent done")
            yield StreamEvent(kind="done", stop_reason="end_turn")

    home = tmp_path / "home"
    daemon = await make_daemon(home)
    try:
        core = daemon.core
        assert core is not None
        core.settings.agent.busy = "queue"
        provider = DelegateProvider()
        core.providers.get = lambda _provider, _model: provider  # type: ignore[assignment]
        workdir = tmp_path / "project"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        recorder = Recorder()
        core.hub.subscribe(recorder, None)

        agent_loop.start_turn(core, session, FIRST_PROMPT)
        deadline = asyncio.get_running_loop().time() + TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            child_shell = [
                event
                for event in recorder.of_kind("tool.call")
                if event["sessionId"] != session.id and event["payload"].get("name") == "shell"
            ]
            if child_shell:
                break
            await asyncio.sleep(0.01)
        assert child_shell

        queued_turn = agent_loop.start_turn(core, session, FOLLOWUP_PROMPT)
        # In queue mode, the queued turn stays queued until the first turn finishes
        assert queued_turn in [q.turn_id for q in session.queued_turns]

        assert await asyncio.wait_for(session.turn_task, timeout=TIMEOUT) is None

        child_messages = "\n".join(
            "\n---\n".join(_user_texts(call)) for call in provider.child_calls
        )
        assert f"[from the user, mid-task] {FOLLOWUP_PROMPT}" not in child_messages
        child_user_events = [
            event["payload"]
            for event in recorder.of_kind("message.user")
            if event["sessionId"] != session.id
        ]
        assert not any(payload.get("steered") is True for payload in child_user_events)
    finally:
        await daemon.stop()


async def test_interrupt_invitation_follows_turn_done(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await _start_session(client, workdir, mode="auto")
    turn_id = await _prompt(client, session_id, FIRST_PROMPT)
    await client.wait(
        lambda e: e["kind"] == "tool.call" and e["payload"].get("name") == "shell",
        timeout=TIMEOUT,
    )

    await client.ok("session.interrupt", {"sessionId": session_id})
    assert await client.wait_turn(turn_id, timeout=TIMEOUT) == "interrupted"
    invitation = await client.wait(
        lambda e: e["kind"] == "message.done"
        and e["payload"].get("kind") == "interrupted",
        timeout=TIMEOUT,
    )
    assert invitation["payload"] == {
        "text": "Interrupted. Tell me what to change — your next message continues this session.",
        "role": "system",
        "truncated": False,
        "continuations": 0,
        "kind": "interrupted",
    }
    turn_index = client.events.index(
        next(
            event
            for event in client.events
            if event["kind"] == "turn.done" and event["payload"].get("turnId") == turn_id
        )
    )
    invite_index = client.events.index(invitation)
    assert invite_index == turn_index + 1
    await client.stop()
