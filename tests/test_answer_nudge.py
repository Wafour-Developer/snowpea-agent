"""Answer-now note for a person's read-only question turn (agent/answer_nudge.py, 0.2.91)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from _support import Recorder, make_daemon

from snowpea_core.agent import answer_nudge
from snowpea_core.agent import loop as agent_loop
from snowpea_core.config.settings import AgentSettings
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage
from snowpea_core.server.settings_leaves import PLAIN
from snowpea_core.session.history import message_text
from snowpea_core.session.session import Session

QUESTION = "관리자 화면에서 월이 변경될 때 출석 체크 상품을 어떻게 등록해야 하는지 알려줘"
ANSWER = "So the answer: register it under the monthly product screen."


class _Reader:
    """Reads (read_file / grep, alternating, each on a new target) until it is told
    to answer, or until ``max_calls``; ``first`` replaces the opening call."""

    vendor = "fake"

    def __init__(self, max_calls: int = 30, first: ToolCall | None = None) -> None:
        self.max_calls = max_calls
        self.first = first
        self.requests: list[list[ChatMessage]] = []

    def _told(self, messages: list[ChatMessage]) -> bool:
        return any("without changing anything" in message_text(m) for m in messages)

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.requests.append(list(messages))
        n = len(self.requests)
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=50, output_tokens=3))
        if self._told(messages) or n > self.max_calls:
            yield StreamEvent(kind="text_delta", text=ANSWER)
            yield StreamEvent(kind="done", stop_reason="end_turn")
            return
        if n == 1 and self.first is not None:
            call = self.first
        elif n % 2:
            call = ToolCall(id=f"c{n}", name="read_file", arguments={"path": f"f{n}.txt"})
        else:
            call = ToolCall(id=f"c{n}", name="grep", arguments={"pattern": f"word{n}"})
        yield StreamEvent(kind="tool_call", tool_call=call)
        yield StreamEvent(kind="done", stop_reason="tool_use")


async def _run(
    tmp_path: Path,
    provider: _Reader,
    *,
    settings: dict[str, Any] | None = None,
    origin: str | None = None,
    mode: str = "auto",
) -> tuple[Recorder, list[dict[str, Any]], Session]:
    daemon = await make_daemon(tmp_path / "home", settings)
    try:
        core = daemon.core
        core.providers.get = lambda _p, _m: provider
        workdir = tmp_path / "w"
        workdir.mkdir()
        for i in range(40):
            (workdir / f"f{i}.txt").write_text(f"word{i}\n")
        session = await core.sessions.create(workdir, mode=mode)
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)
        agent_loop.start_turn(core, session, QUESTION, origin=origin)
        await asyncio.wait_for(session.turn_task, timeout=60)
        stored = await core.store.events_after(session.id, 0)
        return recorder, stored, session
    finally:
        await daemon.stop()


def _notes(recorder: Recorder) -> list[dict[str, Any]]:
    return [
        e["payload"]
        for e in recorder.of_kind("hook.continue")
        if "without changing anything" in str(e["payload"].get("reason"))
    ]


async def test_fires_once_at_round_20_for_a_read_only_question(tmp_path: Path) -> None:
    provider = _Reader()
    recorder, stored, session = await _run(tmp_path, provider)
    notes = _notes(recorder)
    assert len(notes) == 1
    assert notes[0]["kind"] == "other" and notes[0]["count"] == 1 and notes[0]["limit"] == 1
    assert notes[0]["reason"] == answer_nudge.NOTE.format(n=20)
    # Twenty tool rounds ran, then the 21st request carries the note, last.
    assert len(provider.requests) == 21
    assert not any(provider._told(r) for r in provider.requests[:20])
    assert "answer it now" in message_text(provider.requests[20][-1])
    assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "complete"
    assert ANSWER in recorder.texts()
    # Kept in the session's event log (exports) and in its history as a notice.
    assert any(
        e["kind"] == "hook.continue" and "without changing anything" in str(e["payload"])
        for e in stored
    )
    sources = [m.source for m in session.history.snapshot() if m.source]
    assert {"producer": "answer-nudge", "form": "notice"} in sources


async def test_never_twice_in_one_turn(tmp_path: Path) -> None:
    class _Stubborn(_Reader):
        def _told(self, messages: list[ChatMessage]) -> bool:
            return False  # ignores the note and keeps reading

    provider = _Stubborn(max_calls=45)
    recorder, _stored, _session = await _run(tmp_path, provider)
    assert len(_notes(recorder)) == 1
    assert len(provider.requests) == 46


async def test_not_after_a_write(tmp_path: Path) -> None:
    write = ToolCall(id="w0", name="write_file", arguments={"path": "new.py", "content": "x=1\n"})
    provider = _Reader(max_calls=25, first=write)
    recorder, _stored, _session = await _run(tmp_path, provider)
    assert _notes(recorder) == []
    # 26 rounds ran (plus the verify gate's own follow-ups for the .py edit).
    assert len(provider.requests) >= 26


async def test_not_after_a_mutating_shell_command(tmp_path: Path) -> None:
    touch = ToolCall(id="s0", name="shell", arguments={"command": "touch made.txt"})
    provider = _Reader(max_calls=25, first=touch)
    recorder, _stored, _session = await _run(tmp_path, provider)
    assert _notes(recorder) == []


async def test_a_read_only_shell_command_still_counts_as_reading(tmp_path: Path) -> None:
    look = ToolCall(id="s0", name="shell", arguments={"command": "ls"})
    provider = _Reader(first=look)
    recorder, _stored, _session = await _run(tmp_path, provider)
    assert len(_notes(recorder)) == 1


async def test_not_for_a_scheduled_turn(tmp_path: Path) -> None:
    provider = _Reader(max_calls=25)
    recorder, _stored, _session = await _run(tmp_path, provider, origin="schedule")
    assert _notes(recorder) == []


async def test_not_for_a_jarvis_turn(tmp_path: Path) -> None:
    provider = _Reader(max_calls=25)
    recorder, _stored, _session = await _run(tmp_path, provider, origin="jarvis")
    assert _notes(recorder) == []


async def test_setting_zero_turns_it_off(tmp_path: Path) -> None:
    provider = _Reader(max_calls=25)
    recorder, _stored, _session = await _run(
        tmp_path, provider, settings={"agent": {"answerNudgeRounds": 0}}
    )
    assert _notes(recorder) == []


async def test_the_threshold_follows_the_setting(tmp_path: Path) -> None:
    provider = _Reader()
    recorder, _stored, _session = await _run(
        tmp_path, provider, settings={"agent": {"answerNudgeRounds": 5}}
    )
    assert [n["reason"] for n in _notes(recorder)] == [answer_nudge.NOTE.format(n=5)]
    assert len(provider.requests) == 6


# ---------------------------------------------------------------------------
# eligibility, without a loop
# ---------------------------------------------------------------------------


def _core(rounds: int = 20, jarvis: Any = None) -> SimpleNamespace:
    agent = SimpleNamespace(answerNudgeRounds=rounds)
    return SimpleNamespace(settings=SimpleNamespace(agent=agent), jarvis=jarvis)


def _armed(core: Any, session: Session, **kw: Any) -> bool:
    answer_nudge.begin_turn(
        core, session, "t1", unattended=kw.get("unattended", False), system=kw.get("system", False)
    )
    return bool(session.answer_nudge.rounds)


def test_eligibility(tmp_path: Path) -> None:
    def session(**kw: Any) -> Session:
        return Session(id="s1", workdir=tmp_path, **kw)

    assert _armed(_core(), session())
    assert not _armed(_core(), session(is_subagent=True))
    assert not _armed(_core(), session(mode="plan"))
    assert not _armed(_core(), session(origin_surface="gateway:telegram"))
    assert not _armed(_core(), session(), unattended=True)
    assert not _armed(_core(), session(), system=True)
    assert not _armed(_core(0), session())
    member = session()
    member.turn_origins["t1"] = "member"
    assert not _armed(_core(), member)
    family = SimpleNamespace(in_jarvis_family=lambda _s: True)
    assert not _armed(_core(jarvis=family), session())
    outsider = SimpleNamespace(in_jarvis_family=lambda _s: False)
    assert _armed(_core(jarvis=outsider), session())


def test_due_only_after_the_rounds_and_only_reads(tmp_path: Path) -> None:
    s = Session(id="s1", workdir=tmp_path)
    _armed(_core(3), s)
    for name, tag, args in (
        ("read_file", "read", {}),
        ("web_search", "network", {}),
        ("delegate_task", "delegate", {"readOnly": True}),
        ("shell", "exec", {"command": "git status"}),
    ):
        answer_nudge.observe_call(s, name, tag, args)
    s.rounds_used = 2
    assert answer_nudge.take_due(s) is None
    s.rounds_used = 3
    assert answer_nudge.take_due(s) == answer_nudge.NOTE.format(n=3)
    s.rounds_used = 4
    assert answer_nudge.take_due(s) is None  # once

    for name, tag, args in (
        ("patch", "write", {}),
        ("browser_click", "network", {}),
        ("delegate_task", "delegate", {}),
        ("set_mode", "read", {}),
        ("shell", "exec", {"command": "ls", "background": True}),
        ("execute_code", "exec", {}),
    ):
        t = Session(id="s2", workdir=tmp_path)
        _armed(_core(1), t)
        answer_nudge.observe_call(t, name, tag, args)
        t.rounds_used = 5
        assert answer_nudge.take_due(t) is None, name


def test_setting_default_and_classification() -> None:
    assert AgentSettings().answerNudgeRounds == 20
    assert "answerNudgeRounds" in PLAIN["agent"].split()
