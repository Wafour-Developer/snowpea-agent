"""Reads prompt markdown out of this package, with a project override.

Every prompt snowpea sends lives in a ``.md`` file next to this module rather
than in a Python string, so a prompt change shows up as a legible diff in
review.  A project may shadow any of them by dropping a file with the same
relative name into ``<workdir>/.snowpea/prompts/``.
"""

from __future__ import annotations

import string
from functools import lru_cache
from pathlib import Path

#: Where the shipped prompts live.
PACKAGE_DIR = Path(__file__).resolve().parent

#: Relative to a project's working directory: where an override may be found.
PROJECT_SUBDIR = Path(".snowpea") / "prompts"

_project_root: Path | None = None


class PromptNotFound(LookupError):
    """No prompt file matched the requested name."""


def set_project_root(root: Path | str | None) -> None:
    """Point the override search at ``root``/.snowpea/prompts (or nowhere)."""
    global _project_root
    _project_root = Path(root).resolve() if root is not None else None
    load.cache_clear()


def project_root() -> Path | None:
    return _project_root


def _relative(name: str) -> Path:
    """``"modes/plan"`` -> ``Path("modes/plan.md")``; rejects escapes."""
    cleaned = name.strip().strip("/")
    if not cleaned:
        raise PromptNotFound("empty prompt name")
    candidate = Path(cleaned)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PromptNotFound(f"illegal prompt name: {name}")
    if candidate.suffix != ".md":
        candidate = candidate.with_suffix(".md")
    return candidate


def search_path(root: Path | None = None) -> tuple[Path, ...]:
    """Directories consulted for a prompt, most specific first."""
    override = root if root is not None else _project_root
    if override is None:
        return (PACKAGE_DIR,)
    return (override / PROJECT_SUBDIR, PACKAGE_DIR)


@lru_cache(maxsize=256)
def load(name: str, root: Path | None = None) -> str:
    """The text of prompt ``name``, project override first.

    ``name`` is the path under ``prompts/`` without the extension, e.g.
    ``"base"``, ``"modes/plan"``, ``"roles/executor"``.  The result is stripped
    of surrounding blank lines and cached; call :func:`clear_cache` after
    editing a prompt on disk.
    """
    relative = _relative(name)
    for directory in search_path(root):
        path = directory / relative
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    raise PromptNotFound(f"no prompt named {name!r}")


def render(name: str, root: Path | None = None, **variables: object) -> str:
    """:func:`load` with ``$VAR`` / ``${VAR}`` substitution and nothing else.

    Unknown placeholders are left as they are, so a prompt may talk about
    ``$SNOWPEA_HOME`` without the loader eating it.
    """
    text = load(name, root)
    return string.Template(text).safe_substitute(
        {key: "" if value is None else str(value) for key, value in variables.items()}
    ).strip()


def clear_cache() -> None:
    """Forget every cached prompt (tests, and prompts edited at runtime)."""
    load.cache_clear()


__all__ = [
    "PACKAGE_DIR",
    "PROJECT_SUBDIR",
    "PromptNotFound",
    "clear_cache",
    "load",
    "project_root",
    "render",
    "search_path",
    "set_project_root",
]
