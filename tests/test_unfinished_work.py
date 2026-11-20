"""Unfinished work on stop (agent/unfinished_work.py and its turn.stopping listener)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _support import Recorder, make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.agent import turn_stopping as ts
from snowpea_core.agent import unfinished_work as uw
from snowpea_core.agent import verify_gate
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage
from snowpea_core.session.history import message_text
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import ToolResult

#: The answer from the real session that ended "complete" with work left.
CHECKPOINT_KO = (
    "체크포인트입니다. 이번 턴에 확인한 것과 아직 못 한 것을 정리합니다.\n\n"
    "## 확인한 것\n- 빌드 설정을 읽었습니다.\n- 테스트 한 개가 통과합니다.\n\n"
    "## 남은 일 (다음 턴)\n"
    "1. `src/app.ts`의 라우팅 수정\n"
    "2. 다시 빌드\n"
    "3. 스크린샷으로 화면 확인\n"
)
DONE_KO = "모두 끝났습니다. 라우팅을 고치고 빌드했고, 스크린샷으로 화면을 확인했습니다."


# ---------------------------------------------------------------------------
# the phrase table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        CHECKPOINT_KO,
        "이번 턴에서는 스크린샷까지 못 했습니다.",
        "## 남은 작업\n1. 설정 파일 고치기\n2. 다시 실행",
        "Checkpoint.\n\n## Remaining steps\n1. Fix the router\n2. Rebuild\n3. Take screenshots",
        "Remaining work:\n- update the parser\n- rerun the suite",
        "I ran out of time before the migration; I'll pick it up in the next turn.",
        "I couldn't get to the docs update.",
        "TODO:\n- wire the endpoint\n- add tests",
        "チェックポイントです。\n## 残りの作業\n1. ルーターの修正\n2. 再ビルド",
        "続きは次のターンで行います。",
        "检查点。\n## 剩余工作\n1. 修复路由\n2. 重新构建",
        "下一轮继续处理截图。",
        "Punto de control.\n## Tareas pendientes\n1. Corregir el enrutador\n2. Recompilar",
        "Lo termino en el próximo turno.",
    ],
)
def test_answers_that_defer_work_they_could_do_now_continue(text: str) -> None:
    assert uw.detect(text), text


@pytest.mark.parametrize(
    "text",
    [
        DONE_KO,
        "All done: the router is fixed, the build passes and the page renders.",
        # A question, a choice or a credential: the person is needed.
        CHECKPOINT_KO + "\n어느 방식으로 할까요?",
        "## Next steps\n1. Deploy to staging\n2. Rotate the key\n\nShould I deploy now?",
        "## Remaining work\n1. Push the release\n\nPlease provide the API key for the registry.",
        "## 남은 일\n1. 배포\n\n배포 권한이 필요합니다. 권한을 확인해 주세요.",
        "## 残りの作業\n1. デプロイ\n\nどちらの方式にしますか",
        "## 剩余工作\n1. 部署\n\n请确认是否部署。",
        "## Próximos pasos\n1. Desplegar\n\n¿Lo despliego ahora?",
        # Optional follow-ups are offers, not unfinished work.
        "완료했습니다.\n\n다음 단계로 원하시면:\n1. 다크 모드 추가\n2. 테스트 보강",
        "Done.\n\nNext steps, if you want:\n1. Add dark mode\n2. More tests\n\nLet me know.",
        "Hecho. Si quieres, próximos pasos:\n1. Modo oscuro",
        # A heading word with no list behind it, and code that mentions TODO.
        "The next step is already covered by the existing check.",
        "Fixed it.\n```python\n# TODO:\n# - nothing\n```",
    ],
)
def test_finished_or_waiting_answers_do_not(text: str) -> None:
    assert not uw.detect(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Done: the migration script is written and tested.\n\n## Next steps\n"
        "1. Run the migration in production\n2. Rotate the key",
        "마이그레이션 스크립트를 작성하고 테스트했습니다.\n\n## 다음 단계\n"
        "1. 운영 환경에서 마이그레이션 실행\n2. 키 교체",
        "移行スクリプトを作成し、テストしました。\n\n## 次のステップ\n"
        "1. 本番環境で移行を実行\n2. キーをローテーション",
        "迁移脚本已编写并测试。\n\n## 下一步\n1. 在生产环境运行迁移\n2. 轮换密钥",
        "Listo: el script de migración está escrito y probado.\n\n## Próximos pasos\n"
        "1. Ejecutar la migración en producción\n2. Rotar la clave",
        "Hecho.\n\nSiguientes pasos:\n1. Ejecutar la migración\n2. Rotar la clave",
    ],
)
def test_next_steps_for_the_person_after_a_finished_answer_do_not(text: str) -> None:
    assert not uw.detect(text), text


def test_every_language_has_markers_and_waiting_phrases() -> None:
    langs = {m.lang for m in uw.MARKERS}
    assert langs == {"ko", "en", "ja", "zh", "es"}
    assert {lang for lang, _ in uw.WAITING} == langs


# ---------------------------------------------------------------------------
# the listener's bounds
# ---------------------------------------------------------------------------


def _core(limit: int = 2, verify: str = "auto") -> SimpleNamespace:
    agent = SimpleNamespace(unfinished_continue=limit, verifyOnStop=verify)
    return SimpleNamespace(settings=SimpleNamespace(agent=agent))


def _session(tmp_path: Path, **kwargs: Any) -> Session:
    session = Session(id="s-unfinished", workdir=tmp_path, **kwargs)
    verify_gate.begin_turn(session)
    return session


def _tool(session: Session) -> None:
    verify_gate.observe(session, "read_file", {"path": "a.txt"}, ToolResult(ok=True))


async def _stop(core: Any, session: Session, text: str = CHECKPOINT_KO, **kw: Any) -> Any:
    return await ts.default_chain().run(core, session, ts.StopContext(text, **kw))


async def test_continues_up_to_the_setting_with_tool_calls_between(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _tool(session)
    first = await _stop(_core(), session)
    assert first is not None and first.producer == "unfinished-work"
    assert first.text == uw.INSTRUCTION
    assert first.shown == "The answer listed remaining work; continuing (1/2)."
    _tool(session)
    second = await _stop(_core(), session)
    assert second is not None and second.shown.endswith("(2/2).")
    _tool(session)
    assert await _stop(_core(), session) is None


async def test_a_continuation_with_no_tool_call_lets_the_turn_end(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _tool(session)
    assert await _stop(_core(), session) is not None
    assert await _stop(_core(), session) is None


@pytest.mark.parametrize(
    "case",
    ["off", "plan", "goal", "budget", "question-timed-out"],
)
async def test_it_stays_out_of_the_way(tmp_path: Path, case: str) -> None:
    core = _core(0 if case == "off" else 2)
    session = _session(tmp_path, mode="plan" if case == "plan" else "auto")
    if case == "goal":
        session.goal_armed = True
    if case == "question-timed-out":
        session.verify_turn.question_unanswered = True
    _tool(session)
    assert await _stop(core, session, at_budget=case == "budget") is None


async def test_a_turn_with_no_tool_call_never_continues(tmp_path: Path) -> None:
    session = _session(tmp_path)
    text = "## 남은 일\n1. 라우팅 수정\n2. 다시 빌드"
    assert uw.detect(text)
    assert await _stop(_core(), session, text) is None
    assert session.verify_turn.unfinished_continues == 0


async def test_verify_gate_wins_over_unfinished_work(tmp_path: Path) -> None:
    session = _session(tmp_path)
    verify_gate.observe(session, "patch", {"path": "app.py"}, ToolResult(ok=True, path="app.py"))
    decision = await _stop(_core(), session)
    assert decision is not None and decision.producer == "verify-gate"
    assert session.verify_turn.unfinished_continues == 0


# ---------------------------------------------------------------------------
# a real turn
# ---------------------------------------------------------------------------


class _Scripted:
    """Checkpoint answer, then a tool call, then a plain final answer."""

    vendor = "fake"

    def __init__(self, answers: list[str | ToolCall]) -> None:
        self.answers = answers
        self.requests: list[list[ChatMessage]] = []

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        self.requests.append(list(messages))
        answer = self.answers[min(len(self.requests), len(self.answers)) - 1]
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=50, output_tokens=3))
        if isinstance(answer, ToolCall):
            yield StreamEvent(kind="tool_call", tool_call=answer)
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        yield StreamEvent(kind="text_delta", text=answer)
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _run_turn(tmp_path: Path, answers: list[str | ToolCall]) -> tuple[_Scripted, Recorder]:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        provider = _Scripted(answers)
        core.providers.get = lambda _p, _m: provider
        workdir = tmp_path / "w"
        workdir.mkdir()
        (workdir / "a.txt").write_text("hello")
        session = await core.sessions.create(workdir, mode="auto")
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)
        agent_loop.start_turn(core, session, "계속 진행해서 완료까지 해줘")
        await asyncio.wait_for(session.turn_task, timeout=60)
        return provider, recorder
    finally:
        await daemon.stop()


async def test_the_korean_checkpoint_continues_then_completes(tmp_path: Path) -> None:
    read = ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})
    first = ToolCall(id="c0", name="read_file", arguments={"path": "a.txt"})
    provider, recorder = await _run_turn(tmp_path, [first, CHECKPOINT_KO, read, DONE_KO])
    assert len(provider.requests) == 4
    assert uw.INSTRUCTION in message_text(provider.requests[2][-1])
    shown = [e["payload"] for e in recorder.of_kind("hook.continue")]
    assert len(shown) == 1 and "continuing (1/2)" in str(shown[0])
    assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "complete"


async def test_a_restated_checkpoint_ends_the_turn(tmp_path: Path) -> None:
    read = ToolCall(id="c0", name="read_file", arguments={"path": "a.txt"})
    provider, recorder = await _run_turn(tmp_path, [read, CHECKPOINT_KO, CHECKPOINT_KO])
    assert len(provider.requests) == 3
    assert len(recorder.of_kind("hook.continue")) == 1
    assert recorder.of_kind("turn.done")[-1]["payload"]["reason"] == "complete"


# ---------------------------------------------------------------------------
# feedback: a stop that waits on the person was pushed on (three wrap-ups)
# ---------------------------------------------------------------------------

#: The shape of the reported answer: booked up to payment, payment is the person's.
PAYMENT_KO = (
    "예약 단계까지 진행했습니다.\n\n## 남은 일\n"
    "1. 결제는 직접 진행해 주셔야 합니다.\n2. 결제 후 확정 메일 확인\n"
)


@pytest.mark.parametrize(
    "text",
    [
        PAYMENT_KO,
        "## 남은 작업\n1. 카드 결제\n\n결제는 사용자가 직접 하셔야 합니다.",
        "## 남은 작업\n1. 로그인\n\n이 사이트는 로그인이 필요합니다.",
        "## 남은 작업\n1. 본인 인증\n\n인증 단계가 자동화로 차단되었습니다.",
        "## 남은 작업\n1. 결제\n\n여기부터는 직접 입력이 필요합니다.",
        "## Remaining steps\n1. Pay for the booking\n\nYou need to pay for it yourself.",
        "## Remaining steps\n1. Payment\n\nOnly you can enter the card details.",
    ],
)
def test_endings_that_wait_on_the_person_do_not_continue(text: str) -> None:
    assert uw.remaining(text) is not None
    assert uw.waiting(text)
    assert not uw.detect(text)


def test_real_unfinished_work_still_continues() -> None:
    assert uw.detect(CHECKPOINT_KO)
    assert uw.detect("## 남은 작업\n1. 직접 만든 스크립트 정리\n2. 테스트 실행\n")
    assert uw.detect("## Remaining steps\n1. Fix the router\n2. Rebuild\n\nI ran out of time.")


def _todos(session: Session, *contents: str) -> None:
    session.todos = [
        {"id": str(i), "content": c, "status": "pending"} for i, c in enumerate(contents)
    ]


async def test_not_when_every_open_todo_waits_on_the_person(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _tool(session)
    _todos(session, "결제 (waiting on the person: 카드 결제)", "예약 확정 (blocked: 결제 대기)")
    assert await _stop(_core(), session) is None
    # One open item that is plain work: the to-do follow-up asks first.
    _todos(session, "결제 (waiting on the person: 카드)", "라우팅 수정")
    decision = await _stop(_core(), session)
    assert decision is not None and decision.producer == "verify-gate"
    assert "라우팅 수정" in decision.text and "결제" not in decision.text.split(":", 1)[1]


async def test_not_right_after_the_verify_gates_todo_follow_up(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _tool(session)
    _todos(session, "라우팅 수정")
    first = await _stop(_core(), session)
    assert first is not None and first.producer == "verify-gate"
    # The model answers the follow-up with a status list (a to-do update at most).
    _tool(session)
    assert await _stop(_core(), session) is None
    assert session.verify_turn.unfinished_continues == 0
    # Real work after it: the listener is back.
    _tool(session)
    _tool(session)
    later = await _stop(_core(), session)
    assert later is not None and later.producer == "unfinished-work"


async def test_todo_follow_up_waits_after_a_request_human_hand_back(tmp_path: Path) -> None:
    session = _session(tmp_path)
    verify_gate.observe(
        session, "repl", {"code": "await requestHuman('결제를 진행해 주세요')"}, ToolResult(ok=True)
    )
    assert session.verify_turn.handed_to_person
    _todos(session, "결제 완료 확인")
    ending = "결제 화면을 넘겼습니다. 결제는 직접 진행해 주셔야 합니다."
    assert await _stop(_core(), session, ending) is None
    assert verify_gate.todo_waits("결제 (대기: 사용자 결제)")
    assert verify_gate.todo_waits("pay (Waiting on the user)")
    assert not verify_gate.todo_waits("라우팅 수정")
