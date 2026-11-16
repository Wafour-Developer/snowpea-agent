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


class Problems(list):  # type: ignore[type-arg]
    """Problem lines, each with a structured twin in :attr:`structured` (1.55.0).

    ``structured`` holds ``{list: "commands" | "paths" | None, entry, reason}``
    — the list the entry belongs to (``None`` for the shape of the section
    itself), the entry as written (``None`` when there is none), and why — so
    a client need not parse the text.
    """

    def __init__(self) -> None:
        super().__init__()
        self.structured: list[dict[str, Any]] = []

    def note(self, text: str, list_name: str | None, entry: Any, reason: str) -> None:
        self.append(text)
        self.structured.append({
            "list": list_name,
            "entry": None if entry is None else (entry if isinstance(entry, str) else repr(entry)),
            "reason": reason,
        })


def _note(problems: list[str] | None, text: str, list_name: str | None, entry: Any,
          reason: str) -> None:
    if problems is None:
        return
    if isinstance(problems, Problems):
        problems.note(text, list_name, entry, reason)
    else:
        problems.append(text)


def _list_of(where: str) -> str:
    return "commands" if "blockedCommands" in where else "paths"


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
    #: The same, structured (:class:`Problems`, 1.55.0).
    problem_items: tuple[dict[str, Any], ...] = ()

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
            problem_items=tuple(
                {**item} for item in _unique_items((*self.problem_items, *other.problem_items))
            ),
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


def _unique_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = (item.get("list"), item.get("entry"), item.get("reason"))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _clean_presets(raw: Any, known: Iterable[str], where: str,
                   problems: list[str] | None) -> list[str]:
    known = tuple(known)
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item in known:
            out.append(item)
        else:
            _note(problems, f"{where}: unknown preset {item!r}", _list_of(where), item,
                  "unknown preset")
    return list(dict.fromkeys(out))


def _clean_patterns(raw: Any, where: str, problems: list[str] | None) -> list[str]:
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        problem = pl.validate(item) if isinstance(item, str) else "not text"
        if problem is None:
            out.append(item.strip())
        else:
            _note(problems, f"{where}: pattern {item!r} skipped ({problem})", "commands", item,
                  problem)
    return list(dict.fromkeys(out))


def _clean_paths(raw: Any, where: str, problems: list[str] | None) -> list[tuple[str, str]]:
    out: dict[str, str] = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"path": item, "level": "write"}
        if not isinstance(item, Mapping):
            _note(problems, f"{where}: entry {item!r} skipped", "paths", item,
                  "not a path entry")
            continue
        path, level = item.get("path"), item.get("level", "write")
        problem = path_problem(path)
        if problem is not None:
            _note(problems, f"{where}: path {path!r} skipped ({problem})", "paths", path, problem)
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
    problems = Problems()
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
        problem_items=tuple(problems.structured),
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
    found = Problems()
    if raw is None:
        return found
    if not isinstance(raw, Mapping):
        found.note("security must be an object {blockedCommands, protectedPaths}", None, None,
                   "not an object")
        return found
    for key in raw:
        if key not in ("blockedCommands", "protectedPaths"):
            found.note(f"security.{key} is not a setting (blockedCommands, protectedPaths)",
                       None, key, "not a setting")
    for name, ids in (("blockedCommands", catalog.COMMAND_PRESET_IDS),
                      ("protectedPaths", catalog.PATH_PRESET_IDS)):
        which = "commands" if name == "blockedCommands" else "paths"
        section = raw.get(name)
        if section is None:
            continue
        if not isinstance(section, Mapping):
            found.note(f"security.{name} must be an object {{presets, custom}}", which, None,
                       "not an object")
            continue
        for key in section:
            if key not in ("presets", "custom"):
                found.note(f"security.{name}.{key} is not a setting (presets, custom)", which,
                           key, "not a setting")
        presets = section.get("presets")
        if presets is not None:
            if not isinstance(presets, list):
                found.note(f"security.{name}.presets must be a list of preset ids", which, None,
                           "presets is not a list")
            else:
                for item in presets:
                    if item not in ids:
                        found.note(f"security.{name}.presets: unknown preset {item!r} "
                                   f"(known: {', '.join(ids)})", which, item, "unknown preset")
        custom = section.get("custom")
        if custom is None:
            continue
        if not isinstance(custom, list):
            found.note(f"security.{name}.custom must be a list", which, None,
                       "custom is not a list")
            continue
        if len(custom) > MAX_ENTRIES:
            found.note(f"security.{name}.custom holds more than {MAX_ENTRIES} entries", which,
                       None, f"more than {MAX_ENTRIES} entries")
        for item in custom:
            if name == "blockedCommands":
                problem = pl.validate(item) if isinstance(item, str) else "a pattern is text"
                if problem is not None:
                    found.note(f"blocked command {item!r}: {problem}", which, item, problem)
                continue
            if isinstance(item, Mapping):
                path = item.get("path")
                extra = set(item) - {"path", "level"}
                if extra:
                    found.note(f"protected path entry has unknown keys: {sorted(extra)}", which,
                               path, f"unknown keys: {', '.join(sorted(map(str, extra)))}")
                level = item.get("level", "write")
                if level not in LEVELS:
                    found.note(f"protected path level must be 'write' or 'all', not {level!r}",
                               which, path, "level must be 'write' or 'all'")
            else:
                path = item if isinstance(item, str) else None
            problem = path_problem(path)
            if problem is not None:
                found.note(f"protected path {path!r}: {problem}", which, path, problem)
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
