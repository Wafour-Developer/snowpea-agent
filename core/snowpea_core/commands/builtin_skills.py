"""Fallback registration for the bundled markdown skills (M7 contract §4).

Three of the ported OMC commands are prompts, not control flow, so they ship as
``core/snowpea_core/builtin_skills/<name>/SKILL.md`` and are meant to be read by
the same loader that reads a user's own skills (M6 contract §1) — that way the
bundled three exercise the user-skill path from day one.

This module is the safety net, not the main road.  It registers the same three
names directly off disk so ``/help`` lists all nine commands and
``/deep-research`` works even on a daemon whose skill loader is missing or
disabled (``SNOWPEA_DISABLE_SKILLS``).  When the loader is present it registers
the same names afterwards and wins, because registration is last-write-wins by
name.

The parser here is deliberately the one from :mod:`snowpea_core.agent.definition`
rather than the loader's: it handles the small YAML subset these files use and
has no dependency on the M6 package.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snowpea_core.agent.definition import split_frontmatter
from snowpea_core.commands.registry import Command, CommandContext

log = logging.getLogger("snowpea.commands.builtin_skills")

#: ``core/snowpea_core/builtin_skills``.
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent / "builtin_skills"

#: The placeholder a skill body uses for whatever followed the command.
ARGUMENTS = "$ARGUMENTS"

SOURCE = "builtin"


@dataclass
class BundledSkill:
    """One ``SKILL.md`` read off disk."""

    name: str
    description: str
    argument_hint: str
    allowed_tools: list[str]
    body: str

    def render(self, args: str) -> str:
        """The body with ``$ARGUMENTS`` filled in (appended when absent)."""
        text = self.body
        if ARGUMENTS in text:
            return text.replace(ARGUMENTS, args.strip())
        if args.strip():
            return f"{text}\n\n{args.strip()}"
        return text

    def instruction(self, args: str) -> str:
        header = f"Follow these instructions for /{self.name}"
        if self.description:
            header = f"{header} — {self.description}"
        return f"{header}:\n\n{self.render(args)}".strip()


def _tool_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def parse_bundled(path: Path) -> BundledSkill | None:
    """Read one ``SKILL.md``; ``None`` when it is unreadable or unnamed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - unreadable file on disk
        log.debug("could not read bundled skill %s", path, exc_info=True)
        return None
    meta, body = split_frontmatter(text)
    name = str(meta.get("name") or path.parent.name).strip()
    if not name:
        return None
    return BundledSkill(
        name=name,
        description=str(meta.get("description") or "").strip(),
        argument_hint=str(meta.get("argument-hint") or "").strip(),
        allowed_tools=_tool_list(meta.get("allowed-tools")),
        body=body.strip(),
    )


def discover(directory: Path | None = None) -> list[BundledSkill]:
    """Every ``<name>/SKILL.md`` under the bundled directory, by name."""
    root = directory or BUILTIN_SKILLS_DIR
    if not root.is_dir():
        return []
    found: list[BundledSkill] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.is_file():
            skill = parse_bundled(skill_md)
            if skill is not None:
                found.append(skill)
    return found


def command_for(skill: BundledSkill) -> Command:
    """Turn a bundled skill into the slash command that injects it."""
    summary = skill.description or f"Run the bundled {skill.name} skill."
    if skill.argument_hint:
        summary = f"{summary} {skill.argument_hint}".strip()

    async def run(ctx: CommandContext, args: str) -> None:
        from snowpea_core.agent import loop as agent_loop

        session = ctx.session
        previous = getattr(session, "allowed_tools", None)
        if skill.allowed_tools:
            session.allowed_tools = set(skill.allowed_tools)
        ctx.handled_turn = True
        try:
            await agent_loop.run_turn(
                ctx.core, session, skill.instruction(args), turn_id=ctx.turn_id
            )
        finally:
            session.allowed_tools = previous

    return Command(
        name=skill.name,
        summary=summary,
        run=run,
        args_schema={
            "type": "object",
            "properties": {"arguments": {"type": "string", "description": skill.argument_hint}},
        },
        source=SOURCE,
    )


def commands(directory: Path | None = None) -> tuple[Command, ...]:
    """The bundled skills as commands, ready for the registry."""
    return tuple(command_for(skill) for skill in discover(directory))


__all__ = [
    "ARGUMENTS",
    "BUILTIN_SKILLS_DIR",
    "SOURCE",
    "BundledSkill",
    "command_for",
    "commands",
    "discover",
    "parse_bundled",
]
