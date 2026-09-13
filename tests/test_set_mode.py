"""``set_mode``: the picker that ends a plan, and the switch it lands mid-turn.

The tool is one question and one state change, so the things worth pinning are
the ones a user would notice going wrong: the requested mode offered first and
marked as the recommendation; a pick that really changes the session and emits
``mode.changed``; and every non-answer — staying in plan, a decline, a surface
with no picker — leaving the mode exactly as it was.

The last case is the important one. ``set_mode`` is called when the model wants
permission to start writing, and silence is not permission.

One end-to-end case drives a real daemon, because the claim that matters is not
that the attribute changed but that the *next tool call in the same turn* is
judged under the new mode: the write that plan mode refuses goes through
without a new prompt from the user.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, make_daemon
from test_session_loop import prompt

from snowpea_core.config.settings import Settings
from snowpea_core.prompts import loader
from snowpea_core.providers.base import ChatMessage
from snowpea_core.server.app_server import Daemon
from snowpea_core.session.questions import QuestionQueue
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import ToolContext
from snowpea_core.tools.set_mode import TEXT, set_mode

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "set_mode.json"

#: The three rows, in the order a plan-mode call asking for ``accept`` offers
#: them.  Spelled out so a reworded picker is a reviewed change.
KO_LABELS = (
    "accept 모드로 전환하고 구현 시작 (추천)",
    "auto 모드로 전환하고 구현 시작",
    "plan 모드 유지",
)


# ---------------------------------------------------------------------------
# the tool, with the smallest core it reaches into
# ---------------------------------------------------------------------------


class _Hub:
    """Both halves of what the tool touches: ``notify`` and ``emit_event``."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.events: list[Any] = []

    async def notify(self, method: str, params: dict[str, Any], exclude: Any = None) -> None:
        self.sent.append((method, params))

    async def emit_event(self, session_id: str, event: Any) -> None:
        self.events.append(event)


class _Sessions:
    """``SessionManager.set_mode`` without a store behind it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def set_mode(self, session: Any, mode: str) -> str:
        self.calls.append(mode)
        session.mode = mode
        return mode


class _Core:
    def __init__(self, timeout_sec: int = 600) -> None:
        self.hub = _Hub()
        self.sessions = _Sessions()
        self.questions = QuestionQueue(
            Settings.model_validate({"questions": {"timeoutSec": timeout_sec}}), self.hub
        )


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


def _session(origin: Any = None, said: str = "계획을 세워줘") -> Session:
    session = Session(id="s-1", workdir=Path("/tmp"), mode="plan")
    session.origin_conn = origin
    session.history.append(ChatMessage(role="user", content=said))
    return session


def _ctx(core: Any, session: Any) -> ToolContext:
    return ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_it_asks_instead_of_switching_and_recommends_what_it_asked_for() -> None:
    core = _Core()
    origin = _Origin({"selected": [KO_LABELS[0]], "text": None})
    session = _session(origin)

    await set_mode(_ctx(core, session), {"mode": "accept", "reason": "계획이 준비됐습니다."})

    [asked] = origin.asked
    assert asked["header"] == TEXT["ko"]["header"]
    assert TEXT["ko"]["question"] in asked["question"]
    assert "계획이 준비됐습니다." in asked["question"]
    assert [option["label"] for option in asked["options"]] == list(KO_LABELS)
    assert asked["options"][0]["description"] == "파일 편집은 바로, 셸 명령은 물어봅니다"
    # One mode, from a fixed set: there is nothing for free text to add.
    assert asked["allowOther"] is False
    assert asked["multi"] is False


@pytest.mark.asyncio
async def test_the_requested_mode_leads_whichever_one_it_is() -> None:
    core = _Core()
    origin = _Origin({"selected": [], "text": None})
    await set_mode(_ctx(core, _session(origin)), {"mode": "auto"})
    assert [option["label"] for option in origin.asked[0]["options"]] == [
        "auto 모드로 전환하고 구현 시작 (추천)",
        "accept 모드로 전환하고 구현 시작",
        "plan 모드 유지",
    ]


@pytest.mark.asyncio
async def test_picking_accept_switches_the_session_and_announces_it() -> None:
    core = _Core()
    session = _session(_Origin({"selected": [KO_LABELS[0]], "text": None}))

    result = await set_mode(_ctx(core, session), {"mode": "accept"})

    assert result.ok is True
    assert result.output == "모드를 accept 로 바꿨습니다. 구현을 시작합니다."
    assert session.mode == "accept"
    assert core.sessions.calls == ["accept"]
    assert [kind for kind, _ in core.hub.events] == ["mode.changed"]
    assert core.hub.events[0][1]["mode"] == "accept"
    assert result.meta is not None and result.meta["changed"] is True


@pytest.mark.asyncio
async def test_staying_in_plan_changes_nothing() -> None:
    core = _Core()
    session = _session(_Origin({"selected": ["plan 모드 유지"], "text": None}))

    result = await set_mode(_ctx(core, session), {"mode": "accept"})

    assert session.mode == "plan"
    assert core.sessions.calls == []
    assert core.hub.events == []
    assert result.output == TEXT["ko"]["kept"]
    assert result.meta is not None and result.meta["changed"] is False


@pytest.mark.asyncio
async def test_a_declined_question_is_not_permission_to_switch() -> None:
    core = _Core()
    session = _session(_Origin({"selected": [], "text": None}))

    result = await set_mode(_ctx(core, session), {"mode": "accept"})

    assert session.mode == "plan"
    assert core.sessions.calls == []
    assert result.meta is not None and result.meta["declined"] is True
    assert result.output == TEXT["ko"]["declined"]


@pytest.mark.asyncio
async def test_an_unmappable_answer_changes_nothing() -> None:
    """Free text, or a label from an older build: not a mode, so not a switch."""
    core = _Core()
    session = _session(_Origin({"selected": ["turbo mode"], "text": None}))

    result = await set_mode(_ctx(core, session), {"mode": "accept"})

    assert session.mode == "plan"
    assert core.sessions.calls == []
    assert result.output == TEXT["ko"]["declined"]


@pytest.mark.asyncio
async def test_headless_declines_at_once_and_stays_in_plan() -> None:
    """A surface with no picker answers nothing; plan mode must survive it."""
    core = _Core()
    session = _session(_Origin(raises=True))

    result = await asyncio.wait_for(set_mode(_ctx(core, session), {"mode": "accept"}), timeout=2.0)

    assert session.mode == "plan"
    assert core.sessions.calls == []
    assert result.meta is not None and result.meta["declined"] is True
    assert result.meta["timed_out"] is False


@pytest.mark.asyncio
async def test_nobody_answering_is_not_a_switch_either() -> None:
    class _Silent:
        closed = False

        async def call(self, method: str, params: dict[str, Any], timeout: float | None = None):
            await asyncio.sleep(3600)

    core = _Core(timeout_sec=1)
    session = _session(_Silent())
    result = await asyncio.wait_for(set_mode(_ctx(core, session), {"mode": "accept"}), timeout=8.0)

    assert session.mode == "plan"
    assert result.meta is not None and result.meta["timed_out"] is True


@pytest.mark.asyncio
async def test_the_picker_speaks_the_language_the_user_wrote_in() -> None:
    core = _Core()
    origin = _Origin({"selected": [], "text": None})
    await set_mode(_ctx(core, _session(origin, said="draft me a plan")), {"mode": "accept"})
    assert origin.asked[0]["header"] == "Switch mode"
    assert [option["label"] for option in origin.asked[0]["options"]] == [
        "Switch to accept and start implementing (recommended)",
        "Switch to auto and start implementing",
        "Stay in plan mode",
    ]


@pytest.mark.asyncio
async def test_a_mode_that_does_not_exist_is_an_error_not_a_question() -> None:
    core = _Core()
    origin = _Origin({"selected": [], "text": None})
    result = await set_mode(_ctx(core, _session(origin)), {"mode": "yolo"})
    assert result.ok is False
    assert origin.asked == []


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------


def test_plan_mode_is_told_to_call_the_tool_not_to_ask_in_prose() -> None:
    plan = loader.load("modes/plan")
    assert 'set_mode("accept")' in plan
    assert "never ask in prose to switch modes" in plan


# ---------------------------------------------------------------------------
# end to end: the switch lands inside the turn that asked
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("SNOWPEA_PROVIDER", f"fake:{FIXTURE}")
    monkeypatch.setenv("SNOWPEA_TEST", "1")
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()


@pytest_asyncio.fixture
async def http() -> Any:
    async with aiohttp.ClientSession() as session:
        yield session


def _workdir(tmp_path: Path) -> Path:
    workdir = tmp_path / "project"
    workdir.mkdir(exist_ok=True)
    return workdir


async def _plan_session(client: Any, workdir: Path) -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": "plan"})
    return str(result["sessionId"])


@pytest.mark.asyncio
async def test_picking_accept_lets_the_same_turn_start_writing(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = _workdir(tmp_path)
    client = await connect(http, daemon, question_answer={"selected": [KO_LABELS[0]], "text": None})
    session_id = await _plan_session(client, workdir)

    turn_id = await prompt(client, session_id, "계획을 마무리해줘")
    assert await client.wait_turn(turn_id) == "complete"

    assert [event["payload"]["mode"] for event in client.of_kind("mode.changed")] == ["accept"]
    # The write that plan mode refuses ran in the same turn, with no second
    # prompt from the user and no approval request.
    assert (workdir / "out.txt").read_text(encoding="utf-8") == "x\n"
    assert client.of_kind("error") == []
    assert (await client.ok("session.list"))["sessions"][0]["mode"] == "accept"

    await client.stop()


@pytest.mark.asyncio
async def test_staying_in_plan_keeps_the_write_refused(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = _workdir(tmp_path)
    client = await connect(
        http, daemon, question_answer={"selected": ["plan 모드 유지"], "text": None}
    )
    session_id = await _plan_session(client, workdir)

    turn_id = await prompt(client, session_id, "계획을 마무리해줘")
    assert await client.wait_turn(turn_id) in ("complete", "denied")

    assert client.of_kind("mode.changed") == []
    assert [event["payload"]["code"] for event in client.of_kind("error")] == ["mode_denied"]
    assert not (workdir / "out.txt").exists()
    assert (await client.ok("session.list"))["sessions"][0]["mode"] == "plan"

    await client.stop()
