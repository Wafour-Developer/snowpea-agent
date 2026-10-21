"""An answer with no text and no tool call is not a completion.

A reasoning model cut off mid-thought (reasoning only, out=171 of which 170
reasoning) used to end the turn ``complete`` with an empty message.  It is
asked once more with a short nudge appended — the history before it is left
as sent, so the server's prefix cache still matches — and a second empty
answer ends the turn ``empty_response`` with an error a surface can show.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio
from _support import env_vars

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.fake import FakeProvider
from snowpea_core.server.app_server import Daemon

THOUGHT = {"reasoning": "The user wants a plan. Project", "reasoningTokens": 170}


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


async def _session(daemon: Daemon, tmp_path: Path, script: dict[str, Any]) -> Any:
    core = daemon.core
    assert core is not None
    core.providers.register("local", FakeProvider(script))
    work = tmp_path / "w"
    work.mkdir()
    return await core.sessions.create(work, mode="auto", provider="local")


class _Events:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "session.event":
            self.events.append((params["kind"], params["payload"]))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [payload for k, payload in self.events if k == kind]


async def test_an_empty_answer_is_nudged_once_and_the_prefix_is_kept(
    daemon: Daemon, tmp_path: Path
) -> None:
    script = {
        "steps": [
            {"match": agent_loop.EMPTY_NUDGE, "text": "Here is the plan."},
            {"match": "plan my trip", **THOUGHT},
        ]
    }
    session = await _session(daemon, tmp_path, script)
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    reason = await agent_loop.run_turn(daemon.core, session, "plan my trip")
    turn_id = session.turn_origins and next(reversed(session.turn_origins))
    assert turn_id
    roles = [(m.role, m.content) for m in session.history.messages]
    # Appended after the prompt, nothing rewritten, and no empty assistant turn.
    assert roles[-3:] == [
        ("user", "plan my trip"),
        ("user", agent_loop.EMPTY_NUDGE),
        ("assistant", "Here is the plan."),
    ]
    assert [p["reason"] for p in sink.of("turn.done")] == ["complete"]
    assert [p["text"] for p in sink.of("message.done")] == ["Here is the plan."]
    assert reason


async def test_a_second_empty_answer_ends_the_turn_empty_response(
    daemon: Daemon, tmp_path: Path
) -> None:
    session = await _session(daemon, tmp_path, {"steps": [], "default": THOUGHT})
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(daemon.core, session, "plan my trip")
    assert [p["reason"] for p in sink.of("turn.done")] == ["empty_response"]
    assert [p["code"] for p in sink.of("error")] == ["empty_response"]
    assert sink.of("message.done") == []
    assert [m.content for m in session.history.messages if m.role == "assistant"] == []
