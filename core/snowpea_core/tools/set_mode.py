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

A session in plan-then-auto (``auto_after_plan``, "Allow Everything" pressed
while planning, 1.32.0) gets a different picker when the model asks to leave
plan: like Claude Code's plan exit, it shows the plan itself for review and
offers auto first, accept second and "keep revising" third, with free text for
what to change.  A revision keeps the session planning, flag and all, and hands
the user's words back to the model.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, cast

from snowpea_core.permissions.policy import Mode
from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.server.protocol import QuestionItem, QuestionOption
from snowpea_core.session import events
from snowpea_core.tools.delegate import delegation_language
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

log = logging.getLogger("snowpea.tools.set_mode")

#: How often the picker checks whether the user changed mode some other way.
MODE_POLL_SECONDS = 0.25

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
        "already": "이미 {mode} 모드입니다. 물어볼 필요 없이 바로 작업을 계속하세요.",
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
        "already": "Already in {mode} mode. Nothing to ask; carry on with the work now.",
    },
}


#: The same picker for a browser session (originSurface / clientKind
#: "browser"): its modes read as how much the agent may do on a page, and
#: nothing is "implemented" there.
BROWSER_TEXT: dict[str, dict[str, Any]] = {
    "ko": {
        "header": "모드 전환",
        "question": "어떻게 진행할까요?",
        "labels": {
            "accept": "안전 모드로 전환",
            "auto": "전체 권한으로 전환",
            "plan": "읽기 전용 유지",
        },
        "descriptions": {
            "accept": "페이지를 조작하되, 위험한 동작은 먼저 물어봅니다",
            "auto": "묻지 않고 실행합니다",
            "plan": "페이지를 읽기만 하고 바꾸지 않습니다",
        },
        "names": {"accept": "안전", "auto": "전체 권한", "plan": "읽기 전용"},
        "switched": "{label} 모드로 바꿨습니다. 작업을 계속합니다.",
        "kept": "사용자가 읽기 전용을 유지하기로 했습니다. 페이지를 바꾸지 말고, "
        "읽은 내용만으로 답한 뒤 이 턴을 끝내세요.",
        "declined": "사용자가 답하지 않았습니다. 읽기 전용 그대로입니다. "
        "페이지를 바꾸지 말고 이 턴을 끝내세요.",
        "already": "이미 {label} 모드입니다. 물어볼 필요 없이 작업을 계속하세요.",
    },
    "en": {
        "header": "Switch mode",
        "question": "How should we continue?",
        "labels": {
            "accept": "Switch to safe mode",
            "auto": "Switch to full access",
            "plan": "Stay read-only",
        },
        "descriptions": {
            "accept": "act on pages, but ask before anything risky",
            "auto": "runs without asking",
            "plan": "read pages only, change nothing",
        },
        "names": {"accept": "safe mode", "auto": "full access", "plan": "read-only"},
        "switched": "Now in {label}. Continuing.",
        "kept": "The user chose to stay read-only. Change nothing on the page; answer "
        "from what you read and end this turn.",
        "declined": "The user did not answer. Still read-only. Change nothing on the "
        "page and end this turn.",
        "already": "Already in {label}. Nothing to ask; carry on.",
    },
}


#: The plan-review picker of a plan-then-auto session (1.32.0), in the order
#: offered: auto first, because "Allow Everything" already chose it.
REVIEW_MODES: tuple[str, ...] = ("auto", "accept", "plan")

REVIEW_TEXT: dict[str, dict[str, Any]] = {
    "ko": {
        "header": "계획 검토",
        "question": "계획이 끝났습니다. 검토하고 어떻게 진행할지 골라 주세요. "
        "고칠 점은 직접 입력해도 됩니다.",
        "labels": {
            "auto": "Auto 모드로 구현 시작 (추천)",
            "accept": "accept 모드로 구현 시작",
            "plan": "계획 계속 수정",
        },
        "descriptions": {
            "auto": "묻지 않고 실행합니다",
            "accept": "파일 편집은 바로, 셸 명령은 물어봅니다",
            "plan": "plan 모드에 남아 계획을 고칩니다",
        },
        "plan_file": "계획 파일: {path} (계획 내용은 위 대화에 있습니다)",
        "more": "… (나머지는 {path} 에 있습니다)",
        "switched_with": "모드를 {mode} 로 바꿨습니다. 구현을 시작하되, 사용자의 다음 요청을 "
        "따르세요: {text}",
        "revise": "사용자가 계획을 더 고치기로 했습니다. plan 모드 그대로입니다. "
        '다음 요청대로 계획을 고친 뒤, 다시 검토하고 저장하고 set_mode("auto")를 '
        "다시 호출하세요: {text}",
        "revise_plain": "사용자가 계획을 더 고치기로 했습니다. plan 모드 그대로입니다. "
        "무엇을 고칠지 ask_user 로 한 번 물어본 뒤 계획을 고치고, 다시 검토하고 "
        '저장하고 set_mode("auto")를 다시 호출하세요.',
    },
    "en": {
        "header": "Review plan",
        "question": "The plan is done. Review it and choose how to proceed; "
        "you can also type what to change.",
        "labels": {
            "auto": "Start implementing in auto mode (recommended)",
            "accept": "Start implementing in accept mode",
            "plan": "Keep revising the plan",
        },
        "descriptions": {
            "auto": "runs without asking",
            "accept": "edits apply straight away, shell commands ask first",
            "plan": "stay in plan mode and change the plan",
        },
        "plan_file": "Plan file: {path} (the plan is shown above)",
        "more": "… (the rest is in {path})",
        "switched_with": "Mode is now {mode}. Start the implementation, following the "
        "user's words: {text}",
        "revise": "The user wants the plan revised. Still in plan mode. Revise the plan "
        'per the following, then review it again, save it and call set_mode("auto") '
        "again: {text}",
        "revise_plain": "The user wants the plan revised. Still in plan mode. Ask once "
        "with ask_user what to change, revise the plan, review it again, save it and "
        'call set_mode("auto") again.',
    },
}

#: The longest plan copied into the transcript; the rest stays in the file.
REVIEW_MAX_CHARS = 12000

#: Short answers typed instead of picking a row.
REVIEW_ALIASES: dict[str, str] = {
    "1": "auto",
    "auto": "auto",
    "오토": "auto",
    "자동": "auto",
    "2": "accept",
    "accept": "accept",
    "수락": "accept",
    "3": "plan",
    "plan": "plan",
    "계획": "plan",
    "수정": "plan",
    "계속 수정": "plan",
    "계획 수정": "plan",
}


def _is_plan_document(path: str) -> bool:
    """A file under ``.snowpea/plans/`` or named ``PLAN*.md``: a plan, not just a doc."""
    from pathlib import PurePosixPath

    normal = str(path).replace("\\", "/")
    name = PurePosixPath(normal).name.casefold()
    return "/.snowpea/plans/" in f"/{normal}" or (name.startswith("plan") and name.endswith(".md"))


async def _plan_for_review(ctx: ToolContext) -> tuple[str, str]:
    """``(markdown, path)`` of the plan this pass produced; empty when none.

    The saved plan (``plan_save``) wins; then the newest plan document the
    pass wrote, preferring ``.snowpea/plans/`` and ``PLAN*.md`` over any other
    markdown it touched.  Written files are read through the session's
    backend, where the planner wrote them, so a docker or ssh session shows its
    own; the saved plan is read from the local project, where ``plan_store``
    keeps it.
    """
    from pathlib import Path

    from snowpea_core.agent import plan_store

    session = ctx.session
    if getattr(session, "plan_saved", False):
        try:
            plan = plan_store.load_current(session.workdir)
        except Exception:  # noqa: BLE001 - a broken plan file must not block the picker
            plan = None
        if plan is not None and plan.markdown.strip():
            return plan.markdown.strip(), plan_store.CURRENT_PATH
    written = list(reversed(list(getattr(session, "plan_files", None) or [])))
    ordered = [p for p in written if _is_plan_document(p)] + [
        p for p in written if not _is_plan_document(p)
    ]
    backend = getattr(ctx, "backend", None) or getattr(session, "backend", None)
    for path in ordered:
        try:
            if backend is not None:
                text = (await backend.read_file(str(path))).strip()
            else:
                target = Path(path) if Path(path).is_absolute() else Path(session.workdir) / path
                text = target.read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001 - a vanished file is just not the plan
            continue
        if text:
            return text, str(path)
    return "", ""


def _clip_plan(markdown: str, path: str, text: dict[str, Any]) -> str:
    """The plan for the transcript, cut at a line boundary only when too long."""
    if len(markdown) <= REVIEW_MAX_CHARS:
        return markdown
    head = markdown[:REVIEW_MAX_CHARS]
    if "\n" in head:
        head = head[: head.rfind("\n")]
    return f"{head.rstrip()}\n\n{str(text['more']).format(path=path)}"


#: The snowpea browser's coding agent: a browser session that edits a project.
CODING_AGENTS = frozenset({"browser-code"})


def _is_browser(ctx: ToolContext) -> bool:
    """True for a browser session that browses; its coding agent reads as a coder.

    A session running a named definition with every tool (``allowed_tools``
    ``None``) codes like the CLI, so it gets the coding wording too.
    """
    session = ctx.session
    agent = getattr(session, "agent", None)
    if agent in CODING_AGENTS or (agent and getattr(session, "allowed_tools", ()) is None):
        return False
    if getattr(session, "origin_surface", None) == "browser":
        return True
    conn = getattr(session, "origin_conn", None)
    return getattr(conn, "client_kind", None) == "browser"


def _normal(label: str) -> str:
    """A label as compared: markers, spacing and case do not count."""
    for marker in RECOMMENDED.values():
        label = label.replace(marker, "")
    return " ".join(label.split()).casefold()


def _language(ctx: ToolContext) -> str:
    """``ko`` or ``en``; anything the picker has no wording for reads as English."""
    tag = str(delegation_language(ctx) or "en").strip().lower()
    return "ko" if tag.startswith("ko") or "korean" in tag or "한국" in tag else "en"


def _options(
    requested: str, language: str, table: dict[str, dict[str, Any]] | None = None
) -> list[tuple[str, QuestionOption]]:
    """The picker rows: the requested mode first and marked, then the rest.

    Returned as ``(mode, option)`` pairs so the answer's label — the only thing
    that comes back over ``question.respond`` — maps to a mode without parsing
    the human-facing text again.
    """
    text = (table or TEXT)[language]
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
    table = BROWSER_TEXT if _is_browser(ctx) else TEXT
    text = table[language]

    # The picker exists to leave plan mode. A session already out of it (the
    # user approved the plan by switching modes themselves) has nothing to
    # ask: a second picker there was dismissed and then reported as "still in
    # plan mode", and the agent stopped instead of starting.
    current = ctx.session.mode
    if current != "plan" and requested != "plan":
        label = str(text.get("names", {}).get(current, current))
        return ToolResult(
            ok=True,
            output=str(text["already"]).format(mode=current, label=label),
            meta={"requested": requested, "mode": current, "changed": False, "asked": False},
        )
    reason = str(args.get("reason") or "").strip()
    if (
        current == "plan"
        and requested != "plan"
        and getattr(ctx.session, "auto_after_plan", False)
        and table is TEXT
    ):
        return await _review_plan(ctx, queue, requested, language, reason)
    rows = _options(requested, language, table)
    question = f"{reason}\n{text['question']}".strip() if reason else text["question"]
    if current == "plan" and requested != "plan" and table is TEXT:
        # Leaving plan is approving the plan, so it is shown before the ask.
        question += await _show_plan(ctx, language)

    # The user may answer by switching mode somewhere else (the IDE's mode
    # selector, /mode in the TUI) instead of in the picker. That ends the
    # question too, and counts as their answer.
    stop = asyncio.Event()
    watcher = asyncio.ensure_future(_watch_mode(ctx.session, current, stop))
    try:
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
            cancel_event=stop,
        )
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    answer = answers[0]
    log.info(
        "set_mode %s: requested=%s selected=%s by=%s declined=%s timed_out=%s mode=%s",
        ctx.session.id,
        requested,
        list(answer.selected),
        getattr(answer, "by", None),
        answer.declined,
        answer.timed_out,
        ctx.session.mode,
    )
    if ctx.session.mode != current:
        # Switched elsewhere while the picker was up: that is the answer.
        mode = ctx.session.mode
        return ToolResult(
            ok=True,
            output=_switched(text, mode) if mode != "plan" else text["kept"],
            meta={"requested": requested, "mode": mode, "changed": True, "asked": True},
        )

    # Matched loosely: a surface may echo the label without the "(추천)"
    # marker or with its own spacing, and a typed answer may name the option.
    chosen = {_normal(item) for item in answer.selected}
    if getattr(answer, "text", None):
        chosen.add(_normal(str(answer.text)))
    picked = next(
        (mode for mode, option in rows if _normal(option.label) in chosen),
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
        if current != "plan":
            label = str(text.get("names", {}).get(current, current))
            return ToolResult(
                ok=True, output=str(text["already"]).format(mode=current, label=label), meta=meta
            )
        return ToolResult(ok=True, output=text["declined"], meta=meta)
    if picked == ctx.session.mode:
        meta["mode"] = picked
        return ToolResult(
            ok=True,
            output=text["kept"] if picked == "plan" else _switched(text, picked),
            meta=meta,
        )

    mode = await ctx.core.sessions.set_mode(ctx.session, cast(Mode, picked))
    flag = bool(getattr(ctx.session, "auto_after_plan", False))
    await ctx.core.hub.emit_event(ctx.session.id, events.mode_changed(mode, auto_after_plan=flag))
    meta["mode"] = mode
    meta["changed"] = True
    return ToolResult(ok=True, output=_switched(text, mode), meta=meta)


async def _show_plan(ctx: ToolContext, language: str) -> str:
    """Put the plan in the transcript; the line the question adds, or ``""``.

    The plan goes into the transcript as a message of its own, not into the
    picker: a question is drawn in the live region, and a plan as tall as the
    terminal there makes Ink clear the screen every frame.
    """
    text = REVIEW_TEXT[language]
    markdown, path = await _plan_for_review(ctx)
    if not markdown:
        return ""
    await ctx.core.hub.emit_event(
        ctx.session.id,
        events.message_done(_clip_plan(markdown, path, text), kind="plan"),
    )
    return "\n" + str(text["plan_file"]).format(path=path)


async def _review_plan(
    ctx: ToolContext, queue: Any, requested: str, language: str, reason: str
) -> ToolResult:
    """The plan-then-auto exit: show the plan, offer auto first, take revisions.

    Whatever the model asked for, auto leads: "Allow Everything" already said
    how the user wants the implementation to run.  Picking "keep revising" or
    typing anything keeps the session in plan with the flag on, and the typed
    words go back to the model as the revision to make.
    """
    text = REVIEW_TEXT[language]
    question = "\n\n".join(part for part in (reason, text["question"]) if part)
    question += await _show_plan(ctx, language)
    rows = [
        (mode, QuestionOption(label=text["labels"][mode], description=text["descriptions"][mode]))
        for mode in REVIEW_MODES
    ]
    stop = asyncio.Event()
    watcher = asyncio.ensure_future(_watch_mode(ctx.session, "plan", stop))
    try:
        answers = await queue.ask(
            ctx.session,
            [
                QuestionItem(
                    header=text["header"],
                    question=question,
                    options=[option for _, option in rows],
                    multi=False,
                    allowOther=True,
                )
            ],
            cancel_event=stop,
        )
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    answer = answers[0]
    meta: dict[str, Any] = {
        "requested": requested,
        "selected": list(answer.selected),
        "mode": ctx.session.mode,
        "changed": False,
        "asked": True,
        "review": True,
        "timed_out": answer.timed_out,
        "declined": answer.declined,
    }
    plain = TEXT[language]
    if ctx.session.mode != "plan":
        # Switched elsewhere while the picker was up: that is the answer.
        meta["mode"] = ctx.session.mode
        meta["changed"] = True
        return ToolResult(ok=True, output=_switched(plain, ctx.session.mode), meta=meta)
    chosen = {_normal(item) for item in answer.selected}
    picked = next((mode for mode, option in rows if _normal(option.label) in chosen), None)
    typed = str(getattr(answer, "text", None) or "").strip()
    if picked is None and typed:
        # A typed answer that names an option ("auto", "2", "수정") is that option.
        picked = next(
            (mode for mode, option in rows if _normal(option.label) == _normal(typed)),
            REVIEW_ALIASES.get(_normal(typed).rstrip(".")),
        )
        if picked is not None:
            typed = ""
    if picked in ("auto", "accept"):
        mode = await ctx.core.sessions.set_mode(ctx.session, cast(Mode, picked))
        await ctx.core.hub.emit_event(
            ctx.session.id, events.mode_changed(mode, auto_after_plan=False)
        )
        meta["mode"] = mode
        meta["changed"] = True
        if typed:
            # A row picked with words typed beside it: start, and do it their way.
            meta["instruction"] = typed
            output = str(text["switched_with"]).format(mode=mode, text=typed)
            return ToolResult(ok=True, output=output, meta=meta)
        return ToolResult(ok=True, output=_switched(plain, mode), meta=meta)
    if typed:
        meta["revision"] = typed
        return ToolResult(ok=True, output=str(text["revise"]).format(text=typed), meta=meta)
    if picked == "plan":
        return ToolResult(ok=True, output=text["revise_plain"], meta=meta)
    # A decline or a timeout: no permission to start anything.
    return ToolResult(ok=True, output=plain["declined"], meta=meta)


async def _watch_mode(session: Any, start: str, stop: asyncio.Event) -> None:
    """Set ``stop`` on an interrupt or once the session leaves ``start`` mode."""
    interrupt = getattr(session, "interrupt", None)
    while not stop.is_set():
        if getattr(session, "mode", start) != start or (
            interrupt is not None and interrupt.is_set()
        ):
            stop.set()
            return
        await asyncio.sleep(MODE_POLL_SECONDS)


def _switched(text: dict[str, Any], mode: str) -> str:
    label = str(text.get("names", {}).get(mode, mode))
    return str(text["switched"]).format(mode=mode, label=label)


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


__all__ = [
    "BROWSER_TEXT",
    "MODES",
    "RECOMMENDED",
    "REVIEW_MODES",
    "REVIEW_TEXT",
    "TEXT",
    "TOOLS",
    "set_mode",
]
