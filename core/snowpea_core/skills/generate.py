"""``/skill create`` and ``skill.create`` — the SKILL.md generator (M6-M7 skill authoring).

Modelled on ``agent/definition.py``'s ``/agent create``: one non-tool provider
completion produces the document, and the daemon does the deterministic parts
(frontmatter validation, the overwrite check, where the file lands) itself
rather than trusting a single model reply to get a filesystem operation
right.

Unlike ``/agent create``'s JSON payload, the generator's reply *is* the
``SKILL.md`` file verbatim — frontmatter and body — because the document is
prose, not a handful of scalar fields; a JSON envelope around a multi-section
markdown body would only add a round of escaping to get wrong.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from snowpea_core.agent.definition import complete_text
from snowpea_core.prompts.loader import load
from snowpea_core.providers.base import ChatMessage
from snowpea_core.skills.publish import SkillPackage, validate_frontmatter

GENERATOR_SYSTEM = load("workflows/skill-generate")

#: A whole-document code fence the model wrapped its reply in anyway.
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", re.S)

#: The frontmatter block's own ``---`` fences (not the outer code fence).
_FRONTMATTER_RE = re.compile(r"^(---[ \t]*\n)(.*?\n)(---[ \t]*)(\n|$)", re.S)
_NAME_LINE_RE = re.compile(r"(?m)^name:.*$")


class SkillCreateError(ValueError):
    """The target already has a ``SKILL.md`` and ``force`` was not given."""


def generator_messages(name_hint: str, description: str) -> list[ChatMessage]:
    """The two messages :func:`generate_skill_document` sends."""
    hint = f'\n\nThe skill must be named "{name_hint}".' if name_hint else ""
    return [
        ChatMessage(role="system", content=GENERATOR_SYSTEM),
        ChatMessage(
            role="user",
            content=(
                "Write a SKILL.md for this brief. Reply with the file content only."
                f"{hint}\n\nBrief: {description}"
            ),
        ),
    ]


def extract_skill_document(text: str) -> str:
    """Strip a wrapping code fence, when the model added one anyway."""
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    return match.group(1).strip() if match else stripped


async def generate_skill_document(provider: Any, name_hint: str, description: str) -> str:
    """Ask ``provider`` for a complete ``SKILL.md`` document.

    Raises nothing itself; the caller validates the reply with
    :func:`force_frontmatter_name` and
    :func:`snowpea_core.skills.publish.validate_frontmatter`, which is also
    what ``/skill publish`` uses, so the two paths reject the same things.
    """
    text = await complete_text(provider, generator_messages(name_hint, description))
    return extract_skill_document(text)


def force_frontmatter_name(text: str, name: str) -> str:
    """Overwrite (or insert) the frontmatter's ``name:`` line with ``name``.

    The requested name is what the file lives under and what ``/<name>``
    will resolve to (:func:`snowpea_core.skills.skill_md.parse_skill_md`
    prefers the frontmatter's own ``name`` over the directory it was found
    in), so the model's choice of slug is not trusted even when it is a
    reasonable one.
    """
    match = _FRONTMATTER_RE.match(text.lstrip("﻿\n"))
    if not match:
        return text
    block = match.group(2)
    if _NAME_LINE_RE.search(block):
        new_block = _NAME_LINE_RE.sub(f"name: {name}", block, count=1)
    else:
        new_block = f"name: {name}\n{block}"
    return text[: match.start(2)] + new_block + text[match.end(2) :]


def skill_root(name: str, *, workdir: Path | str, home: Path | str, global_: bool) -> Path:
    """``<home>/skills/<name>`` for ``--global``, else ``<workdir>/.snowpea/skills/<name>``."""
    base = Path(home) / "skills" if global_ else Path(workdir) / ".snowpea" / "skills"
    return base / name


def write_skill_document(
    directory: Path, text: str, *, requested_name: str, force: bool = False
) -> SkillPackage:
    """Validate ``text`` as a SKILL.md and write it to ``directory/SKILL.md``.

    Raises :class:`~snowpea_core.skills.publish.PublishError` for bad
    frontmatter (the same check ``/skill publish`` runs) and
    :class:`SkillCreateError` when the target already exists and ``force``
    is false.
    """
    path = directory / "SKILL.md"
    if path.is_file() and not force:
        raise SkillCreateError(f"{path} already exists; pass --force to overwrite it")
    document = force_frontmatter_name(text, requested_name)
    package = validate_frontmatter(document, source="generated SKILL.md")
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(document.strip() + "\n", encoding="utf-8")
    package.root = directory
    return package


__all__ = [
    "GENERATOR_SYSTEM",
    "SkillCreateError",
    "extract_skill_document",
    "force_frontmatter_name",
    "generate_skill_document",
    "generator_messages",
    "skill_root",
    "write_skill_document",
]
