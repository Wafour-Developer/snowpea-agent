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

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from snowpea_core.config.settings import DEFAULT_MAX_TOKENS
from snowpea_core.prompts import compose
from snowpea_core.prompts import environment as prompt_env
from snowpea_core.prompts.loader import load
from snowpea_core.providers.base import ChatMessage, ToolSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.agent.prompt")

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

@dataclass(frozen=True)
class _EnvCacheEntry:
    """One session's rendered blocks, and which instruction files they quote."""

    expires: float
    environment: str
    context_files: str
    loaded: frozenset[str]


_ENV_CACHE: dict[str, _EnvCacheEntry] = {}


@dataclass
class AgentConfig:
    """Per-turn knobs resolved from settings."""

    max_tool_rounds: int = 200
    #: Output tokens one provider call may produce, already clamped to what
    #: the session's model accepts (CORE-reasoning-budget).
    max_tokens: int = DEFAULT_MAX_TOKENS
    #: ``"on"`` or ``"off"`` — ``"auto"`` is resolved before it gets here.
    thinking: str = "on"


def tool_lines(tools: list[ToolSpec]) -> str:
    """One ``- name: description`` line per tool."""
    return compose.tool_lines(tools)


def invalidate_environment(session: Session | None = None) -> None:
    """Forget the cached environment block(s) — ``/backend``, a new workdir, tests."""
    if session is None:
        _ENV_CACHE.clear()
        return
    _ENV_CACHE.pop(f"{session.id}:{session.workdir}", None)


def context_file_settings(core: Core | None) -> tuple[int | None, bool]:
    """``(agent.contextFileMaxChars, agent.ignoreContextFiles)`` from settings."""
    if core is None:
        return None, False
    try:
        agent_settings = core.settings.agent
    except AttributeError:  # pragma: no cover - a partially built Core in a test
        return None, False
    configured = getattr(agent_settings, "contextFileMaxChars", None)
    override = int(configured) if configured else None
    return override, bool(getattr(agent_settings, "ignoreContextFiles", False))


def environment_blocks(session: Session, core: Core | None = None) -> tuple[str, str]:
    """``(environment block, project context files block)`` for ``session``.

    Also records on the session which instruction files the prompt already
    quotes, so the on-demand attachment in :mod:`agent.context_files` fires
    only for the ones the budget could not fit (CORE-context-files).
    """
    key = f"{session.id}:{session.workdir}"
    cached = _ENV_CACHE.get(key)
    if cached is not None and cached.expires > time.monotonic():
        session.loaded_context_files = set(cached.loaded)
        return cached.environment, cached.context_files
    backend = getattr(getattr(session, "backend", None), "kind", "local")
    model = None
    if session.provider or session.model:
        model = f"{session.provider or '?'}:{session.model or '?'}"
    override, ignore = context_file_settings(core)
    env = prompt_env.collect(
        session.workdir,
        model=model,
        mode=session.mode,
        backend=str(backend or "local"),
        read_context=not ignore,
        context_window=session.context_window,
        context_file_chars=override,
    )
    project = env.project_context
    entry = _EnvCacheEntry(
        expires=time.monotonic() + ENVIRONMENT_TTL_SEC,
        environment=prompt_env.build_environment_block(env),
        context_files=prompt_env.context_files_block(project),
        loaded=frozenset(item.name for item in project.files),
    )
    _ENV_CACHE[key] = entry
    session.loaded_context_files = set(entry.loaded)
    return entry.environment, entry.context_files


def context_fill(session: Session) -> float | None:
    """How full the window is, ``0.0``–``1.0``, or ``None`` when unknown.

    Both numbers are written by ``compaction.emit_context`` at the end of every
    turn, so the brevity line needs no access to ``Core``.
    """
    window = session.context_window
    if not window or session.context_used <= 0:
        return None
    return session.context_used / window


def skill_groups(core: Core | None) -> list[tuple[str, list[tuple[str, str]]]]:
    """Grouped skills for the prompt index, or ``[]`` when it is switched off.

    ``skills.indexInPrompt`` turns the whole block off; the loader caches the
    grouping and drops it on every reload, so building this per turn costs a
    dictionary lookup rather than a filesystem walk (M15 §B1).
    """
    loader = getattr(core, "skills", None) if core is not None else None
    if loader is None:
        return []
    try:
        settings = core.settings.skills  # type: ignore[union-attr]
        if not getattr(settings, "indexInPrompt", True):
            return []
        return loader.index_groups()
    except Exception:  # noqa: BLE001 - a broken loader must not cost the prompt
        log.debug("could not build the skills index", exc_info=True)
        return []


def skill_index_max(core: Core | None) -> int:
    """``skills.indexMaxEntries``, floored at 1."""
    if core is None:
        return compose.DEFAULT_SKILL_INDEX_MAX
    try:
        return max(1, int(core.settings.skills.indexMaxEntries))
    except (AttributeError, TypeError, ValueError):  # pragma: no cover - partial Core
        return compose.DEFAULT_SKILL_INDEX_MAX


def memory_enabled(core: Core | None) -> bool:
    """``memory.enabled``; the guidance fragment is pointless when it is off."""
    if core is None:
        return True
    try:
        return bool(core.settings.memory.enabled)
    except AttributeError:  # pragma: no cover - a partially built Core in a test
        return True


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
    environment, context_files = environment_blocks(session, core)
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
        vendor_class=compose.vendor_class_for(
            session.provider,
            local_style=bool(
                core is not None and core.providers.is_local_style(session.provider or "")
            ),
        ),
        role=getattr(session, "prompt_role", None),
        subagent=bool(getattr(session, "is_subagent", False)),
        tools=tools,
        skill_groups=skill_groups(core),
        skill_index_max=skill_index_max(core),
        memory_guidance=memory_enabled(core),
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
    "context_file_settings",
    "context_fill",
    "environment_blocks",
    "invalidate_environment",
    "memory_enabled",
    "reply_language",
    "skill_groups",
    "skill_index_max",
    "tool_lines",
]
