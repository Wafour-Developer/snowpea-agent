"""Agent identity: the system prompt and the per-turn configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from snowpea_core.providers.base import ChatMessage, ToolSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.session import Session

MODE_GUIDANCE: dict[str, str] = {
    "plan": (
        "You are in PLAN mode. You may read files and search, but every write and "
        "every command is refused. Investigate, then describe what you would do."
    ),
    "accept": (
        "You are in ACCEPT mode. File edits apply directly. Running commands, "
        "network calls and outbound messages need the user's approval first, so "
        "call those tools only when they are genuinely needed."
    ),
    "auto": (
        "You are in AUTO mode. Every tool runs without asking. Nobody may be "
        "watching, so prefer reversible steps and check your work."
    ),
}

BASE_PROMPT = (
    "You are snowpea, a local-first coding agent working inside the user's "
    "project. Use the tools to inspect and change real files rather than "
    "guessing. Keep answers short and concrete: say what you did and what it "
    "means, not what you are about to try."
)

#: Appended to every system prompt.  An agent asked to *show* a settings file
#: has rewritten it instead, so the rule is spelled out rather than implied.
CONFIG_RULE = (
    "Snowpea's own configuration — $SNOWPEA_HOME (settings.json, "
    "credentials.json, state.db, token, logs) and <workdir>/.snowpea — is not "
    "ordinary project state.\n"
    "- To show configuration, read it: call settings_get, or read_file. Showing "
    "is never a reason to write.\n"
    "- Never change settings or credentials unless the user explicitly asked "
    "for that change. Guessing at a fix is not a request.\n"
    "- When a change is asked for, make it with settings_set or by telling the "
    "user the `snowpea setup …` command, not by rewriting settings.json by "
    "hand. Those writes always need the user's approval.\n"
    "- If a tool result says it fell back to another provider, say so in your "
    "reply instead of presenting the result as what the user configured."
)

#: What the web-search fallback prefix looks like, so the model recognises it.
SEARCH_HONESTY_HINT = (
    "web_search output beginning with `[search via <id> — fallback from <id>: "
    "<reason>]` means the configured provider did not answer; repeat that "
    "reason to the user."
)


@dataclass
class AgentConfig:
    """Per-turn knobs resolved from settings."""

    max_tool_rounds: int = 50
    max_tokens: int = 4096


def tool_lines(tools: list[ToolSpec]) -> str:
    """One ``- name: description`` line per tool."""
    return "\n".join(f"- {tool.name}: {tool.description}" for tool in tools)


def build_system_prompt(
    session: Session, tools: list[ToolSpec], memory_block: str = ""
) -> str:
    """System prompt for one turn: role, mode, workdir, tools and memories.

    ``memory_block`` is the rendered recall from ``memory.Retrieval`` (M5
    contract §1); it is empty whenever memory is off or nothing matched.
    """
    parts = [
        getattr(session, "system_prompt", None) or BASE_PROMPT,
        MODE_GUIDANCE.get(session.mode, MODE_GUIDANCE["accept"]),
        CONFIG_RULE,
        SEARCH_HONESTY_HINT,
        f"Working directory: {session.workdir}",
    ]
    if tools:
        parts.append("Available tools:\n" + tool_lines(tools))
    if memory_block:
        parts.append(memory_block)
    return "\n\n".join(parts)


def build_messages(
    session: Session, tools: list[ToolSpec], memory_block: str = ""
) -> list[ChatMessage]:
    """System prompt followed by the session's history."""
    return [
        ChatMessage(
            role="system", content=build_system_prompt(session, tools, memory_block)
        ),
        *session.history.snapshot(),
    ]


__all__ = [
    "BASE_PROMPT",
    "CONFIG_RULE",
    "MODE_GUIDANCE",
    "SEARCH_HONESTY_HINT",
    "AgentConfig",
    "build_messages",
    "build_system_prompt",
    "tool_lines",
]
