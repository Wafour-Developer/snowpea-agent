"""Assembles the system prompt out of the prompt library, in three tiers.

The tiers are joined stable -> context -> volatile and nothing reorders them,
because prompt-prefix caching on both Anthropic and OpenAI keys off an exact
leading substring: anything that changes per turn has to come last.

- **stable** — identity and rules, the vendor layer, the config and search
  rules, the mode, and a subagent's role.  Changes only when the mode, model,
  role or reply language changes.
- **context** — the environment block, the project context files, the tool
  list.  Rebuilt once per session and after a compaction.
- **volatile** — the memory block and the context-pressure line.  Per turn.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from snowpea_core.prompts.loader import load, render

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from snowpea_core.providers.base import ToolSpec

#: Prompt layers keyed on how capable the model family is.
VENDOR_CLASSES: tuple[str, ...] = ("anthropic", "openai-family", "small-local")

#: Provider preset -> vendor class.  A small local model needs several hundred
#: words of tool-use enforcement that would be noise for a frontier model.
VENDOR_CLASS_BY_PROVIDER: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai-family",
    "openrouter": "openai-family",
    "xai": "openai-family",
    "gemini": "openai-family",
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


def vendor_class_for(provider: str | None, *, local_style: bool = False) -> str:
    """The prompt layer for a provider preset; unknown vendors get the default.

    ``local_style`` is what a named OpenAI-compatible server (``hon2``) passes:
    its id is whatever the user called it, but it is running the same kind of
    small model as the built-in ``local`` vendor and needs the same tool-use
    enforcement.
    """
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


def tool_lines(tools: Sequence[ToolSpec]) -> str:
    """The tool list: built-ins first, then one heading per MCP server (M15 §E).

    An MCP tool's name already carries its server (``mcp__<server>__<tool>``),
    but a flat list of forty prefixed names reads as noise.  Grouping them under
    ``MCP server <name> (<permission tag>)`` tells the model in one line where a
    tool comes from and what approving it costs, and keeps the built-ins — the
    tools it should reach for first — at the top.
    """
    builtin: list[str] = []
    servers: dict[str, list[str]] = {}
    tags: dict[str, str] = {}
    for tool in tools:
        server = str(getattr(tool, "source", "") or "")
        server = server[4:] if server.startswith("mcp:") else ""
        line = f"- {tool.name}: {tool.description}"
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
) -> str:
    """The rendered ``Available tools:`` fragment for one round.

    ``deferred`` is the half of the catalogue this round names but does not
    describe (CORE-round-cost): one grouped, description-free line instead of
    forty schemas.  Rendering is a pure function of its arguments, which is
    what lets the agent layer cache the string and keep the cached prefix
    byte-identical between rounds.
    """
    from snowpea_core.tools.deferred import deferred_line

    return render(
        "fragments/tools",
        root,
        TOOL_LINES=tool_lines(tools),
        DEFERRED_LINE=deferred_line(deferred),
    )


def skills_index(
    groups: Sequence[tuple[str, Sequence[tuple[str, str]]]],
    max_entries: int = DEFAULT_SKILL_INDEX_MAX,
    root: Path | None = None,
) -> str:
    """The rendered ``## Skills`` block, or ``""`` when nothing is installed.

    ``groups`` is what ``SkillLoader.index_groups()`` returns: a bracketed
    origin label and its ``(name, description)`` rows.  Past ``max_entries``
    the list stops and says how many it did not show, so a machine with two
    hundred skills does not spend the context window on a catalogue.
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
    return render("fragments/skills", root, SKILL_LINES="\n".join(lines))


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


def _join(parts: Sequence[str]) -> str:
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


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
    memory_guidance: bool = True,
    memory_block: str = "",
    environment: str = "",
    context_files: str = "",
    context_fill: float | None = None,
    reply_language: str = "auto",
    identity: str | None = None,
    persona: str = "",
    root: Path | None = None,
) -> PromptTiers:
    """Build the three tiers separately, so a caller can measure or cache them.

    ``identity`` replaces ``base.md`` outright and is for a caller that wants a
    bare prompt.  ``persona`` — an ``AgentDefinition``'s own prompt — is added
    *after* the role instead, so a named agent gains its brief without losing
    the coding discipline every other agent has.
    """
    stable: list[str] = [identity.strip() if identity and identity.strip() else load("base", root)]
    # Right after the identity: the working discipline every family needs
    # (M15 §A1).  The vendor layer below only adds family-specific enforcement.
    stable.append(load("fragments/execution", root))

    vendor = vendor_class if vendor_class in VENDOR_CLASSES else "anthropic"
    stable.append(load(f"vendors/{vendor}", root))
    stable.append(load("config_rule", root))
    stable.append(load("search_honesty", root))
    if memory_guidance:
        stable.append(load("fragments/memory-guidance", root))
    picked = mode if mode in ("plan", "accept", "auto") else DEFAULT_MODE
    stable.append(load(f"modes/{picked}", root))
    if subagent or role:
        stable.append(load("roles/_preamble", root))
    if role:
        stable.append(load(f"roles/{role}", root))
    stable.append(persona)
    stable.append(reply_language_rule(reply_language))

    context: list[str] = [environment, context_files]
    # The skills index sits *before* the tool list and after the project
    # context: both are rebuilt together, and the model should know what a
    # skill covers before it starts picking tools (M15 §B1).
    if skill_groups:
        context.append(skills_index(skill_groups, skill_index_max, root))
    # ``tools_text`` is an already-rendered fragment, handed in by the agent
    # layer's per-session cache so two identical rounds produce byte-identical
    # prompts and the provider's prefix cache actually hits (CORE-round-cost).
    if tools_text:
        context.append(tools_text)
    elif tools:
        context.append(tools_block(tools, deferred_tools or (), root))

    volatile: list[str] = [memory_block]
    if context_fill is not None and context_fill >= CONTEXT_PRESSURE_THRESHOLD:
        volatile.append(load("fragments/context-pressure", root))

    return PromptTiers(stable=_join(stable), context=_join(context), volatile=_join(volatile))


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
