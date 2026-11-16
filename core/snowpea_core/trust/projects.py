"""Project trust: one record of what each project folder may make snowpea do.

Security review 5 (C1, H1, H2, M2, L4).  A project folder — a cloned
repository, or one a worker wrote into — can carry files that make snowpea run
code or widen what runs unasked:

``hooks``
    ``.snowpea/hooks.json``, ``.snowpea/hooks/hooks.json``, ``.claude/hooks.json``,
    ``.claude/hooks/hooks.json``: shell commands run around every tool call.
``mcp``
    ``.mcp.json``: MCP servers started for the project's sessions.
``commands``
    Project commands and skills whose front matter carries ``allowed-tools``
    (``.snowpea|.claude/commands/*.md``, ``.snowpea|.claude|.agents/skills/*/SKILL.md``):
    they pre-approve tools for their turn.
``agents``
    Project agent definitions (``.snowpea|.claude/agents/*.md``) with a ``permission``.
``prompts``
    ``.snowpea/prompts/**``: overrides of snowpea's own prompts.
``settings``
    The WIDENING parts of ``.snowpea/settings.json``: allowlist rules, model
    profiles and routing, providers, MCP servers, hooks.  (What only narrows —
    a stricter ``defaultMode``, ``jarvis.expose: false``, security-list
    additions — always applies.)
``mode``
    ``defaultMode: auto`` from the project file: a raise, its own scope.
``externalCli``
    External CLIs (Claude Code, Codex, Cursor, agy) may start in the project
    and load its own configuration (security review M4; no file pinned).

Each project root (its real path) has one record in
``$SNOWPEA_HOME/trust/projects.json`` (0600; inside ``$SNOWPEA_HOME``, so a
tool's write to it is ``config`` and a shell write ``never`` delegable)::

    {"version": 1, "migrated": "<when>", "projects": {"<root>": {
        "scopes": ["hooks", …], "files": {"<rel>": "sha256:<hex>", …},
        "by": "ide", "at": "<utc>", "trusted": true}}}

Trust pins content: :meth:`TrustStore.set` records the digest of every
control file at that moment, and a file is active only while its digest is the
pinned one — a file changed since, or a new one, waits for the person again.
Only a local person trusts (``project.trust.set`` / ``snowpea project trust``).
``trusted: true`` is the external-CLI flag of the record this extends
(``jarvis/external-trust.json``, folded in on first read).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snowpea_core.config.paths import utc_now

log = logging.getLogger("snowpea.trust")

SCOPES: tuple[str, ...] = ("hooks", "mcp", "commands", "agents", "prompts", "settings", "mode",
                           "externalCli")
#: Scopes whose files are pinned (``mode`` pins ``.snowpea/settings.json`` too).
FILE_SCOPES: frozenset[str] = frozenset({"hooks", "mcp", "commands", "agents", "prompts",
                                         "settings", "mode"})
#: What the migration (and ``snowpea project trust`` without ``--scope``) grants:
#: everything but the two the person names explicitly.
DEFAULT_SCOPES: tuple[str, ...] = ("hooks", "mcp", "commands", "agents", "prompts", "settings")

STORE_DIR = "trust"
STORE_NAME = "projects.json"
LEGACY_PATH = ("jarvis", "external-trust.json")

HOOK_FILES: tuple[str, ...] = (".snowpea/hooks.json", ".snowpea/hooks/hooks.json",
                               ".claude/hooks.json", ".claude/hooks/hooks.json")
MCP_FILE = ".mcp.json"
SETTINGS_FILE = ".snowpea/settings.json"
#: Keys of a project settings file that widen what runs unasked (H2).
WIDENING_KEYS: tuple[str, ...] = ("allowlist", "models", "providers", "mcp", "hooks")

#: Largest control file hashed whole (a bigger one counts as changed every time).
MAX_FILE_BYTES = 4 * 1024 * 1024
#: Most control files of one kind listed (a repository with thousands is odd).
MAX_FILES = 400
SUMMARY_CHARS = 200


def project_root(workdir: str | os.PathLike[str]) -> Path:
    """The key a project is trusted under: its real path."""
    return Path(os.path.realpath(os.path.expanduser(os.fspath(workdir))))


# ---------------------------------------------------------------------------
# what a project brings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlFile:
    """One file of a project that makes snowpea do something."""

    rel: str
    scope: str
    digest: str
    #: Shown to the person deciding (hook commands' first 200 characters, …).
    summary: str = ""


def digest_of(path: Path) -> str:
    """``sha256:<hex>`` of the file, ``missing``, or ``unreadable``."""
    try:
        info = path.stat()
    except OSError:
        return "missing"
    key = (str(path), info.st_mtime_ns, info.st_size, info.st_ino)
    with _DIGEST_LOCK:
        cached = _DIGESTS.get(str(path))
        if cached is not None and cached[0] == key:
            return cached[1]
    if info.st_size > MAX_FILE_BYTES:
        return "unreadable"
    try:
        data = path.read_bytes()
    except OSError:
        return "unreadable"
    value = "sha256:" + hashlib.sha256(data).hexdigest()
    with _DIGEST_LOCK:
        _DIGESTS[str(path)] = (key, value)
        if len(_DIGESTS) > 4096:
            _DIGESTS.clear()
    return value


_DIGESTS: dict[str, tuple[tuple[Any, ...], str]] = {}
_DIGEST_LOCK = threading.Lock()


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1] + "…"


def _hook_summary(path: Path) -> str:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return "unreadable hooks file"
    table = raw.get("hooks") if isinstance(raw, dict) else None
    commands: list[str] = []
    if isinstance(table, dict):
        for event, entries in table.items():
            for entry in entries if isinstance(entries, list) else []:
                specs = entry.get("hooks") if isinstance(entry, dict) else None
                for spec in specs if isinstance(specs, list) else []:
                    if isinstance(spec, dict) and spec.get("command"):
                        commands.append(f"{event}: {_clip(str(spec['command']))}")
    return " | ".join(commands)[:1000] if commands else "no hook commands"


def _mcp_summary(path: Path) -> str:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return "unreadable .mcp.json"
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    rows: list[str] = []
    if isinstance(servers, dict):
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            what = entry.get("url") or " ".join(
                [str(entry.get("command") or ""), *[str(a) for a in entry.get("args") or []]])
            rows.append(f"{name}: {_clip(str(what))}")
    return " | ".join(rows)[:1000] if rows else "no servers"


def settings_widening(raw: Any) -> list[str]:
    """The widening parts a project settings document holds (``allowlist (2)``, …)."""
    if not isinstance(raw, dict):
        return []
    found: list[str] = []
    for key in WIDENING_KEYS:
        value = raw.get(key)
        if value in (None, {}, [], ""):
            continue
        if key == "models" and isinstance(value, dict) and not any(
            value.get(k) for k in ("default", "profiles", "agents")
        ):
            continue
        found.append(f"{key} ({len(value)})" if isinstance(value, (list, dict)) else key)
    if raw.get("defaultMode") == "auto":
        found.append("defaultMode: auto")
    return found


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def _front_matter(path: Path) -> dict[str, Any]:
    from snowpea_core.skills.skill_md import load_skill_md

    doc = load_skill_md(path, default_name=path.stem)
    if doc is None:
        return {}
    meta = dict(doc.frontmatter)
    if doc.allowed_tools:
        meta["allowed-tools"] = doc.allowed_tools
    return meta


def _glob(root: Path, pattern: str) -> list[Path]:
    try:
        found = sorted(root.glob(pattern))
    except OSError:
        return []
    return [p for p in found if p.is_file()][:MAX_FILES]


def discover(root: str | os.PathLike[str]) -> list[ControlFile]:
    """Every control file the project at ``root`` holds now, with its scope."""
    base = project_root(root)
    out: list[ControlFile] = []

    def add(path: Path, scope: str, summary: str) -> None:
        rel = path.relative_to(base).as_posix()
        out.append(ControlFile(rel, scope, digest_of(path), summary))

    for rel in HOOK_FILES:
        path = base / rel
        if path.is_file():
            add(path, "hooks", _hook_summary(path))
    if (base / MCP_FILE).is_file():
        add(base / MCP_FILE, "mcp", _mcp_summary(base / MCP_FILE))
    for pattern in (".snowpea/commands/*.md", ".claude/commands/*.md",
                    ".snowpea/skills/*/SKILL.md", ".claude/skills/*/SKILL.md",
                    ".agents/skills/*/SKILL.md"):
        for path in _glob(base, pattern):
            meta = _front_matter(path)
            tools = meta.get("allowed-tools") or meta.get("allowed_tools")
            if tools:
                shown = ", ".join(tools) if isinstance(tools, list) else str(tools)
                add(path, "commands", _clip(f"allowed-tools: {shown}"))
    for pattern in (".snowpea/agents/*.md", ".claude/agents/*.md"):
        for path in _glob(base, pattern):
            permission = _front_matter(path).get("permission")
            if permission not in (None, "", "inherit"):
                add(path, "agents", _clip(f"permission: {permission}"))
    for path in _glob(base, ".snowpea/prompts/**/*.md"):
        add(path, "prompts", "prompt override")
    settings = base / SETTINGS_FILE
    if settings.is_file():
        widening = settings_widening(_read_json(settings))
        if widening:
            add(settings, "settings", _clip(", ".join(widening)))
    return out


def scope_of(rel: str) -> str | None:
    """The scope a project-relative path belongs to (``None``: not a control file)."""
    rel = rel.replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    if rel in HOOK_FILES:
        return "hooks"
    if rel == MCP_FILE:
        return "mcp"
    if rel == SETTINGS_FILE:
        return "settings"
    parts = rel.split("/")
    if len(parts) >= 3 and parts[0] in (".snowpea", ".claude") and parts[1] == "commands":
        return "commands"
    if len(parts) >= 3 and parts[0] in (".snowpea", ".claude", ".agents") and parts[1] == "skills":
        return "commands"
    if len(parts) >= 3 and parts[0] in (".snowpea", ".claude") and parts[1] == "agents":
        return "agents"
    if len(parts) >= 3 and parts[0] == ".snowpea" and parts[1] == "prompts":
        return "prompts"
    return None


# ---------------------------------------------------------------------------
# control paths: what no tool writes without the person (C1 e)
# ---------------------------------------------------------------------------

#: Under ``<project>/.snowpea``: these are configuration (``config`` for every
#: tool, ``never`` delegable for shell lines).
SNOWPEA_CONTROL_DIRS: frozenset[str] = frozenset(
    {"hooks", "agents", "commands", "skills", "prompts", "plugins"})
SNOWPEA_CONTROL_FILES: frozenset[str] = frozenset(
    {"hooks.json", "settings.json", "credentials.json"})
CLAUDE_CONTROL_DIRS: frozenset[str] = frozenset({"hooks", "agents", "commands", "skills"})


def control_path(path: str | os.PathLike[str]) -> bool:
    """``path`` (absolute, resolved) is a project control file or folder (C1 e).

    ``.snowpea/{hooks.json, hooks/**, agents/**, commands/**, skills/**,
    prompts/**, plugins/**, settings.json, credentials.json, mcp*.json}``,
    ``.claude/{hooks.json, hooks/**, settings*.json, agents/**, commands/**,
    skills/**}``, ``.mcp.json``, ``.codex/**``, ``.cursor/**`` — and the
    ``.snowpea``/``.claude`` folders themselves (a move or copy onto one
    replaces all of it).
    """
    parts = Path(os.fspath(path)).parts
    if not parts:
        return False
    if parts[-1] in (".snowpea", ".claude", ".codex", ".cursor", ".mcp.json"):
        return True
    for index, part in enumerate(parts[:-1]):
        rest = parts[index + 1:]
        if part == ".snowpea":
            head = rest[0]
            if len(rest) == 1 and (head in SNOWPEA_CONTROL_FILES or (
                    head.startswith("mcp") and head.endswith(".json"))):
                return True
            if head in SNOWPEA_CONTROL_DIRS:
                return True
        elif part == ".claude":
            head = rest[0]
            if len(rest) == 1 and (head == "hooks.json" or (
                    head.startswith("settings") and head.endswith(".json"))):
                return True
            if head in CLAUDE_CONTROL_DIRS:
                return True
        elif part in (".codex", ".cursor"):
            return True
    return False


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class TrustStore:
    """The trusted-projects record under ``$SNOWPEA_HOME``; every reader asks it."""

    def __init__(self, home: str | os.PathLike[str]) -> None:
        self.home = Path(home)
        self.path = self.home / STORE_DIR / STORE_NAME
        self.legacy = self.home.joinpath(*LEGACY_PATH)
        self._lock = threading.RLock()
        self._cache: tuple[Any, dict[str, Any]] | None = None

    # -- reading ---------------------------------------------------------
    def _stamp(self) -> Any:
        stamps = []
        for path in (self.path, self.legacy):
            try:
                info = path.stat()
                stamps.append((info.st_mtime_ns, info.st_size, info.st_ino))
            except OSError:
                stamps.append(None)
        return tuple(stamps)

    def read(self) -> dict[str, Any]:
        """The whole document (``projects`` keyed by root); the legacy file folded in."""
        with self._lock:
            stamp = self._stamp()
            if self._cache is not None and self._cache[0] == stamp:
                return self._cache[1]
            data = _read_json(self.path)
            if not isinstance(data, dict):
                data = {}
            projects = data.get("projects")
            if not isinstance(projects, dict):
                projects = {}
            data = {**data, "projects": {k: v for k, v in projects.items()
                                         if isinstance(v, dict)}}
            legacy = _read_json(self.legacy)
            rows = legacy.get("projects") if isinstance(legacy, dict) else None
            for key, entry in (rows or {}).items() if isinstance(rows, dict) else ():
                if isinstance(entry, dict) and entry.get("trusted") is True:
                    current = data["projects"].setdefault(str(key), {
                        "scopes": [], "files": {}, "by": entry.get("by"), "at": entry.get("at")})
                    scopes = list(current.get("scopes") or [])
                    if "externalCli" not in scopes and not current.get("revokedExternal"):
                        scopes.append("externalCli")
                        current["scopes"] = scopes
                        current["trusted"] = True
            self._cache = (stamp, data)
            return data

    def entry(self, root: str | os.PathLike[str]) -> dict[str, Any] | None:
        found = self.read()["projects"].get(str(project_root(root)))
        return found if isinstance(found, dict) else None

    def scopes(self, root: str | os.PathLike[str]) -> list[str]:
        entry = self.entry(root)
        return [s for s in (entry or {}).get("scopes") or [] if s in SCOPES]

    def active(self, root: str | os.PathLike[str] | None, scope: str,
               rel: str | None = None) -> bool:
        """``scope`` is trusted for ``root`` and ``rel``'s content is the pinned one.

        Fails closed: an unreadable store, a file not pinned, a changed digest
        are all "not active".
        """
        if root is None:
            return False
        try:
            entry = self.entry(root)
            if entry is None or scope not in (entry.get("scopes") or []):
                return False
            if rel is None:
                return True
            pinned = (entry.get("files") or {}).get(rel)
            if not isinstance(pinned, str) or not pinned.startswith("sha256:"):
                return False
            return digest_of(project_root(root) / rel) == pinned
        except Exception:  # noqa: BLE001 - trust fails closed
            log.warning("could not read project trust for %s", root, exc_info=True)
            return False

    def status(self, root: str | os.PathLike[str]) -> dict[str, Any]:
        """What the project brings, what is active and what changed since trust."""
        base = project_root(root)
        entry = self.entry(base) or {}
        scopes = [s for s in entry.get("scopes") or [] if s in SCOPES]
        pinned: dict[str, str] = {k: v for k, v in (entry.get("files") or {}).items()
                                  if isinstance(v, str)}
        files: list[dict[str, Any]] = []
        for control in discover(base):
            state = "active"
            if control.scope not in scopes:
                state = "untrusted"
            elif control.rel not in pinned:
                state = "new"
            elif pinned[control.rel] != control.digest:
                state = "changed"
            files.append({"path": control.rel, "scope": control.scope, "state": state,
                          "summary": control.summary})
        settings = _read_json(base / SETTINGS_FILE)
        mode_auto = isinstance(settings, dict) and settings.get("defaultMode") == "auto"
        return {
            "workdir": str(base),
            "trusted": bool(scopes),
            "scopes": scopes,
            "by": entry.get("by"),
            "at": entry.get("at"),
            "files": files,
            "inactive": [f["path"] for f in files if f["state"] != "active"],
            "changed": [f["path"] for f in files if f["state"] in ("changed", "new")
                        and f["scope"] in scopes],
            "defaultModeAuto": mode_auto,
            "modeActive": mode_auto and self.active(base, "mode", SETTINGS_FILE),
        }

    def notice(self, root: str | os.PathLike[str]) -> str | None:
        """One line for a session that opens in ``root`` with inactive content, else ``None``."""
        try:
            status = self.status(root)
        except Exception:  # noqa: BLE001 - a notice is best effort
            return None
        inactive = [f for f in status["files"] if f["state"] != "active"]
        if not inactive:
            return None
        kinds = sorted({f["scope"] for f in inactive})
        changed = [f["path"] for f in inactive if f["state"] in ("changed", "new")]
        line = (f"This project brings {', '.join(kinds)} that are not active until you trust "
                f"it (snowpea project trust {status['workdir']}, or the project's trust card)")
        if changed:
            shown = ", ".join(changed[:5]) + (" …" if len(changed) > 5 else "")
            line += f"; changed since you trusted it: {shown}"
        return line + "."

    # -- writing (a local person's only; the RPC and CLI check that) ------
    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_name(f".{STORE_NAME}.{os.getpid()}.{threading.get_ident()}.tmp")
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self.path)
        self._cache = None

    def _mutate(self, root: Path, change: Any) -> dict[str, Any]:
        with self._lock:
            data = json.loads(json.dumps(self.read()))
            data["version"] = 1
            projects = data.setdefault("projects", {})
            entry = projects.get(str(root)) or {"scopes": [], "files": {}}
            change(entry)
            entry["scopes"] = [s for s in SCOPES if s in set(entry.get("scopes") or [])]
            entry["trusted"] = "externalCli" in entry["scopes"]
            if entry["trusted"]:
                entry.pop("revokedExternal", None)
            elif root_in_legacy(self.legacy, root):
                # The legacy file still says trusted: remember that the person took it back.
                entry["revokedExternal"] = True
            if entry["scopes"] or entry.get("revokedExternal"):
                projects[str(root)] = entry
            else:
                projects.pop(str(root), None)
            self._write(data)
            return entry

    def set(self, root: str | os.PathLike[str], scopes: Iterable[str] | None = None, *,
            by: str) -> dict[str, Any]:
        """Trust ``root`` for ``scopes`` (default :data:`DEFAULT_SCOPES` plus what it
        already has), pinning every control file's current digest."""
        base = project_root(root)
        wanted = list(scopes) if scopes is not None else list(DEFAULT_SCOPES)
        unknown = [s for s in wanted if s not in SCOPES]
        if unknown:
            raise ValueError(f"unknown trust scope(s): {', '.join(unknown)}")
        files = {c.rel: c.digest for c in discover(base) if c.digest.startswith("sha256:")}
        settings = base / SETTINGS_FILE
        if settings.is_file():
            digest = digest_of(settings)
            if digest.startswith("sha256:"):
                files[SETTINGS_FILE] = digest

        def change(entry: dict[str, Any]) -> None:
            entry["scopes"] = list(dict.fromkeys([*(entry.get("scopes") or []), *wanted]))
            if set(wanted) & FILE_SCOPES:
                # Trusting content pins it as it is now; trusting the project for
                # external CLIs alone leaves snowpea's own pins as they were.
                entry["files"] = files
            entry.setdefault("files", {})
            entry["by"] = by
            entry["at"] = utc_now()

        return self._mutate(base, change)

    def revoke(self, root: str | os.PathLike[str], scopes: Iterable[str] | None = None, *,
               by: str) -> dict[str, Any]:
        """Take ``scopes`` (default: all) back from ``root``."""
        base = project_root(root)
        dropped = set(scopes) if scopes is not None else set(SCOPES)

        def change(entry: dict[str, Any]) -> None:
            entry["scopes"] = [s for s in entry.get("scopes") or [] if s not in dropped]
            entry["by"] = by
            entry["at"] = utc_now()
            if not set(entry["scopes"]) & FILE_SCOPES:
                entry["files"] = {}

        return self._mutate(base, change)

    def person_wrote(self, root: str | os.PathLike[str], rel: str, before: str) -> None:
        """A local person changed ``rel`` through snowpea itself (a rule, a setting).

        The new content stays active when what was there before was trusted
        (its digest the pinned one) or there was nothing (``before`` is
        ``missing``): the person's own change, on top of what they trusted.
        Anything else waits for :meth:`set`.
        """
        base = project_root(root)
        scope = scope_of(rel) or "settings"
        entry = self.entry(base) or {}
        pinned = (entry.get("files") or {}).get(rel)
        trusted_before = scope in (entry.get("scopes") or []) and pinned == before
        if not trusted_before and before != "missing":
            return
        digest = digest_of(base / rel)
        if not digest.startswith("sha256:"):
            return

        def change(current: dict[str, Any]) -> None:
            current.setdefault("files", {})[rel] = digest
            scopes = list(current.get("scopes") or [])
            if scope not in scopes:
                scopes.append(scope)
            current["scopes"] = scopes
            current.setdefault("by", "person")
            current.setdefault("at", utc_now())

        self._mutate(base, change)

    # -- migration (decision d) ------------------------------------------
    def migrated(self) -> bool:
        return bool(self.read().get("migrated"))

    def migrate(self, roots: Iterable[str | os.PathLike[str]]) -> list[str]:
        """First start with project trust: projects that already had sessions keep what
        they bring, pinned as it is now (their hooks already ran); new ones start
        untrusted.  Runs once; returns the projects trusted this way."""
        with self._lock:
            if self.migrated():
                return []
            trusted: list[str] = []
            for raw in roots:
                try:
                    base = project_root(raw)
                    if not base.is_dir() or not discover(base):
                        continue
                    if self.entry(base) and set(self.scopes(base)) & FILE_SCOPES:
                        continue
                    self.set(base, DEFAULT_SCOPES, by="migration")
                    trusted.append(str(base))
                except Exception:  # noqa: BLE001 - one project must not stop the migration
                    log.warning("could not migrate the trust of %s", raw, exc_info=True)
            data = json.loads(json.dumps(self.read()))
            data["version"] = 1
            data["migrated"] = utc_now()
            data["migratedProjects"] = trusted
            self._write(data)
            return trusted


def root_in_legacy(legacy: Path, root: Path) -> bool:
    data = _read_json(legacy)
    rows = data.get("projects") if isinstance(data, dict) else None
    return isinstance(rows, dict) and isinstance(rows.get(str(root)), dict)


_STORES: dict[str, TrustStore] = {}
_STORES_LOCK = threading.Lock()
#: The daemon's home (``Core`` sets it): readers without a ``core`` at hand
#: (project settings, the prompt loader) ask this store.
_DEFAULT_HOME: list[str] = []


def set_default_home(home: str | os.PathLike[str]) -> None:
    _DEFAULT_HOME[:] = [os.fspath(home)]


def default_store() -> TrustStore:
    """The daemon's store (``$SNOWPEA_HOME``'s when no daemon set one)."""
    if _DEFAULT_HOME:
        return store_for(_DEFAULT_HOME[0])
    from snowpea_core.config.paths import resolve_home

    return store_for(resolve_home())


def settings_trusted(workdir: str | os.PathLike[str]) -> bool:
    """The widening parts of ``<workdir>/.snowpea/settings.json`` apply (H2)."""
    return default_store().active(workdir, "settings", SETTINGS_FILE)


def mode_trusted(workdir: str | os.PathLike[str]) -> bool:
    """``defaultMode: auto`` of ``<workdir>/.snowpea/settings.json`` applies (H2)."""
    return default_store().active(workdir, "mode", SETTINGS_FILE)


def store_for(home: str | os.PathLike[str]) -> TrustStore:
    """The one store of ``home`` (shared, so its cache is shared)."""
    key = os.path.realpath(os.fspath(home))
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = _STORES[key] = TrustStore(key)
        return store


__all__ = [
    "DEFAULT_SCOPES",
    "FILE_SCOPES",
    "SCOPES",
    "ControlFile",
    "TrustStore",
    "control_path",
    "default_store",
    "digest_of",
    "discover",
    "mode_trusted",
    "project_root",
    "scope_of",
    "set_default_home",
    "settings_trusted",
    "settings_widening",
    "store_for",
]
