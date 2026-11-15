"""Projects as Jarvis and the person call them, and which ones Jarvis may see.

A project is a conversation folder: a workspace row (``session/workspaces.py``)
or the workdir of a live top-level session, keyed by its canonical path.  Its
display name is what the person settled on with ``jarvis.project.rename``,
else the workspace title, else the folder name; aliases are other names the
person uses for it.  Names and aliases live in ``$SNOWPEA_HOME/jarvis/projects.json``,
outside every project, so a session working in a project cannot rename it.

Exposure is the project's own ``jarvis.expose`` (``<root>/.snowpea/settings.json``,
default true), read through a cache keyed by the file's stamp because the
intake asks for every event it sees.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.config.project import ProjectSettings
from snowpea_core.server.protocol import JarvisProject
from snowpea_core.session.workspaces import canonical

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.jarvis.projects")

NAME_MAX = 80
ALIAS_MAX = 64
ALIASES_MAX = 20


class ProjectError(ValueError):
    """A rename or alias change that cannot be applied."""


def _clean(text: str, limit: int, what: str) -> str:
    value = " ".join(str(text or "").split())
    if len(value) > limit:
        raise ProjectError(f"{what} is longer than {limit} characters")
    return value


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


class ProjectBook:
    """Names, aliases and exposure of the projects Jarvis oversees."""

    def __init__(self, core: Core, path: Path) -> None:
        self.core = core
        self.path = path
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._titles: dict[str, str] = {}
        self._exposure: dict[str, tuple[tuple[int, int] | None, bool]] = {}
        self._load()

    # -- storage -------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("ignoring unreadable %s", self.path, exc_info=True)
            return
        projects = raw.get("projects") if isinstance(raw, dict) else None
        if not isinstance(projects, dict):
            return
        for root, entry in projects.items():
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            aliases = entry.get("aliases")
            self._entries[str(root)] = {
                "name": str(name) if isinstance(name, str) and name.strip() else None,
                "aliases": [str(a) for a in aliases if isinstance(a, str) and a.strip()]
                if isinstance(aliases, list)
                else [],
            }

    def _save(self) -> None:
        data = {
            "projects": {
                root: entry
                for root, entry in sorted(self._entries.items())
                if entry.get("name") or entry.get("aliases")
            }
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # -- exposure ------------------------------------------------------
    def exposed(self, root: str | None) -> bool:
        """Whether Jarvis may see the project at ``root`` (security review M2).

        Hidden when its own ``jarvis.expose`` is false or its settings file
        exists but is unreadable, and also when any registered project above it
        is hidden: a subfolder never re-exposes what its parent hides.  (A
        folder too wide to stand for its contents — home, ``/`` — hides only
        itself.)
        """
        if not root:
            return True
        if not self._own_exposed(root):
            return False
        try:
            target = Path(root)
        except (TypeError, ValueError):
            return False
        from snowpea_core.external.proc import _wide_root

        for other in self.known_roots():
            base = Path(other)
            if base == target or base not in target.parents:
                continue
            with contextlib.suppress(OSError, RuntimeError):
                if _wide_root(base.resolve()):
                    continue
            if not self._own_exposed(other):
                return False
        return True

    def _own_exposed(self, root: str) -> bool:
        settings_path = ProjectSettings.path_for(root)
        stamp = _stamp(settings_path)
        cached = self._exposure.get(root)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        value = True if stamp is None else ProjectSettings.exposed(root)
        self._exposure[root] = (stamp, value)
        return value

    # -- roots ---------------------------------------------------------
    def _excluded(self) -> set[str]:
        """Folders that are not projects: Jarvis's own folder and the home."""
        out: set[str] = set()
        with contextlib.suppress(Exception):
            out.add(canonical(os.path.expanduser(self.core.settings.jarvis.folder)))
        with contextlib.suppress(Exception):
            out.add(canonical(self.core.paths.home))
        return out

    async def roots(self) -> list[str]:
        """Every project root snowpea knows, in workspace order, then the rest by name."""
        ordered: list[str] = []
        store = getattr(self.core, "store", None)
        if store is not None:
            from snowpea_core.session import workspaces

            try:
                for row in await workspaces.list_all(store):
                    path = str(row["path"])
                    ordered.append(path)
                    if row.get("title"):
                        self._titles[path] = str(row["title"])
            except Exception:  # noqa: BLE001 - a listing never fails over the sidebar table
                log.debug("could not read the workspaces", exc_info=True)
        extra: set[str] = set(self._entries)
        for session in self.core.sessions.all():
            if getattr(session, "parent_session_id", None):
                continue
            extra.add(canonical(session.workdir))
        excluded = self._excluded()
        seen: set[str] = set()
        out: list[str] = []
        for root in [*ordered, *sorted(extra)]:
            if root in seen or root in excluded:
                continue
            seen.add(root)
            out.append(root)
        self._known = list(out)
        return out

    def known_roots(self) -> list[str]:
        """The project roots as of the last :meth:`roots`, plus what is live now (sync).

        For the Jarvis session's read limits, which run inside the agent loop.
        """
        found: set[str] = set(getattr(self, "_known", []) or [])
        found.update(self._entries)
        for session in self.core.sessions.all():
            if getattr(session, "parent_session_id", None):
                continue
            with contextlib.suppress(Exception):
                found.add(canonical(session.workdir))
        return sorted(found - self._excluded())

    # -- names ---------------------------------------------------------
    def name_of(self, root: str) -> str:
        entry = self._entries.get(root) or {}
        if entry.get("name"):
            return str(entry["name"])
        if self._titles.get(root):
            return self._titles[root]
        return Path(root).name or root

    def project(self, root: str) -> JarvisProject:
        entry = self._entries.get(root) or {}
        return JarvisProject(
            root=root,
            name=self.name_of(root),
            aliases=list(entry.get("aliases") or []),
            exposed=self.exposed(root),
            named=bool(entry.get("name")),
        )

    async def list(self) -> list[JarvisProject]:
        return [self.project(root) for root in await self.roots()]

    async def hidden(self) -> list[str]:
        return [root for root in await self.roots() if not self.exposed(root)]

    def resolve(self, text: str) -> str | None:
        """The root a name, alias, folder name or path refers to; ``None`` if none or ambiguous."""
        wanted = " ".join(str(text or "").split()).casefold()
        if not wanted:
            return None
        if os.path.isabs(str(text).strip()):
            return canonical(str(text).strip())
        candidates = set(self._entries) | set(self._titles)
        hits = {
            root
            for root in candidates
            if wanted == self.name_of(root).casefold()
            or wanted in {a.casefold() for a in (self._entries.get(root) or {}).get("aliases", [])}
        }
        if not hits:
            hits = {root for root in candidates if Path(root).name.casefold() == wanted}
        return next(iter(hits)) if len(hits) == 1 else None

    async def _known_root(self, root: str) -> str:
        if not root or not os.path.isabs(root):
            raise ProjectError("root must be an absolute project folder")
        key = canonical(root)
        if key in self._excluded():
            raise ProjectError(f"{key} is not a project folder")
        if key in await self.roots() or Path(key).is_dir():
            return key
        raise ProjectError(f"no project at {key}")

    def _taken(self, label: str, root: str) -> str | None:
        """The other project already called ``label``, if any."""
        folded = label.casefold()
        for other in set(self._entries) | set(self._titles):
            if other == root:
                continue
            names = {self.name_of(other).casefold()}
            names |= {a.casefold() for a in (self._entries.get(other) or {}).get("aliases", [])}
            if folded in names:
                return other
        return None

    async def rename(self, root: str, name: str | None) -> JarvisProject:
        key = await self._known_root(root)
        clean = _clean(name or "", NAME_MAX, "name")
        if clean:
            other = self._taken(clean, key)
            if other is not None:
                raise ProjectError(f"{clean!r} already names {other}")
        with self._lock:
            entry = self._entries.setdefault(key, {"name": None, "aliases": []})
            entry["name"] = clean or None
            self._save()
        return self.project(key)

    async def alias(self, root: str, add: list[str], remove: list[str]) -> JarvisProject:
        key = await self._known_root(root)
        adding = [_clean(item, ALIAS_MAX, "alias") for item in add]
        removing = {_clean(item, ALIAS_MAX, "alias").casefold() for item in remove}
        with self._lock:
            entry = self._entries.setdefault(key, {"name": None, "aliases": []})
            aliases = [a for a in entry["aliases"] if a.casefold() not in removing]
            for alias in adding:
                if not alias or alias.casefold() in {a.casefold() for a in aliases}:
                    continue
                other = self._taken(alias, key)
                if other is not None:
                    raise ProjectError(f"{alias!r} already names {other}")
                aliases.append(alias)
            if len(aliases) > ALIASES_MAX:
                raise ProjectError(f"a project keeps at most {ALIASES_MAX} aliases")
            entry["aliases"] = aliases
            self._save()
        return self.project(key)


__all__ = ["ProjectBook", "ProjectError"]
