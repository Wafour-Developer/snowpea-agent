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
    Project agent definitions (``.snowpea|.claude/agents/*.md``), every one of them.
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
from snowpea_core.security.tamper import FileGuard
from snowpea_core.util import pathcase
from snowpea_core.util.atomic import write_atomic

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
#: ``{"at": "<utc>", "projects": […]}``: the one-time migration ran (review 6, M-4).
MARKER_NAME = "migrated.json"
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
    #: Shown to the person deciding (one line; long commands as head … tail).
    summary: str = ""
    #: The commands it runs, each as :func:`display` data (hooks, MCP servers).
    commands: tuple[dict[str, Any], ...] = ()
    #: Project files those commands (or the skill) run, pinned with it (review 6, M-3).
    runs: tuple[str, ...] = ()
    #: ``(display data, reason)`` of commands whose code cannot be pinned.
    not_pinned: tuple[tuple[dict[str, Any], str], ...] = ()


def read_digest(path: Path) -> tuple[bytes | None, str]:
    """The file's bytes and their digest, read ONCE: ``(bytes, "sha256:<hex>")``, or
    ``(None, "missing" | "unreadable")``.

    Security review 6, M-2: no cache.  A trust decision hashes what is on disk
    now (a cache keyed by mtime/size/inode was fooled by a rewrite that kept all
    three), and a reader that goes on to load the file loads these very bytes.
    """
    try:
        info = path.stat()
    except OSError:
        return None, "missing"
    if path.is_symlink():
        # Security review 8, H8-2: a control file that is a link is not read
        # (what it points to can change without the project changing).
        return None, "unreadable"
    if info.st_size > MAX_FILE_BYTES:
        return None, "unreadable"
    try:
        data = path.read_bytes()
    except OSError:
        return None, "unreadable"
    if len(data) > MAX_FILE_BYTES:
        return None, "unreadable"
    return data, "sha256:" + hashlib.sha256(data).hexdigest()


def digest_of(path: Path) -> str:
    """``sha256:<hex>`` of the file as it is now, ``missing``, or ``unreadable``."""
    return read_digest(path)[1]


def _clip(text: str) -> str:
    """One line for a summary: the head AND the tail of a long text, with its length
    (security review 6, L-4: a clipped command must never hide what it ends with)."""
    text = " ".join(str(text).split())
    if len(text) <= SUMMARY_CHARS:
        return text
    head, tail = display(text)["head"], display(text)["tail"]
    return f"{head} … {tail} ({len(text)} characters)"


#: Characters of a command shown from its start and from its end.
HEAD_CHARS = 120
TAIL_CHARS = 60


def display(text: str, label: str = "") -> dict[str, Any]:
    """How a command is shown on the trust card: ``{label, head, tail, length}``.

    ``head`` is the whole text when it is short (``tail`` empty); a long one is
    its first :data:`HEAD_CHARS` and its last :data:`TAIL_CHARS` characters.
    ``length`` is always the full length, so a card can say what it left out.
    """
    flat = " ".join(str(text).split())
    if len(flat) <= HEAD_CHARS + TAIL_CHARS:
        return {"label": label, "head": flat, "tail": "", "length": len(flat)}
    return {"label": label, "head": flat[:HEAD_CHARS], "tail": flat[-TAIL_CHARS:],
            "length": len(flat)}


def _hook_commands(raw: Any) -> list[tuple[str, str]]:
    """``(event, command)`` of every command hook in a parsed hooks file."""
    table = raw.get("hooks") if isinstance(raw, dict) else None
    commands: list[tuple[str, str]] = []
    if isinstance(table, dict):
        for event, entries in table.items():
            for entry in entries if isinstance(entries, list) else []:
                specs = entry.get("hooks") if isinstance(entry, dict) else None
                for spec in specs if isinstance(specs, list) else []:
                    if isinstance(spec, dict) and spec.get("command"):
                        commands.append((str(event), str(spec["command"])))
    return commands


def _mcp_commands(raw: Any) -> list[tuple[str, str, list[str]]]:
    """``(server, shown line, argv)`` of every server in a parsed ``.mcp.json``."""
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    rows: list[tuple[str, str, list[str]]] = []
    if isinstance(servers, dict):
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            argv = [str(entry.get("command") or ""), *[str(a) for a in entry.get("args") or []
                                                         if isinstance(a, (str, int, float))]]
            argv = [word for word in argv if word]
            rows.append((str(name), str(entry.get("url") or " ".join(argv)), argv))
    return rows


def _json_bytes(data: bytes | None) -> Any:
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None


# ---------------------------------------------------------------------------
# what a control file runs (security review 6, M-3)
# ---------------------------------------------------------------------------

#: Why a hook or server command's code is not pinned (``notPinned[].reason``).
NOT_PINNED_REASONS: tuple[str, ...] = (
    "computed",        # the command holds an expansion or substitution: what runs is not known
    "project-script",  # npm/yarn/pnpm run, make…: the project's own script table decides
    "package-runner",  # npx/uvx/pipx run…: a package fetched or resolved when it runs
    "module",          # python -m <module>: resolved by the interpreter when it runs
    "missing",         # it names a project file that is not there now
    "too-large",       # the file is too large to pin
    "too-many",        # more files than one control file may pin
    "unreadable",      # the control file itself could not be read
    "outside-project",  # it runs a file outside the project: not the project's to pin
    "symlink",         # it runs a link inside the project that leads out of it
)
#: What a pin does not cover, said with every status (security review 7, M7-6).
PIN_LIMITS: tuple[str, ...] = (
    "Only the files listed are pinned. What one of them loads or runs in turn (a sourced "
    "script, an imported module, a program it starts) is not.",
)
_SCRIPT_SUFFIXES: tuple[str, ...] = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts",
                                     ".rb", ".pl", ".php", ".lua", ".ps1")
#: Where the system's own programs live: never the project's code, never noted.
_SYSTEM_PREFIXES: tuple[str, ...] = ("/usr/", "/bin/", "/sbin/", "/opt/", "/lib/", "/lib64/",
                                     "/nix/", "/snap/", "/System/", "/Library/",
                                     "/Applications/")
_PROJECT_SCRIPT_PROGRAMS: frozenset[str] = frozenset(
    {"npm", "yarn", "pnpm", "bun", "make", "gmake", "just", "task", "rake", "gradle", "gradlew",
     "mvn", "mvnw", "cargo", "go", "tox", "nox", "invoke"})
_PACKAGE_RUNNERS: frozenset[str] = frozenset({"npx", "bunx", "pnpx", "uvx", "pipx", "dlx"})
_INTERPRETER_NAMES: tuple[str, ...] = ("python", "pypy", "node", "nodejs", "deno", "bun", "bash",
                                       "sh", "zsh", "dash", "ruby", "perl", "php", "lua")
#: Most files one control file pins as what it runs.
MAX_RUNS = 64


def _words(command: str) -> list[str] | None:
    import shlex

    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None


def runs_of(base: Path, argv: list[str], *, raw: str, folder: Path) -> tuple[list[str], str | None]:
    """The project files a command line would run, and why it cannot all be pinned.

    ``argv``: the command's words; ``raw``: the line as written (searched for
    expansions); ``folder``: where the control file lives (``${CLAUDE_PLUGIN_ROOT}``).
    Returns project-relative paths of existing regular files inside ``base`` that a
    word names, and a :data:`NOT_PINNED_REASONS` value or ``None``.
    """
    found: list[str] = []
    reason: str | None = None
    text = raw
    for name in ("${CLAUDE_PLUGIN_ROOT}", "$CLAUDE_PLUGIN_ROOT", "${SNOWPEA_PLUGIN_ROOT}",
                 "$SNOWPEA_PLUGIN_ROOT"):
        text = text.replace(name, str(folder))
    for name in ("${CLAUDE_PROJECT_DIR}", "$CLAUDE_PROJECT_DIR", "${SNOWPEA_PROJECT_DIR}",
                 "$SNOWPEA_PROJECT_DIR", "${PWD}", "$PWD"):
        text = text.replace(name, str(base))
    if "$" in text or "`" in text:
        reason = "computed"
    words = [word for word in argv]
    programs = [os.path.basename(word).lower() for word in words[:3]]
    if any(program in _PACKAGE_RUNNERS for program in programs[:2]):
        reason = reason or "package-runner"
    elif any(program in _PROJECT_SCRIPT_PROGRAMS for program in programs[:1]):
        reason = reason or "project-script"
    interpreter = bool(programs) and programs[0].rstrip("0123456789.") in _INTERPRETER_NAMES
    if interpreter and "-m" in words[1:3]:
        reason = reason or "module"
    # The first word after an interpreter and its options is the script it runs,
    # whatever it looks like (``sh build``; security review 7, M7-6).
    script_index = next((i for i, w in enumerate(words) if i and not w.startswith("-")),
                        None) if interpreter else None
    for index, word in enumerate(words):
        for name in ("${CLAUDE_PLUGIN_ROOT}", "$CLAUDE_PLUGIN_ROOT", "${SNOWPEA_PLUGIN_ROOT}",
                     "$SNOWPEA_PLUGIN_ROOT"):
            word = word.replace(name, str(folder))
        for name in ("${CLAUDE_PROJECT_DIR}", "$CLAUDE_PROJECT_DIR", "${SNOWPEA_PROJECT_DIR}",
                     "$SNOWPEA_PROJECT_DIR", "${PWD}", "$PWD"):
            word = word.replace(name, str(base))
        if not word or word.startswith("-") or "$" in word or "`" in word or "://" in word:
            continue
        pathlike = "/" in word or "." in os.path.basename(word) or index == script_index
        if not pathlike and index:
            continue
        candidate = Path(os.path.expanduser(word))
        if not candidate.is_absolute():
            candidate = base / candidate
        real = Path(os.path.realpath(candidate))
        # Code the command runs: its program, an interpreter's script, a word that
        # is a script by its name.  (Any other word is data the command is given.)
        runs_it = index in (0, script_index) or word.lower().endswith(_SCRIPT_SUFFIXES)
        if real != base and base not in real.parents:
            # Not the project's own code, so not pinned — said, not skipped
            # (security review 7, M7-6): a link inside the project that leads out
            # of it, or a file elsewhere.  The system's own programs are let be.
            try:
                exists = real.is_file()
            except OSError:
                exists = False
            if exists and runs_it and not str(real).startswith(_SYSTEM_PREFIXES):
                lexical = Path(os.path.normpath(candidate))
                inside = lexical == base or base in lexical.parents
                reason = reason or ("symlink" if inside else "outside-project")
            continue
        try:
            if real.is_file():
                found.append(real.relative_to(base).as_posix())
            elif interpreter and index == script_index:
                reason = reason or "missing"  # the script it is told to run is not there
        except OSError:
            continue
    return list(dict.fromkeys(found)), reason


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


def _parse(data: bytes | None) -> Any:
    """JSON from bytes a guard verified; ``None`` when missing or not JSON."""
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
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


def _auto_inject(path: Path) -> list[str]:
    try:
        from snowpea_core.skills.skill_md import auto_inject_keywords

        return list(auto_inject_keywords(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ImportError):
        return []


def builtin_agent_names() -> frozenset[str]:
    """Names of the agents snowpea ships (a project definition never takes one's
    place without the project's ``agents`` trust; review 6, L-1)."""
    try:
        from snowpea_core.agent.definition import builtin_agent_definitions

        return frozenset(defn.name for defn in builtin_agent_definitions())
    except Exception:  # noqa: BLE001 - unknown: nothing counts as built in
        return frozenset()


def agent_definition_active(project: str | os.PathLike[str], path: str | os.PathLike[str],
                            name: str, permission: Any,
                            store: TrustStore | None = None, *,
                            digest: str | None = None) -> bool:
    """A project's agent definition may be used (review 6, L-1; review 7).

    EVERY project definition needs the project's ``agents`` trust with the
    file as pinned — a prompt and a tool list are instructions too, so one
    without a ``permission`` waits for the person like the others (the rule
    decided in review 6 and built too narrowly then).

    ``digest``: of the bytes the caller already loaded (review 7, M7-7) — the
    pin is compared with what was loaded, not with what is on disk by now.
    """
    del name, permission
    try:
        base = project_root(project)
        rel = os.path.relpath(os.path.realpath(os.fspath(path)), base).replace(os.sep, "/")
        trusted = store or default_store()
        if digest is not None:
            return trusted.pinned_as(base, "agents", rel, digest)
        return trusted.active(base, "agents", rel)
    except Exception:  # noqa: BLE001 - trust fails closed
        return False


def _skill_files(base: Path, skill_md: Path) -> tuple[list[str], list[tuple[dict[str, Any], str]]]:
    """Everything else in a skill's folder: the scripts its text tells the model to run."""
    runs: list[str] = []
    loose: list[tuple[dict[str, Any], str]] = []
    try:
        entries = sorted(p for p in skill_md.parent.rglob("*") if p.is_file() and p != skill_md)
    except OSError:
        return runs, loose
    for path in entries:
        rel = path.relative_to(base).as_posix()
        if len(runs) >= MAX_RUNS:
            loose.append((display(skill_md.parent.name), "too-many"))
            break
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                loose.append((display(rel), "too-large"))
                continue
        except OSError:
            continue
        runs.append(rel)
    return runs, loose


def discover(root: str | os.PathLike[str]) -> list[ControlFile]:
    """Every control file the project at ``root`` holds now, with its scope."""
    base = project_root(root)
    out: list[ControlFile] = []

    def add(path: Path, scope: str, summary: str, *, digest: str | None = None,
            commands: list[dict[str, Any]] | None = None, runs: list[str] | None = None,
            not_pinned: list[tuple[dict[str, Any], str]] | None = None) -> None:
        rel = path.relative_to(base).as_posix()
        out.append(ControlFile(rel, scope, digest or digest_of(path), summary,
                               tuple(commands or ()), tuple(dict.fromkeys(runs or ())),
                               tuple(not_pinned or ())))

    def commanded(path: Path, scope: str, rows: list[tuple[str, str, list[str] | None]],
                  digest: str, empty: str, unreadable: bool) -> None:
        commands: list[dict[str, Any]] = []
        runs: list[str] = []
        loose: list[tuple[dict[str, Any], str]] = []
        for label, line, argv in rows:
            shown = display(line, label)
            commands.append(shown)
            words = argv if argv is not None else _words(line)
            if words is None:
                loose.append((shown, "computed"))
                continue
            files, reason = runs_of(base, words, raw=line, folder=path.parent)
            runs.extend(files)
            if reason is not None:
                loose.append((shown, reason))
        if unreadable:
            loose.append((display(path.name), "unreadable"))
        if len(runs) > MAX_RUNS:
            runs = runs[:MAX_RUNS]
            loose.append((display(path.name), "too-many"))
        summary = " | ".join(
            f"{c['label']}: {c['head']}" + (f" … {c['tail']} ({c['length']} characters)"
                                           if c["tail"] else "")
            for c in commands) or empty
        add(path, scope, summary, digest=digest, commands=commands, runs=runs,
            not_pinned=loose)

    for rel in HOOK_FILES:
        path = base / rel
        if path.is_file():
            data, digest = read_digest(path)
            parsed = _json_bytes(data)
            commanded(path, "hooks", [(event, line, None)
                                      for event, line in _hook_commands(parsed)],
                      digest, "no hook commands" if parsed is not None
                      else "unreadable hooks file", parsed is None)
    if (base / MCP_FILE).is_file():
        data, digest = read_digest(base / MCP_FILE)
        parsed = _json_bytes(data)
        commanded(base / MCP_FILE, "mcp",
                  [(name, line, argv or None) for name, line, argv in _mcp_commands(parsed)],
                  digest, "no servers" if parsed is not None else "unreadable .mcp.json",
                  parsed is None)
    for pattern in (".snowpea/commands/*.md", ".claude/commands/*.md",
                    ".snowpea/skills/*/SKILL.md", ".claude/skills/*/SKILL.md",
                    ".agents/skills/*/SKILL.md"):
        for path in _glob(base, pattern):
            meta = _front_matter(path)
            tools = meta.get("allowed-tools") or meta.get("allowed_tools")
            # Review 6, M-6: a skill that brings itself into a turn (autoInject) is a
            # control file too, like one that pre-approves tools.
            inject = _auto_inject(path)
            if tools or inject:
                parts = []
                if tools:
                    parts.append("allowed-tools: " + (", ".join(tools) if isinstance(tools, list)
                                                      else str(tools)))
                if inject:
                    parts.append("autoInject: " + ", ".join(inject))
                runs, loose = _skill_files(base, path) if path.name == "SKILL.md" else ([], [])
                add(path, "commands", _clip("; ".join(parts)), runs=runs, not_pinned=loose)
    builtin = builtin_agent_names()
    for pattern in (".snowpea/agents/*.md", ".claude/agents/*.md"):
        for path in _glob(base, pattern):
            meta = _front_matter(path)
            permission = meta.get("permission")
            name = str(meta.get("name") or path.stem)
            parts = []
            if permission not in (None, "", "inherit"):
                parts.append(f"permission: {permission}")
            if name in builtin or path.stem in builtin:
                # Review 6, L-1: it would take a built-in agent's place.
                parts.append(f"replaces the built-in {name} agent")
            # Review 7 (6A L-1 as decided): every project definition is a control
            # file — inactive until the project is trusted for agents.
            add(path, "agents", _clip("; ".join(parts) or f"agent definition: {name}"))
    for path in _glob(base, ".snowpea/prompts/**/*.md"):
        add(path, "prompts", "prompt override")
    settings = base / SETTINGS_FILE
    if settings.is_file():
        widening = settings_widening(_read_json(settings))
        if widening:
            add(settings, "settings", _clip(", ".join(widening)))
    return out


def pinnable(root: str | os.PathLike[str], controls: list[ControlFile] | None = None,
             ) -> tuple[dict[str, str], dict[str, list[str]]]:
    """What trusting the project now would pin: ``{rel: digest}`` of every control
    file, of every project file one of them runs and of the project settings file;
    and ``{control rel: [files it runs]}``."""
    base = project_root(root)
    files: dict[str, str] = {}
    runs: dict[str, list[str]] = {}
    for control in controls if controls is not None else discover(base):
        if control.digest.startswith("sha256:"):
            files[control.rel] = control.digest
        kept: list[str] = []
        for rel in control.runs:
            digest = files.get(rel) or digest_of(base / rel)
            if digest.startswith("sha256:"):
                files[rel] = digest
                kept.append(rel)
        if kept:
            runs[control.rel] = kept
    settings = base / SETTINGS_FILE
    if settings.is_file():
        digest = digest_of(settings)
        if digest.startswith("sha256:"):
            files[SETTINGS_FILE] = digest
    return files, runs


class TrustChanged(Exception):
    """``project.trust.set``: the project is not what the person was shown (M-5)."""

    def __init__(self, changed: list[str], added: list[str], missing: list[str]) -> None:
        super().__init__("the project's control files changed since they were shown")
        self.changed, self.added, self.missing = changed, added, missing


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
    # Security review 6, M-7: ``.Snowpea/Hooks.json`` is the hooks file where the
    # file system ignores case.
    parts = tuple(pathcase.name_key(part) for part in Path(os.fspath(path)).parts)
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
        #: Written once, when the one-time migration ran (security review 6, M-4).
        self.marker = self.home / STORE_DIR / MARKER_NAME
        self.legacy = self.home.joinpath(*LEGACY_PATH)
        self._lock = threading.RLock()
        #: The tamper guard (security review 7, P2): while the daemon runs it is
        #: the only writer of the record, the marker and the legacy file.  Not
        #: armed until :meth:`guard` (a CLI or a test without a daemon).
        self._guards = [
            FileGuard(self.path, home=self.home, what="the trusted-projects record"),
            FileGuard(self.marker, home=self.home, what="the trust migration marker"),
            FileGuard(self.legacy, home=self.home, what="the external-CLI trust record"),
        ]
        #: ``(path, what happened)`` when one of them was changed from outside and put back.
        self.tamper_listeners: list[Any] = []
        for one in self._guards:
            one.listeners = self.tamper_listeners

    # -- the tamper guard --------------------------------------------------
    def guard(self) -> None:
        """Take the files as they are now and refuse every later change that is not
        this store's own (the daemon calls this once, at start)."""
        with self._lock:
            for one in self._guards:
                one.arm()

    def _verified(self) -> tuple[bytes | None, bytes | None, bytes | None]:
        """The record, the marker and the legacy file as a decision may use them.

        Security review 8, H8-1: while the daemon runs these are the guard's
        copies (after a look that puts back any foreign change), never a second
        read of the disk; without a daemon each file is read once, refusing a
        link or a file that is not the user's."""
        record, marker, legacy = (one.verified() for one in self._guards)
        return record, marker, legacy

    def _wrote(self, path: Path, data: bytes | None) -> None:
        for one in self._guards:
            if one.path == path:
                one.wrote(data)

    # -- reading ---------------------------------------------------------
    def read(self) -> dict[str, Any]:
        """The whole document (``projects`` keyed by root); the legacy file folded in.

        Read every time (review 6, M-2: a trust decision never comes from a
        cache) — from the bytes the tamper guard verified (review 8, H8-1).
        When the migration marker says a record was written and the record is
        gone or unreadable (:meth:`problem`), nothing is trusted — not even
        what the legacy external-CLI file says.
        """
        with self._lock:
            record, marker, legacy_bytes = self._verified()
            data = _parse(record)
            lost = not isinstance(data, dict) and marker is not None
            if not isinstance(data, dict):
                data = {}
            projects = data.get("projects")
            if not isinstance(projects, dict):
                projects = {}
            data = {**data, "projects": {k: v for k, v in projects.items()
                                         if isinstance(v, dict)}}
            if lost:
                return data
            legacy = _parse(legacy_bytes)
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
            return data

    def problem(self) -> str | None:
        """Why no project is trusted although some were (review 6, M-4), else ``None``.

        The marker outlives the record: a record that is missing or does not
        parse while the marker exists was lost or tampered with.  It fails
        closed — every project waits for the person again — and says so.
        """
        try:
            with self._lock:
                record, marker, _legacy = self._verified()
            if marker is None:
                return None
            if isinstance(_parse(record), dict):
                return None
        except OSError:
            pass
        return ("the trusted-projects record ($SNOWPEA_HOME/trust/projects.json) is missing or "
                "unreadable: no project is trusted until you trust it again")

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
        if rel is None:
            if root is None:
                return False
            try:
                entry = self.entry(root)
                return entry is not None and scope in (entry.get("scopes") or [])
            except Exception:  # noqa: BLE001 - trust fails closed
                log.warning("could not read project trust for %s", root, exc_info=True)
                return False
        return self.read_pinned(root, scope, rel) is not None

    def read_pinned(self, root: str | os.PathLike[str] | None, scope: str,
                    rel: str) -> bytes | None:
        """The bytes of ``rel`` when ``scope`` is trusted for ``root`` and they are the
        pinned ones — read ONCE, so a caller that loads the file loads exactly what
        was verified (security review 6, M-2).  ``None``: not active.

        The files ``rel`` runs (its hook scripts, a skill's folder; review 6, M-3)
        must be as pinned too: one that changed switches the control file off.
        """
        if root is None:
            return None
        try:
            entry = self.entry(root)
            if entry is None or scope not in (entry.get("scopes") or []):
                return None
            files = entry.get("files") or {}
            pinned = files.get(rel)
            if not isinstance(pinned, str) or not pinned.startswith("sha256:"):
                return None
            base = project_root(root)
            data, digest = read_digest(base / rel)
            if data is None or digest != pinned:
                return None
            for dependent in (entry.get("runs") or {}).get(rel) or []:
                if not isinstance(dependent, str) or digest_of(base / dependent) != files.get(
                        dependent):
                    return None
            return data
        except Exception:  # noqa: BLE001 - trust fails closed
            log.warning("could not read project trust for %s", root, exc_info=True)
            return None

    def pinned_as(self, root: str | os.PathLike[str] | None, scope: str, rel: str,
                  digest: str) -> bool:
        """``scope`` is trusted and ``digest`` (of bytes the caller already loaded) is
        the pinned digest of ``rel``, with what ``rel`` runs unchanged."""
        if root is None:
            return False
        try:
            entry = self.entry(root)
            if entry is None or scope not in (entry.get("scopes") or []):
                return False
            files = entry.get("files") or {}
            if not digest.startswith("sha256:") or files.get(rel) != digest:
                return False
            base = project_root(root)
            return all(isinstance(dep, str) and digest_of(base / dep) == files.get(dep)
                       for dep in (entry.get("runs") or {}).get(rel) or [])
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
        runs: list[dict[str, Any]] = []
        not_pinned: list[dict[str, Any]] = []
        controls = discover(base)
        digests, _runs = pinnable(base, controls)
        seen_runs: dict[str, dict[str, Any]] = {}
        for control in controls:
            state = "active"
            if control.scope not in scopes:
                state = "untrusted"
            elif control.rel not in pinned:
                state = "new"
            elif pinned[control.rel] != control.digest:
                state = "changed"
            run_states: list[str] = []
            for rel in control.runs:
                digest = digests.get(rel, "unreadable")
                run_state = "active"
                if control.scope not in scopes:
                    run_state = "untrusted"
                elif rel not in pinned:
                    run_state = "new"
                elif pinned[rel] != digest:
                    run_state = "changed"
                run_states.append(run_state)
                row = seen_runs.get(rel)
                if row is None:
                    row = seen_runs[rel] = {"path": rel, "digest": digest, "state": run_state,
                                            "by": []}
                    runs.append(row)
                elif run_state != "active":
                    row["state"] = run_state
                row["by"].append(control.rel)
            if state == "active" and any(s != "active" for s in run_states):
                state = "changed"  # what it runs is not what was trusted
            files.append({"path": control.rel, "scope": control.scope, "state": state,
                          "summary": control.summary, "digest": control.digest,
                          "commands": list(control.commands)})
            for shown, reason in control.not_pinned:
                not_pinned.append({"source": control.rel, "command": shown, "reason": reason})
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
            "runs": runs,
            "notPinned": not_pinned,
            "digests": digests,
            "limits": list(PIN_LIMITS),
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
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        # A guarded record is replaced even when a link was planted in its place
        # (the link is replaced, never followed); without a daemon a link refuses.
        write_atomic(self.path, payload, replace_link=self._guards[0].armed)
        # The daemon's own write: the bytes it wrote are the copy the guard
        # compares with, never what is read back (review 8, H8-1).
        self._wrote(self.path, payload)

    def _mutate(self, root: Path, change: Any) -> dict[str, Any]:
        with self._lock:
            # From the verified copy (``read``), never a fresh look at the disk.
            data = json.loads(json.dumps(self.read()))
            data["version"] = 1
            projects = data.setdefault("projects", {})
            entry = projects.get(str(root)) or {"scopes": [], "files": {}}
            change(entry)
            entry["scopes"] = [s for s in SCOPES if s in set(entry.get("scopes") or [])]
            entry["trusted"] = "externalCli" in entry["scopes"]
            if entry["trusted"]:
                entry.pop("revokedExternal", None)
            elif _in_legacy(_parse(self._guards[2].verified()), root):
                # The legacy file still says trusted: remember that the person took it back.
                entry["revokedExternal"] = True
            if entry["scopes"] or entry.get("revokedExternal"):
                projects[str(root)] = entry
            else:
                projects.pop(str(root), None)
            self._write(data)
            return entry

    def set(self, root: str | os.PathLike[str], scopes: Iterable[str] | None = None, *,
            by: str, digests: dict[str, str] | None = None) -> dict[str, Any]:
        """Trust ``root`` for ``scopes`` (default :data:`DEFAULT_SCOPES` plus what it
        already has), pinning every control file — and what it runs — as ``digests``
        says they are.

        ``digests`` is ``status()["digests"]`` as the person saw it (security
        review 6, M-5).  What is pinned is what was SHOWN: when the disk differs
        now — a digest changed, a file appeared or went away — nothing is trusted
        and :class:`TrustChanged` names the paths.  ``None`` (a caller inside
        core: the migration) pins what is on disk now.
        """
        base = project_root(root)
        wanted = list(scopes) if scopes is not None else list(DEFAULT_SCOPES)
        unknown = [s for s in wanted if s not in SCOPES]
        if unknown:
            raise ValueError(f"unknown trust scope(s): {', '.join(unknown)}")
        files, runs = pinnable(base)
        if digests is not None:
            shown = {str(k): str(v) for k, v in digests.items()}
            changed = sorted(rel for rel in files if rel in shown and shown[rel] != files[rel])
            added = sorted(rel for rel in files if rel not in shown)
            missing = sorted(rel for rel in shown if rel not in files)
            if changed or added or missing:
                raise TrustChanged(changed, added, missing)

        def change(entry: dict[str, Any]) -> None:
            entry["scopes"] = list(dict.fromkeys([*(entry.get("scopes") or []), *wanted]))
            if set(wanted) & FILE_SCOPES:
                # Trusting content pins it as it is now; trusting the project for
                # external CLIs alone leaves snowpea's own pins as they were.
                entry["files"] = files
                entry["runs"] = runs
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
                entry.pop("runs", None)

        return self._mutate(base, change)

    def person_wrote(self, root: str | os.PathLike[str], rel: str, before: str, *,
                     digest: str | None = None) -> None:
        """A local person changed ``rel`` through snowpea itself (a rule, a setting).

        The new content stays active when what was there before was trusted
        (its digest the pinned one) or there was nothing (``before`` is
        ``missing``): the person's own change, on top of what they trusted.
        Anything else waits for :meth:`set`.  ``digest``: of the bytes snowpea
        wrote — pinned instead of what the disk holds by now (review 8, M8-1).
        """
        base = project_root(root)
        scope = scope_of(rel) or "settings"
        entry = self.entry(base) or {}
        pinned = (entry.get("files") or {}).get(rel)
        trusted_before = scope in (entry.get("scopes") or []) and pinned == before
        if not trusted_before and before != "missing":
            return
        if digest is None:
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
        """The one-time migration ran: its marker file exists.

        Security review 6, M-4: the marker is its own file, so losing the record
        does not make the migration run again (and trust every project with a
        session again).  A record written before the marker existed carries a
        ``migrated`` key: that counts, and the marker is written from it.
        """
        with self._lock:
            record, marker, _legacy = self._verified()
        if marker is not None:
            return True
        old = _parse(record)
        if isinstance(old, dict) and old.get("migrated"):
            with self._lock:
                self._write_marker(str(old.get("migrated")),
                                   [str(p) for p in old.get("migratedProjects") or []])
            return True
        return False

    def restore_marker(self, at: str) -> None:
        """The daemon's own record says the migration ran at ``at`` but the marker is
        gone: write it again, so the migration can never run a second time
        (security review 7, M7-8)."""
        with self._lock:
            self._write_marker(at, [])

    def migrated_at(self) -> str | None:
        data = _parse(self._guards[1].verified())
        at = data.get("at") if isinstance(data, dict) else None
        return str(at) if at else None

    def _write_marker(self, at: str, projects: list[str]) -> None:
        if self._guards[1].verified() is not None:
            return  # written once, never rewritten
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.marker.parent, 0o700)
        payload = json.dumps({"at": at, "projects": projects}, indent=2).encode("utf-8")
        write_atomic(self.marker, payload, replace_link=self._guards[1].armed)
        self._wrote(self.marker, payload)

    def migrate(self, roots: Iterable[str | os.PathLike[str]],
                at: str | None = None) -> list[str]:
        """First start with project trust: projects that already had sessions keep what
        they bring, pinned as it is now (their hooks already ran); new ones start
        untrusted.  Runs once — the marker is written FIRST, so a crash or a lost
        record never makes it run twice; returns the projects trusted this way.

        ``roots`` are the projects whose sessions were created before ``at`` (the
        upgrade's timestamp, now by default): the caller asks the session store
        for exactly those.
        """
        with self._lock:
            if self.migrated():
                return []
            stamp = at or utc_now()
            self._write_marker(stamp, [])
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
            data["migrated"] = stamp
            data["migratedProjects"] = trusted
            self._write(data)
            return trusted


def root_in_legacy(legacy: Path, root: Path) -> bool:
    return _in_legacy(_parse(FileGuard(legacy).verified()), root)


def _in_legacy(data: Any, root: Path) -> bool:
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
    "NOT_PINNED_REASONS",
    "SCOPES",
    "ControlFile",
    "TrustChanged",
    "TrustStore",
    "display",
    "pinnable",
    "read_digest",
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
