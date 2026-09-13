"""``snowpea skill publish`` — package a skill directory and ship it to the
hosted registry (M6-M7 contract §1, v0.3).

Zipping and frontmatter validation are kept separate from the network call
(:class:`snowpea_core.skills.registry_client.HttpRegistryClient`) so the CLI
can print a precise "fix this" message before it ever opens a socket.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from snowpea_core.skills.skill_md import split_frontmatter

#: The registry's own validation (docs/design/registry-contract.md §3); checked
#: client-side first so a typo is a local error, not a round trip.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
DESCRIPTION_MIN = 8
DESCRIPTION_MAX = 500

#: Directories/files never worth shipping in a skill package.
JUNK_NAMES = {
    ".git",
    ".DS_Store",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
JUNK_SUFFIXES = (".pyc", ".pyo", ".swp", "~")


class PublishError(ValueError):
    """The skill directory or its SKILL.md failed local validation."""


@dataclass
class SkillPackage:
    """A validated skill, ready to zip."""

    name: str
    description: str
    version: str | None
    root: Path


def _is_junk(name: str) -> bool:
    if name in JUNK_NAMES:
        return True
    return any(name.endswith(suffix) for suffix in JUNK_SUFFIXES)


def validate_frontmatter(skill_md_text: str, *, source: str = "SKILL.md") -> SkillPackage:
    """Raise :class:`PublishError` for anything the registry would also reject."""
    frontmatter, _body = split_frontmatter(skill_md_text)
    if not frontmatter:
        raise PublishError(f"{source} has no --- frontmatter block")
    name = str(frontmatter.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise PublishError(
            f"{source}: name {name!r} must match {NAME_RE.pattern} "
            "(lowercase letters, digits, '.', '_', '-', 2-64 chars)"
        )
    description = str(frontmatter.get("description") or "").strip()
    if not (DESCRIPTION_MIN <= len(description) <= DESCRIPTION_MAX):
        raise PublishError(
            f"{source}: description must be {DESCRIPTION_MIN}-{DESCRIPTION_MAX} characters "
            f"(got {len(description)})"
        )
    version = str(frontmatter.get("version") or "").strip() or None
    return SkillPackage(name=name, description=description, version=version, root=Path("."))


def load_skill_dir(directory: Path | str) -> SkillPackage:
    """Read and validate ``<directory>/SKILL.md``; raise :class:`PublishError` otherwise."""
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise PublishError(f"{root} is not a directory")
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        raise PublishError(f"{root} has no SKILL.md at its root")
    package = validate_frontmatter(
        skill_md.read_text(encoding="utf-8"), source=str(skill_md)
    )
    package.root = root
    return package


def build_zip(package: SkillPackage) -> bytes:
    """Zip ``package.root``'s files under ``<name>/...``, junk excluded."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.root.rglob("*")):
            if path.is_dir():
                continue
            if any(_is_junk(part) for part in path.relative_to(package.root).parts):
                continue
            arcname = f"{package.name}/{path.relative_to(package.root).as_posix()}"
            archive.write(path, arcname)
    return buffer.getvalue()


__all__ = [
    "DESCRIPTION_MAX",
    "DESCRIPTION_MIN",
    "JUNK_NAMES",
    "JUNK_SUFFIXES",
    "NAME_RE",
    "PublishError",
    "SkillPackage",
    "build_zip",
    "load_skill_dir",
    "validate_frontmatter",
]
