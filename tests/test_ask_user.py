"""``ask_user``: the tool, the ``question.*`` round trip and what declines it.

The tool blocks a turn on a human, so the things worth pinning are the ones
that could hang it or answer it wrongly: a real daemon asking a real client
over ``question.request``; a second client answering through
``question.respond``; a client that has never heard of the method (headless);
and a timeout.  The argument normalisation is tested on its own because the
schema is a superset of two other tools' and a regression there is silent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import connect, make_daemon

from snowpea_core.config.settings import Settings
from snowpea_core.gateway.base import (
    parse_question_callback,
    question_buttons,
    question_text,
)
from snowpea_core.gateway.router import _picked_labels
from snowpea_core.server.app_server import Core, Daemon
from snowpea_core.session.questions import QuestionQueue
from snowpea_core.session.session import Session
from snowpea_core.tools.ask_user import AskUserError, _questions, ask_user, queue_command
from snowpea_core.tools.registry import ToolContext, ToolRegistry, register_builtin_tools

TIMEOUT = 10.0

RENDERER = {
    "header": "렌더러",
    "question": "무엇으로 렌더링할까요? 여기서 고른 것이 엔진 통제력을 정합니다.",
    "options": [
        {"label": "three.js (추천)", "description": "빠른 시작, 엔진 통제력 낮음"},
        {"label": "Raw WebGL2", "description": "공수 큼, 완전한 통제"},
    ],
}


# ---------------------------------------------------------------------------
# argument normalisation (the Claude Code and Hermes shapes)
# ---------------------------------------------------------------------------


def test_flat_shape_is_one_question() -> None:
    [question] = _questions(dict(RENDERER))
    assert question.header == "렌더러"
    assert [option.label for option in question.options] == ["three.js (추천)", "Raw WebGL2"]
    assert question.multi is False
    assert question.allow_other is True


def test_claude_code_shape_round_trips() -> None:
    """``questions[]`` with ``header`` / ``multiSelect`` — a ported skill's call."""
    questions = _questions(
        {
            "questions": [
                {
                    "header": "Auth method",
                    "question": "How should users sign in?",
                    "options": [
                        {"label": "OAuth", "description": "no passwords to store"},
                        {"label": "Magic link", "description": "email round trip"},
                    ],
                    "multiSelect": True,
                },
                {"question": "Anything else?"},
            ]
        }
    )
    assert [q.header for q in questions] == ["Auth method", ""]
    assert questions[0].multi is True
    assert questions[1].options == []


def test_hermes_shape_round_trips() -> None:
    """``choices`` of bare strings and ``multi_select``."""
    [question] = _questions(
        {"questions": [{"question": "Which?", "choices": ["a", "b"], "multi_select": True}]}
    )
    assert [option.label for option in question.options] == ["a", "b"]
    assert question.multi is True


def test_preview_survives() -> None:
    [question] = _questions(
        {"question": "Which layout?", "options": [{"label": "A", "preview": "+--+\n|  |"}, "B"]}
    )
    assert question.options[0].preview.startswith("+--+")


@pytest.mark.parametrize(
    "args",
    [
        {"question": ""},
        {"question": "Which?", "options": [{"label": "only one"}]},
        {"question": "Which?", "options": [{"label": str(n)} for n in range(9)]},
        {"questions": [{"question": f"q{n}"} for n in range(6)]},
    ],
)
def test_malformed_calls_are_refused(args: dict[str, Any]) -> None:
    with pytest.raises(AskUserError):
        _questions(args)


@pytest.mark.asyncio
async def test_a_bad_call_is_an_error_not_a_hang() -> None:
    result = await ask_user(_ctx(_core(), _session()), {"question": "Which?", "options": [{}]})
    assert result.ok is False
    assert "label" in (result.error or "")


# ---------------------------------------------------------------------------
# the queue, without a daemon
# ---------------------------------------------------------------------------


def _core(**kwargs: Any) -> Any:
    """The smallest ``Core``-shaped thing the tool reaches into."""

    class _Hub:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, Any]]] = []

        async def notify(self, method: str, params: dict[str, Any], exclude: Any = None) -> None:
            self.sent.append((method, params))

    class _Core:
        def __init__(self) -> None:
            self.questions = QuestionQueue(Settings(), _Hub())
            self.commands = kwargs.get("commands")

    core = _Core()
    core.questions.settings = Settings.model_validate(
        {"questions": {"timeoutSec": kwargs.get("timeout_sec", 600)}}
    )
    return core


def _session(origin: Any = None) -> Any:
    session = Session(id="s-1", workdir="/tmp")
    session.origin_conn = origin
    return session


def _ctx(core: Any, session: Any) -> ToolContext:
    return ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]


class _Origin:
    """A connection that answers ``question.request`` with a canned reply."""

    closed = False

    def __init__(self, reply: dict[str, Any] | None = None, raises: bool = False) -> None:
        self.reply = reply or {"selected": [], "text": None}
        self.raises = raises
        self.asked: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        assert method == "question.request"
        self.asked.append(params)
        if self.raises:
            raise RuntimeError("question.request is not supported by this client")
        return self.reply


@pytest.mark.asyncio
async def test_the_answer_comes_back_as_text_and_a_payload() -> None:
    origin = _Origin({"selected": ["Raw WebGL2"], "text": None})
    result = await ask_user(_ctx(_core(), _session(origin)), dict(RENDERER))
    assert result.ok is True
    assert "선택: Raw WebGL2" in result.output
    assert result.meta == {
        "answers": [
            {
                "question": RENDERER["question"],
                "header": "렌더러",
                "selected": ["Raw WebGL2"],
                "text": None,
                "timed_out": False,
                "declined": False,
            }
        ],
        "byQuestion": {RENDERER["question"]: ["Raw WebGL2"]},
        "selected": ["Raw WebGL2"],
        "text": None,
        "timed_out": False,
        "declined": False,
    }
    # The options reach the client in the order they were offered, with their
    # descriptions, which is the whole point of the picker.
    [asked] = origin.asked
    assert [option["label"] for option in asked["options"]] == [
        "three.js (추천)",
        "Raw WebGL2",
    ]
    assert asked["options"][0]["description"] == "빠른 시작, 엔진 통제력 낮음"
    assert asked["allowOther"] is True


@pytest.mark.asyncio
async def test_multi_select_returns_every_label() -> None:
    origin = _Origin({"selected": ["three.js (추천)", "Raw WebGL2"], "text": None})
    result = await ask_user(_ctx(_core(), _session(origin)), {**RENDERER, "multi": True})
    assert result.meta is not None and result.meta["selected"] == [
        "three.js (추천)",
        "Raw WebGL2",
    ]
    assert origin.asked[0]["multi"] is True


@pytest.mark.asyncio
async def test_other_text_comes_back_as_text() -> None:
    origin = _Origin({"selected": [], "text": "babylon.js"})
    result = await ask_user(_ctx(_core(), _session(origin)), dict(RENDERER))
    assert "answer: babylon.js" in result.output
    assert result.meta is not None and result.meta["text"] == "babylon.js"
    assert result.meta["declined"] is False


@pytest.mark.asyncio
async def test_an_empty_answer_is_declined_not_agreement() -> None:
    origin = _Origin({"selected": [], "text": None})
    result = await ask_user(_ctx(_core(), _session(origin)), dict(RENDERER))
    assert result.meta is not None and result.meta["declined"] is True
    assert "Do not treat this as agreement" in result.output


@pytest.mark.asyncio
async def test_a_client_that_cannot_ask_declines_at_once() -> None:
    """Headless and every other surface with no picker: an error, not a hang."""
    origin = _Origin(raises=True)
    result = await asyncio.wait_for(
        ask_user(_ctx(_core(), _session(origin)), dict(RENDERER)), timeout=2.0
    )
    assert result.meta is not None and result.meta["declined"] is True
    assert result.meta["timed_out"] is False


@pytest.mark.asyncio
async def test_a_question_nobody_answers_times_out() -> None:
    class _Silent:
        closed = False

        async def call(self, method: str, params: dict[str, Any], timeout: float | None = None):
            await asyncio.sleep(3600)

    core = _core(timeout_sec=1)
    result = await asyncio.wait_for(
        ask_user(_ctx(core, _session(_Silent())), dict(RENDERER)), timeout=8.0
    )
    assert result.meta is not None and result.meta["timed_out"] is True
    assert "No interactive client answered" in result.output


@pytest.mark.asyncio
async def test_questions_are_asked_in_sequence_and_stop_at_a_decline() -> None:
    core = _core()
    origin = _Origin({"selected": [], "text": None})
    result = await ask_user(
        _ctx(core, _session(origin)),
        {"questions": [{"question": "first?"}, {"question": "second?"}]},
    )
    assert len(origin.asked) == 1
    assert origin.asked[0]["total"] == 2
    assert result.meta is not None and len(result.meta["answers"]) == 1


@pytest.mark.asyncio
async def test_a_pending_question_is_announced_and_then_resolved() -> None:
    """``question.pending`` / ``question.resolved`` so a second surface can follow."""
    core = _core()
    hub = core.questions.hub
    origin = _Origin({"selected": ["Raw WebGL2"], "text": None})
    await ask_user(_ctx(core, _session(origin)), dict(RENDERER))
    assert [method for method, _ in hub.sent] == ["question.pending", "question.resolved"]


# ---------------------------------------------------------------------------
# queue_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_command_refuses_anything_that_is_not_a_command() -> None:
    class _Commands:
        def parse(self, text: str) -> Any:
            return ("ralph", "x") if text.startswith("/ralph") else None

        def get(self, name: str) -> Any:
            return object() if name == "ralph" else None

    ctx = _ctx(_core(commands=_Commands()), _session())
    assert (await queue_command(ctx, {"command": "ralph do it"})).ok is False
    assert (await queue_command(ctx, {"command": "/nope"})).ok is False


@pytest.mark.asyncio
async def test_queue_command_queues_rather_than_running_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It must go through ``start_turn``; running a command inline would
    overwrite the turn that is calling the tool."""
    from snowpea_core.agent import loop as agent_loop

    class _Commands:
        def parse(self, text: str) -> Any:
            return ("ralph", text.partition(" ")[2]) if text.startswith("/ralph") else None

        def get(self, name: str) -> Any:
            return object() if name == "ralph" else None

    queued: list[str] = []
    monkeypatch.setattr(
        agent_loop, "start_turn", lambda core, session, text, **kw: queued.append(text) or "t-9"
    )
    ctx = _ctx(_core(commands=_Commands()), _session())
    result = await queue_command(ctx, {"command": "/ralph 복셀 샌드박스"})
    assert result.ok is True
    assert queued == ["/ralph 복셀 샌드박스"]
    assert result.meta == {"command": "ralph", "turnId": "t-9"}
    assert "queued: /ralph 복셀 샌드박스" in result.output


# ---------------------------------------------------------------------------
# the messenger path (Hermes' shape: buttons, numbered text, numeric replies)
# ---------------------------------------------------------------------------


def test_a_messenger_gets_numbered_options_and_a_button_each() -> None:
    request = {
        "requestId": "qu-1",
        **RENDERER,
        "options": RENDERER["options"],
        "allowOther": True,
        "index": 1,
        "total": 2,
    }
    text = question_text(request)
    assert "1. three.js (추천) — 빠른 시작, 엔티" not in text  # no mangling
    assert "1. three.js (추천)" in text
    assert "2. Raw WebGL2" in text
    assert "(1/2)" in text
    buttons = question_buttons("qu-1", list(RENDERER["options"]), True)
    assert [button.data for button in buttons] == [
        "qst:qu-1:1",
        "qst:qu-1:2",
        "qst:qu-1:other",
    ]
    assert parse_question_callback("qst:qu-1:2") == ("qu-1", "2")
    assert parse_question_callback("apr:qu-1:allow") is None


def test_a_typed_number_answers_a_messenger_question() -> None:
    options = list(RENDERER["options"])
    assert _picked_labels("2", options, False) == ["Raw WebGL2"]
    assert _picked_labels("1,2", options, True) == ["three.js (추천)", "Raw WebGL2"]
    # A single-select question takes the first of several rather than guessing.
    assert _picked_labels("1,2", options, False) == ["three.js (추천)"]
    # Anything that is not a choice is free text, and says so by matching nothing.
    assert _picked_labels("babylon.js 로 하죠", options, False) == []


# ---------------------------------------------------------------------------
# the wire: a real daemon, a real client
# ---------------------------------------------------------------------------


async def _tool_ctx(daemon: Daemon, session_id: str) -> ToolContext:
    core: Core | None = daemon.core
    assert core is not None
    session = core.sessions.get(session_id)
    assert session is not None
    return ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_question_request_round_trips_over_the_wire(
    snowpea_home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """Tool -> ``question.request`` -> the client's answer -> tool."""
    daemon = await make_daemon(snowpea_home / "wire")
    try:
        client = await connect(
            http, daemon, question_answer={"selected": ["Raw WebGL2"], "text": None}
        )
        created = await client.ok("session.create", {"workdir": str(tmp_path)})
        ctx = await _tool_ctx(daemon, created["sessionId"])
        result = await asyncio.wait_for(ask_user(ctx, dict(RENDERER)), TIMEOUT)
        assert result.meta is not None and result.meta["selected"] == ["Raw WebGL2"]
        assert client.questions[0]["question"] == RENDERER["question"]
        assert client.questions[0]["header"] == "렌더러"
        await client.stop()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_a_client_that_never_answers_times_the_question_out(
    snowpea_home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(snowpea_home / "silent", {"questions": {"timeoutSec": 1}})
    try:
        client = await connect(http, daemon)  # question_answer=None: never answers
        created = await client.ok("session.create", {"workdir": str(tmp_path)})
        ctx = await _tool_ctx(daemon, created["sessionId"])
        result = await asyncio.wait_for(ask_user(ctx, dict(RENDERER)), TIMEOUT)
        assert result.meta is not None and result.meta["timed_out"] is True
        await client.stop()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_a_second_client_can_list_and_answer_a_question(
    snowpea_home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """``question.list`` / ``question.respond``: the IDE half of the feature."""
    daemon = await make_daemon(snowpea_home / "second")
    try:
        watcher = await connect(http, daemon)
        created = await watcher.ok("session.create", {"workdir": str(tmp_path)})
        session_id = created["sessionId"]
        core: Core | None = daemon.core
        assert core is not None
        session = core.sessions.get(session_id)
        assert session is not None
        # No origin to ask — an unattended turn, a scheduled job, a gateway
        # message — so the question goes to the shared queue instead.
        session.origin_conn = None
        ctx = ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]
        task = asyncio.ensure_future(ask_user(ctx, dict(RENDERER)))

        pending = await watcher.wait_notification("question.pending", TIMEOUT)
        request_id = pending["request"]["requestId"]
        listed = await watcher.ok("question.list", {"sessionId": session_id})
        assert [row["requestId"] for row in listed["requests"]] == [request_id]

        await watcher.ok(
            "question.respond",
            {"requestId": request_id, "selected": ["three.js (추천)"], "text": None},
        )
        result = await asyncio.wait_for(task, TIMEOUT)
        assert result.meta is not None and result.meta["selected"] == ["three.js (추천)"]
        resolved = await watcher.wait_notification("question.resolved", TIMEOUT)
        assert resolved["requestId"] == request_id
        await watcher.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# the skills that ask
# ---------------------------------------------------------------------------

SKILLS = Path(__file__).resolve().parent.parent / "core" / "snowpea_core" / "builtin_skills"


def _skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["deep-interview", "ralplan"])
def test_the_asking_skills_may_use_the_asking_tools(name: str) -> None:
    text = _skill(name)
    [line] = [row for row in text.splitlines() if row.startswith("allowed-tools:")]
    assert "ask_user" in line
    assert "queue_command" in line


def test_deep_interview_turns_open_questions_into_pickers() -> None:
    text = _skill("deep-interview")
    assert "ask_user" in text
    assert "지금은 미결로 둡니다" in text
    assert "/ralplan" in text and "/ralph" in text and "/ultrawork" in text
    # The Language section the interview already had must survive.
    assert "## Language" in text or "## 언어" in text


def test_ralplan_runs_its_whole_loop_in_one_turn() -> None:
    text = _skill("ralplan")
    assert "Do not stop to ask whether to continue." in text
    assert "delegate_task(architect)" in text
    assert "Before ending this turn" in text


def test_ralplan_plans_in_the_users_language() -> None:
    text = _skill("ralplan")
    assert "## Language" in text
    for heading in ("원칙", "결정 요인", "선택지", "계획", "위험", "완료 조건"):
        assert heading in text


def test_the_manual_documents_the_tool() -> None:
    docs = Path(__file__).resolve().parent.parent / "docs" / "manual"
    for language in ("en", "ko"):
        text = (docs / language / "tui.md").read_text(encoding="utf-8")
        assert "ask_user" in text


def test_the_protocol_reference_documents_the_methods() -> None:
    text = (Path(__file__).resolve().parent.parent / "docs" / "protocol.md").read_text(
        encoding="utf-8"
    )
    for method in ("question.request", "question.respond", "question.list"):
        assert method in text


def test_every_builtin_tool_is_still_registered() -> None:
    """A smoke check that registering ask_user did not disturb the catalog."""
    registry = register_builtin_tools(ToolRegistry())
    names = {info.name for info in registry.list()}
    assert {"ask_user", "queue_command"} <= names
    assert json.dumps(registry.get("ask_user").input_schema)  # serialisable for a provider
