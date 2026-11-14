"""The system prompt as a list of named, ordered sections (M16 P1.1).

A section is ``(name, order, render)``: ``render`` gets the turn's
:class:`PromptInputs` and returns its text, or ``""`` to drop out.  Sections are
sorted by ``order`` (ties by name) and their non-empty texts joined by a blank
line.  Positions come from one table, :data:`SECTION_ORDERS`, so where a piece
of the prompt sits is decided in one place rather than by the order of
``append`` calls (dsh ``packages/core/system-prompt``, ``SECTION_ORDERS``).

The order bands are the three tiers that keep the provider's prefix cache
warm: ``stable`` (< 1000) changes only with the mode, model, role or reply
language; ``context`` (1000–1999) once per session and after a compaction;
``volatile`` (>= 2000) per turn.  Nothing may move text between bands without
saying what it costs the cache.

Every registration returns a disposer, so a plugin that adds a section takes it
away again when it is unloaded.  A section may name the tool it explains
(``requires_tool``): it renders only while that tool is offered, so guidance
for a hidden tool never reaches the model.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from snowpea_core.prompts.loader import load

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from snowpea_core.providers.base import ToolSpec

Tier = Literal["stable", "context", "volatile"]

#: Where each built-in section sits.  Plugins pick any number; a number inside a
#: band is what decides its tier (see :func:`tier_of`).
SECTION_ORDERS: dict[str, int] = {
    # stable
    "identity": -1000,
    "execution": -900,
    "plan-continuity": -800,
    "vendor": 0,
    "config-rule": 100,
    "search-honesty": 200,
    "memory-guidance": 300,
    "mode": 400,
    "role-preamble": 500,
    "role": 510,
    "persona": 600,
    "reply-language": 700,
    # context
    "environment": 1000,
    "context-files": 1100,
    "skills": 1200,
    "tools": 1300,
    # volatile
    "memory": 2000,
    "plan-line": 2100,
    "context-pressure": 2200,
}

#: First order of the ``context`` and ``volatile`` bands.
CONTEXT_BAND = 1000
VOLATILE_BAND = 2000


def section_order(name: str) -> int:
    """The table position of a built-in section; ``KeyError`` for an unknown one."""
    return SECTION_ORDERS[name]


def tier_of(order: int) -> Tier:
    if order >= VOLATILE_BAND:
        return "volatile"
    if order >= CONTEXT_BAND:
        return "context"
    return "stable"


@dataclass(frozen=True)
class PromptInputs:
    """Everything a section may read; the keyword arguments of ``build_tiers``."""

    mode: str = "accept"
    vendor_class: str = "anthropic"
    role: str | None = None
    subagent: bool = False
    tools: Sequence[ToolSpec] | None = None
    deferred_tools: Sequence[ToolSpec] | None = None
    tools_text: str | None = None
    skill_groups: Sequence[tuple[str, Sequence[tuple[str, str]]]] | None = None
    skill_index_max: int = 60
    skills_scoped: bool = False
    memory_guidance: bool = True
    memory_block: str = ""
    plan_line: str = ""
    environment: str = ""
    context_files: str = ""
    context_fill: float | None = None
    reply_language: str = "auto"
    identity: str | None = None
    own_identity: bool = False
    persona: str = ""
    root: Path | None = None
    #: Free-form values a plugin section may read (``extra["key"]``).
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(getattr(tool, "name", "") for tool in (self.tools or ()))


Render = Callable[[PromptInputs], str]


@dataclass(frozen=True)
class PromptSection:
    """One named piece of the system prompt."""

    name: str
    order: int
    render: Render
    #: Rendered only while this tool is offered this round.
    requires_tool: str | None = None
    #: ``builtin`` or the plugin that registered it.
    source: str = "builtin"

    @property
    def tier(self) -> Tier:
        return tier_of(self.order)


class SectionRegistry:
    """Named sections, sorted by order then name; registration returns a disposer."""

    def __init__(self) -> None:
        self._sections: dict[str, PromptSection] = {}
        self._lock = threading.Lock()

    def register(self, section: PromptSection) -> Callable[[], None]:
        """Add (or replace) ``section``; the returned callable removes exactly it."""
        with self._lock:
            previous = self._sections.get(section.name)
            self._sections[section.name] = section

        def dispose() -> None:
            with self._lock:
                if self._sections.get(section.name) is section:
                    if previous is not None:
                        self._sections[section.name] = previous
                    else:
                        del self._sections[section.name]

        return dispose

    def sections(self) -> list[PromptSection]:
        with self._lock:
            items = list(self._sections.values())
        return sorted(items, key=lambda item: (item.order, item.name))

    def render(self, inputs: PromptInputs) -> list[tuple[PromptSection, str]]:
        """``(section, text)`` for every section that renders something, in order."""
        out: list[tuple[PromptSection, str]] = []
        names = inputs.tool_names
        for section in self.sections():
            if section.requires_tool and section.requires_tool not in names:
                continue
            text = (section.render(inputs) or "").strip()
            if text:
                out.append((section, text))
        return out


# ---------------------------------------------------------------------------
# built-in sections — byte for byte what the tier builder used to append
# ---------------------------------------------------------------------------


def _identity(i: PromptInputs) -> str:
    if i.own_identity:
        return load("fragments/essentials", i.root)
    return i.identity.strip() if i.identity and i.identity.strip() else load("base", i.root)


def _execution(i: PromptInputs) -> str:
    # Right after the identity: the working discipline every family needs
    # (M15 §A1).  The vendor layer only adds family-specific enforcement.
    return "" if i.own_identity else load("fragments/execution", i.root)


def _plan_continuity(i: PromptInputs) -> str:
    # How to treat the "Current plan:" line.  Only the main agent works a plan:
    # a subagent or a role gets a brief, not the plan (CORE-plan-continuity).
    if i.own_identity or i.subagent or i.role:
        return ""
    return load("fragments/plan-continuity", i.root)


def _vendor(i: PromptInputs) -> str:
    from snowpea_core.prompts.compose import VENDOR_CLASSES

    vendor = i.vendor_class if i.vendor_class in VENDOR_CLASSES else "anthropic"
    return load(f"vendors/{vendor}", i.root)


def _memory_guidance(i: PromptInputs) -> str:
    if i.own_identity or not i.memory_guidance:
        return ""
    return load("fragments/memory-guidance", i.root)


def _mode(i: PromptInputs) -> str:
    from snowpea_core.prompts.compose import DEFAULT_MODE

    picked = i.mode if i.mode in ("plan", "accept", "auto") else DEFAULT_MODE
    return load(f"modes/{picked}", i.root)


def _role_preamble(i: PromptInputs) -> str:
    return load("roles/_preamble", i.root) if (i.subagent or i.role) else ""


def _role(i: PromptInputs) -> str:
    return load(f"roles/{i.role}", i.root) if i.role else ""


def _reply_language(i: PromptInputs) -> str:
    from snowpea_core.prompts.compose import reply_language_rule

    return reply_language_rule(i.reply_language)


def _skills(i: PromptInputs) -> str:
    # Before the tool list and after the project context: the model should
    # know what a skill covers before it starts picking tools (M15 §B1).
    if not i.skill_groups:
        return ""
    from snowpea_core.prompts.compose import skills_index

    return skills_index(i.skill_groups, i.skill_index_max, i.root, scoped=i.skills_scoped)


def _tools(i: PromptInputs) -> str:
    # ``tools_text`` is the agent layer's per-session cached fragment, so two
    # identical rounds give byte-identical prompts (CORE-round-cost).
    if i.tools_text:
        return i.tools_text
    if i.tools:
        from snowpea_core.prompts.compose import tools_block

        return tools_block(i.tools, i.deferred_tools or (), i.root)
    return ""


def _context_pressure(i: PromptInputs) -> str:
    from snowpea_core.prompts.compose import CONTEXT_PRESSURE_THRESHOLD

    if i.context_fill is not None and i.context_fill >= CONTEXT_PRESSURE_THRESHOLD:
        return load("fragments/context-pressure", i.root)
    return ""


BUILTIN_SECTIONS: tuple[PromptSection, ...] = (
    PromptSection("identity", SECTION_ORDERS["identity"], _identity),
    PromptSection("execution", SECTION_ORDERS["execution"], _execution),
    PromptSection("plan-continuity", SECTION_ORDERS["plan-continuity"], _plan_continuity),
    PromptSection("vendor", SECTION_ORDERS["vendor"], _vendor),
    PromptSection(
        "config-rule", SECTION_ORDERS["config-rule"], lambda i: load("config_rule", i.root)
    ),
    PromptSection(
        "search-honesty", SECTION_ORDERS["search-honesty"], lambda i: load("search_honesty", i.root)
    ),
    PromptSection("memory-guidance", SECTION_ORDERS["memory-guidance"], _memory_guidance),
    PromptSection("mode", SECTION_ORDERS["mode"], _mode),
    PromptSection("role-preamble", SECTION_ORDERS["role-preamble"], _role_preamble),
    PromptSection("role", SECTION_ORDERS["role"], _role),
    PromptSection("persona", SECTION_ORDERS["persona"], lambda i: i.persona),
    PromptSection("reply-language", SECTION_ORDERS["reply-language"], _reply_language),
    PromptSection("environment", SECTION_ORDERS["environment"], lambda i: i.environment),
    PromptSection("context-files", SECTION_ORDERS["context-files"], lambda i: i.context_files),
    PromptSection("skills", SECTION_ORDERS["skills"], _skills),
    PromptSection("tools", SECTION_ORDERS["tools"], _tools),
    PromptSection("memory", SECTION_ORDERS["memory"], lambda i: i.memory_block),
    # The current plan's one-line summary: per turn, because a step can be marked
    # between turns, and volatile so a changed plan never costs the cached
    # prefix (CORE-plan-continuity).
    PromptSection("plan-line", SECTION_ORDERS["plan-line"], lambda i: i.plan_line),
    PromptSection("context-pressure", SECTION_ORDERS["context-pressure"], _context_pressure),
)


def default_registry() -> SectionRegistry:
    """A registry holding the built-in sections only."""
    registry = SectionRegistry()
    for section in BUILTIN_SECTIONS:
        registry.register(section)
    return registry


#: The daemon's registry: built-ins plus whatever plugins add.
REGISTRY = default_registry()


__all__ = [
    "BUILTIN_SECTIONS",
    "CONTEXT_BAND",
    "REGISTRY",
    "SECTION_ORDERS",
    "VOLATILE_BAND",
    "PromptInputs",
    "PromptSection",
    "SectionRegistry",
    "Tier",
    "default_registry",
    "section_order",
    "tier_of",
]
