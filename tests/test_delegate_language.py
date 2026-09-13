"""The output language a delegation asks for, and the title it is shown under.

A subagent cannot see the conversation, so nothing in its brief says what
language to answer in — and an English brief comes back in English even when
the user writes Korean. ``delegate_task`` therefore appends one short English
line naming the language, taken from ``agent.replyLanguage`` or, on ``auto``,
detected from the user's own last message.

The ``title`` argument is the other half: the user should read what the
delegation *is* in their own language, not "Ran delegate_task".
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from _support import Recorder

from snowpea_core.agent.subagent import get_manager
from snowpea_core.providers.base import ChatMessage
from snowpea_core.server.app_server import Core, Daemon
from snowpea_core.tools.delegate import (
    delegate_task,
    delegation_language,
    detected_language,
    language_line,
    last_user_text,
)
from snowpea_core.tools.registry import ToolContext
from snowpea_core.util.lang import detect_language

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "subagents.json"
TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# the detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("요약을 한국어로 읽기 쉽게 해줘", "ko"),
        ("ログを読みやすくしてください", "ja"),
        ("请把这个文件重构一下", "zh"),
        ("Перепиши этот модуль", "ru"),
        ("make the summaries readable", "en"),
        ("", "en"),
        # Code and paths on their own say nothing about the user's language.
        ("core/snowpea_core/tools/delegate.py", "en"),
        # A mixed message follows the prose, not the identifiers in it.
        ("delegate_task 의 summary 가 읽기 어려워", "ko"),
        # Hangul wins over kana even when both appear.
        ("ドキュメント 문서", "ko"),
    ],
)
def test_detect_language(text: str, expected: str) -> None:
    assert detect_language(text) == expected


def test_language_line_is_short_english_and_names_the_tag() -> None:
    assert language_line("ko") == (
        "Answer in Korean (ko). Keep code, paths and commands as they are."
    )
    assert language_line("en").startswith("Answer in English (en).")


# ---------------------------------------------------------------------------
# the daemon
# ---------------------------------------------------------------------------


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


def core_of(daemon: Daemon) -> Core:
    core = daemon.core
    assert core is not None
    return core


async def open_session(core: Core, workdir: Path):  # type: ignore[no-untyped-def]
    return await core.sessions.create(workdir, mode="auto")


def context(core: Core, session) -> ToolContext:  # type: ignore[no-untyped-def]
    return ToolContext(session=session, core=core, backend=session.backend)


# ---------------------------------------------------------------------------
# which language is asked for
# ---------------------------------------------------------------------------


async def test_configured_language_wins_over_what_the_user_wrote(
    daemon: Daemon, workdir: Path
) -> None:
    core = core_of(daemon)
    session = await open_session(core, workdir)
    session.history.append(ChatMessage(role="user", content="plain english question"))
    core.settings.agent.replyLanguage = "ko"

    assert delegation_language(context(core, session)) == "ko"


async def test_auto_follows_the_last_user_message(daemon: Daemon, workdir: Path) -> None:
    core = core_of(daemon)
    session = await open_session(core, workdir)
    core.settings.agent.replyLanguage = "auto"
    session.history.append(ChatMessage(role="user", content="델리게이트 요약을 고쳐줘"))

    assert delegation_language(context(core, session)) == "ko"
    # Cached on the session, and a newer user message refreshes it.
    assert session.detected_language == "ko"
    session.history.append(ChatMessage(role="assistant", content="네, 확인했습니다"))
    session.history.append(ChatMessage(role="user", content="now in english please"))
    assert delegation_language(context(core, session)) == "en"
    assert session.detected_language == "en"


async def test_last_user_text_ignores_the_assistant(daemon: Daemon, workdir: Path) -> None:
    core = core_of(daemon)
    session = await open_session(core, workdir)
    session.history.append(ChatMessage(role="user", content="첫 번째 요청"))
    session.history.append(ChatMessage(role="assistant", content="ok"))

    assert last_user_text(session) == "첫 번째 요청"
    assert detected_language(session) == "ko"


# ---------------------------------------------------------------------------
# what the child actually receives
# ---------------------------------------------------------------------------


async def test_the_brief_carries_the_language_line(daemon: Daemon, workdir: Path) -> None:
    """The wrapper appends it, so the child's brief names the language."""
    core = core_of(daemon)
    session = await open_session(core, workdir)
    core.settings.agent.replyLanguage = "auto"
    session.history.append(ChatMessage(role="user", content="요약이 읽기 어려워"))
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    result = await asyncio.wait_for(
        delegate_task(context(core, session), {"task": "quick child for the language test"}),
        timeout=TIMEOUT,
    )
    assert result.ok, result.error

    spawn = recorder.of_kind("subagent.spawn")[0]["payload"]
    assert spawn["task"].startswith("quick child for the language test")
    assert spawn["task"].endswith(language_line("ko"))


async def test_the_configured_language_reaches_the_brief(daemon: Daemon, workdir: Path) -> None:
    core = core_of(daemon)
    session = await open_session(core, workdir)
    core.settings.agent.replyLanguage = "ja"
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    result = await asyncio.wait_for(
        delegate_task(context(core, session), {"task": "quick child in japanese"}),
        timeout=TIMEOUT,
    )
    assert result.ok, result.error
    spawn = recorder.of_kind("subagent.spawn")[0]["payload"]
    assert spawn["task"].endswith(language_line("ja"))


# ---------------------------------------------------------------------------
# the title
# ---------------------------------------------------------------------------


async def test_title_reaches_the_record_and_every_event(daemon: Daemon, workdir: Path) -> None:
    core = core_of(daemon)
    session = await open_session(core, workdir)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    result = await asyncio.wait_for(
        delegate_task(
            context(core, session),
            {"task": "quick child with a title", "title": "요약 표시 방식 조사"},
        ),
        timeout=TIMEOUT,
    )
    assert result.ok, result.error

    records = [r for r in get_manager(core).records() if r.parent_session_id == session.id]
    assert records[-1].title == "요약 표시 방식 조사"

    for kind in ("subagent.spawn", "subagent.update", "subagent.done"):
        payloads = [event["payload"] for event in recorder.of_kind(kind)]
        assert payloads, f"no {kind} event"
        assert all(payload["title"] == "요약 표시 방식 조사" for payload in payloads), kind


async def test_title_defaults_to_empty(daemon: Daemon, workdir: Path) -> None:
    core = core_of(daemon)
    session = await open_session(core, workdir)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    await asyncio.wait_for(
        delegate_task(context(core, session), {"task": "quick child without a title"}),
        timeout=TIMEOUT,
    )
    assert recorder.of_kind("subagent.spawn")[0]["payload"]["title"] == ""
