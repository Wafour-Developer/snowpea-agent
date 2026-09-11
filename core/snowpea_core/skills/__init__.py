"""snowpea_core.skills — the Claude Code compatible plugin/skill layer (M6).

``core.skills`` is a :class:`~snowpea_core.skills.loader.SkillLoader`.  It owns
the search roots, turns skills and command files into slash commands, keeps the
plugin hook table, and merges plugin MCP servers into the tool registry.
"""

from __future__ import annotations

from snowpea_core.skills.hooks import Hook, HookOutcome, HookRegistry
from snowpea_core.skills.loader import (
    LoadedAgent,
    LoadedPlugin,
    LoadedSkill,
    ReloadReport,
    SkillLoader,
)
from snowpea_core.skills.marketplace import InstallError, SkillHit
from snowpea_core.skills.skill_md import SkillDoc, parse_skill_md, split_frontmatter

__all__ = [
    "Hook",
    "HookOutcome",
    "HookRegistry",
    "InstallError",
    "LoadedAgent",
    "LoadedPlugin",
    "LoadedSkill",
    "ReloadReport",
    "SkillDoc",
    "SkillHit",
    "SkillLoader",
    "parse_skill_md",
    "split_frontmatter",
]
