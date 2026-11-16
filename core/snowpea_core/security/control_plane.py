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
    A secret: a call that reads, lists, copies, writes, deletes or moves it is refused.
``write``
    Never written, deleted, moved or re-moded; reading it is an ordinary read.
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
    ControlEntry(".claude", "ask", "the Claude Code folder kept in snowpea's home"),
    ControlEntry(".codex", "ask", "the Codex folder kept in snowpea's home"),
)

#: Working state under the home that tools use every day (never in the table):
#: plans, session workspaces, what a person attached, caches, checkpoints, the
#: ordinary logs.  Security review 8, H8-2: this is the explicit allow-list of a
#: default-deny home — any other name directly under it is ``write`` level
#: (:func:`unlisted`).
WORKING: tuple[str, ...] = (
    "plans", "sessions", "attachments", "tool-content", "audio", "cache", "checkpoints",
    "logs", "update-check.json", "teams", "memory.md", "extension-data",
)

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
            hit = judge_parts(_relative(form, base, None), access)
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
                fold: bool | None = None) -> tuple[str, ControlEntry] | None:
    """The verdict for a path inside a home, given as its parts under it.

    ``access``: ``read`` | ``write`` | ``delete``; ``recursive``: the act
    reaches everything below the path (``grep -r``, ``cp -r`` onto it).
    """
    if parts is None:
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


def is_secret(path: str | os.PathLike[str], home: str | os.PathLike[str] | None) -> bool:
    """``path`` is one of the secrets of ``home`` (any name its patterns cover)."""
    hit = control_plane_effect(path, "read", home=home)
    return hit is not None


__all__ = [
    "CONTROL",
    "LEVELS",
    "WORKING",
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
]
