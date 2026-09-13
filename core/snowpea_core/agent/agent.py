"""Agent identity: the system prompt and the per-turn configuration.

The prompt text itself lives in :mod:`snowpea_core.prompts` as markdown, and
:mod:`snowpea_core.prompts.compose` assembles it into three cache-friendly
tiers.  This module is the seam between a :class:`Session` and that library: it
turns session state (mode, provider, role, context fill, workdir) into the
composer's arguments, and it keeps ``BASE_PROMPT``, ``MODE_GUIDANCE``,
``CONFIG_RULE`` and ``SEARCH_HONESTY_HINT`` as loader-backed shims so nothing
downstream had to change when the text moved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from snowpea_core.prompts import compose
from snowpea_core.prompts import environment as prompt_env
from snowpea_core.prompts.loader import load
from snowpea_core.providers.base import ChatMessage, ToolSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

#: The mode files, loaded once.  Still a ``dict[str, str]`` keyed on the mode.
MODE_GUIDANCE: dict[str, str] = {name: load(f"modes/{name}") for name in ("plan", "accept", "auto")}

#: Identity, coding discipline, finishing the job, batching, response style,
#: trust and language — ``prompts/base.md``.
BASE_PROMPT = load("base")

#: Appended to every system prompt.  An agent asked to *show* a settings file
#: has rewritten it instead, so the rule is spelled out rather than implied.
CONFIG_RULE = load("config_rule")

#: What the web-search fallback prefix looks like, so the model recognises it.
SEARCH_HONESTY_HINT = load("search_honesty")

#: How long a session's environment block is reused.  ``compaction`` rebuilds
#: the whole prompt purely to measure it, on every turn and every ``context``
#: event, and probing git and reading AGENTS.md on an accounting path would put
#: a subprocess where none belongs.
ENVIRONMENT_TTL_SEC = 30.0

_ENV_CACHE: dict[str, tuple[float, str, str]] = {}


@dataclass
class AgentConfig:
    """Per-turn knobs resolved from settings."""

    max_tool_rounds: int = 50
    max_tokens: int = 4096


def tool_lines(tools: list[ToolSpec]) -> str:
    """One ``- name: description`` line per tool."""
    return compose.tool_lines(tools)


def invalidate_environment(session: Session | None = None) -> None:
    """Forget the cached environment block(s) — ``/backend``, a new workdir, tests."""
    if session is None:
        _ENV_CACHE.clear()
        return
    _ENV_CACHE.pop(f"{session.id}:{session.workdir}", None)


def environment_blocks(session: Session) -> tuple[str, str]:
    """``(environment block, project context files block)`` for ``session``."""
    key = f"{session.id}:{session.workdir}"
    cached = _ENV_CACHE.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1], cached[2]
    backend = getattr(getattr(session, "backend", None), "kind", "local")
    model = None
    if session.provider or session.model:
        model = f"{session.provider or '?'}:{session.model or '?'}"
    env = prompt_env.collect(
        session.workdir, model=model, mode=session.mode, backend=str(backend or "local")
    )
    blocks = (
        prompt_env.build_environment_block(env),
        prompt_env.context_files_block(env.context_files),
    )
    _ENV_CACHE[key] = (time.monotonic() + ENVIRONMENT_TTL_SEC, blocks[0], blocks[1])
    return blocks


def context_fill(session: Session) -> float | None:
    """How full the window is, ``0.0``–``1.0``, or ``None`` when unknown.

    Both numbers are written by ``compaction.emit_context`` at the end of every
    turn, so the brevity line needs no access to ``Core``.
    """
    window = session.context_window
    if not window or session.context_used <= 0:
        return None
    return session.context_used / window


def reply_language(core: Core | None) -> str:
    """``agent.replyLanguage``; ``"auto"`` when there are no settings to hand."""
    if core is None:
        return "auto"
    try:
        return str(core.settings.agent.replyLanguage or "auto")
    except AttributeError:  # pragma: no cover - a partially built Core in a test
        return "auto"


def build_system_prompt(
    session: Session,
    tools: list[ToolSpec],
    memory_block: str = "",
    *,
    core: Core | None = None,
) -> str:
    """System prompt for one turn, as three tiers (stable, context, volatile).

    ``memory_block`` is the rendered recall from ``memory.Retrieval`` (M5
    contract §1); it is empty whenever memory is off or nothing matched, and it
    sits in the volatile tier so that the prefix in front of it stays cacheable.
    """
    environment, context_files = environment_blocks(session)
    persona = getattr(session, "system_prompt", None) or ""
    if session.team_agents:
        team_rule = (
            f"Active delegation team: {session.team}. Delegate only to these agents: "
            + ", ".join(session.team_agents)
            + ". Every delegate_task call must include one of those names in its agent field."
        )
        persona = f"{persona}\n\n{team_rule}".strip()
    return compose.build_system_prompt(
        mode=session.mode,
        vendor_class=compose.vendor_class_for(session.provider),
        role=getattr(session, "prompt_role", None),
        subagent=bool(getattr(session, "is_subagent", False)),
        tools=tools,
        memory_block=memory_block,
        environment=environment,
        context_files=context_files,
        context_fill=context_fill(session),
        reply_language=reply_language(core),
        persona=persona,
        root=session.workdir,
    )


def build_messages(
    session: Session,
    tools: list[ToolSpec],
    memory_block: str = "",
    *,
    core: Core | None = None,
) -> list[ChatMessage]:
    """System prompt followed by the session's history."""
    return [
        ChatMessage(
            role="system",
            content=build_system_prompt(session, tools, memory_block, core=core),
        ),
        *session.history.snapshot(),
    ]


__all__ = [
    "BASE_PROMPT",
    "CONFIG_RULE",
    "ENVIRONMENT_TTL_SEC",
    "MODE_GUIDANCE",
    "SEARCH_HONESTY_HINT",
    "AgentConfig",
    "build_messages",
    "build_system_prompt",
    "context_fill",
    "environment_blocks",
    "invalidate_environment",
    "reply_language",
    "tool_lines",
]
