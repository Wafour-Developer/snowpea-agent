"""Only the person's own words become a session's ``user_turn`` (1.22.0).

A browser host shows a payment card only for a request that means to pay, so
text an agent wrote — a queued command, a delegation's announce turn, a
queued ``/ralplan`` — or a schedule or another group member sent must never
pass for the user's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from _support import env_vars

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.fake import FakeProvider
from snowpea_core.server.app_server import Daemon


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    with env_vars(SNOWPEA_PROVIDER=None):
        instance = Daemon(port=0, home=home)
        await instance.start()
        try:
            yield instance
        finally:
            await instance.stop()


async def test_an_agent_started_turn_keeps_the_persons_last_words(
    daemon: Daemon, tmp_path: Path
) -> None:
    core = daemon.core
    assert core is not None
    core.providers.register("local", FakeProvider({"steps": [], "default": {"text": "ok"}}))
    work = tmp_path / "w"
    work.mkdir()
    session = await core.sessions.create(work, mode="auto", provider="local")

    await agent_loop.run_turn(core, session, "쿠팡 장바구니 보여줘")
    first = dict(session.user_turn or {})
    assert first["text"] == "쿠팡 장바구니 보여줘"

    # queue_command / announce / plan gate: the agent's text, not the user's.
    turn_id = agent_loop.start_turn(core, session, "지금 결제해줘", origin="agent")
    await asyncio.wait_for(session.turn_task, 10)
    assert session.turn_origins[turn_id] == "agent"
    assert session.user_turn == first

    for origin in ("schedule", "member"):
        await agent_loop.run_turn(core, session, "결제해줘", origin=origin)
        assert session.user_turn == first

    # The person typing again replaces it.
    turn_id = agent_loop.start_turn(core, session, "그냥 닫아줘")
    await asyncio.wait_for(session.turn_task, 10)
    assert session.user_turn == {"messageId": turn_id, "text": "그냥 닫아줘", "steers": []}


async def test_agent_use_only_from_the_persons_own_turn(daemon: Daemon, tmp_path: Path) -> None:
    """A queued command or another origin cannot switch the session's agent."""
    from snowpea_core.commands.agent_cmd import _use
    from snowpea_core.commands.registry import CommandContext

    core = daemon.core
    assert core is not None
    work = tmp_path / "w"
    work.mkdir()
    session = await core.sessions.create(work, mode="auto")
    said: list[str] = []
    ctx = CommandContext(core=core, session=session, turn_id="t-agent", conn=None)
    ctx.say = lambda text: _record(said, text)  # type: ignore[method-assign]
    agent_loop.note_origin(session, "t-agent", "agent")
    await _use(ctx, "explore")
    assert session.agent is None and "only works when you type it" in said[-1]


async def _record(said: list[str], text: str) -> None:
    said.append(text)


async def test_a_stored_session_with_an_old_system_summary_sends_one_system_message(
    daemon: Daemon, tmp_path: Path
) -> None:
    """Sessions compacted before the fix hold the summary as role=system in
    their saved history; after a restore the request still opens with exactly
    one system message, so they work again with no migration."""
    from snowpea_core.agent.agent import build_messages
    from snowpea_core.providers.base import ChatMessage
    from snowpea_core.providers.normalize import messages_to_openai
    from snowpea_core.session.compaction import summary_message
    from snowpea_core.session.manager import persist_history

    core = daemon.core
    assert core is not None
    work = tmp_path / "w"
    work.mkdir()
    session = await core.sessions.create(work, mode="auto")
    session.history.append(summary_message("earlier: the login page was fixed"))
    session.history.append(ChatMessage(role="user", content="continue"))
    await persist_history(core.store, session)
    session_id = session.id
    await core.sessions.close(session_id)

    restored = await core.sessions.restore(session_id)
    assert restored is not None
    assert any(m.role == "system" for m in restored.history.messages)
    wire = messages_to_openai(build_messages(restored, [], "", core=core))
    assert [m["role"] for m in wire].count("system") == 1 and wire[0]["role"] == "system"
    assert any("the login page was fixed" in str(m["content"]) for m in wire[1:])
