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
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from snowpea_core.config.settings import DEFAULT_EFFORT, DEFAULT_MAX_TOKENS
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


@dataclass(frozen=True)
class _EnvCacheEntry:
    """One session's rendered blocks, and which instruction files they quote."""

    #: ``History.compactions`` when the blocks were rendered.  They are kept
    #: until the next compaction rewrites the conversation anyway: re-rendered
    #: every round, a git status that changed after an edit sat at the top of
    #: the system prompt and made vLLM prefill the whole conversation again.
    epoch: int
    environment: str
    context_files: str
    loaded: frozenset[str]


_ENV_CACHE: dict[str, _EnvCacheEntry] = {}

#: session id -> (cache key, rendered tools fragment).  The provider's prefix
#: cache keys off an exact leading substring, so two rounds that offer the same
#: tools must produce the *same bytes*, not merely equivalent text
#: (CORE-round-cost).
_TOOLS_CACHE: dict[str, tuple[tuple[Any, ...], str]] = {}


@dataclass
class AgentConfig:
    """Per-turn knobs resolved from settings."""

    max_tool_rounds: int = 200
    #: Output tokens one provider call may produce, already clamped to what
    #: the session's model accepts (CORE-reasoning-budget).
    max_tokens: int = DEFAULT_MAX_TOKENS
    #: ``"on"`` or ``"off"`` — ``"auto"`` is resolved before it gets here.
    thinking: str = "on"
    #: ``low|medium|high|max`` — how hard the model may think this turn. Each
    #: adapter maps it to its vendor's own field (CORE-effort).
    effort: str = DEFAULT_EFFORT
    #: Which rule decided it: ``session``, ``model``, ``vendor`` or ``default``.
    effort_source: str = "default"


def tool_lines(tools: list[ToolSpec], *, describe: bool = False) -> str:
    """One ``- name`` line per tool (``- name: description`` with ``describe``)."""
    return compose.tool_lines(tools, describe=describe)


def list_descriptions(core: Core | None) -> bool:
    """``tools.listDescriptions``: descriptions in the prompt's tool list too."""
    tools = getattr(getattr(core, "settings", None), "tools", None)
    return bool(getattr(tools, "listDescriptions", False))


def invalidate_environment(session: Session | None = None) -> None:
    """Forget the cached environment block(s) — ``/backend``, a new workdir, tests."""
    if session is None:
        _ENV_CACHE.clear()
        return
    _ENV_CACHE.pop(f"{session.id}:{session.workdir}", None)


def invalidate_tools(session: Session | Any | None = None) -> None:
    """Forget the cached tool fragment — ``tool_search``, an MCP sync, a reload."""
    if session is None:
        _TOOLS_CACHE.clear()
        return
    _TOOLS_CACHE.pop(str(getattr(session, "id", "")), None)


def tools_fragment(
    session: Session,
    tools: list[ToolSpec],
    deferred: list[ToolSpec] | None = None,
    root: Any = None,
    *,
    describe: bool = False,
) -> str:
    """The rendered tool list for this round, cached per session.

    Keyed on what can change the text — the tool names offered, the mode and
    the role — so an unchanged round reuses the identical string instead of
    re-rendering one that merely happens to match.
    """
    deferred = deferred or []
    key = (
        tuple(spec.name for spec in tools),
        tuple(sorted(spec.name for spec in deferred)),
        str(getattr(session, "mode", "")),
        str(getattr(session, "prompt_role", "") or ""),
        describe,
    )
    session_id = str(getattr(session, "id", ""))
    cached = _TOOLS_CACHE.get(session_id)
    if cached is not None and cached[0] == key:
        return cached[1]
    text = compose.tools_block(tools, deferred, root, describe=describe)
    _TOOLS_CACHE[session_id] = (key, text)
    return text


def child_context(core: Core | None) -> str:
    """``agents.childContext`` — ``"lean"`` or ``"full"``."""
    if core is None:
        return "lean"
    try:
        value = str(core.settings.agents.childContext or "lean").strip().lower()
    except AttributeError:  # pragma: no cover - a partially built Core in a test
        return "lean"
    return value if value in ("lean", "full") else "lean"


def is_lean_child(session: Session, core: Core | None) -> bool:
    """True when this session is a subagent running on the diet prompt.

    A child starts with an empty history and a brief that already says what it
    is for, so the parent's memory recall, the skills index and every nested
    ``AGENTS.md`` are context it was not asked to carry (CORE-round-cost).  It
    keeps its role, its definition prompt, its budget line and the root
    instruction file, and can still reach a skill by name with ``skill_view``.

    The child's own definition decides first (``childContext: full`` on a
    ``browser`` agent keeps its site skills and recall), then the setting.
    """
    if not getattr(session, "is_subagent", False):
        return False
    own = getattr(session, "child_context", None)
    if own in ("lean", "full"):
        return own == "lean"
    return child_context(core) == "lean"


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


def context_files_total_chars(core: Core | None) -> int | None:
    """``agent.contextFilesMaxChars`` — the cap on the whole block, or ``None``.

    Separate from :func:`context_file_settings` because the agent loop unpacks
    that pair on the tool-dispatch path and a third element would break it.
    """
    if core is None:
        return None
    try:
        total = getattr(core.settings.agent, "contextFilesMaxChars", None)
    except AttributeError:  # pragma: no cover - a partially built Core in a test
        return None
    return int(total) if total else None


def environment_blocks(session: Session, core: Core | None = None) -> tuple[str, str]:
    """``(environment block, project context files block)`` for ``session``.

    Also records on the session which instruction files the prompt already
    quotes, so the on-demand attachment in :mod:`agent.context_files` fires
    only for the ones the budget could not fit (CORE-context-files).

    A lean child gets the root instruction file only: the nested ones are still
    attached on demand when a tool touches their directory, which is where they
    are actually useful (CORE-round-cost).
    """
    nested = not is_lean_child(session, core)
    # Mode and model are in the block; changing either rebuilds the stable
    # tier as well, so that cache is lost either way.
    key = (
        f"{session.id}:{session.workdir}:{int(nested)}:{session.mode}:"
        f"{session.provider}:{session.model}"
    )
    epoch = int(getattr(session.history, "compactions", 0) or 0)
    cached = _ENV_CACHE.get(key)
    if cached is not None and cached.epoch == epoch:
        session.loaded_context_files = set(cached.loaded)
        return cached.environment, cached.context_files
    backend = getattr(getattr(session, "backend", None), "kind", "local")
    model = None
    if session.provider or session.model:
        model = f"{session.provider or '?'}:{session.model or '?'}"
    override, ignore = context_file_settings(core)
    total_override = context_files_total_chars(core)
    env = prompt_env.collect(
        session.workdir,
        model=model,
        mode=session.mode,
        backend=str(backend or "local"),
        workspace=getattr(session, "workspace_dir", None),
        read_context=not ignore,
        context_window=session.context_window,
        context_file_chars=override,
        context_files_chars=total_override,
        include_nested=nested,
    )
    project = env.project_context
    entry = _EnvCacheEntry(
        epoch=epoch,
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


@dataclass(frozen=True)
class PromptPlan:
    """What a session's system prompt is built from, fixed until the next compaction.

    A setting flipped, a skill edited or a host reattached in the middle of a
    session must not rewrite the prefix a server has cached, so every choice
    that shapes the prompt is made here once and read back on every round.
    """

    key: tuple[Any, ...]
    own_identity: bool = False
    describe_tools: bool = False
    #: ``tools.agentDeferred`` on and a fixed tool list: ``deferred`` applies.
    agent_deferred: bool = False
    deferred: tuple[str, ...] = ()
    skill_groups: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    skills_scoped: bool = False


def _switch(env: str, setting: Any) -> bool:
    """An A/B switch: the environment variable when set, else the setting."""
    raw = os.environ.get(env, "").strip().lower()
    if raw:
        return raw in ("1", "true", "yes", "on")
    return bool(setting)


def prompt_plan(session: Session, core: Core | None) -> PromptPlan:
    """The session's :class:`PromptPlan`, made on first use and per compaction.

    Also remade when the provider, model or mode changes, which rewrite the
    prompt anyway.
    """
    key = (
        getattr(getattr(session, "history", None), "compactions", 0),
        session.provider,
        session.model,
        session.mode,
    )
    plan = getattr(session, "prompt_plan", None)
    if isinstance(plan, PromptPlan) and plan.key == key:
        return plan
    settings = getattr(core, "settings", None)
    fixed = bool(
        core is not None
        and getattr(core, "providers", None) is not None
        and core.providers.is_local_style(session.provider or "")
    )
    scoped = getattr(session, "agent_skills_index", None) == "plugin"
    groups: list[tuple[str, list[tuple[str, str]]]] = (
        [] if is_lean_child(session, core) else skill_groups(core)
    )
    if scoped:
        plugin = getattr(session, "agent_plugin", None)
        wanted = {"[builtin]", f"[plugin:{plugin}]"} if plugin else {"[builtin]"}
        groups = [group for group in groups if group[0] in wanted]
    agents = getattr(settings, "agents", None)
    tools = getattr(settings, "tools", None)
    plan = PromptPlan(
        key=key,
        own_identity=getattr(session, "agent_identity", None) == "own"
        and _switch("SNOWPEA_AGENT_IDENTITY", getattr(agents, "ownIdentity", False)),
        describe_tools=list_descriptions(core),
        agent_deferred=fixed
        and _switch("SNOWPEA_AGENT_DEFERRED", getattr(tools, "agentDeferred", False)),
        deferred=tuple(getattr(session, "agent_deferred", None) or ()),
        skill_groups=tuple((label, tuple(rows)) for label, rows in groups),
        skills_scoped=scoped,
    )
    try:
        session.prompt_plan = plan
    except AttributeError:  # pragma: no cover - a frozen stand-in in a test
        pass
    return plan


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


def memory_enabled(core: Core | None, session: Any = None) -> bool:
    """``memory.enabled``; the guidance fragment is pointless when it is off.

    A session's ``memory_override`` wins (addendum 27, C1).
    """
    override = getattr(session, "memory_override", None)
    if override is not None:
        return bool(override)
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


#: A BCP 47 language tag, loosely: ``ko``, ``ko-KR``, ``zh-Hant-TW``, ``pt_BR``.
_LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")


def locale_tag(locale: str | None) -> str | None:
    """``"ko-KR"`` -> ``"ko"``; ``None`` for anything that is not a language tag."""
    text = (locale or "").strip()
    if not text or len(text) > 35 or not _LOCALE.match(text):
        return None
    return re.split(r"[-_]", text, maxsplit=1)[0].lower()


def session_reply_language(core: Core | None, session: Any) -> str:
    """``agent.replyLanguage``; on ``auto``, the session's UI locale when it has one.

    ``"auto"`` comes back when neither says anything, and the caller falls
    back to guessing from what the user typed.
    """
    configured = (reply_language(core) or "auto").strip() or "auto"
    if configured.lower() != "auto":
        return configured
    return locale_tag(getattr(session, "locale", None)) or "auto"


def plan_prompt_line(session: Session) -> str:
    """``Current plan: …`` for the main agent while the project has an active plan.

    Only the user's own words survive a compaction verbatim; the plan body the
    assistant wrote does not.  One line every turn keeps "그냥 구현해줘" pointed
    at the plan on disk (CORE-plan-continuity).
    """
    from snowpea_core.agent import plan_store

    return plan_store.prompt_line(session.workdir)


def turn_context(session: Session, memory_block: str = "", *, core: Core | None = None) -> str:
    """What changes from turn to turn: recall, the plan line, context pressure.

    It rides on the turn's own user message (``agent.loop``), not in the
    system prompt: the conversation follows the system prompt, so anything
    that changed there made the server prefill all of it again.  Stored with
    the message, it is sent byte for byte on every later request — what turn
    N sends is what turn N+1 replays (hermes' invariant).

    ``memory_block`` is the rendered recall from ``memory.Retrieval`` (M5
    contract §1); it is empty whenever memory is off or nothing matched.
    """
    if is_lean_child(session, core):
        return ""
    parts = [memory_block]
    if not getattr(session, "is_subagent", False):
        parts.append(plan_prompt_line(session))
    fill = context_fill(session)
    if fill is not None and fill >= compose.CONTEXT_PRESSURE_THRESHOLD:
        parts.append(compose.load("fragments/context-pressure", session.workdir))
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def build_system_prompt(
    session: Session,
    tools: list[ToolSpec],
    memory_block: str = "",
    *,
    core: Core | None = None,
) -> str:
    """System prompt for a session: the stable and context tiers.

    It stays byte-identical from round to round and turn to turn, so a
    server's prefix cache covers the whole conversation after it: the
    environment is rendered once per compaction, and the per-turn volatile
    tier is :func:`turn_context`, sent on the turn's user message instead.
    ``memory_block`` is accepted for the callers that still pass it and
    otherwise ignored.
    """
    del memory_block
    plan = prompt_plan(session, core)
    environment, context_files = environment_blocks(session, core)
    lean = is_lean_child(session, core)
    deferred_specs: list[ToolSpec] = []
    if core is not None and getattr(core, "tools", None) is not None:
        deferred_specs = core.tools.deferred_specs(session)
    persona = getattr(session, "system_prompt", None) or ""
    if session.team_agents:
        team_rule = (
            f"Active delegation team: {session.team}. Delegate only to these agents: "
            + ", ".join(session.team_agents)
            + ". Every delegate_task call must include one of those names in its agent field."
        )
        persona = f"{persona}\n\n{team_rule}".strip()
    if core is not None:
        from typing import Literal

        from snowpea_core.agent.team_guide import guide_for_session, render_team_guide

        is_child = bool(getattr(session, "is_subagent", False))
        guide_session = session
        audience: Literal["lead", "worker"] = "lead"
        if is_child and session.parent_session_id:
            parent = core.sessions.get(session.parent_session_id)
            if parent is not None:
                guide_session = parent
                audience = "worker"
        guide = guide_for_session(core, guide_session)
        if guide is not None:
            rendered = render_team_guide(guide, audience=audience)
            if rendered:
                persona = f"{persona}\n\n{rendered}".strip()
    from snowpea_core.agent.delegation import delegation_prompt

    delegating = delegation_prompt(core, session)
    if delegating:
        persona = f"{persona}\n\n{delegating}".strip()
    return compose.build_system_prompt(
        mode=session.mode,
        vendor_class=compose.vendor_class_for(
            session.provider,
            model=session.model,
            local_style=bool(
                core is not None and core.providers.is_local_style(session.provider or "")
            ),
        ),
        role=getattr(session, "prompt_role", None),
        subagent=bool(getattr(session, "is_subagent", False)),
        tools=tools,
        deferred_tools=deferred_specs,
        tools_text=tools_fragment(
            session, tools, deferred_specs, session.workdir, describe=plan.describe_tools
        )
        if tools
        else None,
        skill_groups=[(label, list(rows)) for label, rows in plan.skill_groups],
        skill_index_max=skill_index_max(core),
        skills_scoped=plan.skills_scoped,
        memory_guidance=memory_enabled(core, session) and not lean,
        own_identity=plan.own_identity,
        environment=environment,
        context_files=context_files,
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
    """System prompt followed by the session's history, as stored.

    Nothing already sent is rewritten here: a request is the last one plus
    what happened since, so a server's prefix cache covers all of it.  Old
    snapshots and tool outputs are shrunk when the conversation is compacted
    (``compaction.compact_session``).  The exception is a turn that outgrows
    the auto-compact threshold before it ends (compaction waits for the turn
    boundary): its outgoing request is pruned as before, on the copy only.
    """
    # Local import: ``session/compaction.py`` imports this function.
    from snowpea_core.session import compaction

    history = compaction.pair_tool_results(session.history.snapshot())
    on, keep, max_chars = compaction.tool_prune_settings(core)
    if on and compaction.over_compact_threshold(core, session, history):
        pruned: list[str] = []
        stored = history
        history = compaction.supersede_snapshots(history, pruned)
        history = compaction.prune_old_tool_outputs(history, keep, max_chars, pruned)
        if pruned:
            # A result the model can no longer see is not one it should be
            # refused when it asks again (CORE-repeat-guard): the guard's
            # "unchanged since your earlier read" only holds while that
            # earlier read is still in the request.
            from snowpea_core.tools import repeat_guard

            repeat_guard.forget_pruned(session, stored, pruned)
    vision = (
        core.providers.vision_for(session.provider, session.model)
        if core is not None and session.provider
        else False
    )
    if core is not None and session.provider and core.providers.is_local_style(session.provider):
        # ``None`` means try-once: images still go out. Many local VLMs cap at two.
        if vision is not False:
            history = compaction.prune_request_images(history)
    return [
        ChatMessage(
            role="system",
            content=build_system_prompt(session, tools, memory_block, core=core),
        ),
        *history,
    ]


__all__ = [
    "BASE_PROMPT",
    "CONFIG_RULE",
    "MODE_GUIDANCE",
    "SEARCH_HONESTY_HINT",
    "AgentConfig",
    "build_messages",
    "build_system_prompt",
    "child_context",
    "context_file_settings",
    "context_files_total_chars",
    "context_fill",
    "environment_blocks",
    "invalidate_environment",
    "invalidate_tools",
    "is_lean_child",
    "locale_tag",
    "memory_enabled",
    "reply_language",
    "session_reply_language",
    "skill_groups",
    "skill_index_max",
    "tool_lines",
    "tools_fragment",
]
