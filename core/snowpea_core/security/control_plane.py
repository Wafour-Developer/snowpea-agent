"""snowpea's own control files: one table, always on (security review 6, H-1).

The files below decide what snowpea trusts and who may talk to the daemon.  A
call that snowpea reads is refused (or put to a person) when it would change
them or read the secrets among them, whatever the mode, the presets or the
person's lists say — this rule set is not a preset and no setting removes it.
It judges what a call says, not what the programs it starts do later.  The
same table answers for every path that reaches a tool: shell effects and code
effects (:mod:`snowpea_core.security.check`), the file tools (the same check,
and :mod:`snowpea_core.tools.config_guard`), and the ``fs.*`` methods through
:func:`control_plane_effect`.

Levels:

``all``
    A secret: a call that reads, lists or copies it is refused, always
    (:func:`unallowable_read`); one that writes, deletes or moves it is
    blocked, which (0.2.94, owner decision 2026-11-18) the person may still
    allow once on a person-only card.
``write``
    Blocked when written, deleted, moved or re-moded (a person may allow it
    once); reading it is an ordinary read.
``ask``
    snowpea's own executable content (user hooks, plugins, extensions, user
    skills, commands and agent definitions): a shell line or code that writes
    there is put to a person in every mode (never Jarvis, never remembered).
    The file tools already ask for everything under ``$SNOWPEA_HOME``.

The limit, stated honestly: this is a static check of the paths a call names.
A program of the same user that computes the path while it runs is not stopped
here; only OS-level confinement would stop it (separate work).
"""

from __future__ import annotations

import fnmatch
import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path

from snowpea_core.util import pathcase

LEVELS: tuple[str, ...] = ("all", "write", "ask")


@dataclass(frozen=True)
class ControlEntry:
    """One control file or folder, named under ``$SNOWPEA_HOME``."""

    #: Path under the home, ``/``-separated; the last part may hold ``*``.
    rel: str
    level: str
    #: What it is, for the refusal a model or a person reads.
    what: str
    #: Concrete names a pattern stands for (a glob judged against a glob).
    names: tuple[str, ...] = ()
    #: The file tools may edit it behind a person's answer (``settings.json``:
    #: its ``security`` section stays blocked, see ``check._settings_tool``).
    tool_edit: bool = False
    #: A name under the home that is not one of snowpea's own (:func:`own_name`):
    #: a folder of the person's that is no registered project (the refusal says
    #: how to make it one).
    stray: bool = False

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.rel.split("/"))


#: The table.  ``tests/test_security_review6_control.py`` holds it against
#: :class:`snowpea_core.config.paths.Paths`: a new path property that is in
#: neither this table nor :data:`WORKING` fails that test.
CONTROL: tuple[ControlEntry, ...] = (
    # Security review 8, H8-2: a secret's sidecars (``credentials.json.tmp``,
    # ``daemon.json.<x>.tmp``) are the secret too — never read, never written.
    ControlEntry("daemon.json*", "all", "the daemon's address and login",
                 names=("daemon.json",)),
    ControlEntry("token*", "all", "the daemon's token", names=("token",)),
    ControlEntry("credentials*", "all",
                 "stored credentials (providers, messengers, the web password hash)",
                 names=("credentials.json",)),
    ControlEntry("mcp-oauth.json*", "all", "MCP server logins", names=("mcp-oauth.json",)),
    ControlEntry(".daemon.json*", "all", "the daemon's address and login (being written)",
                 names=(".daemon.json",)),
    ControlEntry(".token*", "all", "the daemon's token (being written)", names=(".token",)),
    ControlEntry(".credentials*", "all", "stored credentials (being written)",
                 names=(".credentials.json",)),
    ControlEntry(".mcp-oauth.json*", "all", "MCP server logins (being written)",
                 names=(".mcp-oauth.json",)),
    # The logins of the external CLIs kept in snowpea's home (security review 7,
    # L7-2): before the folders they sit in, which are level ``ask``.
    ControlEntry(".codex/auth.json", "all", "the Codex login"),
    ControlEntry(".claude/.credentials.json", "all", "the Claude Code login"),
    ControlEntry("settings.json", "write", "snowpea's settings", tool_edit=True),
    # The global MCP file starts commands for every project, as a trusted source
    # (security review 7): only a person, or ``mcp.*`` behind a person, writes it.
    ControlEntry(".mcp.json", "write", "the MCP servers started for every project"),
    ControlEntry("daemon.lock", "write", "the lock that keeps one daemon per home"),
    ControlEntry("setup-state.json", "write", "what the setup remembers as tested"),
    ControlEntry("trust", "write", "the trusted-projects record"),
    ControlEntry("jarvis", "write", "Jarvis mode's delegation rules and ledger"),
    ControlEntry("security", "write", "the security check's own records"),
    ControlEntry("state.db*", "write", "the session store",
                 names=("state.db", "state.db-wal", "state.db-shm")),
    ControlEntry("install.json", "write", "where updates are installed from"),
    ControlEntry("update-plan.json", "write", "the pending update"),
    ControlEntry("update-helper.ps1", "write", "the update helper script"),
    ControlEntry("gateway-chats.json", "write", "who may talk to snowpea from a messenger"),
    ControlEntry("external", "write", "external CLI sessions' state"),
    ControlEntry("logs/approvals.jsonl", "write", "the approval log"),
    ControlEntry("logs/security.jsonl", "write", "the security log"),
    ControlEntry("hooks.json", "ask", "your own hooks"),
    ControlEntry("hooks", "ask", "your own hooks"),
    ControlEntry("plugins", "ask", "installed plugins"),
    ControlEntry("extensions", "ask", "installed extensions"),
    ControlEntry("skills", "ask", "your own skills"),
    ControlEntry("commands", "ask", "your own commands"),
    ControlEntry("agents", "ask", "your own agent definitions"),
    ControlEntry("lsp", "ask", "the language servers snowpea installed and runs"),
    ControlEntry("bin", "ask", "programs snowpea installed and runs (the web tunnel)"),
    ControlEntry(".claude", "ask", "the Claude Code folder kept in snowpea's home"),
    ControlEntry(".codex", "ask", "the Codex folder kept in snowpea's home"),
)

#: Working state under the home that tools use every day (never in the table):
#: plans, session workspaces, scratch files (``tmp``, m32), what a person
#: attached, caches, checkpoints, the ordinary logs.  Security review 8, H8-2:
#: this is the explicit allow-list of a default-deny home — any other name
#: directly under it is ``write`` level (:func:`unlisted`).
WORKING: tuple[str, ...] = (
    "plans", "sessions", "attachments", "tool-content", "audio", "cache", "checkpoints",
    "logs", "update-check.json", "teams", "memory.md", "extension-data", "tmp",
)

#: Every other name snowpea keeps, or may keep later, directly under its home
#: (0.2.90, home projects).  With the first parts of :data:`CONTROL` and
#: :data:`WORKING` this is the AUTHORITATIVE set of snowpea's own names
#: (:func:`own_name`): a folder under the home whose first name is one of them
#: is never a person's project.  Patterns are matched without case.
OWN_NAMES: tuple[str, ...] = (
    "budget.json", "escalations.json", "projects.json", "jarvis",
    "external", "security", "trust", "models", "web", "web_dist", "browser", "bin", "tmp",
    "temp", "worktrees", "backups", "backup", "machine-id", "mcp*", "browser-guard*",
    "gateway*", "update*", "install*", "setup*", "daemon*", "state*", "token*", "credential*",
    "settings*", "hooks*", "plugins*", "extension*", "skills*", "commands*", "agents*",
    "lsp*", "snowpea*", "*.json", "*.jsonl", "*.json5", "*.toml", "*.yaml", "*.yml",
    "*.db", "*.db-*", "*.sqlite*", "*.lock", "*.log", "*.pid", "*.sock", "*.tmp", "*.ps1",
    "*.sh", "*.bat", "*.cmd", "*.exe", "*.py", "*.md",
    # Hidden names and spellings Windows reads as another name (8.3 short
    # names ``SKILLS~1``, alternate streams ``skills::$DATA``).
    ".*", "*~*", "*:*",
)


def own_name(name: str) -> bool:
    """``name`` (directly under the home) is one of snowpea's own names.

    Compared without case and without the trailing dots and spaces Windows
    drops (``Skills.`` IS ``skills`` there), whatever the platform: a name
    that is snowpea's anywhere is never a person's project anywhere.
    """
    text = name.rstrip(" .") if name not in (".", "..") else name
    if not text or text in (".", ".."):
        return True
    key = pathcase.fold_text(text)
    patterns = (*(entry.parts[0] for entry in CONTROL), *WORKING, *OWN_NAMES)
    return any(fnmatch.fnmatchcase(key, pathcase.fold_text(pattern)) for pattern in patterns)


# -- a person's projects under the home (0.2.90) ------------------------------
#
# A person may keep a project inside the home folder (``SNOWPEA_HOME`` set to a
# folder they also work in).  A folder a PERSON registered — ``workspace.add``
# or ``session.create`` from a person's connection — whose first name under the
# home is not one of snowpea's own is an ordinary place, for reads and writes;
# its own control files are judged by the project rules like any project's.
# Recorded in ``$SNOWPEA_HOME/security/home-projects.json`` (``security/`` is a
# write-level control folder: no tool adds a row).

#: Where the registered projects are kept, under the home.
PERSON_PROJECTS_FILE: tuple[str, ...] = ("security", "home-projects.json")

#: ``home key`` -> the registered roots, as parts under that home.
_PERSON_PROJECTS: dict[str, tuple[tuple[str, ...], ...]] = {}


def _home_key(home: str | os.PathLike[str]) -> str:
    real = os.path.realpath(os.path.expanduser(os.fspath(home)))
    return str(pathcase.path_key(Path(real), None))


def _real_parts(root: str | os.PathLike[str],
                home: str | os.PathLike[str]) -> tuple[str, ...] | None:
    """``root`` under ``home`` as parts, both with symlinks resolved; ``None`` outside."""
    try:
        real_root = Path(os.path.realpath(os.path.expanduser(os.fspath(root))))
        real_home = Path(os.path.realpath(os.path.expanduser(os.fspath(home))))
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    return _relative(real_root, real_home, None)


def home_project_problem(root: str | os.PathLike[str],
                         home: str | os.PathLike[str] | None) -> str | None:
    """Why ``root`` cannot be a person's project inside ``home``.

    ``None``: it may (a folder under the home whose first name is not
    snowpea's).  ``"home"``: it is the home.  ``"own"``: its first name under
    the home is one of snowpea's own (:func:`own_name`).  ``"outside"``: it is
    not under the home at all (an ordinary project; nothing to register).
    """
    if not home:
        return "outside"
    rel = _real_parts(root, home)
    if rel is None:
        return "outside"
    if not rel:
        return "home"
    if own_name(rel[0]):
        return "own"
    return None


def person_projects(home: str | os.PathLike[str] | None) -> tuple[tuple[str, ...], ...]:
    """The registered roots of ``home``, as parts under it."""
    if not home or not _PERSON_PROJECTS:
        return ()
    try:
        return _PERSON_PROJECTS.get(_home_key(home), ())
    except (OSError, RuntimeError, ValueError, TypeError):
        return ()


def person_project(parts: tuple[str, ...] | None, home: str | os.PathLike[str] | None,
                   *, fold: bool | None = None) -> tuple[str, ...] | None:
    """The registered root that ``parts`` (a path under ``home``) is or lies in."""
    if not parts or not home or not _PERSON_PROJECTS or own_name(parts[0]):
        return None
    for rel in person_projects(home):
        if len(parts) >= len(rel) and all(
            pathcase.name_key(got, fold) == pathcase.name_key(want, fold)
            for got, want in zip(parts, rel, strict=False)
        ):
            return rel
    return None


def _projects_file(home: str | os.PathLike[str]) -> Path:
    return Path(os.path.expanduser(os.fspath(home))).joinpath(*PERSON_PROJECTS_FILE)


def _save_person_projects(home: str | os.PathLike[str]) -> None:
    import json

    from snowpea_core.util.atomic import write_atomic

    path = _projects_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [list(rel) for rel in person_projects(home)]
    data: dict[str, object] = {"version": 1, "projects": rows}
    # The rows the one-time 0.2.90 migration registered, kept while registered.
    migrated = [row for row in _MIGRATED.get(_home_key(home), ()) if list(row) in rows]
    if migrated:
        data["migrated"] = [list(row) for row in migrated]
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


#: ``home key`` -> the rows the one-time migration registered (``migrated: true``).
_MIGRATED: dict[str, tuple[tuple[str, ...], ...]] = {}

#: Written once the 0.2.90 migration ran for a home (never runs again).
MIGRATION_MARKER: tuple[str, ...] = ("security", "home-projects-migrated.json")


def migrate_person_projects(home: str | os.PathLike[str],
                            folders: list[str]) -> list[tuple[str, ...]]:
    """ONE time per home (0.2.90): register ``folders`` — the existing projects a
    person used inside the home before 0.2.88 made them unusable — when each,
    resolved, lies under the home with a first name that is not snowpea's own.

    The caller picks the folders (the session store's top-level sessions that
    no agent, Jarvis, scheduler or messenger created, and the workspaces of
    such sessions).  The marker makes it once: a later start does nothing.
    """
    import json
    import logging

    from snowpea_core.util.atomic import write_atomic

    marker = Path(os.path.expanduser(os.fspath(home))).joinpath(*MIGRATION_MARKER)
    if marker.exists():
        return []
    added: list[tuple[str, ...]] = []
    for folder in folders:
        if home_project_problem(folder, home) is not None:
            continue
        rel = _real_parts(folder, home)
        if not rel or rel in person_projects(home) or rel in added:
            continue
        added.append(add_person_project(home, folder, save=False))
        logging.getLogger("snowpea.security").info(
            "registered %s as a project inside snowpea's home (it was used before 0.2.88)",
            "/".join(rel))
    key = _home_key(home)
    if added:
        _MIGRATED[key] = (*_MIGRATED.get(key, ()), *added)
        _save_person_projects(home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(marker, json.dumps({"version": 1, "registered": [list(r) for r in added]},
                                    ensure_ascii=False) + "\n")
    return added


def add_person_project(home: str | os.PathLike[str], root: str | os.PathLike[str], *,
                       save: bool = True) -> tuple[str, ...]:
    """Register ``root`` (under ``home``) as a person's project.

    The CALLER has checked that a person asked (``server.person_gate``).
    ``ValueError(problem)`` when it may not be one (:func:`home_project_problem`).
    """
    problem = home_project_problem(root, home)
    if problem is not None:
        raise ValueError(problem)
    rel = _real_parts(root, home)
    assert rel and not own_name(rel[0])
    # The home's secrets are never inside a project root (their first names
    # are snowpea's own), so no registration reaches them.
    assert entry_for(rel[:1], fold=True) is None and not working(rel[0], fold=True)
    key = _home_key(home)
    current = list(_PERSON_PROJECTS.get(key, ()))
    if rel not in current:
        current.append(rel)
        _PERSON_PROJECTS[key] = tuple(current)
        if save:
            _save_person_projects(home)
    return rel


def remove_person_project(home: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Forget ``root``; True when it was registered."""
    rel = _real_parts(root, home)
    key = _home_key(home)
    current = list(_PERSON_PROJECTS.get(key, ()))
    if rel is None or rel not in current:
        return False
    current.remove(rel)
    _PERSON_PROJECTS[key] = tuple(current)
    _save_person_projects(home)
    return True


def load_person_projects(home: str | os.PathLike[str]) -> tuple[tuple[str, ...], ...]:
    """Read the registered projects of ``home`` (the daemon's start).  A row whose
    first name is snowpea's own is dropped, whatever the file says."""
    import json

    found: list[tuple[str, ...]] = []
    try:
        data = json.loads(_projects_file(home).read_text(encoding="utf-8"))
        rows = data.get("projects") if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        rows = None
    for row in rows if isinstance(rows, list) else ():
        if (isinstance(row, list) and row and all(isinstance(p, str) and p for p in row)
                and not own_name(row[0])
                and not any(p in (".", "..") or "/" in p or "\\" in p for p in row)):
            rel = tuple(row)
            if rel not in found:
                found.append(rel)
    _PERSON_PROJECTS[_home_key(home)] = tuple(found)
    marked = data.get("migrated") if rows is not None and isinstance(data, dict) else None
    _MIGRATED[_home_key(home)] = tuple(
        tuple(row) for row in (marked if isinstance(marked, list) else ())
        if isinstance(row, list) and tuple(row) in found)
    return tuple(found)


def clear_person_projects() -> None:
    """Forget every registration in this process (tests)."""
    _PERSON_PROJECTS.clear()
    _MIGRATED.clear()

#: The logins of external CLIs in the USER's home (security review 7, L7-2):
#: never read by a tool, whatever the person's own lists say — the same answer
#: as for a secret of snowpea's own.  Paths under the user's home.
EXTERNAL_SECRETS: tuple[tuple[str, str], ...] = (
    (".codex/auth.json", "the Codex login"),
    (".claude/.credentials.json", "the Claude Code login"),
    (".claude.json", "the Claude Code login"),
    (".gemini/oauth_creds.json", "the Gemini CLI login"),
    (".config/cursor/auth.json", "the Cursor login"),
    (".cursor/cli-config.json", "the Cursor login"),
    (".config/anthropic", "the Anthropic credentials"),
)


def unallowable_read(entry: ControlEntry | None) -> bool:
    """The ONE thing a person cannot allow (owner decision 2026-11-18): a tool
    READING ``entry`` — a level ``all`` entry of :data:`CONTROL` (the daemon's
    token and address, saved logins, MCP logins) or of :data:`EXTERNAL_SECRETS`.

    An agent that read one could take the person's authority.  Every other
    block — a write, delete or move of these same files included — is put to
    the person as a person-only ask, once (``approval_queue``)."""
    return entry is not None and entry.level == "all"


def unallowable_reads() -> tuple[str, ...]:
    """The names :func:`unallowable_read` covers: under ``$SNOWPEA_HOME``, then
    (``~/``) under the user's home.  Tests hold this list."""
    own = tuple(name for entry in CONTROL if unallowable_read(entry)
                for name in (entry.names or (entry.rel,)))
    return own + tuple("~/" + rel for rel, _what in EXTERNAL_SECRETS)


@functools.lru_cache(maxsize=1)
def install_roots() -> tuple[Path, ...]:
    """snowpea's own install (security review 8, M8-6): the ``snowpea_core``
    package folder, the Python environment it runs in (``sys.prefix``) and the
    launchers found on ``PATH``."""
    import shutil
    import sys

    import snowpea_core

    found: list[Path] = []
    candidates: list[str | None] = [os.path.dirname(snowpea_core.__file__), sys.prefix,
                                    shutil.which("snowpea"), shutil.which("snowpea-core")]
    for raw in candidates:
        if not raw:
            continue
        for form in (os.path.normpath(raw), os.path.realpath(raw)):
            path = Path(form)
            if path not in found and len(path.parts) > 2:  # never "/" or "/usr"-like roots
                found.append(path)
    return tuple(found)


def install_root(path: str | os.PathLike[str], projects: tuple[str, ...] | list[str] = (),
                 workdir: str | os.PathLike[str] | None = None) -> Path | None:
    """The install root ``path`` lies in (M8-6), else ``None``.  A root inside a
    project the person works on (an editable checkout, a project's own
    ``.venv``) is that project's, not guarded here."""
    target = Path(os.path.normpath(os.fspath(path)))
    owners = [Path(os.path.normpath(os.fspath(p))) for p in (*projects, workdir) if p]
    for root in install_roots():
        if target != root and root not in target.parents:
            continue
        if any(root == owner or owner in root.parents for owner in owners):
            continue
        return root
    return None


def external_secret(path: str | os.PathLike[str],
                    user_home: str | os.PathLike[str] | None) -> ControlEntry | None:
    """The external-CLI login ``path`` is (or lies inside); ``None`` otherwise.

    ``path`` is absolute and normalised by the caller; it is compared as written
    and with symlinks resolved."""
    try:
        base = Path(os.fspath(user_home)) if user_home else Path.home()
        forms = {Path(os.path.normpath(os.fspath(path))), Path(os.path.realpath(os.fspath(path)))}
        bases = {Path(os.path.normpath(str(base))), Path(os.path.realpath(str(base)))}
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    for rel, what in EXTERNAL_SECRETS:
        for root in bases:
            target = pathcase.path_key(root / rel, None)
            for form in forms:
                key = pathcase.path_key(form, None)
                if key == target or target in key.parents:
                    return ControlEntry(rel, "all", what)
    return None


@dataclass(frozen=True)
class ControlEffect:
    """What :func:`control_plane_effect` found."""

    #: ``blocked`` (never by a tool) or ``ask`` (a person decides, in every mode).
    verdict: str
    entry: ControlEntry
    #: The home the path is in, and the path under it as shown to a person.
    home: str
    shown: str


def entry_for(parts: tuple[str, ...], *, fold: bool | None = None) -> ControlEntry | None:
    """The entry a path under the home (as parts) is, or lies inside."""
    if not parts:
        return None
    for entry in CONTROL:
        wanted = entry.parts
        if len(parts) < len(wanted):
            continue
        if all(fnmatch.fnmatchcase(pathcase.name_key(got, fold), pathcase.name_key(want, fold))
               for got, want in zip(parts, wanted, strict=False)):
            return entry
    return None


def working(name: str, *, fold: bool | None = None) -> bool:
    """``name`` (directly under the home) is working state tools use every day."""
    key = pathcase.name_key(name, fold)
    return any(key == pathcase.name_key(entry, fold) for entry in WORKING)


def unlisted(parts: tuple[str, ...], *, fold: bool | None = None) -> ControlEntry | None:
    """Security review 8, H8-2: the home is default-deny.  A name directly under it
    that is in neither :data:`CONTROL` nor :data:`WORKING` — a temp file, a
    sidecar, a file a later release adds — is at ``write`` level like its
    siblings: read freely, never written by a tool."""
    if not parts or entry_for(parts[:1], fold=fold) is not None or working(parts[0], fold=fold):
        return None
    if not own_name(parts[0]):
        # 0.2.90: a folder of the person's, not registered as a project.
        return ControlEntry(parts[0], "write",
                            "a folder inside snowpea's data folder that is not a registered "
                            "project", stray=True)
    return ControlEntry(parts[0], "write",
                        "a file in snowpea's own folder that is not working state; a hook, "
                        "plugin or extension keeps its files in $SNOWPEA_HOME/extension-data/")


def entries_below(parts: tuple[str, ...], *, fold: bool | None = None) -> list[ControlEntry]:
    """The entries inside the folder ``parts`` names (``()`` is the home itself)."""
    found: list[ControlEntry] = []
    for entry in CONTROL:
        wanted = entry.parts
        if len(wanted) > len(parts) and all(
            pathcase.name_key(got, fold) == pathcase.name_key(want, fold)
            for got, want in zip(parts, wanted, strict=False)
        ):
            found.append(entry)
    return found


def homes(home: str | os.PathLike[str] | None,
          user_home: str | os.PathLike[str] | None = None) -> list[Path]:
    """Every snowpea home in reach: the daemon's and the user's default, with
    their symlink-resolved forms."""
    raw: list[Path] = []
    if home:
        raw.append(Path(os.fspath(home)))
    try:
        base = Path(os.fspath(user_home)) if user_home else Path.home()
        raw.append(base / ".snowpea")
    except (OSError, RuntimeError):
        pass
    found: list[Path] = []
    for path in raw:
        for form in (Path(os.path.normpath(os.path.expanduser(str(path)))),
                     Path(os.path.realpath(os.path.expanduser(str(path))))):
            if form not in found:
                found.append(form)
    return found


#: The parts of ``settings.json`` under the home (a linked target is judged as it).
SETTINGS_PARTS: tuple[str, ...] = ("settings.json",)


def settings_target(home: str | os.PathLike[str] | None) -> Path | None:
    """Security review 9, F2: the file ``<home>/settings.json`` resolves to when it
    is a symbolic link (snowpea reads and writes that file), else ``None``.

    Resolved at every check, so a link re-pointed while the daemon runs is
    judged where it points now.  The one helper the shell, code and file-tool
    checks and ``fs.*`` use to treat that target exactly like ``settings.json``.
    """
    if not home:
        return None
    try:
        link = Path(os.path.expanduser(os.fspath(home))) / SETTINGS_PARTS[0]
        if not os.path.islink(link):
            return None
        return Path(os.path.realpath(link))
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def is_settings_target(path: str | os.PathLike[str], home: str | os.PathLike[str] | None,
                       *, fold: bool | None = None) -> bool:
    """``path`` (absolute) is, as written or resolved, the linked target of
    ``<home>/settings.json`` (:func:`settings_target`)."""
    target = settings_target(home)
    if target is None:
        return False
    try:
        text = os.fspath(path)
        forms = {Path(os.path.normpath(text)), Path(os.path.realpath(text))}
    except (OSError, RuntimeError, ValueError, TypeError):
        return False
    key = pathcase.path_key(target, fold)
    return any(pathcase.path_key(form, fold) == key for form in forms)


def shown(entry_home: Path, parts: tuple[str, ...]) -> str:
    del entry_home
    return "$SNOWPEA_HOME" + ("/" + "/".join(parts) if parts else "")


def _relative(path: Path, home: Path, fold: bool | None) -> tuple[str, ...] | None:
    key, base = pathcase.path_key(path, fold), pathcase.path_key(home, fold)
    if key != base and base not in key.parents:
        return None
    # The parts as written (the fold only decides whether it is inside).
    return path.parts[len(home.parts):]


def control_plane_effect(
    path: str | os.PathLike[str],
    access: str = "write",
    *,
    home: str | os.PathLike[str] | None = None,
    user_home: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> ControlEffect | None:
    """Is reading (``access="read"``), writing (``"write"``) or deleting/moving
    (``"delete"``) ``path`` an act on snowpea's control plane?

    ``None``: no.  ``verdict == "blocked"``: no tool does it.  ``"ask"``: only
    behind a person's answer.  ``path`` is taken as written and with symlinks
    resolved; a relative one resolves against ``cwd``.  ``home`` is the
    daemon's ``$SNOWPEA_HOME`` (the user's default ``~/.snowpea`` is always
    judged too).  A ``delete`` of a folder that holds control files counts.
    Pure apart from resolving symlinks; never raises.
    """
    try:
        text = os.path.expanduser(os.fspath(path))
        if not text.strip():
            return None
        if not os.path.isabs(text):
            if cwd is None:
                return None
            text = os.path.join(os.fspath(cwd), text)
        forms = list(dict.fromkeys((Path(os.path.normpath(text)), Path(os.path.realpath(text)))))
    except (OSError, ValueError, TypeError, RuntimeError):
        return None
    for base in homes(home, user_home):
        for form in forms:
            hit = judge_parts(_relative(form, base, None), access, home=base)
            if hit is not None:
                parts = _relative(form, base, None) or ()
                return ControlEffect(hit[0], hit[1], str(base), shown(base, parts))
        # Review 9, F2: the file a linked settings.json points at is settings.json.
        if any(is_settings_target(form, base) for form in forms):
            hit = judge_parts(SETTINGS_PARTS, access)
            if hit is not None:
                return ControlEffect(hit[0], hit[1], str(base), shown(base, SETTINGS_PARTS))
    # Security review 7, L7-2: the logins of external CLIs in the user's home are
    # secrets by the same table (never read or written through ``fs.*`` either).
    for form in forms:
        entry = external_secret(form, user_home)
        if entry is not None:
            try:
                base_home = str(Path(os.fspath(user_home)) if user_home else Path.home())
            except (OSError, RuntimeError):
                base_home = "~"
            return ControlEffect("blocked", entry, base_home, "~/" + entry.rel)
    return None


def judge_parts(parts: tuple[str, ...] | None, access: str, *, recursive: bool = False,
                fold: bool | None = None,
                home: str | os.PathLike[str] | None = None) -> tuple[str, ControlEntry] | None:
    """The verdict for a path inside a home, given as its parts under it.

    ``access``: ``read`` | ``write`` | ``delete``; ``recursive``: the act
    reaches everything below the path (``grep -r``, ``cp -r`` onto it).
    ``home``: the home the parts are under; a path in a project a person
    registered there (:func:`person_project`) is an ordinary path (0.2.90).
    """
    if parts is None:
        return None
    if home is not None and person_project(parts, home, fold=fold) is not None:
        return None
    entry = entry_for(parts, fold=fold) or unlisted(parts, fold=fold)
    if entry is not None:
        if access == "read":
            return ("blocked", entry) if entry.level == "all" else None
        return ("ask" if entry.level == "ask" else "blocked", entry)
    below = entries_below(parts, fold=fold)
    if not below:
        return None
    if access == "read":
        secret = [e for e in below if e.level == "all"]
        return ("blocked", secret[0]) if secret and recursive else None
    if access == "delete" or recursive:
        hard = [e for e in below if e.level != "ask"]
        return ("blocked", hard[0]) if hard else ("ask", below[0])
    return None


def secret_files(home: str | os.PathLike[str]) -> list[Path]:
    """The concrete secret files of ``home`` (level ``all``), for a read guard."""
    base = Path(os.fspath(home))
    found: list[Path] = []
    for entry in CONTROL:
        if entry.level == "all":
            for name in entry.names or (entry.rel,):
                found.append(base / name)
    return found


# -- scratch space (m32, owner decision 2026-11-20) ---------------------------
#
# Where an agent writes and runs throw-away files: ``$SNOWPEA_HOME/tmp``, each
# session's workspace (``$SNOWPEA_HOME/sessions/<day>_<id>/``) and the system's
# temp folders.  Writing, deleting and running files there is never a
# protected-path block, a control-plane ask or an outside-the-project ask.
# The path is judged with symlinks and ``..`` resolved, so a link in a scratch
# folder to a protected file is that file; a temp folder that holds a snowpea
# home is not scratch inside that home (only the home's own scratch is).

#: The scratch folders under a home, as its first part.
SCRATCH_HOME: tuple[str, ...] = ("tmp",)
#: ``<day>_<session id>``: a session workspace under ``sessions/``.
_WORKSPACE = re.compile(r"^\d{4}.*_.")


def temp_dirs() -> tuple[Path, ...]:
    """The system's temp folders, as written and resolved: ``/tmp``,
    ``/private/tmp``, ``/var/tmp``, ``/private/var/tmp`` and whatever
    ``$TMPDIR``, ``%TEMP%``, ``%TMP%`` and :func:`tempfile.gettempdir` name (macOS
    ``/var/folders/…/T``).  A folder that is ``/``, the user's home or above it
    is never one."""
    raw = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp",
           os.environ.get("TMPDIR"), os.environ.get("TEMP"), os.environ.get("TMP"))
    return _temp_dirs(raw, os.path.expanduser("~"))


@functools.lru_cache(maxsize=8)
def _temp_dirs(raw: tuple[str | None, ...], user_home: str) -> tuple[Path, ...]:
    import tempfile

    found: list[Path] = []
    try:
        system: str | None = tempfile.gettempdir()
    except (OSError, RuntimeError):  # pragma: no cover - no temp folder at all
        system = None
    users = {Path(os.path.normpath(user_home)), Path(os.path.realpath(user_home))}
    for text in (*raw, system):
        if not text or not os.path.isabs(text):
            continue
        for form in (Path(os.path.normpath(text)), Path(os.path.realpath(text))):
            if len(form.parts) < 2 or any(form == u or form in u.parents for u in users):
                continue
            if form not in found:
                found.append(form)
    return tuple(found)


def _scratch_parts(parts: tuple[str, ...], fold: bool | None) -> bool:
    if not parts:
        return False
    first = pathcase.name_key(parts[0], fold)
    if any(first == pathcase.name_key(name, fold) for name in SCRATCH_HOME):
        return True
    return (first == pathcase.name_key("sessions", fold) and len(parts) >= 2
            and _WORKSPACE.match(parts[1]) is not None)


def scratch(path: str | os.PathLike[str], *, home: str | os.PathLike[str] | None = None,
            user_home: str | os.PathLike[str] | None = None,
            fold: bool | None = None) -> bool:
    """``path`` (absolute) is scratch space, judged where it really lands
    (symlinks and ``..`` resolved).  Never raises."""
    try:
        text = os.path.expanduser(os.fspath(path))
        if not os.path.isabs(text):
            return False
        real = Path(os.path.realpath(text))
    except (OSError, ValueError, TypeError, RuntimeError):
        return False
    for base in homes(home, user_home):
        parts = _relative(real, base, fold)
        if parts is not None:
            # Inside a snowpea home only its own scratch counts, wherever the home is.
            return _scratch_parts(parts, fold)
    return any(_relative(real, root, fold) is not None for root in temp_dirs())


def scratch_exempt(home: str | os.PathLike[str] | None) -> tuple[Path, ...]:
    """The scratch folders as fixed paths, for a protected tree that holds them:
    ``<home>/tmp``, ``<home>/sessions`` (every workspace) and the temp folders
    that do not hold the home (a home kept in ``/tmp`` stays protected)."""
    found: list[Path] = []
    base: Path | None = None
    if home:
        base = Path(os.path.realpath(os.path.expanduser(os.fspath(home))))
        found.extend(base / name for name in (*SCRATCH_HOME, "sessions"))
    found.extend(root for root in temp_dirs()
                 if base is None or not (root == base or root in base.parents))
    return tuple(found)


def is_secret(path: str | os.PathLike[str], home: str | os.PathLike[str] | None) -> bool:
    """``path`` is one of the secrets of ``home`` (any name its patterns cover)."""
    hit = control_plane_effect(path, "read", home=home)
    return hit is not None


__all__ = [
    "CONTROL",
    "LEVELS",
    "OWN_NAMES",
    "PERSON_PROJECTS_FILE",
    "WORKING",
    "add_person_project",
    "clear_person_projects",
    "home_project_problem",
    "load_person_projects",
    "own_name",
    "person_project",
    "person_projects",
    "remove_person_project",
    "SCRATCH_HOME",
    "scratch",
    "scratch_exempt",
    "temp_dirs",
    "ControlEffect",
    "ControlEntry",
    "control_plane_effect",
    "entries_below",
    "entry_for",
    "homes",
    "is_secret",
    "is_settings_target",
    "settings_target",
    "judge_parts",
    "secret_files",
    "unallowable_read",
    "unallowable_reads",
]
