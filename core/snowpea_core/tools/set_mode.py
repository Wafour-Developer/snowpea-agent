"""``set_mode``: the agent asking to leave plan mode, through a picker.

A finished plan used to end in a sentence — "구현을 시작하려면 Plan 모드를
종료해 주세요" — which is the same mistake ``ask_user`` exists to fix: a closed
set of answers typed into a transcript, with the work of choosing pushed onto
the reader's keyboard. Leaving plan mode is exactly three answers (accept,
auto, stay), so it deserves the picker, not a request.

So the tool never switches silently. It puts the question to the human through
:class:`~snowpea_core.session.questions.QuestionQueue` — the same path
``ask_user`` takes, which means the TUI's arrow-key list, the messenger's
buttons and headless' automatic decline all work with no new client code — and
only then calls ``sessions.set_mode`` and emits ``mode.changed``, the way
``/mode`` and ``session.setMode`` do.

The switch lands *inside* the turn that asked. The agent loop re-reads
``session.mode`` for every call (``policy.decide(session.mode, …)`` in
``agent/loop._run_one_call``), so the next tool call after this one is judged
under the new mode and implementation can begin in the same turn rather than
waiting for the user to prompt again.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.server.protocol import QuestionItem, QuestionOption
from snowpea_core.session import events
from snowpea_core.tools.delegate import delegation_language
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

#: The modes a session can be in, in the order the picker offers the ones the
#: model did not ask for.
MODES: tuple[str, ...] = ("accept", "auto", "plan")

#: Marker on the option the model recommends, per language.
RECOMMENDED = {"ko": "(추천)", "en": "(recommended)"}

#: Header, question and per-mode option text, in the user's language.
TEXT: dict[str, dict[str, Any]] = {
    "ko": {
        "header": "모드 전환",
        "question": "계획이 끝났습니다. 어떻게 진행할까요?",
        "labels": {
            "accept": "accept 모드로 전환하고 구현 시작",
            "auto": "auto 모드로 전환하고 구현 시작",
            "plan": "plan 모드 유지",
        },
        "descriptions": {
            "accept": "파일 편집은 바로, 셸 명령은 물어봅니다",
            "auto": "묻지 않고 실행합니다",
            "plan": "계획만 남깁니다",
        },
        "switched": "모드를 {mode} 로 바꿨습니다. 구현을 시작합니다.",
        "kept": "사용자가 plan 모드를 유지하기로 했습니다. 계획만 남기고 이 턴을 끝내세요.",
        "declined": "사용자가 모드 전환을 선택하지 않았습니다. plan 모드 그대로입니다. "
        "계획만 남기고 이 턴을 끝내세요.",
    },
    "en": {
        "header": "Switch mode",
        "question": "The plan is done. How should we proceed?",
        "labels": {
            "accept": "Switch to accept and start implementing",
            "auto": "Switch to auto and start implementing",
            "plan": "Stay in plan mode",
        },
        "descriptions": {
            "accept": "edits apply straight away, shell commands ask first",
            "auto": "runs without asking",
            "plan": "leave the plan and nothing else",
        },
        "switched": "Mode is now {mode}. Starting the implementation.",
        "kept": "The user chose to stay in plan mode. Leave the plan and end this turn.",
        "declined": "The user did not choose a mode. Still in plan mode. "
        "Leave the plan and end this turn.",
    },
}


def _language(ctx: ToolContext) -> str:
    """``ko`` or ``en``; anything the picker has no wording for reads as English."""
    tag = str(delegation_language(ctx) or "en").strip().lower()
    return "ko" if tag.startswith("ko") or "korean" in tag or "한국" in tag else "en"


def _options(requested: str, language: str) -> list[tuple[str, QuestionOption]]:
    """The picker rows: the requested mode first and marked, then the rest.

    Returned as ``(mode, option)`` pairs so the answer's label — the only thing
    that comes back over ``question.respond`` — maps to a mode without parsing
    the human-facing text again.
    """
    text = TEXT[language]
    order = [requested, *(mode for mode in MODES if mode != requested)]
    rows: list[tuple[str, QuestionOption]] = []
    for index, mode in enumerate(order):
        label = text["labels"][mode]
        if index == 0:
            label = f"{label} {RECOMMENDED[language]}"
        rows.append((mode, QuestionOption(label=label, description=text["descriptions"][mode])))
    return rows


async def set_mode(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Ask the user which mode to continue in, then switch to what they picked."""
    requested = str(args.get("mode") or "").strip().lower()
    if requested not in MODES:
        return ToolResult(
            ok=False,
            error=f"mode must be one of {', '.join(MODES)}; got {args.get('mode')!r}",
        )

    queue = getattr(ctx.core, "questions", None)
    if queue is None:  # pragma: no cover - a core without a queue is a test double
        return ToolResult(ok=False, error="this daemon cannot ask the user questions")

    language = _language(ctx)
    text = TEXT[language]
    rows = _options(requested, language)
    reason = str(args.get("reason") or "").strip()
    question = f"{reason}\n{text['question']}".strip() if reason else text["question"]

    # One question, so one item in the batch; the queue always answers in kind.
    answers = await queue.ask(
        ctx.session,
        [
            QuestionItem(
                header=text["header"],
                question=question,
                options=[option for _, option in rows],
                multi=False,
                allowOther=False,
            )
        ],
        cancel_event=getattr(ctx.session, "interrupt", None),
    )
    answer = answers[0]

    picked = next(
        (mode for mode, option in rows if option.label in answer.selected),
        None,
    )
    meta = {
        "requested": requested,
        "selected": list(answer.selected),
        "mode": ctx.session.mode,
        "changed": False,
        "timed_out": answer.timed_out,
        "declined": answer.declined,
    }
    if picked is None:
        # A decline, a timeout, or free text nobody can map to a mode. None of
        # those is permission to change what the user can be harmed by.
        return ToolResult(ok=True, output=text["declined"], meta=meta)
    if picked == ctx.session.mode:
        meta["mode"] = picked
        return ToolResult(
            ok=True,
            output=text["kept"] if picked == "plan" else text["switched"].format(mode=picked),
            meta=meta,
        )

    mode = await ctx.core.sessions.set_mode(ctx.session, picked)
    await ctx.core.hub.emit_event(ctx.session.id, events.mode_changed(mode))
    meta["mode"] = mode
    meta["changed"] = True
    return ToolResult(ok=True, output=text["switched"].format(mode=mode), meta=meta)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="set_mode",
        category="interaction",
        description=descriptions.SET_MODE,
        input_schema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": list(MODES),
                    "description": (
                        "The mode you recommend continuing in; it is offered first "
                        "and marked as the recommendation. 'accept' after a plan."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One line shown above the question, in the user's language, "
                        "saying what happens next if they agree."
                    ),
                },
            },
            "required": ["mode"],
        },
        permission="read",
        run=set_mode,
    ),
)


__all__ = ["MODES", "RECOMMENDED", "TEXT", "TOOLS", "set_mode"]
