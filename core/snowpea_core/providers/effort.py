"""Reasoning effort — one knob, four tiers, mapped per vendor.

Every frontier model now has a dial for how long it may think before it
answers, and every vendor spells it differently: OpenAI takes a
``reasoning_effort`` string, the Codex backend takes its own four-value scale,
Anthropic and Gemini take a *token budget*, and a self-hosted server usually
takes nothing at all.  Asking a user to learn four spellings to say "think
harder" is what this module exists to avoid.

The user-facing scale is ``low | medium | high | max``.  Everything below is a
pure function from that tier to one vendor's request field, so the mapping is
testable without a network and lives in exactly one place per vendor
(CORE-effort).

Resolution order for one turn, highest first:

1. an explicit override passed to the call,
2. the session pin (``session.setEffort``, ``/effort``),
3. ``agent.effortBy["<vendor>:<model>"]``,
4. ``agent.effortBy["<vendor>"]``,
5. ``providers.openai.reasoning_effort`` — read-only legacy, OpenAI only,
6. ``agent.effort`` (default ``medium``).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

log = logging.getLogger("snowpea.providers.effort")

Effort = Literal["low", "medium", "high", "max"]
EffortSource = Literal["session", "model", "vendor", "default"]

#: The user-facing scale, weakest first.
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "max")
DEFAULT_EFFORT = "medium"
#: What ``/effort auto`` and ``session.setEffort null`` mean: no pin.
AUTO = "auto"

#: ``reasoning_effort`` values the OpenAI API accepts.  ``minimal`` is in the
#: wire vocabulary but not on our scale — ``low`` is the weakest tier a user
#: can ask for, because ``minimal`` disables reasoning on models that need it
#: to call tools correctly.
OPENAI_EFFORTS: tuple[str, ...] = ("minimal", "low", "medium", "high")
_OPENAI_BY_TIER: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    # The API has no fourth rung; ``max`` is "as hard as this endpoint goes".
    "max": "high",
}

#: The Codex backend's own scale, which *does* have a fourth rung.
CODEX_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
_CODEX_BY_TIER: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
}

#: Thinking budgets in tokens, for the vendors that take a number rather than
#: a word.  The tiers double and then double again: a tier that is not clearly
#: more thinking than the one below it is a tier nobody can feel.
BUDGET_BY_TIER: dict[str, int] = {
    "low": 2_048,
    "medium": 8_192,
    "high": 32_768,
    "max": 65_536,
}

#: A thinking budget must leave room for the answer itself, so it is clamped to
#: this fraction of the call's output budget.  Anthropic rejects a budget that
#: is not strictly smaller than ``max_tokens``; leaving a quarter of the budget
#: for the reply is what keeps a "think hard" turn from answering nothing.
BUDGET_SHARE = 0.75
#: Anthropic refuses a budget below this outright.
MIN_ANTHROPIC_BUDGET = 1_024

#: Model-id prefixes whose OpenAI endpoint accepts ``reasoning_effort``.
#: Anything else answers HTTP 400 ``Unsupported parameter``, which is why
#: :func:`supports_openai_effort` is consulted before the field is sent — and
#: why :class:`UnsupportedEffort` remembers a model that answered it anyway.
OPENAI_EFFORT_PREFIXES: tuple[str, ...] = (
    "o1",
    "o3",
    "o4",
    "gpt-5",
    "gpt5",
    "codex",
    "gpt-4.1-reasoning",
)


def normalize(value: Any) -> str | None:
    """One of :data:`EFFORTS`, or ``None`` for anything else.

    ``"auto"``, ``""`` and ``None`` all mean "nothing is pinned here"; a typo
    is logged and treated the same way, because a misspelled tier must degrade
    to the configured default rather than fail a turn.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text == AUTO:
        return None
    if text in EFFORTS:
        return text
    # Accept the two wire spellings a user may copy out of a vendor's docs.
    if text == "xhigh":
        return "max"
    if text == "minimal":
        return "low"
    log.warning("ignoring unknown reasoning effort %r (one of %s)", value, ", ".join(EFFORTS))
    return None


def _agent_block(settings: Any) -> Any:
    """``settings.agent``, whether ``settings`` is a model or a plain dict.

    The CLI reads settings over RPC as JSON, so the same resolution has to work
    on a mapping; making the accessor tolerant is cheaper than a second copy of
    the precedence chain that could drift from this one.
    """
    if isinstance(settings, dict):
        block = settings.get("agent")
        return block if isinstance(block, dict) else {}
    return getattr(settings, "agent", None)


def _field(block: Any, name: str) -> Any:
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _rule(table: Any, key: str) -> str | None:
    if not isinstance(table, dict):
        return None
    return normalize(table.get(key))


def resolve(
    settings: Any,
    vendor: str | None,
    model: str | None = None,
    *,
    session_effort: str | None = None,
    override: str | None = None,
) -> tuple[str, str]:
    """The effort in force for ``vendor``/``model``, and where it came from.

    Returns ``(effort, source)`` where source is ``"session"``, ``"model"``,
    ``"vendor"`` or ``"default"`` — surfaces print the source next to the tier
    so "why is it thinking this hard?" is answerable without reading
    ``settings.json``.
    """
    picked = normalize(override) or normalize(session_effort)
    if picked is not None:
        return picked, "session"
    agent = _agent_block(settings)
    table = _field(agent, "effortBy")
    name = str(vendor or "")
    if name and model:
        by_model = _rule(table, f"{name}:{model}")
        if by_model is not None:
            return by_model, "model"
    if name:
        by_vendor = _rule(table, name)
        if by_vendor is not None:
            return by_vendor, "vendor"
        legacy = _legacy_openai_effort(settings, name)
        if legacy is not None:
            return legacy, "vendor"
    configured = normalize(_field(agent, "effort"))
    return (configured or DEFAULT_EFFORT), "default"


def _legacy_openai_effort(settings: Any, vendor: str) -> str | None:
    """``providers.openai.reasoning_effort`` — read-only, OpenAI only.

    It predates this module and is what a Codex user already has in their
    settings file.  It is still honoured so that upgrading does not silently
    change how hard their sessions think, but nothing writes it any more:
    ``/effort`` and ``session.setEffort`` write the unified keys.
    """
    if vendor != "openai":
        return None
    providers = settings.get("providers") if isinstance(settings, dict) else None
    if providers is None:
        providers = getattr(settings, "providers", None)
    block = providers.get(vendor) if isinstance(providers, dict) else None
    if not isinstance(block, dict):
        return None
    return normalize(block.get("reasoning_effort"))


# ---------------------------------------------------------------------------
# per-vendor mappings
# ---------------------------------------------------------------------------


def supports_openai_effort(model: str | None) -> bool:
    """True when OpenAI's ``/chat/completions`` accepts ``reasoning_effort``.

    Only the reasoning families take it; sending it to ``gpt-4.1`` is an
    HTTP 400 rather than a slower answer.
    """
    name = str(model or "").strip().lower()
    if not name:
        return False
    return any(name.startswith(prefix) for prefix in OPENAI_EFFORT_PREFIXES)


def openai_reasoning_effort(effort: str | None) -> str | None:
    """Tier -> OpenAI's ``reasoning_effort``; ``None`` when nothing applies."""
    tier = normalize(effort)
    return _OPENAI_BY_TIER.get(tier) if tier else None


def codex_effort(effort: str | None) -> str | None:
    """Tier -> the Codex backend's ``reasoning.effort`` (``max`` -> ``xhigh``)."""
    tier = normalize(effort)
    return _CODEX_BY_TIER.get(tier) if tier else None


def thinking_budget(effort: str | None, max_tokens: int | None = None) -> int | None:
    """Tier -> a thinking budget in tokens, clamped to the output budget.

    ``max_tokens`` is the budget for the whole call, so the answer needs part
    of it: the thinking budget is capped at :data:`BUDGET_SHARE` of it.
    ``None`` means "do not ask for a budget".
    """
    tier = normalize(effort)
    if tier is None:
        return None
    budget = BUDGET_BY_TIER[tier]
    if max_tokens is not None and max_tokens > 0:
        budget = min(budget, int(max_tokens * BUDGET_SHARE))
    return budget


def anthropic_thinking(
    effort: str | None, max_tokens: int | None = None, *, thinking: str | None = None
) -> dict[str, Any] | None:
    """The Anthropic ``thinking`` request block, or ``None`` to omit it.

    ``thinking="off"`` wins over every tier: a user who turned thinking off
    asked for no hidden reasoning at all, and an effort tier must not put it
    back (CORE-reasoning-budget).  A budget the output limit cannot afford is
    dropped rather than clamped to something the API would refuse.
    """
    if thinking == "off":
        return None
    budget = thinking_budget(effort, max_tokens)
    if budget is None or budget < MIN_ANTHROPIC_BUDGET:
        return None
    return {"type": "enabled", "budget_tokens": budget}


def gemini_thinking_config(
    effort: str | None, max_tokens: int | None = None, *, thinking: str | None = None
) -> dict[str, Any] | None:
    """The Gemini ``thinkingConfig`` block, or ``None`` to omit it.

    ``thinking="off"`` asks for budget ``0``, which is how Gemini is told not
    to think at all; every other tier is the same token ladder the other
    budget-based vendors use.
    """
    if thinking == "off":
        return {"thinkingBudget": 0}
    budget = thinking_budget(effort, max_tokens)
    if budget is None:
        return None
    return {"thinkingBudget": budget}


class UnsupportedEffort:
    """Remembers the models whose endpoint refused ``reasoning_effort``.

    The supported-prefix list is a guess about someone else's catalog, and a
    wrong guess costs a failed turn.  A 400 naming the parameter is recorded
    here, the call is retried without the field, and the model is never sent
    it again for the life of the process — so the cost of being wrong is one
    retry, not one failure per prompt.
    """

    def __init__(self) -> None:
        self._refused: set[tuple[str, str]] = set()

    def clear(self) -> None:
        self._refused.clear()

    def refused(self, vendor: str, model: str) -> bool:
        return (vendor, str(model or "")) in self._refused

    def remember(self, vendor: str, model: str) -> None:
        log.info("%s: %s does not accept reasoning_effort; dropping it", vendor, model)
        self._refused.add((vendor, str(model or "")))

    @staticmethod
    def is_unsupported_error(detail: str) -> bool:
        """True for the HTTP 400 that means "I do not know this parameter"."""
        text = (detail or "").lower()
        if "reasoning_effort" not in text and "reasoning" not in text:
            return False
        return any(
            phrase in text
            for phrase in (
                "unsupported parameter",
                "unsupported_parameter",
                "unknown parameter",
                "unrecognized request argument",
                "does not support",
                "is not supported",
                "extra inputs are not permitted",
                "unexpected keyword",
            )
        )


#: Process-wide memory of the models that refused the field.
UNSUPPORTED = UnsupportedEffort()


__all__ = [
    "AUTO",
    "BUDGET_BY_TIER",
    "BUDGET_SHARE",
    "CODEX_EFFORTS",
    "DEFAULT_EFFORT",
    "EFFORTS",
    "MIN_ANTHROPIC_BUDGET",
    "OPENAI_EFFORTS",
    "OPENAI_EFFORT_PREFIXES",
    "UNSUPPORTED",
    "Effort",
    "EffortSource",
    "UnsupportedEffort",
    "anthropic_thinking",
    "codex_effort",
    "gemini_thinking_config",
    "normalize",
    "openai_reasoning_effort",
    "resolve",
    "supports_openai_effort",
    "thinking_budget",
]
