"""The security lists as settings: defaults, validation, project additions.

``security.blockedCommands {presets, custom}`` and ``security.protectedPaths
{presets, custom[{path, level}]}`` live in the global settings
(:class:`~snowpea_core.config.settings.SecuritySettings`).  A key that was
never written means the defaults (preset ``essential`` for both lists); an
explicit empty list stays empty.  A project's ``.snowpea/settings.json`` may add
presets and entries of its own (``security`` with the same shape); the
effective lists are the union, so a project can never take anything away.
"""

from __future__ import annotations

import logging
import ntpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from snowpea_core.security import catalog
from snowpea_core.security import patterns as pl

log = logging.getLogger("snowpea.security")

LEVELS: tuple[str, ...] = ("write", "all")
#: Most custom entries one list may hold.
MAX_ENTRIES = pl.MAX_PATTERNS
MAX_PATH_CHARS = 4096


@dataclass(frozen=True)
class Lists:
    """Effective lists: preset ids and custom entries, deduplicated, valid ones only."""

    command_presets: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    path_presets: tuple[str, ...] = ()
    #: ``(path as written, level)``.
    paths: tuple[tuple[str, str], ...] = ()
    #: What was skipped as unusable (a hand-edited file), for ``security.get``.
    problems: tuple[str, ...] = ()

    @property
    def commands_empty(self) -> bool:
        return not self.command_presets and not self.patterns

    @property
    def paths_empty(self) -> bool:
        return not self.path_presets and not self.paths

    @property
    def empty(self) -> bool:
        return self.commands_empty and self.paths_empty

    def merged(self, other: Lists) -> Lists:
        levels: dict[str, str] = {}
        for path, level in (*self.paths, *other.paths):
            # The same path twice: the stricter level wins.
            levels[path] = "all" if "all" in (level, levels.get(path)) else level
        return Lists(
            command_presets=tuple(dict.fromkeys((*self.command_presets, *other.command_presets))),
            patterns=tuple(dict.fromkeys((*self.patterns, *other.patterns))),
            path_presets=tuple(dict.fromkeys((*self.path_presets, *other.path_presets))),
            paths=tuple(levels.items()),
            problems=tuple(dict.fromkeys((*self.problems, *other.problems))),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "blockedCommands": {"presets": list(self.command_presets),
                                "custom": list(self.patterns)},
            "protectedPaths": {"presets": list(self.path_presets),
                               "custom": [{"path": p, "level": lvl} for p, lvl in self.paths]},
        }


def path_problem(raw: Any) -> str | None:
    """Why ``raw`` cannot be a protected path entry, or ``None``."""
    if not isinstance(raw, str) or not raw.strip():
        return "a protected path must be a non-empty path"
    text = raw.strip()
    if len(text) > MAX_PATH_CHARS or "\x00" in text or "\n" in text:
        return "a protected path must be one line of at most 4096 characters"
    absolute = text.startswith(("/", "~/")) or text == "~" or ntpath.isabs(text) and (
        len(text) > 2 and text[1] == ":"
    )
    if not absolute:
        return (f"{text!r} is not an absolute path; write it from the root (/home/me/x) or "
                "from your home (~/x)")
    if text.startswith("~") and not (text == "~" or text.startswith("~/")):
        return "write another user's home as an absolute path, not ~user"
    if text.rstrip("/") == "" or (len(text) <= 3 and text[1:2] == ":"):
        return ("protecting the whole disk would block every change anywhere; the essential "
                "preset already protects / itself")
    return None


def _clean_presets(raw: Any, known: Iterable[str], where: str,
                   problems: list[str] | None) -> list[str]:
    known = tuple(known)
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item in known:
            out.append(item)
        elif problems is not None:
            problems.append(f"{where}: unknown preset {item!r}")
    return list(dict.fromkeys(out))


def _clean_patterns(raw: Any, where: str, problems: list[str] | None) -> list[str]:
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        problem = pl.validate(item) if isinstance(item, str) else "not text"
        if problem is None:
            out.append(item.strip())
        elif problems is not None:
            problems.append(f"{where}: pattern {item!r} skipped ({problem})")
    return list(dict.fromkeys(out))


def _clean_paths(raw: Any, where: str, problems: list[str] | None) -> list[tuple[str, str]]:
    out: dict[str, str] = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"path": item, "level": "write"}
        if not isinstance(item, Mapping):
            if problems is not None:
                problems.append(f"{where}: entry {item!r} skipped")
            continue
        path, level = item.get("path"), item.get("level", "write")
        problem = path_problem(path)
        if problem is not None:
            if problems is not None:
                problems.append(f"{where}: path {path!r} skipped ({problem})")
            continue
        level = level if level in LEVELS else "all"
        text = str(path).strip()
        out[text] = "all" if "all" in (level, out.get(text)) else level
    return list(out.items())


def _section(raw: Any, name: str) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        value = raw.get(name)
        if isinstance(value, Mapping):
            return value
    return {}


def from_raw(raw: Any, *, where: str = "settings", defaults: bool = True) -> Lists:
    """Lists from a ``security`` object; ``defaults``: an absent key means the defaults."""
    problems: list[str] = []
    commands = _section(raw, "blockedCommands")
    paths = _section(raw, "protectedPaths")
    command_presets = commands.get("presets") if "presets" in commands else (
        list(catalog.DEFAULT_COMMAND_PRESETS) if defaults else []
    )
    path_presets = paths.get("presets") if "presets" in paths else (
        list(catalog.DEFAULT_PATH_PRESETS) if defaults else []
    )
    # Security review 5, L2: a preset value that is not a list never means
    # "no protection" — the defaults, and a problem security.get shows.
    if not isinstance(command_presets, list):
        problems.append(f"{where} blockedCommands: presets must be a list; read as the defaults")
        command_presets = list(catalog.DEFAULT_COMMAND_PRESETS) if defaults else []
    if not isinstance(path_presets, list):
        problems.append(f"{where} protectedPaths: presets must be a list; read as the defaults")
        path_presets = list(catalog.DEFAULT_PATH_PRESETS) if defaults else []
    return Lists(
        command_presets=tuple(_clean_presets(command_presets, catalog.COMMAND_PRESET_IDS,
                                             f"{where} blockedCommands", problems)),
        patterns=tuple(_clean_patterns(commands.get("custom"), f"{where} blockedCommands",
                                       problems)),
        path_presets=tuple(_clean_presets(path_presets, catalog.PATH_PRESET_IDS,
                                          f"{where} protectedPaths", problems)),
        paths=tuple(_clean_paths(paths.get("custom"), f"{where} protectedPaths", problems)),
        problems=tuple(problems),
    )


def global_lists(settings: Any) -> Lists:
    """The global lists from a loaded :class:`~snowpea_core.config.settings.Settings`."""
    security = getattr(settings, "security", None)
    if security is None:
        return from_raw({})
    try:
        raw = security.model_dump(mode="json")
    except Exception:  # noqa: BLE001 - an unreadable section fails closed to the defaults
        return from_raw({})
    return from_raw(raw)


def project_additions(workdir: Any) -> Lists:
    """What ``<workdir>/.snowpea/settings.json`` adds (nothing when there is none)."""
    if not workdir:
        return Lists()
    try:
        from snowpea_core.config.project import ProjectSettings

        raw = ProjectSettings.load(str(workdir)).security
    except Exception:  # noqa: BLE001 - a broken project file adds nothing
        return Lists()
    return from_raw(raw, where="project", defaults=False)


def effective(settings: Any, workdir: Any = None) -> Lists:
    """Global lists plus the project's additions."""
    return global_lists(settings).merged(project_additions(workdir))


def problems_in(raw: Any) -> list[str]:
    """Every reason ``raw`` (a ``security`` patch value) would be refused by ``settings.set``."""
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        return ["security must be an object {blockedCommands, protectedPaths}"]
    found: list[str] = []
    for key in raw:
        if key not in ("blockedCommands", "protectedPaths"):
            found.append(f"security.{key} is not a setting (blockedCommands, protectedPaths)")
    for name, ids in (("blockedCommands", catalog.COMMAND_PRESET_IDS),
                      ("protectedPaths", catalog.PATH_PRESET_IDS)):
        section = raw.get(name)
        if section is None:
            continue
        if not isinstance(section, Mapping):
            found.append(f"security.{name} must be an object {{presets, custom}}")
            continue
        for key in section:
            if key not in ("presets", "custom"):
                found.append(f"security.{name}.{key} is not a setting (presets, custom)")
        presets = section.get("presets")
        if presets is not None:
            if not isinstance(presets, list):
                found.append(f"security.{name}.presets must be a list of preset ids")
            else:
                for item in presets:
                    if item not in ids:
                        found.append(f"security.{name}.presets: unknown preset {item!r} "
                                     f"(known: {', '.join(ids)})")
        custom = section.get("custom")
        if custom is None:
            continue
        if not isinstance(custom, list):
            found.append(f"security.{name}.custom must be a list")
            continue
        if len(custom) > MAX_ENTRIES:
            found.append(f"security.{name}.custom holds more than {MAX_ENTRIES} entries")
        for item in custom:
            if name == "blockedCommands":
                problem = pl.validate(item) if isinstance(item, str) else "a pattern is text"
                if problem is not None:
                    found.append(f"blocked command {item!r}: {problem}")
                continue
            if isinstance(item, Mapping):
                extra = set(item) - {"path", "level"}
                if extra:
                    found.append(f"protected path entry has unknown keys: {sorted(extra)}")
                level = item.get("level", "write")
                if level not in LEVELS:
                    found.append(f"protected path level must be 'write' or 'all', not {level!r}")
                path = item.get("path")
            else:
                path = item if isinstance(item, str) else None
            problem = path_problem(path)
            if problem is not None:
                found.append(f"protected path {path!r}: {problem}")
    return found


def removed_by(current: Any, new: Any) -> list[str]:
    """Entries of ``current`` missing from ``new`` (the project scope may only add)."""
    before = from_raw(current, defaults=False)
    after = from_raw(new, defaults=False)
    gone: list[str] = []
    gone.extend(f"preset {p}" for p in before.command_presets if p not in after.command_presets)
    gone.extend(f"pattern {p!r}" for p in before.patterns if p not in after.patterns)
    gone.extend(f"preset {p}" for p in before.path_presets if p not in after.path_presets)
    after_paths = dict(after.paths)
    for path, level in before.paths:
        if path not in after_paths:
            gone.append(f"path {path!r}")
        elif level == "all" and after_paths[path] != "all":
            gone.append(f"path {path!r} level all")
    return gone


__all__ = [
    "LEVELS",
    "Lists",
    "effective",
    "from_raw",
    "global_lists",
    "path_problem",
    "problems_in",
    "project_additions",
    "removed_by",
]
