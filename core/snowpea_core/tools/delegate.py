"""The ``delegate_task`` tool (M7 contract §3).

Hand a self-contained task to a child agent and get its final answer back as
the tool result.  Everything else — the concurrency limit, the child session,
the ``subagent.*`` events — is :mod:`snowpea_core.agent.subagent`'s job.

This replaces the inactive M1 stub of the same name; the name, category and
permission tag are unchanged, so a client that read ``tool.list`` before M7
sees the same tool simply become ``active``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from snowpea_core.agent.agent import reply_language
from snowpea_core.agent.definition import builtin_agent_definitions
from snowpea_core.agent.subagent import BUDGET, SubagentResult, get_manager
from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.prompts.compose import language_name
from snowpea_core.session.history import message_text
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


#: Reasons whose report the parent has to read as partial: the child stopped
#: for a reason of its own, not because the work was finished.
PARTIAL_REASONS = frozenset({BUDGET, "timeout", "error", "interrupted", "denied"})

#: What is said when the child never wrote a final answer — it was cut off
#: between tool calls.  A delegation never comes back as an empty string, and
#: never as the prose the child happened to write before its last tool call:
#: an empty (or misleading) tool result is what made the parent re-delegate
#: the same task three times (CORE-subagent-budget).
NO_REPORT = "(the sub-agent ended without a final report; see the steps below)"


def render_report(result: SubagentResult) -> str:
    """The text the parent model reads: the header, the report, the last calls.

    The header is three plain lines rather than JSON so a small model reads it
    as surely as a frontier one, and the report follows unchanged, because that
    is what the parent is going to relay.
    """
    head = [
        f"status: {result.status}",
        f"reason: {result.reason}",
        f"roundsUsed: {result.rounds_used}",
    ]
    summary = (result.summary or "").strip()
    parts = ["\n".join(head), summary or NO_REPORT]
    if result.error and result.error not in (result.summary or ""):
        parts.append(f"error: {result.error}")
    if result.last_calls and (not summary or result.reason in PARTIAL_REASONS):
        calls = "\n".join(f"- {call}" for call in result.last_calls)
        parts.append(f"The last tool calls it made:\n{calls}")
    if result.reason == BUDGET:
        parts.append(
            "This task is unfinished: the child used its whole tool-round budget. "
            "Use the report above and decide what still needs doing — do not simply "
            "delegate the same task again."
        )
    return "\n\n".join(parts)


async def delegate_task(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Run one subagent and report what it answered."""
    task = str(args.get("task", "") or "").strip()
    if not task:
        return ToolResult(ok=False, error="task is required")
    agent = str(args.get("agent", "") or "").strip() or None
    # The model is told to write briefs in whatever language suits it; the
    # output language is not left to chance, because the parent has to relay
    # the report to a user who may read neither.
    task = f"{task}\n\n{language_line(delegation_language(ctx))}"
    result = await get_manager(ctx.core).run(
        ctx.session,
        task,
        agent=agent,
        title=str(args.get("title", "") or "").strip(),
        tools=_tool_list(args.get("tools")),
        timeout=_timeout(args.get("timeout")),
        model=str(args.get("model", "") or "").strip() or None,
        # A delegation can run for minutes with nothing to show; the child's
        # own progress is republished on this call (IDE-PROGRESS D2).
        progress=ctx.progress,
    )
    report = render_report(result)
    if not result.ok and result.reason != BUDGET:
        return ToolResult(
            ok=False,
            output=report,
            error=result.error or "the subagent did not finish",
        )
    return ToolResult(ok=True, output=report)


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
            },
            "required": ["task"],
        },
        permission="delegate",
        run=delegate_task,
    ),
)


__all__ = [
    "BUILTIN_AGENT_HINT",
    "BUILTIN_AGENT_NAMES",
    "MAX_TIMEOUT",
    "NO_REPORT",
    "PARTIAL_REASONS",
    "TOOLS",
    "delegate_task",
    "delegation_language",
    "detected_language",
    "language_line",
    "last_user_text",
    "render_report",
]
