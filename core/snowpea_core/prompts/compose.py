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


def vendor_class_for(provider: str | None) -> str:
    """The prompt layer for a provider preset; unknown vendors get the default."""
    if not provider:
        return "anthropic"
    key = provider.strip().lower().partition(":")[0]
    return VENDOR_CLASS_BY_PROVIDER.get(key, "openai-family")


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
        f"they are — never translate them. When you delegate, tell the subagent to "
        f"answer in {name} too, because it cannot see this conversation."
    )


def tool_lines(tools: Sequence[ToolSpec]) -> str:
    """One ``- name: description`` line per tool, in the order given."""
    return "\n".join(f"- {tool.name}: {tool.description}" for tool in tools)


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

    vendor = vendor_class if vendor_class in VENDOR_CLASSES else "anthropic"
    stable.append(load(f"vendors/{vendor}", root))
    stable.append(load("config_rule", root))
    stable.append(load("search_honesty", root))
    picked = mode if mode in ("plan", "accept", "auto") else DEFAULT_MODE
    stable.append(load(f"modes/{picked}", root))
    if subagent or role:
        stable.append(load("roles/_preamble", root))
    if role:
        stable.append(load(f"roles/{role}", root))
    stable.append(persona)
    stable.append(reply_language_rule(reply_language))

    context: list[str] = [environment, context_files]
    if tools:
        context.append(render("fragments/tools", root, TOOL_LINES=tool_lines(tools)))

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
    "VENDOR_CLASSES",
    "VENDOR_CLASS_BY_PROVIDER",
    "PromptTiers",
    "build_system_prompt",
    "build_tiers",
    "language_name",
    "reply_language_rule",
    "tool_lines",
    "vendor_class_for",
    "workflow_brief",
]
