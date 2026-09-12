"""The prompt library: every string snowpea sends to a model.

Prompts are markdown files in this package, loaded through :mod:`loader` and
assembled into three cache-friendly tiers by :mod:`compose`.  A project may
shadow any file by name from ``<workdir>/.snowpea/prompts/``.
"""

from __future__ import annotations

from snowpea_core.prompts.compose import (
    CONTEXT_PRESSURE_THRESHOLD,
    VENDOR_CLASSES,
    PromptTiers,
    build_system_prompt,
    build_tiers,
    reply_language_rule,
    tool_lines,
    vendor_class_for,
    workflow_brief,
)
from snowpea_core.prompts.loader import (
    PromptNotFound,
    clear_cache,
    load,
    render,
    set_project_root,
)

__all__ = [
    "CONTEXT_PRESSURE_THRESHOLD",
    "VENDOR_CLASSES",
    "PromptNotFound",
    "PromptTiers",
    "build_system_prompt",
    "build_tiers",
    "clear_cache",
    "load",
    "render",
    "reply_language_rule",
    "set_project_root",
    "tool_lines",
    "vendor_class_for",
    "workflow_brief",
]
