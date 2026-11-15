"""house-style: a system-prompt section.

The section sits at order 650, in the stable band (below 1000): between the
persona (600) and the reply-language rule (700). Its text depends only on the
mode, so it does not change from one model call to the next and the provider's
prompt cache stays warm.
"""

from __future__ import annotations

from snowpea_core.extensions import PluginContext, PromptInputs, PromptSection

RULES = """House style for this team:
- Commit messages: imperative mood, at most 72 characters in the subject.
- Never add a dependency without saying why in the same answer."""

PLAN_NOTE = "In plan mode, list the commits you would make, one line each."


def render(inputs: PromptInputs) -> str:
    """Runs for every model call; return "" to leave the section out."""
    if inputs.subagent:
        return ""  # delegated agents get a brief, not the house rules
    if inputs.mode == "plan":
        return f"{RULES}\n{PLAN_NOTE}"
    return RULES


def apply(ctx: PluginContext) -> None:
    ctx.prompt.section(PromptSection(name="house-style:rules", order=650, render=render))
