"""``ask_user`` and ``queue_command``: the agent asking a human, and acting on it.

``ask_user`` exists because "(a) … (b) … (c) …" typed into a chat transcript is
not a question, it is a guess that the reader will type back the right letter.
A closed set of answers deserves a picker, so the tool hands the options to the
client and the TUI draws an arrow-key list.

The argument schema is deliberately a *superset* of two tools people already
have skills written against:

* **Claude Code's ``AskUserQuestion``** — ``questions[]``, each with ``header``,
  ``question``, ``options[]`` of ``{label, description}``, ``multiSelect``, and
  an optional ``preview``.  A skill ported from a Claude Code plugin works
  here unchanged.
* **Hermes' ``clarify``** — ``questions[]`` of ``{question, choices[]}`` with
  ``multi_select``; ``choices`` are bare strings.

Both normalise to the same internal question, and either may be written flat
(``question`` / ``options`` at the top level) when there is only one.  "Other"
is added by the client, not by the caller, exactly as in both originals.

``queue_command`` is the other half of an interview that ends in a choice.  A
tool cannot *run* a slash command: ``CommandRegistry.start``/``run`` overwrite
``session.turn_task`` and ``session.current_turn``, which the turn calling the
tool is still using, and a skill command would re-enter the agent loop on the
same session.  The one safe seam is :func:`agent_loop.start_turn`, which
appends to ``session.queued_turns`` while a turn is in flight — so the command
the user picked runs the moment this turn ends, in its own turn, with its own
history.  Queued, not run inline, and the difference is why.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.server.protocol import QuestionOption
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

#: Options a single question may offer.  Claude Code caps at 4 and Hermes at 5;
#: eight is the most a terminal list stays readable at.
MIN_OPTIONS = 2
MAX_OPTIONS = 8
#: Questions one call may ask, in sequence.  Matches Hermes' ``clarify``.
MAX_QUESTIONS = 5


class AskUserError(ValueError):
    """A malformed ``ask_user`` call; the message is what the model is told."""


def _option(raw: Any) -> QuestionOption:
    """One option from either shape: a Hermes string, or a Claude Code object."""
    if isinstance(raw, str):
        return QuestionOption(label=raw.strip())
    if not isinstance(raw, dict):
        raise AskUserError("each option must be a string or an object with a label")
    label = str(raw.get("label") or raw.get("title") or raw.get("value") or "").strip()
    if not label:
        raise AskUserError("each option needs a non-empty label")
    return QuestionOption(
        label=label,
        description=str(raw.get("description") or raw.get("hint") or "").strip(),
        preview=str(raw.get("preview") or "").strip(),
    )


def _first(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """The first key that is actually present; the shapes spell things differently."""
    for key in keys:
        if raw.get(key) is not None:
            return raw[key]
    return default


class _Question:
    """One normalised question, whichever shape it arrived in."""

    def __init__(self, raw: dict[str, Any]) -> None:
        text = str(_first(raw, "question", "prompt", "text", default="") or "").strip()
        if not text:
            raise AskUserError("every question needs a non-empty 'question'")
        self.text = text
        self.header = str(_first(raw, "header", "title", default="") or "").strip()[:24]
        raw_options = _first(raw, "options", "choices", default=[]) or []
        if not isinstance(raw_options, list):
            raise AskUserError("'options' must be a list")
        self.options = [_option(item) for item in raw_options]
        if self.options and not MIN_OPTIONS <= len(self.options) <= MAX_OPTIONS:
            raise AskUserError(
                f"a question with options needs between {MIN_OPTIONS} and "
                f"{MAX_OPTIONS} of them; got {len(self.options)}. "
                "Ask a narrower question, or leave options out for free text."
            )
        self.multi = bool(_first(raw, "multi", "multiSelect", "multi_select", default=False))
        self.allow_other = bool(
            _first(raw, "allow_other", "allowOther", "allow_free_text", default=True)
        )


def _questions(args: dict[str, Any]) -> list[_Question]:
    """Every question this call asks, flat shape or ``questions[]``."""
    batch = args.get("questions")
    if isinstance(batch, list) and batch:
        if len(batch) > MAX_QUESTIONS:
            raise AskUserError(f"at most {MAX_QUESTIONS} questions per call; got {len(batch)}")
        return [_Question(item if isinstance(item, dict) else {"question": item}) for item in batch]
    return [_Question(args)]


def _describe(question: _Question, answer: Any) -> str:
    """One question's answer, in the words the model should read back."""
    head = f"[{question.header}] " if question.header else ""
    if answer.timed_out:
        return f"{head}{question.text}\n  timed out: nobody answered."
    if answer.declined:
        return f"{head}{question.text}\n  declined: the user did not answer this."
    lines = [f"{head}{question.text}"]
    if answer.selected:
        lines.append("  선택: " + ", ".join(answer.selected))
    if answer.text:
        lines.append(f"  answer: {answer.text}")
    return "\n".join(lines)


async def ask_user(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Put one or more questions to the human and block until they answer."""
    try:
        questions = _questions(args)
    except AskUserError as exc:
        return ToolResult(ok=False, error=str(exc))

    queue = getattr(ctx.core, "questions", None)
    if queue is None:  # pragma: no cover - a core without a queue is a test double
        return ToolResult(ok=False, error="this daemon cannot ask the user questions")

    answers: list[Any] = []
    payload: list[dict[str, Any]] = []
    by_question: dict[str, list[str]] = {}
    for index, question in enumerate(questions, start=1):
        answer = await queue.ask(
            ctx.session,
            question.text,
            header=question.header,
            options=question.options,
            multi=question.multi,
            allow_other=question.allow_other,
            cancel_event=getattr(ctx.session, "interrupt", None),
            index=index,
            total=len(questions),
        )
        answers.append(answer)
        payload.append(
            {
                "question": question.text,
                "header": question.header,
                "selected": list(answer.selected),
                "text": answer.text,
                "timed_out": answer.timed_out,
                "declined": answer.declined,
            }
        )
        by_question[question.text] = list(answer.selected)
        # A person who walked away or said no is not going to answer the next
        # one either; asking anyway just burns the remaining timeouts.
        if answer.timed_out or answer.declined:
            break

    first = answers[0]
    output = "\n".join(_describe(q, a) for q, a in zip(questions, answers, strict=False))
    if first.timed_out:
        output += (
            "\n\nNo interactive client answered. Say what you will assume and why, "
            "or ask again once the user is back."
        )
    elif first.declined:
        output += "\n\nThe user declined to choose. Do not treat this as agreement."
    return ToolResult(
        ok=True,
        output=output,
        meta={
            "answers": payload,
            "byQuestion": by_question,
            "selected": list(first.selected),
            "text": first.text,
            "timed_out": first.timed_out,
            "declined": first.declined,
        },
    )


async def queue_command(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Queue a slash command the user just chose; it runs after this turn."""
    text = str(args.get("command") or "").strip()
    if not text.startswith("/"):
        return ToolResult(
            ok=False, error="command must start with '/', e.g. '/ralph fix the build'"
        )
    parsed = ctx.core.commands.parse(text)
    if parsed is None:
        return ToolResult(ok=False, error=f"{text!r} is not a slash command")
    name, _ = parsed
    if ctx.core.commands.get(name) is None:
        return ToolResult(ok=False, error=f"/{name} is not a registered command")

    from snowpea_core.agent import loop as agent_loop

    turn_id = agent_loop.start_turn(ctx.core, ctx.session, text)
    return ToolResult(
        ok=True,
        output=(
            f"queued: {text}\n"
            "It starts as its own turn the moment this one ends. "
            "Finish what you are saying; do not wait for it here."
        ),
        meta={"command": name, "turnId": turn_id},
    )


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="ask_user",
        category="interaction",
        description=descriptions.ASK_USER,
        input_schema={
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": (
                        f"Up to {MAX_QUESTIONS} questions, asked one at a time. "
                        "Use this form for more than one question; otherwise use the "
                        "flat 'question'/'options' fields."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "header": {
                                "type": "string",
                                "description": "Short chip above the question, e.g. 'Auth method'.",
                            },
                            "question": {
                                "type": "string",
                                "description": "The full question, ending in '?'.",
                            },
                            "options": {
                                "type": "array",
                                "description": (
                                    f"{MIN_OPTIONS}-{MAX_OPTIONS} answers. Put the one you "
                                    "recommend first and mark it '(추천)' / '(recommended)'."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "1-5 words; this is what comes back.",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "One line on the trade-off it buys.",
                                        },
                                        "preview": {
                                            "type": "string",
                                            "description": "Monospace block; show, don't tell.",
                                        },
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": "Allow more than one option (default false).",
                            },
                        },
                        "required": ["question"],
                    },
                },
                "header": {"type": "string", "description": "Short chip, for the flat form."},
                "question": {"type": "string", "description": "The question, for the flat form."},
                "options": {
                    "type": "array",
                    "description": (
                        f"{MIN_OPTIONS}-{MAX_OPTIONS} answers for the flat form; objects with "
                        "a label and a description, or plain strings."
                    ),
                    "items": {"type": "object"},
                },
                "multi": {"type": "boolean", "description": "Allow more than one option."},
                "allow_other": {
                    "type": "boolean",
                    "description": "Offer a free-text '기타 / Other' row (default true).",
                },
            },
        },
        permission="read",
        run=ask_user,
    ),
    Tool(
        name="queue_command",
        category="interaction",
        description=descriptions.QUEUE_COMMAND,
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The whole line, e.g. '/ralph voxel sandbox'.",
                }
            },
            "required": ["command"],
        },
        permission="read",
        run=queue_command,
    ),
)


__all__ = [
    "MAX_OPTIONS",
    "MAX_QUESTIONS",
    "MIN_OPTIONS",
    "TOOLS",
    "AskUserError",
    "ask_user",
    "queue_command",
]
