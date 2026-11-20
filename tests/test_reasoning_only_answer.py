"""A reasoning model that answers only in its reasoning must not end the turn empty.

Feedback 4ae83ca4: right after a mid-turn compaction, a Qwen/DeepSeek-style
model wrote its whole answer ("Answer: Yes — web2 is a static page…") into the
reasoning and nothing visible, twice, and the turn ended ``empty_response``.

Now: the model is told plainly what happened and asked again (up to twice) with
thinking off; if it still writes nothing, the reasoning is shown under a marker
line (``message.done`` ``reasoningFallback``, 1.70.0).  A truly empty step keeps
the old single retry.  After a mid-turn compaction the person's newest words are
quoted in a note, and stay in the summary's verbatim list.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _support import env_vars

from snowpea_core.agent import loop as agent_loop
from snowpea_core.providers.base import ChatMessage, StreamEvent, Usage
from snowpea_core.providers.fake import FakeProvider
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION
from snowpea_core.session import compaction

QUESTION = "테스트 어떻게 해보면 되..? 웹으로 해보면 되..?"
ANSWER_THOUGHT = (
    "User asks: How do I test it, on the web? Answer: Yes — web2 is a static page, "
    "open web2/index.html in a browser and click through the booking form."
)
THINKING = {"reasoning": ANSWER_THOUGHT, "reasoningTokens": 186}


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


async def _session(daemon: Daemon, tmp_path: Path, provider: Any) -> Any:
    core = daemon.core
    assert core is not None
    core.providers.register("local", provider)
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    (work / "a.txt").write_text("hello\n", encoding="utf-8")
    return await core.sessions.create(work, mode="auto", provider="local")


class _Events:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "session.event":
            self.events.append((params["kind"], params["payload"]))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [payload for k, payload in self.events if k == kind]


def test_protocol_advertises_the_fallback() -> None:
    assert PROTOCOL_VERSION == "1.70.0"
    assert "reasoningOnlyFallback" in CAPABILITIES


async def test_reasoning_only_twice_then_text_continues_with_thinking_off(
    daemon: Daemon, tmp_path: Path
) -> None:
    provider = FakeProvider(
        {
            "steps": [
                {"match": "웹으로", **THINKING},
                {"match": agent_loop.REASONING_ONLY_NUDGE, **THINKING},
                {
                    "match": agent_loop.REASONING_ONLY_NUDGE,
                    "text": "네, 웹으로 열어 보시면 됩니다.",
                },
            ]
        }
    )
    session = await _session(daemon, tmp_path, provider)
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(daemon.core, session, QUESTION)
    assert [p["reason"] for p in sink.of("turn.done")] == ["complete"]
    assert [p["text"] for p in sink.of("message.done")] == ["네, 웹으로 열어 보시면 됩니다."]
    assert sink.of("error") == []
    conts = sink.of("hook.continue")
    assert [(c["count"], c["limit"]) for c in conts] == [(1, 2), (2, 2)]
    assert all(c["reason"] == agent_loop.REASONING_ONLY_NUDGE for c in conts)
    # The first call thinks; both retries ask with thinking off.
    assert [thinking for _, thinking in provider.calls] == ["on", "off", "off"]


async def test_reasoning_only_every_time_ends_with_the_marked_reasoning(
    daemon: Daemon, tmp_path: Path
) -> None:
    provider = FakeProvider({"steps": [], "default": THINKING})
    session = await _session(daemon, tmp_path, provider)
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(daemon.core, session, QUESTION)
    assert [p["reason"] for p in sink.of("turn.done")] == ["complete"]
    assert sink.of("error") == []
    (done,) = sink.of("message.done")
    assert done["reasoningFallback"] is True
    # The person wrote Korean: the marker follows; the reasoning stays as it was.
    assert done["text"].startswith(agent_loop.REASONING_FALLBACK_MARKERS["ko"])
    assert done["text"].endswith(ANSWER_THOUGHT)
    assert session.history.messages[-1].role == "assistant"
    assert session.history.messages[-1].content == done["text"]
    assert len(provider.calls) == 1 + agent_loop.REASONING_ONLY_RETRIES


async def test_the_fallback_keeps_the_end_of_a_long_reasoning(
    daemon: Daemon, tmp_path: Path
) -> None:
    long_thought = "x" * 9000 + " Answer: open it in a browser."
    provider = FakeProvider({"steps": [], "default": {"reasoning": long_thought}})
    session = await _session(daemon, tmp_path, provider)
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(daemon.core, session, "how do I test it?")
    (done,) = sink.of("message.done")
    assert done["text"].startswith(agent_loop.REASONING_FALLBACK_MARKERS["en"])
    assert done["text"].endswith("Answer: open it in a browser.")
    body = done["text"].split("\n\n", 1)[1]
    assert len(body) <= agent_loop.REASONING_FALLBACK_MAX_CHARS


async def test_a_truly_empty_answer_keeps_the_old_behaviour(daemon: Daemon, tmp_path: Path) -> None:
    provider = FakeProvider({"steps": [], "default": {"text": ""}})
    session = await _session(daemon, tmp_path, provider)
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(daemon.core, session, QUESTION)
    assert [p["reason"] for p in sink.of("turn.done")] == ["empty_response"]
    assert [p["code"] for p in sink.of("error")] == ["empty_response"]
    assert sink.of("message.done") == []
    (cont,) = sink.of("hook.continue")
    assert (cont["reason"], cont["limit"]) == (agent_loop.EMPTY_NUDGE, agent_loop.EMPTY_RETRIES)
    # No reasoning was seen: nothing asks for thinking off.
    assert [thinking for _, thinking in provider.calls] == ["on", "on"]


class _EffortOnly:
    """No thinking switch, an effort tier: the retry asks for the lowest effort."""

    vendor = "local"
    model = "m"
    supports_thinking_option = False
    supports_effort_option = True

    def __init__(self) -> None:
        self.efforts: list[str | None] = []

    async def stream(self, messages: list[Any], tools: list[Any], **kw: Any) -> Any:
        self.efforts.append(kw.get("effort"))
        if len(self.efforts) == 1:
            yield StreamEvent(kind="reasoning_delta", text=ANSWER_THOUGHT)
        else:
            yield StreamEvent(kind="text_delta", text="done")
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=10, output_tokens=5))
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def test_a_provider_without_a_thinking_switch_is_retried_at_low_effort(
    daemon: Daemon, tmp_path: Path
) -> None:
    provider = _EffortOnly()
    session = await _session(daemon, tmp_path, provider)
    await agent_loop.run_turn(daemon.core, session, "how do I test it?")
    assert len(provider.efforts) == 2
    assert provider.efforts[1] == "low"


def _seed(session: Any, pairs: int = 10) -> None:
    for n in range(pairs):
        session.history.append(ChatMessage(role="user", content=f"old question {n}"))
        session.history.append(ChatMessage(role="assistant", content=f"old reply {n} " * 40))


async def _fake_summary(*_: Any, **__: Any) -> str:
    return "Earlier work: built web2."


async def test_compaction_keeps_the_newest_question_as_the_last_user_turn(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _session(daemon, tmp_path, FakeProvider())
    _seed(session)
    session.history.append(ChatMessage(role="user", content=QUESTION))
    monkeypatch.setattr(compaction, "summarise", _fake_summary)

    result = await compaction.compact_session(daemon.core, session, auto=True)
    assert result.compacted
    messages = session.history.messages
    assert messages[0].role == "system"
    # The rebuilt history ends with the person's latest message, as a user turn.
    assert (messages[-1].role, messages[-1].content) == ("user", QUESTION)
    assert compaction.latest_user_words(messages) == QUESTION


async def test_a_question_summarised_away_is_the_newest_verbatim_entry(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tool rounds after the question push it out of the kept tail: the
    verbatim list then carries it, newest first."""
    session = await _session(daemon, tmp_path, FakeProvider())
    _seed(session)
    session.history.append(ChatMessage(role="user", content=QUESTION))
    for n in range(4):
        session.history.append(ChatMessage(role="assistant", content=f"checking {n}"))
    monkeypatch.setattr(compaction, "summarise", _fake_summary)

    await compaction.compact_session(daemon.core, session, auto=True)
    messages = session.history.messages
    assert QUESTION not in [m.content for m in messages[1:]]
    block = str(messages[0].content).split(compaction.VERBATIM_HEADING, 1)[1]
    assert block.split("\n» ")[1].strip() == QUESTION  # newest first
    assert compaction.latest_user_words(messages) == QUESTION


def test_the_newest_words_stay_clipped_when_they_exceed_the_budget() -> None:
    older = ChatMessage(role="user", content="an older question")
    newest = ChatMessage(role="user", content="q" * 3000)
    text = compaction.verbatim_section([older, newest], budget=100)
    assert "\n» " + "q" * 99 + "…" in text
    assert "an older question" not in text


async def test_a_reasoning_only_step_after_compaction_is_nudged_with_the_question(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(
        {
            "steps": [
                {
                    "match": "웹으로",
                    "tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}],
                },
                {"match": "context was compacted", **THINKING},
                {"match": agent_loop.REASONING_ONLY_NUDGE, "text": "웹으로 열어 보세요."},
            ],
            "default": {"text": "unexpected"},
        }
    )
    session = await _session(daemon, tmp_path, provider)
    _seed(session)
    monkeypatch.setattr(compaction, "summarise", _fake_summary)
    real_maybe = compaction.maybe_auto_compact

    async def compact_on_round_two(core: Any, sess: Any) -> Any:
        if sess.rounds_used == 2:
            return await compaction.compact_session(core, sess, auto=True)
        return await real_maybe(core, sess)

    monkeypatch.setattr(compaction, "maybe_auto_compact", compact_on_round_two)
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(daemon.core, session, QUESTION)
    assert [p["reason"] for p in sink.of("turn.done")] == ["complete"]
    assert sink.of("compaction")
    assert [p["text"] for p in sink.of("message.done") if p["role"] == "assistant"] == [
        "웹으로 열어 보세요."
    ]
    note = agent_loop.COMPACTED_NOTE_WORDS.format(words=QUESTION)
    contents = [m.content for m in session.history.messages if m.role == "user"]
    assert note in contents
    # The nudge after the reasoning-only step repeats the note, then the plain ask.
    (cont,) = sink.of("hook.continue")
    assert cont["reason"] == f"{note}\n{agent_loop.REASONING_ONLY_NUDGE}"
    assert [thinking for _, thinking in provider.calls][-1] == "off"
