"""The system prompt as registered sections (M16 P1.1).

The first rule is that moving to sections changed nothing a model sees: the
registry renders byte for byte what the old tier builder did, for every
combination of the inputs that steer it.  The old builder is frozen below as
the reference.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from snowpea_core.prompts import compose, sections
from snowpea_core.prompts.loader import load


@dataclass(frozen=True)
class _Spec:
    name: str
    description: str = "does a thing"
    source: str = "builtin"
    permission: str = "read"


def _legacy(
    *,
    mode: str = "accept",
    vendor_class: str = "anthropic",
    role: str | None = None,
    subagent: bool = False,
    tools: Sequence[object] | None = None,
    deferred_tools: Sequence[object] | None = None,
    tools_text: str | None = None,
    skill_groups=None,
    skill_index_max: int = 60,
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
    root=None,
) -> compose.PromptTiers:
    """``compose.build_tiers`` as it was before M16, kept as the reference."""
    if own_identity:
        stable: list[str] = [load("fragments/essentials", root)]
        memory_guidance = False
    else:
        stable = [identity.strip() if identity and identity.strip() else load("base", root)]
        stable.append(load("fragments/execution", root))
        if not (subagent or role):
            stable.append(load("fragments/plan-continuity", root))
    vendor = vendor_class if vendor_class in compose.VENDOR_CLASSES else "anthropic"
    stable.append(load(f"vendors/{vendor}", root))
    stable.append(load("config_rule", root))
    stable.append(load("search_honesty", root))
    if memory_guidance:
        stable.append(load("fragments/memory-guidance", root))
    picked = mode if mode in ("plan", "accept", "auto") else "accept"
    stable.append(load(f"modes/{picked}", root))
    if subagent or role:
        stable.append(load("roles/_preamble", root))
    if role:
        stable.append(load(f"roles/{role}", root))
    stable.append(persona)
    stable.append(compose.reply_language_rule(reply_language))
    context: list[str] = [environment, context_files]
    if skill_groups:
        context.append(
            compose.skills_index(skill_groups, skill_index_max, root, scoped=skills_scoped)
        )
    if tools_text:
        context.append(tools_text)
    elif tools:
        context.append(compose.tools_block(tools, deferred_tools or (), root))  # type: ignore[arg-type]
    volatile: list[str] = [memory_block, plan_line]
    if context_fill is not None and context_fill >= compose.CONTEXT_PRESSURE_THRESHOLD:
        volatile.append(load("fragments/context-pressure", root))

    def join(parts: list[str]) -> str:
        return "\n\n".join(part.strip() for part in parts if part and part.strip())

    return compose.PromptTiers(stable=join(stable), context=join(context), volatile=join(volatile))


TOOLS = [
    _Spec("read_file"),
    _Spec("shell", permission="exec"),
    _Spec("mcp__gh__issue", source="mcp:gh"),
]
GROUPS = [("[builtin]", [("pdf", "read pdfs"), ("xlsx", "")])]

MATRIX = {
    "mode": ["plan", "accept", "auto", "weird"],
    "vendor_class": ["anthropic", "small-local", "gemini", "nope"],
    "role": [None, "executor"],
    "subagent": [False, True],
    "own_identity": [False, True],
    "memory_guidance": [True, False],
}


@pytest.mark.parametrize(
    "combo", list(itertools.product(*MATRIX.values())), ids=lambda c: "-".join(map(str, c))
)
def test_sections_render_exactly_what_the_tier_builder_did(combo: tuple) -> None:
    kwargs = dict(zip(MATRIX, combo, strict=True))
    extra = dict(
        persona="You are the release manager.",
        reply_language="ko",
        environment="<env>cwd: /w</env>",
        context_files="AGENTS.md says hi",
        skill_groups=GROUPS,
        tools=TOOLS,
        memory_block="recalled: x",
        plan_line="Current plan: ship it",
        context_fill=0.9,
    )
    old = _legacy(**kwargs, **extra)
    new = compose.build_tiers(**kwargs, **extra)
    assert (new.stable, new.context, new.volatile) == (old.stable, old.context, old.volatile)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"identity": "  BARE  "},
        {"tools_text": "cached tools text", "tools": TOOLS},
        {"context_fill": 0.1},
        {"skill_groups": GROUPS, "skills_scoped": True},
    ],
)
def test_the_edge_inputs_match_too(extra: dict) -> None:
    old = _legacy(**extra)
    new = compose.build_tiers(**extra)
    assert new.text() == old.text()


def test_a_plugin_section_lands_where_its_order_says_and_leaves_cleanly() -> None:
    registry = sections.default_registry()
    before = compose.build_tiers(registry=registry).text()
    dispose = registry.register(
        sections.PromptSection("plugin:house-style", 650, lambda i: "House style: be terse.")
    )
    text = compose.build_tiers(registry=registry, persona="P", reply_language="ko").text()
    assert text.index("P") < text.index("House style") < text.index("Language override")
    dispose()
    assert compose.build_tiers(registry=registry).text() == before


def test_a_section_for_a_hidden_tool_is_not_rendered() -> None:
    registry = sections.default_registry()
    registry.register(
        sections.PromptSection(
            "tool:browser", 1350, lambda i: "Browser tips.", requires_tool="browser_open"
        )
    )
    assert "Browser tips" not in compose.build_tiers(registry=registry, tools=TOOLS).text()
    with_browser = [*TOOLS, _Spec("browser_open")]
    assert "Browser tips" in compose.build_tiers(registry=registry, tools=with_browser).context


def test_replacing_a_builtin_then_disposing_restores_it() -> None:
    registry = sections.default_registry()
    original = compose.build_tiers(registry=registry).stable
    dispose = registry.register(sections.PromptSection("config-rule", 100, lambda i: "CUSTOM"))
    assert "CUSTOM" in compose.build_tiers(registry=registry).stable
    dispose()
    assert compose.build_tiers(registry=registry).stable == original


def test_the_order_decides_the_tier() -> None:
    assert sections.tier_of(-1000) == "stable"
    assert sections.tier_of(1300) == "context"
    assert sections.tier_of(2100) == "volatile"
