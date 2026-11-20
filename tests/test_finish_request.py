"""Feedback e7d5155b: "중간에 원치 않게 중단되었어".

The person wrote "끝까지 진행해줘".  At the tool-round budget the probe told
the model "if it is not finished, do not call a tool: report…", the model did
exactly that ("The work is not finished. Here is an honest checkpoint."), and
the loop read "no tool call" as "done": the turn ended ``complete``.  The
reply was in English, too — after a compaction the note quoted a picture a
tool had shown as the person's latest message, and every note after it was
English.

Now the probe asks for a ``BUDGET-STATUS`` verdict, an unfinished checkpoint
that waits on nobody renews the budget, a "finish it" turn gets a generous cap,
and every daemon note after the person's words restates their language.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _support import env_vars

from snowpea_core.agent import loop as agent_loop
from snowpea_core.agent import person_intent, turn_stopping, unfinished_work
from snowpea_core.providers.base import ChatMessage
from snowpea_core.providers.fake import FakeProvider
from snowpea_core.server.app_server import Daemon
from snowpea_core.session import compaction
from snowpea_core.session.history import source as message_source

FINISH_PROMPT = "파일 세 번 써줘. 끝까지 진행해줘"
CHECKPOINT = (
    "The work is not finished. Here is an honest checkpoint.\n\n"
    "I wrote a.txt and b.txt; c.txt is still missing.\n"
    "BUDGET-STATUS: UNFINISHED"
)
BLOCKED = (
    "The work is not finished: the upload needs your API key, which I do not have. "
    "Please provide it.\nBUDGET-STATUS: UNFINISHED"
)


def _write(name: str, text: str = "") -> dict[str, Any]:
    return {
        "text": text,
        "tool_calls": [{"name": "write_file", "arguments": {"path": name, "content": "x\n"}}],
    }


def _script(probe_text: str) -> dict[str, Any]:
    return {
        "steps": [
            {"match": "세 번 써줘", **_write("a.txt", "하나")},
            {"match": "used the tool budget", "text": probe_text},
            {"match": "continue the work", **_write("c.txt", "셋")},
            {"after_tool": "write_file", **_write("b.txt", "둘")},
            {"after_tool": "write_file", "text": "세 파일을 모두 썼습니다."},
        ],
        "default": {"text": "unexpected"},
    }


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
    core.settings.agent.max_tool_rounds = 2
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    return await core.sessions.create(work, mode="auto", provider="local")


class _Events:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "session.event":
            self.events.append((params["kind"], params["payload"]))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [payload for k, payload in self.events if k == kind]


class _Session:
    """Just what :mod:`person_intent` reads."""

    def __init__(self, text: str, steers: list[str] | None = None) -> None:
        self.user_turn = {"messageId": "t", "text": text, "steers": steers or []}
        self.is_subagent = False
        self.locale = None


# ---------------------------------------------------------------------------
# the readings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "steers", "expected"),
    [
        ("MAke A Character도 제대로 진행해", ["끝까지 진행해줘"], True),
        ("마무리까지 해줘", [], True),
        ("Please finish it, keep going until it's done", [], True),
        ("carry this through to the end", [], True),
        ("잘 진행되고 있어..?", [], False),
        ("what does this function do?", [], False),
    ],
)
def test_finish_requests_are_read_from_the_persons_own_words(
    text: str, steers: list[str], expected: bool
) -> None:
    assert person_intent.finish_requested(_Session(text, steers)) is expected


def test_a_delegated_child_never_counts_as_a_finish_request() -> None:
    child = _Session("끝까지 진행해줘")
    child.is_subagent = True
    assert person_intent.finish_requested(child) is False


def test_the_language_note_follows_the_person_not_the_notes() -> None:
    korean = _Session("이미지로 아바타 만들어줘", ["keep going"])
    # The newest words are English, so English it is; then the Korean ones.
    assert person_intent.language_note(None, korean) == ""
    korean.user_turn["steers"] = ["끝까지 진행해줘"]
    note = person_intent.language_note(None, korean)
    assert "Korean" in note
    assert person_intent.with_language(None, korean, "[system] Verify.") == (
        f"[system] Verify.\n{note}"
    )
    assert person_intent.language_note(None, _Session("make an avatar")) == ""


@pytest.mark.parametrize(
    ("text", "checkpoint"),
    [
        ("Done: all three files are written.\nBUDGET-STATUS: DONE", False),
        (CHECKPOINT, True),
        # No verdict line: the wording decides (the e7d5155b reply verbatim).
        ("The work is not finished. Here is an honest checkpoint.", True),
        ("작업은 아직 끝나지 않았습니다. 지금까지 한 일을 정리합니다.", True),
        ("All three files are written.", False),
        # Waits on the person: never run past.
        (BLOCKED, False),
    ],
)
def test_a_tool_less_probe_reply_is_a_checkpoint_only_when_it_says_so(
    text: str, checkpoint: bool
) -> None:
    body, verdict = agent_loop.split_budget_status(text)
    assert agent_loop._probe_is_checkpoint(body, verdict) is checkpoint


def test_the_probe_asks_for_a_verdict() -> None:
    assert "BUDGET-STATUS: UNFINISHED" in agent_loop.BUDGET_PROBE_INSTRUCTION
    assert "used the tool budget" in agent_loop.BUDGET_PROBE_INSTRUCTION
    assert unfinished_work.detect("The work is not finished. Here is an honest checkpoint.")


async def test_a_finish_request_raises_the_continuation_caps(
    daemon: Daemon, tmp_path: Path
) -> None:
    session = await _session(daemon, tmp_path, FakeProvider())
    core = daemon.core
    core.settings.agent.auto_budget_continuations = 2
    session.user_turn = {"messageId": "t", "text": "고쳐줘", "steers": []}
    assert agent_loop._auto_continue_limit(core, session) == 2
    session.user_turn["steers"].append("끝까지 진행해줘")
    assert agent_loop._auto_continue_limit(core, session) == 30
    # Zero still means no automatic renewals at all.
    core.settings.agent.auto_budget_continuations = 0
    assert agent_loop._auto_continue_limit(core, session) == 0


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


async def test_finish_it_and_an_unfinished_checkpoint_keeps_the_turn_going(
    daemon: Daemon, tmp_path: Path
) -> None:
    provider = FakeProvider(_script(CHECKPOINT))
    session = await _session(daemon, tmp_path, provider)
    daemon.core.settings.agent.auto_budget_continuations = 1
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(daemon.core, session, FINISH_PROMPT)

    assert [p["reason"] for p in sink.of("turn.done")] == ["complete"]
    assert sorted(p.name for p in session.workdir.glob("*.txt")) == ["a.txt", "b.txt", "c.txt"]
    said = [p["text"] for p in sink.of("message.done")]
    # The checkpoint is shown without its verdict line, then the work goes on.
    assert said[0].startswith("The work is not finished.")
    assert "BUDGET-STATUS" not in said[0]
    assert said[-1] == "세 파일을 모두 썼습니다."
    budget = [c for c in sink.of("hook.continue") if c["kind"] == "budget"]
    assert [(c["count"], c["limit"]) for c in budget] == [(1, 30)]
    # The renewal note restates the person's language.
    renew = [
        m.content
        for m in session.history.messages
        if m.role == "user" and (m.source or {}).get("producer") == "budget"
    ]
    assert len(renew) == 1 and "Korean" in str(renew[0])


async def test_a_checkpoint_that_needs_the_person_still_stops(
    daemon: Daemon, tmp_path: Path
) -> None:
    provider = FakeProvider(_script(BLOCKED))
    session = await _session(daemon, tmp_path, provider)
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(daemon.core, session, FINISH_PROMPT)

    assert [p["reason"] for p in sink.of("turn.done")] == ["complete"]
    assert sorted(p.name for p in session.workdir.glob("*.txt")) == ["a.txt", "b.txt"]
    assert [p["text"] for p in sink.of("message.done")] == [BLOCKED.rsplit("\n", 1)[0]]
    assert [c for c in sink.of("hook.continue") if c["kind"] == "budget"] == []


async def test_language_stays_korean_after_compaction_and_a_hook_nudge(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(
        {
            "steps": [
                {"match": "세 번 써줘", **_write("a.txt", "하나")},
                {"after_tool": "write_file", "text": "다 했습니다."},
                {"match": "check it now", "text": "확인했습니다. 다 했습니다."},
            ],
            "default": {"text": "unexpected"},
        }
    )
    session = await _session(daemon, tmp_path, provider)
    daemon.core.settings.agent.max_tool_rounds = 50
    # The incident: a picture a tool showed sat after the person's words, with
    # no source (stored before tool images carried one), plus a tagged one.
    session.history.append(ChatMessage(role="user", content="(images from view_image: old.png)"))
    session.history.append(
        ChatMessage(
            role="user",
            content="(images from view_image: anime_female.png)",
            source=message_source("tool-image", "snapshot"),
        )
    )

    async def fake_summary(*_: Any, **__: Any) -> str:
        return "Earlier work: an English summary of everything."

    monkeypatch.setattr(compaction, "summarise", fake_summary)
    real_maybe = compaction.maybe_auto_compact

    async def compact_on_round_two(core: Any, sess: Any) -> Any:
        if sess.rounds_used == 2:
            return await compaction.compact_session(core, sess, auto=True)
        return await real_maybe(core, sess)

    monkeypatch.setattr(compaction, "maybe_auto_compact", compact_on_round_two)
    nudges = iter(["[system] You changed code but have no fresh evidence; check it now."])
    monkeypatch.setattr(agent_loop.verify_gate, "nudge", lambda *_: next(nudges, None))
    sink = _Events()
    daemon.core.hub.subscribe(sink, session.id)

    await agent_loop.run_turn(daemon.core, session, FINISH_PROMPT)

    assert [p["reason"] for p in sink.of("turn.done")] == ["complete"]
    assert sink.of("compaction")
    notes = {
        (m.source or {}).get("producer"): str(m.content)
        for m in session.history.messages
        if m.role == "user" and m.source
    }
    # After the compaction: the person's words are quoted, never the picture.
    assert f"«{FINISH_PROMPT}»" in notes["compaction"]
    assert "view_image" not in notes["compaction"]
    assert "Korean" in notes["compaction"]
    # After the verify-gate nudge.
    assert "Korean" in notes["verify-gate"]
    # The surfaces still show the nudge as it was written.
    assert [c["reason"] for c in sink.of("hook.continue")] == [
        "[system] You changed code but have no fresh evidence; check it now."
    ]
    # The summary's verbatim list holds the person's words, not the pictures.
    assert compaction.latest_user_words(session.history.snapshot()) == FINISH_PROMPT


def test_a_stop_decision_note_carries_the_language(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Fake(_Session):
        def __init__(self) -> None:
            super().__init__("끝까지 진행해줘")
            self.pending_notices: list[str] = []

    session = _Fake()
    decision = turn_stopping.StopDecision("Stop hook: run tests", "stop-hook", via="notice")
    agent_loop._steer_after_stop(session, decision, None)  # type: ignore[arg-type]
    assert session.pending_notices[0].startswith("Stop hook: run tests\n")
    assert "Korean" in session.pending_notices[0]
