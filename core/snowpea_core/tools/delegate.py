"""The ``delegate_task`` tool (M7 contract §3).

Hand a self-contained task to a child agent and get its final answer back as
the tool result.  Everything else — the concurrency limit, the child session,
the ``subagent.*`` events — is :mod:`snowpea_core.agent.subagent`'s job.

This replaces the inactive M1 stub of the same name; the name, category and
permission tag are unchanged, so a client that read ``tool.list`` before M7
sees the same tool simply become ``active``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.agent import reply_language
from snowpea_core.agent.definition import builtin_agent_definitions
from snowpea_core.agent.role_pick import PARENT
from snowpea_core.agent.subagent import (
    BUDGET,
    COMPLETE,
    BackgroundRun,
    SubagentResult,
    get_manager,
)
from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.prompts.compose import language_name
from snowpea_core.session.history import message_text
from snowpea_core.tools.output_spill import spill, spill_chars
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.util.lang import DEFAULT_LANGUAGE, detect_language

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.session import Session

#: Upper bound on ``timeout``, mirroring the ``shell`` tool's own ceiling.
MAX_TIMEOUT = 3600.0


def _builtin_agent_names() -> str:
    return ", ".join(defn.name for defn in builtin_agent_definitions())


BUILTIN_AGENT_NAMES = _builtin_agent_names()
BUILTIN_AGENT_HINT = (
    f" Built-in agents available by name: {BUILTIN_AGENT_NAMES}." if BUILTIN_AGENT_NAMES else ""
)


def _tool_list(value: Any) -> list[str] | None:
    if isinstance(value, str):
        names = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        names = [str(part).strip() for part in value]
    else:
        return None
    filtered = [name for name in names if name]
    return filtered or None


def _timeout(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(MAX_TIMEOUT, seconds)


def last_user_text(session: Session) -> str:
    """The most recent thing the user themselves wrote, or ``""``."""
    for message in reversed(session.history.snapshot()):
        if message.role == "user":
            return message_text(message).strip()
    return ""


def detected_language(session: Session) -> str:
    """Language of the session's last user message, cached on the session.

    The cache key is the text itself, so a new user message refreshes it
    without a hook in the turn path: the agent loop is owned elsewhere, and a
    lazy read here costs one history scan per delegation.
    """
    text = last_user_text(session)
    if not text:
        return session.detected_language or DEFAULT_LANGUAGE
    if session.detected_language and session.detected_language_source == text:
        return session.detected_language
    tag = detect_language(text)
    session.detected_language = tag
    session.detected_language_source = text
    return tag


def delegation_language(ctx: ToolContext) -> str:
    """What the child should answer in: the setting, or what the user wrote."""
    configured = (reply_language(ctx.core) or "auto").strip()
    if configured and configured.lower() != "auto":
        return configured
    return detected_language(ctx.session)


def language_line(tag: str) -> str:
    """The short English instruction appended to every brief.

    English on purpose: it is read by the child *model*, and a technical brief
    is more precisely understood in English than the user's reply has to be.
    """
    return f"Answer in {language_name(tag)} ({tag}). Keep code, paths and commands as they are."


#: Lines of a child's report kept inline before the rest is spilled to disk
#: (M15 §C5).  A report is meant to be a dozen lines; one that runs to hundreds
#: is a transcript, and pasting it into the parent is exactly the context flood
#: delegation exists to avoid.
REPORT_HEAD_LINES = 120
REPORT_TAIL_LINES = 40
#: And at most this many characters, however few the lines: the parent gets
#: the child's final message up to here, with a read_file pointer to the rest.
REPORT_MAX_CHARS = 16_000
REPORT_HEAD_CHARS = 12_000

#: One line of "what to do with this", keyed on why the child stopped.  The
#: parent reads the reason before the report, so the instruction belongs next
#: to it rather than in the tool description it read three turns ago.
NEXT_STEPS: dict[str, str] = {
    COMPLETE: (
        "Next: check anything with an external effect yourself — this is the child's "
        "own account of what it did — then answer the user in your own words."
    ),
    BUDGET: (
        "This task is unfinished: the child used its whole tool-round budget "
        "(the runtime already re-issued once with a continuation brief when "
        "agents.incompleteRetries > 0). Next: take what is done from the report "
        "and delegate only what is left, with a narrower brief. Do not re-send "
        "this exact task unchanged."
    ),
    "timeout": (
        "The child ran out of time, so the report is partial (a continue retry "
        "may already have run). Next: work out from the last calls below how far "
        "it got, and either finish that part here or delegate a smaller slice."
    ),
    "error": (
        "The child failed. Next: read the error, fix what caused it (a missing path, a "
        "bad agent name, a tool it was not given) and try once — do not re-delegate the "
        "same brief unchanged. Empty failures may already have been re-issued once."
    ),
    "interrupted": (
        "The child was interrupted, so nothing here is final. Next: say so plainly, and "
        "do not report its partial work as done."
    ),
    "denied": (
        "The child was denied a permission it needed. Next: tell the user what was "
        "blocked and what you need from them; do not retry it silently."
    ),
    PARENT: (
        "No suitable team agent was available. Next: do this task yourself in this "
        "session. Do not re-delegate the same brief unchanged."
    ),
}


#: Reasons whose report the parent has to read as partial: the child stopped
#: for a reason of its own, not because the work was finished.
PARTIAL_REASONS = frozenset({BUDGET, "timeout", "error", "interrupted", "denied", PARENT})

#: What is said when the child never wrote a final answer — it was cut off
#: between tool calls.  A delegation never comes back as an empty string, and
#: never as the prose the child happened to write before its last tool call:
#: an empty (or misleading) tool result is what made the parent re-delegate
#: the same task three times (CORE-subagent-budget).
NO_REPORT = "(the sub-agent ended without a final report; see the steps below)"


def _denied_note(result: SubagentResult) -> str:
    denied = [name for name in result.denied_tools if name]
    if not denied:
        return ""
    names = ", ".join(dict.fromkeys(denied))
    count = len(denied)
    noun = "call" if count == 1 else "calls"
    verb = "was" if count == 1 else "were"
    return f"{count} tool {noun} {verb} denied: {names}"


def render_report(result: SubagentResult) -> str:
    """The text the parent model reads: the header, the report, the last calls.

    The header is three plain lines rather than JSON so a small model reads it
    as surely as a frontier one.  A long report is head/tail trimmed through
    the shared spill helper, with a ``read_file`` pointer to the whole thing on
    disk, and one "next step" line closes it so the parent knows what this
    reason asks of it.
    """
    head = [
        f"status: {result.status}",
        f"reason: {result.reason}",
        f"roundsUsed: {result.rounds_used}",
    ]
    summary = (result.summary or "").strip()
    if summary:
        summary = spill(
            summary,
            head_lines=REPORT_HEAD_LINES,
            tail_lines=REPORT_TAIL_LINES,
            kind="report",
        ).text
        summary = spill_chars(
            summary, max_chars=REPORT_MAX_CHARS, head_chars=REPORT_HEAD_CHARS, kind="report"
        ).text
    parts = ["\n".join(head), summary or NO_REPORT]
    denied_note = _denied_note(result)
    if denied_note:
        parts.append(denied_note)
    if result.error and result.error not in (result.summary or ""):
        parts.append(f"error: {result.error}")
    if result.last_calls and (not summary or result.reason in PARTIAL_REASONS):
        calls = "\n".join(f"- {call}" for call in result.last_calls)
        parts.append(f"The last tool calls it made:\n{calls}")
    hint = NEXT_STEPS.get(result.reason)
    if hint:
        parts.append(hint)
    return "\n\n".join(parts)


#: ``delegate_task(profile=…)`` values.
PROFILES = ("default", "fork_self")

#: ``delegate_task(model_category=…)`` values, resolved via ``models.categories``.
MODEL_CATEGORIES = ("standard", "fast")

#: Longest ``subagent_wait`` the model may ask for, in seconds.
MAX_WAIT = 1800.0


def _category_model(ctx: ToolContext, category: str) -> str | None:
    """The model reference ``models.categories[category]`` names, if any."""
    models = getattr(getattr(ctx.core, "settings", None), "models", None)
    categories = getattr(models, "categories", None) or {}
    value = categories.get(category) if isinstance(categories, dict) else None
    return str(value).strip() or None if value else None


async def delegate_task(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Run one subagent and report what it answered, or start it in the background."""
    task = str(args.get("task", "") or "").strip()
    if not task:
        return ToolResult(ok=False, error="task is required")
    agent = str(args.get("agent", "") or "").strip() or None
    profile = str(args.get("profile", "") or "default").strip() or "default"
    if profile not in PROFILES:
        return ToolResult(ok=False, error=f"profile must be one of {', '.join(PROFILES)}")
    model = str(args.get("model", "") or "").strip() or None
    category = str(args.get("model_category", "") or "").strip()
    if category and model is None:
        if category not in MODEL_CATEGORIES:
            return ToolResult(
                ok=False, error=f"model_category must be one of {', '.join(MODEL_CATEGORIES)}"
            )
        model = _category_model(ctx, category)
    # The model is told to write briefs in whatever language suits it; the
    # output language is not left to chance, because the parent has to relay
    # the report to a user who may read neither.
    task = f"{task}\n\n{language_line(delegation_language(ctx))}"
    title = str(args.get("title", "") or "").strip()
    manager = get_manager(ctx.core)
    background = bool(args.get("run_in_background", False))
    coro = manager.run(
        ctx.session,
        task,
        agent=agent,
        title=title,
        tools=_tool_list(args.get("tools")),
        timeout=_timeout(args.get("timeout")),
        model=model,
        force=bool(args.get("force", False)),
        # A delegation can run for minutes with nothing to show; the child's
        # own progress is republished on this call (IDE-PROGRESS D2).
        progress=None if background else ctx.progress,
        fork=profile == "fork_self",
    )
    if background:
        task_id = f"bg-{uuid.uuid4().hex[:10]}"
        manager.background[task_id] = BackgroundRun(
            task_id=task_id,
            parent_session_id=ctx.session.id,
            title=title,
            task=asyncio.ensure_future(coro),
        )
        return ToolResult(
            ok=True,
            output=(
                f"started in the background as task_id {task_id}"
                + (f" ({title})" if title else "")
                + ". Keep working; call subagent_wait with this task_id to collect "
                "its report."
            ),
            meta={"task_id": task_id},
        )
    result = await coro
    report = render_report(result)
    if not result.ok and result.reason != BUDGET:
        return ToolResult(
            ok=False,
            output=report,
            error=result.error or "the subagent did not finish",
        )
    return ToolResult(ok=True, output=report)


async def subagent_wait(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Wait for background delegations and return their reports."""
    raw = args.get("task_ids") or []
    ids = [str(item) for item in raw] if isinstance(raw, list) else [str(raw)]
    if not ids:
        return ToolResult(ok=False, error="task_ids is required")
    manager = get_manager(ctx.core)
    runs = []
    for task_id in ids:
        run = manager.background.get(task_id)
        if run is None or run.parent_session_id != ctx.session.id:
            return ToolResult(ok=False, error=f"no background task {task_id} in this session")
        runs.append(run)
    try:
        timeout = min(float(args.get("timeout") or MAX_WAIT), MAX_WAIT)
    except (TypeError, ValueError):
        timeout = MAX_WAIT
    pending = [run.task for run in runs if not run.task.done()]
    if pending:
        interrupt = asyncio.ensure_future(ctx.session.interrupt.wait())
        try:
            await asyncio.wait(
                [asyncio.gather(*pending, return_exceptions=True), interrupt],
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            interrupt.cancel()
    sections: list[str] = []
    for run in runs:
        head = f"## {run.task_id}" + (f" — {run.title}" if run.title else "")
        if not run.task.done():
            sections.append(f"{head}\nstill running; call subagent_wait again later.")
            continue
        run.collected = True
        error = run.task.exception() if not run.task.cancelled() else None
        if run.task.cancelled() or error is not None:
            sections.append(f"{head}\nfailed: {error or 'cancelled'}")
            continue
        result = run.task.result()
        sections.append(f"{head}\n{render_report(result)}")
    return ToolResult(ok=True, output="\n\n".join(sections))


def get_time(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """The current date and time, local and UTC."""
    now = datetime.now().astimezone()
    utc = now.astimezone(UTC)
    return ToolResult(
        ok=True,
        output=(
            f"local: {now.isoformat(timespec='seconds')} ({now.strftime('%A')}, "
            f"{now.tzname()})\nutc: {utc.isoformat(timespec='seconds')}"
        ),
    )


async def _get_time(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return get_time(ctx, args)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="delegate_task",
        category="delegate",
        description=descriptions.DELEGATE_TASK + BUILTIN_AGENT_HINT,
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "What the sub-agent should do, written so it needs no "
                        "other context than the project itself."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "A one-line title for this delegation in the user's "
                        "language, shown in the UI."
                    ),
                },
                "agent": {
                    "type": "string",
                    "description": (
                        "Name of an agent definition to run it as."
                        + BUILTIN_AGENT_HINT
                        + " Project, global, and plugin custom agent names also resolve "
                        "when present."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict the sub-agent to these tool names.",
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "Run this even though an identical task is already in flight. "
                        "Only for a deliberate best-of-N comparison; the default refuses "
                        "a duplicate so two children never own the same work."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": "Seconds to wait before giving up on the sub-agent.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Run this one delegation on a specific model: a configured "
                        "profile id, a 'vendor:model' pair, or a bare vendor name. "
                        "Outranks the agent's own assignment; omit it to use that."
                    ),
                },
                "model_category": {
                    "type": "string",
                    "enum": list(MODEL_CATEGORIES),
                    "description": (
                        "'fast' for mechanical work, 'standard' otherwise; mapped to a "
                        "model by the user's settings. Ignored when model is given."
                    ),
                },
                "profile": {
                    "type": "string",
                    "enum": list(PROFILES),
                    "description": (
                        "'fork_self' starts the child from a copy of this conversation, "
                        "so the brief can be short; 'default' gives it only the brief."
                    ),
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Return a task_id at once and keep working; collect the report "
                        "later with subagent_wait. Use it for independent work that can "
                        "run while you do something else."
                    ),
                },
            },
            "required": ["task"],
        },
        permission="delegate",
        run=delegate_task,
    ),
    Tool(
        name="subagent_wait",
        category="delegate",
        description=(
            "Wait for delegations started with run_in_background and return their "
            "reports. Returns early when the user interrupts; a task still running "
            "after the timeout is reported as such."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "task_id values delegate_task returned.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Seconds to wait at most (default and cap 1800).",
                },
            },
            "required": ["task_ids"],
        },
        permission="delegate",
        run=subagent_wait,
    ),
    Tool(
        name="get_time",
        category="interaction",
        description="The current date and time, in local time and UTC.",
        input_schema={"type": "object", "properties": {}},
        permission="read",
        run=_get_time,
    ),
)


__all__ = [
    "BUILTIN_AGENT_HINT",
    "NEXT_STEPS",
    "REPORT_HEAD_LINES",
    "REPORT_TAIL_LINES",
    "BUILTIN_AGENT_NAMES",
    "MAX_TIMEOUT",
    "NO_REPORT",
    "PARTIAL_REASONS",
    "TOOLS",
    "delegate_task",
    "get_time",
    "subagent_wait",
    "delegation_language",
    "detected_language",
    "language_line",
    "last_user_text",
    "render_report",
]
