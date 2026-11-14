"""Assembles the system prompt out of the prompt library, in three tiers.

The tiers are joined stable -> context -> volatile and nothing reorders them,
because prompt-prefix caching on both Anthropic and OpenAI keys off an exact
leading substring: anything that changes per turn has to come last.

- **stable** — identity and rules, the vendor layer, the config and search
  rules, the mode, and a subagent's role.  Changes only when the mode, model,
  role or reply language changes.
- **context** — the environment block, the project context files, the tool
  list.  Rebuilt once per session and after a compaction.
- **volatile** — the memory block, the current-plan line and the
  context-pressure line.  Per turn.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from snowpea_core.prompts.loader import load, render  # noqa: F401 - ``compose.load`` is used
from snowpea_core.prompts.sections import REGISTRY, PromptInputs, SectionRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from snowpea_core.providers.base import ToolSpec

#: Prompt layers keyed on how capable the model family is.
VENDOR_CLASSES: tuple[str, ...] = (
    "anthropic", "openai-family", "gemini", "meta", "small-local"
)

#: Provider preset -> vendor class.  A small local model needs several hundred
#: words of tool-use enforcement that would be noise for a frontier model.
VENDOR_CLASS_BY_PROVIDER: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai-family",
    "openrouter": "openai-family",
    "xai": "openai-family",
    "gemini": "gemini",
    "meta": "meta",
    "local": "small-local",
    "qwen": "small-local",
    "glm": "small-local",
    "deepseek": "small-local",
    "minimax": "small-local",
    "kimi": "small-local",
}

#: Fill ratio at which the brevity line joins the volatile tier.
CONTEXT_PRESSURE_THRESHOLD = 0.75

#: Default mode when a session carries something unrecognised.
DEFAULT_MODE = "accept"

#: Permission tag shown next to an MCP server whose tools carry none.
DEFAULT_MCP_PERMISSION = "network"

#: Printed once, before the first MCP heading, so the headings are readable.
MCP_GROUP_NOTE = (
    "The tools below come from MCP servers the user configured. The tag after a "
    "server's name is the permission every one of its tools carries."
)

#: Skills listed in the prompt index before it elides (M15 §B1).
DEFAULT_SKILL_INDEX_MAX = 60

#: What the index says when it could not list everything.
SKILL_INDEX_MORE = "… and {count} more — skill_list shows all"

#: What a workflow brief prepends in place of the full rule set, which the
#: child already carries in its own system prompt.
BRIEF_RULES = (
    "The rules in your system prompt apply in full: read before you edit, change "
    "only what the task needs, run the project's checks, and never report a result "
    "you did not actually produce."
)

_LANGUAGE_NAMES: dict[str, str] = {
    "ko": "Korean",
    "en": "English",
    "ja": "Japanese",
    "zh": "Chinese",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "ru": "Russian",
}


#: Model id -> prompt layer, checked before the provider (opencode picks its
#: prompt by model id the same way): Muse Spark through OpenRouter still gets
#: the Meta layer, Claude through a gateway the Anthropic one.
MODEL_FAMILY_CLASSES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"muse[-_.]?spark", re.IGNORECASE), "meta"),
    (re.compile(r"claude", re.IGNORECASE), "anthropic"),
    (re.compile(r"gemini", re.IGNORECASE), "gemini"),
    (re.compile(r"(?:^|[/:])(?:gpt|o\d|codex|grok)", re.IGNORECASE), "openai-family"),
    (re.compile(r"qwen|glm|deepseek|kimi|minimax|llama|mistral", re.IGNORECASE), "small-local"),
)


def vendor_class_for(
    provider: str | None, *, local_style: bool = False, model: str | None = None
) -> str:
    """The prompt layer for a provider preset; unknown vendors get the default.

    ``local_style`` is what a named OpenAI-compatible server (``hon2``) passes:
    its id is whatever the user called it, but it is running the same kind of
    small model as the built-in ``local`` vendor and needs the same tool-use
    enforcement.
    """
    if local_style:
        return "small-local"
    if model:
        for pattern, family in MODEL_FAMILY_CLASSES:
            if pattern.search(model):
                return family
    if not provider:
        return "anthropic"
    key = provider.strip().lower().partition(":")[0]
    if key in VENDOR_CLASS_BY_PROVIDER:
        return VENDOR_CLASS_BY_PROVIDER[key]
    return "small-local" if local_style else "openai-family"


def language_name(tag: str) -> str:
    """``"ko"`` -> ``"Korean"``; an unknown tag is used as written."""
    key = tag.strip().replace("_", "-")
    return _LANGUAGE_NAMES.get(key.lower().partition("-")[0], key)


def reply_language_rule(language: str) -> str:
    """The directed reply-language line, or ``""`` for ``"auto"``.

    ``auto`` needs no line: ``base.md`` already says to answer in the language
    the user wrote in.
    """
    tag = (language or "auto").strip()
    if not tag or tag.lower() == "auto":
        return ""
    name = language_name(tag)
    return (
        f"Language override. Reply in {name}, whatever language the user writes in. "
        "Keep code, file paths, commands, identifiers and log excerpts exactly as "
        "they are — never translate them."
    )


def tool_lines(tools: Sequence[ToolSpec], *, describe: bool = False) -> str:
    """The tool list: built-ins first, then one heading per MCP server (M15 §E).

    An MCP tool's name already carries its server (``mcp__<server>__<tool>``),
    but a flat list of forty prefixed names reads as noise.  Grouping them under
    ``MCP server <name> (<permission tag>)`` tells the model in one line where a
    tool comes from and what approving it costs, and keeps the built-ins — the
    tools it should reach for first — at the top.

    Names only unless ``describe``: every adapter sends the tools natively,
    descriptions included, so repeating them here only doubled the cost.
    """
    builtin: list[str] = []
    servers: dict[str, list[str]] = {}
    tags: dict[str, str] = {}
    for tool in tools:
        server = str(getattr(tool, "source", "") or "")
        server = server[4:] if server.startswith("mcp:") else ""
        line = f"- {tool.name}: {tool.description}" if describe else f"- {tool.name}"
        if not server:
            builtin.append(line)
            continue
        servers.setdefault(server, []).append(line)
        tags.setdefault(server, str(getattr(tool, "permission", "") or DEFAULT_MCP_PERMISSION))
    out = list(builtin)
    for index, (server, lines) in enumerate(servers.items()):
        out.append("")
        if index == 0:
            out.append(MCP_GROUP_NOTE)
        out.append(f"MCP server {server} ({tags[server]})")
        out.extend(lines)
    return "\n".join(out)


def tools_block(
    tools: Sequence[ToolSpec],
    deferred: Sequence[ToolSpec] = (),
    root: Path | None = None,
    *,
    describe: bool = False,
    loaded: Sequence[ToolSpec] = (),
) -> str:
    """The rendered ``Available tools:`` fragment for one round.

    ``deferred`` is the half of the catalogue this round names but does not
    describe (CORE-round-cost): one grouped, description-free line instead of
    forty schemas.  Rendering is a pure function of its arguments, which is
    what lets the agent layer cache the string and keep the cached prefix
    byte-identical between rounds.
    """
    from snowpea_core.tools.deferred import deferred_line
    from snowpea_core.tools.tool_search import render as render_specs

    block = render(
        "fragments/tools",
        root,
        TOOL_LINES=tool_lines(tools, describe=describe),
        DEFERRED_LINE=deferred_line(deferred),
    )
    if not loaded:
        return block
    return (
        f"{block}\n\nLoaded earlier with tool_search (still callable by name; the full "
        f"text again, after a compaction):\n{render_specs(list(loaded))}"
    )


def skills_index(
    groups: Sequence[tuple[str, Sequence[tuple[str, str]]]],
    max_entries: int = DEFAULT_SKILL_INDEX_MAX,
    root: Path | None = None,
    *,
    scoped: bool = False,
) -> str:
    """The rendered ``## Skills`` block, or ``""`` when nothing is installed.

    ``groups`` is what ``SkillLoader.index_groups()`` returns: a bracketed
    origin label and its ``(name, description)`` rows.  Past ``max_entries``
    the list stops and says how many it did not show, so a machine with two
    hundred skills does not spend the context window on a catalogue.
    ``scoped`` is an agent's own short list (``skillsIndex: plugin``), under
    a header that does not insist on loading one for every task.
    """
    lines: list[str] = []
    shown = 0
    total = sum(len(rows) for _, rows in groups)
    for label, rows in groups:
        if shown >= max_entries:
            break
        room = max_entries - shown
        lines.append(label)
        for name, description in list(rows)[:room]:
            lines.append(f"- {name}: {description}" if description else f"- {name}")
        shown += min(room, len(rows))
    if not lines:
        return ""
    if total > shown:
        lines.append(SKILL_INDEX_MORE.format(count=total - shown))
    fragment = "fragments/skills-scoped" if scoped else "fragments/skills"
    return render(fragment, root, SKILL_LINES="\n".join(lines))


@dataclass(frozen=True)
class PromptTiers:
    """The three tiers, each already joined, any of them possibly empty."""

    stable: str = ""
    context: str = ""
    volatile: str = ""

    def text(self) -> str:
        return "\n\n".join(part for part in (self.stable, self.context, self.volatile) if part)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.text()


def build_tiers(
    *,
    mode: str = DEFAULT_MODE,
    vendor_class: str = "anthropic",
    role: str | None = None,
    subagent: bool = False,
    tools: Sequence[ToolSpec] | None = None,
    deferred_tools: Sequence[ToolSpec] | None = None,
    tools_text: str | None = None,
    skill_groups: Sequence[tuple[str, Sequence[tuple[str, str]]]] | None = None,
    skill_index_max: int = DEFAULT_SKILL_INDEX_MAX,
    skills_scoped: bool = False,
    memory_guidance: bool = True,
    memory_block: str = "",
    plan_line: str = "",
    environment: str = "",
    context_files: str = "",
    context_fill: float | None = None,
    reply_language: str = "auto",
    identity: str | None = None,
    own_identity: bool = False,
    persona: str = "",
    root: Path | None = None,
    registry: SectionRegistry | None = None,
) -> PromptTiers:
    """Build the three tiers separately, so a caller can measure or cache them.

    The text comes from the registered sections (:mod:`prompts.sections`, M16
    P1.1), sorted by their order; the order band of each decides its tier.

    ``identity`` replaces ``base.md`` outright and is for a caller that wants a
    bare prompt.  ``persona`` — an ``AgentDefinition``'s own prompt — is added
    *after* the role instead, so a named agent gains its brief without losing
    the coding discipline every other agent has.  ``own_identity`` is the
    other way round: the persona *is* the identity (``identity: own``), and of
    the core coding layer only ``fragments/essentials`` stays — honesty, reply
    length, trust, language.
    """
    inputs = PromptInputs(
        mode=mode,
        vendor_class=vendor_class,
        role=role,
        subagent=subagent,
        tools=tools,
        deferred_tools=deferred_tools,
        tools_text=tools_text,
        skill_groups=skill_groups,
        skill_index_max=skill_index_max,
        skills_scoped=skills_scoped,
        memory_guidance=memory_guidance,
        memory_block=memory_block,
        plan_line=plan_line,
        environment=environment,
        context_files=context_files,
        context_fill=context_fill,
        reply_language=reply_language,
        identity=identity,
        own_identity=own_identity,
        persona=persona,
        root=root,
    )
    tiers: dict[str, list[str]] = {"stable": [], "context": [], "volatile": []}
    for section, text in (registry or REGISTRY).render(inputs):
        tiers[section.tier].append(text)
    return PromptTiers(
        stable="\n\n".join(tiers["stable"]),
        context="\n\n".join(tiers["context"]),
        volatile="\n\n".join(tiers["volatile"]),
    )


def build_system_prompt(**kwargs: Any) -> str:
    """The whole system prompt for one turn: the three tiers, in order.

    Takes exactly the keyword arguments of :func:`build_tiers`.
    """
    return build_tiers(**kwargs).text()


def workflow_brief(
    name: str,
    *,
    reply_language: str = "auto",
    base_rules: bool = True,
    root: Path | None = None,
    **variables: object,
) -> str:
    """Render ``workflows/<name>.md``.

    Briefs handed to a subagent carry ``${BASE_RULES}`` and ``${LANGUAGE_RULE}``
    so that a worker is never given a task with no discipline attached and never
    has to guess what language to answer in.
    """
    return render(
        f"workflows/{name}",
        root,
        BASE_RULES=BRIEF_RULES if base_rules else "",
        LANGUAGE_RULE=reply_language_rule(reply_language),
        **variables,
    )


__all__ = [
    "BRIEF_RULES",
    "CONTEXT_PRESSURE_THRESHOLD",
    "DEFAULT_MCP_PERMISSION",
    "DEFAULT_SKILL_INDEX_MAX",
    "MCP_GROUP_NOTE",
    "SKILL_INDEX_MORE",
    "VENDOR_CLASSES",
    "VENDOR_CLASS_BY_PROVIDER",
    "PromptTiers",
    "build_system_prompt",
    "build_tiers",
    "language_name",
    "reply_language_rule",
    "skills_index",
    "tool_lines",
    "tools_block",
    "vendor_class_for",
    "workflow_brief",
]
