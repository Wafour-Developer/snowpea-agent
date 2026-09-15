"""The opening acknowledgement: the agent says what it is about to do.

A spoken request answered by a silent minute feels dead, and the model has
already written the answer — the line before its first tool call. So that line
is spoken, once, and the rest of the turn's intermediate muttering is not.

Everything here drives the real loop with the scripted fake provider; the
synthesiser is the only thing replaced, because what is being checked is *when*
speech happens and *what* is handed to it, not whether espeak-ng is installed.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _support import Recorder, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.server.app_server import Daemon

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "session.json"


# ---------------------------------------------------------------------------
# the trimmer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        (
            "네, 알겠습니다. 파일을 읽어서 수정하겠습니다. 그 다음 테스트를 돌리겠습니다.",
            "네, 알겠습니다. 파일을 읽어서 수정하겠습니다.",
        ),
        (
            "Sure. I will read the file and fix it. Then I will run the tests.",
            "Sure. I will read the file and fix it.",
        ),
        ("One sentence only", "One sentence only"),
        ("  spaced   out  ", "spaced out"),
        ("", ""),
    ],
)
def test_only_the_first_sentence_or_two_is_spoken(written: str, spoken: str) -> None:
    assert agent_loop.opening_ack(written) == spoken


def test_a_long_opening_is_cut_rather_than_read_out(  ) -> None:
    """It is "yes, I am on it", not a summary read over the work already starting."""
    spoken = agent_loop.opening_ack("x" * 400)
    assert len(spoken) <= agent_loop.ACK_MAX_CHARS + 1
    assert spoken.endswith("…")


# ---------------------------------------------------------------------------
# when it speaks
# ---------------------------------------------------------------------------


class Spoken:
    """Replaces the synthesiser and records every utterance, in order."""

    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []

    async def __call__(
        self, _core: Any, _session: Any, text: str, *, utterance: str = "reply"
    ) -> None:
        body = (text or "").strip()
        if body:
            self.said.append((utterance, body))

    def of(self, utterance: str) -> list[str]:
        return [text for kind, text in self.said if kind == utterance]


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    instance = await make_daemon(
        tmp_path / "home", {"audio": {"tts": {"autoSpeak": True, "provider": "espeak-ng"}}}
    )
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
    (project / "greeting.txt").write_text("hello\n", encoding="utf-8")
    return project


async def _run(
    daemon: Daemon, workdir: Path, prompt: str, spoken: Spoken, monkeypatch: pytest.MonkeyPatch
) -> Recorder:
    core = daemon.core
    assert core is not None
    monkeypatch.setattr(agent_loop, "speak_reply", spoken)
    session = await core.sessions.create(workdir, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    await agent_loop.run_turn(core, session, prompt)
    return recorder


async def test_the_opening_line_is_spoken_before_the_tools_run(
    daemon: Daemon, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spoken = Spoken()
    await _run(daemon, workdir, "edit the greeting", spoken, monkeypatch)

    # The script writes prose, calls two tools, then answers.
    assert spoken.of("ack") == ["editing"]
    assert spoken.of("reply") == ["edited greeting.txt"]
    # And the acknowledgement came first, which is the whole point.
    assert [kind for kind, _ in spoken.said] == ["ack", "reply"]


async def test_only_the_first_opening_of_a_turn_is_spoken(
    daemon: Daemon, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Later intermediate prose is thinking aloud; narrating a turn is not the ask."""
    spoken = Spoken()
    core = daemon.core
    assert core is not None
    monkeypatch.setattr(agent_loop, "speak_reply", spoken)
    session = await core.sessions.create(workdir, mode="auto")

    await agent_loop.run_turn(core, session, "edit the greeting")
    assert len(spoken.of("ack")) == 1

    # A second turn gets its own acknowledgement: the flag is per turn.
    spoken.said.clear()
    (workdir / "greeting.txt").write_text("hello\n", encoding="utf-8")
    await agent_loop.run_turn(core, session, "edit the greeting")
    assert len(spoken.of("ack")) == 1


async def test_a_turn_with_no_tool_call_speaks_only_its_reply(
    daemon: Daemon, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spoken = Spoken()
    await _run(daemon, workdir, "say something else entirely", spoken, monkeypatch)
    assert spoken.of("ack") == []
    assert len(spoken.of("reply")) == 1


async def test_speak_ack_off_leaves_only_the_final_reply(
    daemon: Daemon, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    core.settings.audio.tts.speakAck = False

    spoken = Spoken()
    await _run(daemon, workdir, "edit the greeting", spoken, monkeypatch)
    assert spoken.of("ack") == []
    assert spoken.of("reply") == ["edited greeting.txt"]


async def test_a_delegated_turn_says_nothing_out_loud(
    daemon: Daemon, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is nobody in the room: a child reports, it does not narrate."""
    core = daemon.core
    assert core is not None
    monkeypatch.setattr(agent_loop, "speak_reply", (spoken := Spoken()))
    session = await core.sessions.create(workdir, mode="auto")
    session.is_subagent = True

    await agent_loop.run_turn(core, session, "edit the greeting")
    assert spoken.of("ack") == []


async def test_an_unattended_turn_says_nothing_out_loud(
    daemon: Daemon, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    monkeypatch.setattr(agent_loop, "speak_reply", (spoken := Spoken()))
    session = await core.sessions.create(workdir, mode="auto")
    session.unattended = True

    await agent_loop.run_turn(core, session, "edit the greeting", unattended=True)
    assert spoken.of("ack") == []


async def test_autospeak_off_means_nothing_is_spoken_at_all(
    tmp_path: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    instance = await make_daemon(tmp_path / "home2")
    try:
        core = instance.core
        assert core is not None
        monkeypatch.setattr(agent_loop, "speak_reply", (spoken := Spoken()))
        session = await core.sessions.create(workdir, mode="auto")
        await agent_loop.run_turn(core, session, "edit the greeting")
        assert spoken.of("ack") == []
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


# ---------------------------------------------------------------------------
# what the event says
# ---------------------------------------------------------------------------


def test_the_event_labels_which_utterance_it_was() -> None:
    from snowpea_core.session import events

    kind, payload = events.audio_spoken(path="/tmp/a.wav", utterance="ack")
    assert kind == "audio.spoken"
    assert payload["utterance"] == "ack"
    # A surface that does not know the field sees a reply, which is what
    # everything was before this existed.
    _kind, default = events.audio_spoken(path="/tmp/a.wav")
    assert default["utterance"] == "reply"
