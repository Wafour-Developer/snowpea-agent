"""What ``/ralph``, ``/team`` and ``/ultrawork`` make of their argument (CORE-plan-continuity).

Three questions, answered the same way for all three commands:

* **Is this "just run the plan"?**  ``/ralplan`` → ``/ralph 실행해줘`` used to
  plan the words "실행해줘".  An empty argument, a carry-on or execute-only
  phrase, or the plan's own title now means *the current plan*
  (:mod:`snowpea_core.agent.plan_store`).
* **Is it too vague to run at all?**  OMC sends "ralph fix this" or "team
  improve performance" to ralplan before anything executes, and lets a request
  with a concrete anchor (a path, a symbol, numbered steps) straight through.
  Same gate here, kept conservative: short *and* anchorless, with no plan to
  fall back on.  Attended sessions are asked; unattended ones run as before.
* **Did the user say "no gate"?**  ``--force``/``--now`` or a leading ``!``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from snowpea_core.agent import plan_store
from snowpea_core.agent.plan_store import Plan

log = logging.getLogger("snowpea.commands.plan_gate")

#: Words in an argument short enough to be a gesture rather than a task.  Three
#: eojeol/words: "게임 만들어줘", "ralph fix this" are gestures; "fix the flaky
#: test" and "로그인 페이지에 소셜 로그인 버튼 추가해줘" name their target.
VAGUE_MAX_WORDS = 3
VAGUE_MAX_CHARS = 40

_QUOTES = "\"'“”‘’「」"

_BYPASS_FLAG = re.compile(r"(?:^|\s)--(?:force|now)(?=\s|$)")

#: English: an instruction to carry out what is already decided, nothing more.
_EXECUTE_EN = re.compile(
    r"^(?:(?:ok(?:ay)?|yes|sure)[,.!]?\s+)?(?:please\s+)?(?:"
    r"go(?:\s+ahead)?|do\s+it|run(?:\s+it|\s+the\s+plan)?|execute(?:\s+it|\s+the\s+plan)?|"
    r"implement(?:\s+it|\s+this|\s+the\s+plan)?|start(?:\s+it|\s+the\s+plan)?|proceed|"
    r"continue|carry\s+on|resume|--?resume|follow\s+the\s+plan|build\s+it|go\s+on"
    r")(?:\s+please)?[.!?\s]*$",
    re.IGNORECASE,
)

#: Korean: an optional "as planned" lead, a verb, a polite or plain ending.
#: "구현해줘", "진행해줘", "이대로 해줘", "그대로 진행", "계획대로 실행해 주세요".
_EXECUTE_KO = re.compile(
    r"^(?:(?:네|응|좋아|오케이|ㅇㅋ)[,.!\s]*)?"
    r"(?P<lead>(?:계획|플랜)(?:대로|을|를)?|이대로|그대로|바로)?\s*"
    r"(?P<verb>구현|진행|실행|시작|계속|재개|착수|작업)?\s*"
    r"(?P<ending>해|해줘|해 줘|해주세요|해 주세요|하자|해라|합시다|해봐|해 봐|"
    r"하세요|해요|하기|해줄래)?"
    r"[.!?~\s]*$"
)

#: Words that only say "the rest of it" / "now": stripped before matching, so
#: "그냥 구현해줘", "나머지 구현해줘", "계속 진행해줘" and "ㄱㄱ" all mean carry on.
_FILLERS = re.compile(
    r"(?:^|(?<=\s))(?:그냥|전부|모두|나머지|이어서|다음\s*단계(?:를|도)?|계속|바로|ㄱ{2,}|고고+|"
    r"just|all|the\s+rest(?:\s+of\s+it)?|next\s+step)(?=\s|$|[.!?~])",
    re.IGNORECASE,
)

#: A leading step id: "S3 진행해줘", "step S3".
_STEP_PREFIX = re.compile(
    r"^(?:step\s+)?(S\d{1,3})(?:\s*(?:번|단계|만|부터|을|를))?[.:,]?(?=\s|$)", re.IGNORECASE
)


def _clean(args: str) -> str:
    return " ".join(str(args or "").strip().strip(_QUOTES).strip().split())


def strip_bypass(args: str) -> tuple[str, bool]:
    """``(args without the bypass, bypassed)`` for ``--force``/``--now``/a leading ``!``."""
    text = str(args or "").strip()
    forced = False
    if _BYPASS_FLAG.search(text):
        text = _BYPASS_FLAG.sub(" ", text).strip()
        forced = True
    if text.startswith("!"):
        text = text[1:].strip()
        forced = True
    return text, forced


def _split_step(text: str) -> tuple[str | None, str]:
    match = _STEP_PREFIX.match(text)
    if match is None:
        return None, text
    return match.group(1).upper(), text[match.end() :].strip()


def is_carry_on(args: str) -> bool:
    """True for an argument that only says "carry on / do it" — nothing new.

    The one matcher ``/ralph``'s resume and every command's "run the plan"
    share, so the two can never disagree about what "구현해줘" means.  Empty
    counts; filler words and a leading step id are ignored.
    """
    text = _clean(args)
    if not text:
        return True
    _step, text = _split_step(text)
    stripped = _FILLERS.sub(" ", text)
    trimmed = " ".join(stripped.split())
    if trimmed != text or _step is not None:
        # Something was only filler: what is left may be a bare ending ("이어서
        # 해줘") or nothing at all ("ㄱㄱ", "계속").
        if not trimmed.strip(".!?~ "):
            return True
    text = trimmed
    if _EXECUTE_EN.match(text):
        return True
    match = _EXECUTE_KO.match(text)
    if match is None:
        return False
    if match.group("lead") or match.group("verb"):
        return True
    return bool(match.group("ending")) and (trimmed != _clean(args))


def is_execute_request(args: str, plan: Plan | None = None) -> bool:
    """True for an argument that only says "carry out the plan".

    A carry-on phrase (:func:`is_carry_on`), or the current plan's own title
    (ralplan used to hand off ``/ralph <title>``).
    """
    if is_carry_on(args):
        return True
    text = _clean(args)
    return plan is not None and text.casefold() == _clean(plan.title).casefold()


def target_step(args: str, plan: Plan | None) -> str | None:
    """The one step a carry-on phrase names ("S3 진행해줘"), when the plan has it."""
    if plan is None or not is_carry_on(args):
        return None
    step_id, _rest = _split_step(_clean(args))
    return step_id if step_id and plan.step(step_id) is not None else None


#: Anything that names a concrete target: a path or file name, a call, an
#: identifier that is clearly code (snake_case, camelCase, PascalCase), an
#: ALL-CAPS name (README), a version (3.12), a URL, an issue number, numbered
#: or bulleted lines, quoted text.
_ANCHORS = (
    re.compile(r"[\w.-]+/[\w./-]+"),  # a path
    re.compile(r"\b[\w-]+\.[A-Za-z][A-Za-z0-9]{0,7}\b"),  # file.ext
    re.compile(r"\w+\(\)?"),  # call()
    re.compile(r"\b[a-z0-9]+_[a-z0-9_]+\b", re.IGNORECASE),  # snake_case
    re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b"),  # camelCase
    re.compile(r"\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b"),  # PascalCase
    re.compile(r"\b[A-Z][A-Z0-9]{1,}\b"),  # README, API, CI
    re.compile(r"\b\d+\.\d+(?:\.\d+)*\b"),  # a version
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"#\d+\b"),  # an issue
    re.compile(r":\d+\b"),  # file:line
    re.compile(r"(?m)^\s*(?:\d+[.)]|[-*])\s+\S"),  # a list
    re.compile(r"[`\"“「'][^`\"”」']{2,}[`\"”」']"),  # quoted text
)
_HANGUL = re.compile(r"[가-힣]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


def is_vague(args: str) -> bool:
    """True for a short request with nothing concrete to aim at.

    "ralph fix this", "개선해줘", "게임 만들어줘", "improve performance" are
    vague; "fix the flaky test", "README 정리해줘", "pytest 실패 고쳐줘" and
    "결제 실패 시 재시도 로직 추가해줘" are not.  At most three words (or
    eojeol) with no anchor; a Latin word inside Korean is a technical term,
    so it is an anchor.  The gate guards against gestures; it is not a review
    of the task.
    """
    # Quotes around the whole argument are how commands are typed, not an anchor.
    raw = str(args or "").strip().strip(_QUOTES).strip()
    if not raw or "\n" in raw:
        return False
    if any(pattern.search(raw) for pattern in _ANCHORS):
        return False
    if _HANGUL.search(raw) and _LATIN_WORD.search(raw):
        return False
    text = _clean(raw)
    return len(text.split()) <= VAGUE_MAX_WORDS and len(text) <= VAGUE_MAX_CHARS


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def plan_request(
    plan: Plan, *, unit: str = "story", field: str = "plan_step", only: str | None = None
) -> str:
    """The task text a planner call gets when it is to carry out ``plan``.

    ``unit`` is what the command calls one piece of work (story, task,
    subtask); ``field`` is the key each piece carries the step id in; ``only``
    narrows the run to one step ("S3 진행해줘").
    """
    scope = (
        f"Work on step {only} only this time: make {unit}s for {only} alone."
        if only
        else (
            f"Make one {unit} per pending plan step, in plan order (split a step only "
            f"when it is too big for one {unit}; when there are more steps than "
            f"{unit}s allowed, group consecutive steps). Skip steps marked done."
        )
    )
    return (
        f'Carry out the user\'s current plan "{plan.title}", saved at '
        f"{plan_store.CURRENT_PATH}. {scope} Give every {unit} a \"{field}\" field naming "
        f"the step id it implements, or a list of ids for a grouped one."
        f"\n\nPlan steps:\n{plan_store.steps_text(plan)}"
        f"\n\nThe plan:\n{plan_store.prompt_markdown(plan)}"
    )


def plan_context(task: str, plan: Plan) -> str:
    """``task`` with the current plan attached.

    A concrete task is a new task, and the plan is only background; a vague
    one ("개선해줘") may well be about the plan, so it is not told otherwise.
    """
    if is_vague(task):
        lead = "The user's current plan, which this request may be about"
    else:
        lead = (
            "The user's current plan, for context only — the request above is a new task, "
            "not this plan; use the plan where relevant"
        )
    return (
        f"{task}\n\n{lead} ({plan_store.CURRENT_PATH}): \"{plan.title}\"\n"
        f"{plan_store.steps_text(plan)}"
    )


def plan_label(plan: Plan) -> str:
    """The short task a plan run records (prd.json ``task``, a team's brief)."""
    return f"{plan.title} (the current plan, {plan_store.CURRENT_PATH})"


def step_ids(value: Any) -> tuple[str, ...]:
    """A ``plan_step`` field — one id, a list, or "S1, S2" — as a tuple of ids."""
    if isinstance(value, str):
        items = re.split(r"[,\s]+", value)
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        return ()
    return tuple(dict.fromkeys(item.strip().upper() for item in items if item.strip()))


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

TEXT: dict[str, dict[str, str]] = {
    "ko": {
        "header": "계획 먼저",
        "question": "'{arg}' 는 아직 무엇을 할지 정해지지 않은 요청입니다. 어떻게 할까요?",
        "plan": "/ralplan 으로 계획부터 (추천)",
        "plan_hint": "계획을 세우고 저장한 뒤 /{command} 로 실행합니다",
        "run": "그대로 실행",
        "run_hint": "이 문장 그대로 /{command} 를 돌립니다",
        "cancel": "취소",
        "cancel_hint": "아무것도 하지 않습니다",
        "queued": "{command}: 먼저 계획합니다 — /ralplan {arg} 를 대기열에 넣었습니다. "
        "계획이 저장되면 /{command} 만 입력해 실행하세요.",
        "cancelled": "{command}: 취소했습니다.",
        "no_answer": "{command}: 답이 없어 입력한 그대로 실행합니다.",
    },
    "en": {
        "header": "Plan first",
        "question": "'{arg}' does not say what to build yet. How should we go on?",
        "plan": "Plan it first with /ralplan (recommended)",
        "plan_hint": "make and save a plan, then run it with /{command}",
        "run": "Run it as typed",
        "run_hint": "run /{command} on exactly these words",
        "cancel": "Cancel",
        "cancel_hint": "do nothing",
        "queued": "{command}: planning first — queued /ralplan {arg}. Once the plan is "
        "saved, type /{command} alone to run it.",
        "cancelled": "{command}: cancelled.",
        "no_answer": "{command}: no answer to the planning question; running it as typed.",
    },
}


def gate_mode(core: Any) -> str:
    """``planning.gate``: ``"ask"`` (default) or ``"off"``."""
    planning = getattr(getattr(core, "settings", None), "planning", None)
    value = str(getattr(planning, "gate", "ask") or "ask").strip().lower()
    return "off" if value == "off" else "ask"


def attended(session: Any, core: Any = None) -> bool:
    """True when a person can answer a question for this session.

    Never for an unattended session (a schedule, a named agent's job).  Yes
    for one with an origin connection (a TUI, an IDE, ``snowpea -c``), and for
    a messenger chat whose binding has an approver: chats answer ``ask_user``
    with buttons (``gateway/router.py``).
    """
    if getattr(session, "unattended", False):
        return False
    if getattr(session, "origin_conn", None) is not None:
        return True
    surface = str(getattr(session, "origin_surface", "") or "")
    if not surface.startswith("gateway:"):
        return False
    check = getattr(getattr(core, "gateway", None), "has_approver", None)
    return bool(check is not None and check(session))


def _language(ctx: Any, text: str) -> str:
    if re.search(r"[가-힣]", text):
        return "ko"
    try:
        from snowpea_core.agent.agent import session_reply_language

        tag = str(session_reply_language(ctx.core, ctx.session) or "")
    except Exception:  # noqa: BLE001 - the wording is not worth failing over
        tag = ""
    return "ko" if tag.lower().startswith("ko") else "en"


async def ask_before_running(ctx: Any, command: str, args: str, *, forced: bool = False) -> bool:
    """The plan-first gate.  True means run the command; False means it is handled.

    Only ever asks about a vague argument when there is no active plan to run
    instead; the caller has already ruled out a resumable PRD.  An unattended
    session is never asked — a schedule that types ``/ralph 개선해줘`` gets what
    it got before — but the run is logged so a surprising result can be traced.
    """
    if forced or gate_mode(ctx.core) == "off" or not is_vague(args):
        return True
    if not attended(ctx.session, ctx.core):
        log.info("/%s %r is vague but the session is unattended; running it ungated", command, args)
        return True
    queue = getattr(ctx.core, "questions", None)
    if queue is None:
        return True
    from snowpea_core.server.protocol import QuestionItem, QuestionOption

    arg = _clean(args)
    text = TEXT[_language(ctx, arg)]
    labels = {key: text[key] for key in ("plan", "run", "cancel")}
    answers = await queue.ask(
        ctx.session,
        [
            QuestionItem(
                header=text["header"],
                question=text["question"].format(arg=arg),
                options=[
                    QuestionOption(
                        label=labels[key],
                        description=text[f"{key}_hint"].format(command=command),
                    )
                    for key in ("plan", "run", "cancel")
                ],
                multi=False,
                allowOther=False,
            )
        ],
        cancel_event=getattr(ctx.session, "interrupt", None),
    )
    answer = answers[0]
    picked = {" ".join(str(item).split()).casefold() for item in answer.selected}
    if answer.text:
        picked.add(" ".join(str(answer.text).split()).casefold())

    def chose(key: str) -> bool:
        return " ".join(labels[key].split()).casefold() in picked

    if chose("plan"):
        from snowpea_core.agent import loop as agent_loop

        agent_loop.start_turn(ctx.core, ctx.session, f"/ralplan {arg}")
        await ctx.say(text["queued"].format(command=command, arg=arg))
        return False
    if chose("cancel"):
        await ctx.say(text["cancelled"].format(command=command))
        return False
    if not chose("run"):
        # A headless client declines every question and a timeout is nobody
        # there; neither is a reason to drop what was typed.
        log.info("/%s %r: plan-first question went unanswered; running it", command, args)
        await ctx.say(text["no_answer"].format(command=command))
    return True


__all__ = [
    "TEXT",
    "VAGUE_MAX_CHARS",
    "VAGUE_MAX_WORDS",
    "ask_before_running",
    "attended",
    "is_carry_on",
    "gate_mode",
    "is_execute_request",
    "is_vague",
    "plan_context",
    "plan_label",
    "plan_request",
    "step_ids",
    "strip_bypass",
    "target_step",
]
