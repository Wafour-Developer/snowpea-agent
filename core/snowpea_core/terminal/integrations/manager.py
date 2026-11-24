"""Install / uninstall / status of the CLI hook integrations (design m37 §2.7).

One shared hook script, ``$SNOWPEA_HOME/hooks/snowpea-agent-state.sh`` (``.ps1``
on Windows), is registered in each CLI's own hook config:

======  ==========================================  ==========================================
CLI     config (env override, default)              events → action
======  ==========================================  ==========================================
claude  ``settings.json`` (``CLAUDE_CONFIG_DIR``,    SessionStart → session, UserPromptSubmit →
        ``~/.claude``)                              working, Stop → done, Notification
                                                    (permission_prompt|elicitation_dialog) →
                                                    blocked
codex   ``hooks.json`` + ``[features] hooks = true``  SessionStart → session, UserPromptSubmit →
        in ``config.toml`` (``CODEX_HOME``,          working, Stop → done
        ``~/.codex``)
cursor  ``hooks.json`` (``CURSOR_CONFIG_DIR``,       sessionStart → session, stop → done
        ``~/.cursor``)
agy     ``hooks.json`` block ``snowpea``             PreInvocation → session
        (``ANTIGRAVITY_CLI_CONFIG_DIR``,
        ``~/.gemini/config``)
======  ==========================================  ==========================================

Cursor's ``beforeSubmitPrompt`` is left out on purpose: its output decides
whether the prompt is sent, and a hook that answers wrongly would block the
person.  agy is session-only like herdr: it has no blocked event, and
``PostInvocation`` is skipped on an interrupt, so a hook state could go stale —
the screen rules own its state.

Rules (shared with herdr, plus the uninstall promise):

* our entries are found by the exact command string; nothing marks the
  person's config, and only our handlers (and the groups / event arrays they
  leave empty) are removed;
* writes keep the file's formatting (:mod:`.jsonedit`, :mod:`.tomledit`), are
  atomic, keep the mode and refuse hard links (:mod:`.fileio`);
* a repeat install writes nothing; before the first change to an existing file
  one backup is kept;
* ``$SNOWPEA_HOME/integrations/state.json`` records what we added (the
  ``hooks`` object or file we created, Cursor's ``version`` key, the Codex
  feature line), and uninstall removes exactly that.

Portions derived from herdr (https://github.com/herdrdev/herdr), Apache-2.0
(``src/integration/{targets,config_edit,registry,command}.rs``).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from snowpea_core.terminal.integrations import fileio, jsonedit, tomledit

INTEGRATION_ID = "snowpea-agent-state"
INTEGRATION_VERSION = 1
VERSION_MARKER = "SNOWPEA_INTEGRATION_VERSION="
ID_MARKER = "SNOWPEA_INTEGRATION_ID="
HOOK_TIMEOUT_S = 10
AGY_BLOCK = "snowpea"
STATE_VERSION = 1

CLIS: tuple[str, ...] = ("claude", "codex", "cursor", "agy")
LABELS = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor agent",
          "agy": "Antigravity CLI"}
BINARIES = {"claude": ("claude",), "codex": ("codex",), "cursor": ("cursor-agent", "agent"),
            "agy": ("agy", "antigravity")}
ALL_ACTIONS = ("session", "working", "done", "blocked")

#: Claude's documented SessionStart sources (herdr's matcher).
CLAUDE_SESSION_MATCHER = "^(startup|resume|clear|compact|fork)$"
CLAUDE_BLOCKED_MATCHER = "permission_prompt|elicitation_dialog"

# (event, matcher or None, action)
CLAUDE_EVENTS: tuple[tuple[str, str | None, str], ...] = (
    ("SessionStart", CLAUDE_SESSION_MATCHER, "session"),
    ("UserPromptSubmit", None, "working"),
    ("Stop", None, "done"),
    ("Notification", CLAUDE_BLOCKED_MATCHER, "blocked"),
)
CODEX_EVENTS: tuple[tuple[str, str | None, str], ...] = (
    ("SessionStart", None, "session"),
    ("UserPromptSubmit", None, "working"),
    ("Stop", None, "done"),
)
CURSOR_EVENTS: tuple[tuple[str, str], ...] = (("sessionStart", "session"), ("stop", "done"))
AGY_EVENTS: tuple[tuple[str, str], ...] = (("PreInvocation", "session"),)

STATUS_NOT_INSTALLED = "not-installed"
STATUS_CURRENT = "current"
STATUS_OUTDATED = "outdated"
STATUS_MODIFIED = "modified"


class IntegrationError(RuntimeError):
    """An install or uninstall that cannot go ahead (missing CLI folder, bad config …)."""


# -- environment -----------------------------------------------------------------


@dataclass
class Env:
    """Where things live; every path comes from here so tests never touch the real home."""

    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    snowpea_home: Path | None = None
    windows: bool = sys.platform == "win32"

    def user_home(self) -> Path:
        for key in (("USERPROFILE", "HOME") if self.windows else ("HOME",)):
            value = self.environ.get(key)
            if value:
                return Path(value).expanduser()
        return Path.home()

    def home(self) -> Path:
        if self.snowpea_home is not None:
            return Path(self.snowpea_home)
        value = self.environ.get("SNOWPEA_HOME")
        if value:
            return Path(value).expanduser()
        return self.user_home() / ".snowpea"

    def _dir(self, var: str, *default: str) -> Path:
        value = self.environ.get(var)
        if value:
            path = Path(value)
            if str(path).startswith("~"):
                path = self.user_home() / str(path)[2:]
            return path
        return self.user_home().joinpath(*default)

    def config_dir(self, cli: str) -> Path:
        if cli == "claude":
            return self._dir("CLAUDE_CONFIG_DIR", ".claude")
        if cli == "codex":
            return self._dir("CODEX_HOME", ".codex")
        if cli == "cursor":
            return self._dir("CURSOR_CONFIG_DIR", ".cursor")
        if cli == "agy":
            return self._dir("ANTIGRAVITY_CLI_CONFIG_DIR", ".gemini", "config")
        raise IntegrationError(f"unknown CLI {cli!r}")

    def script_path(self) -> Path:
        name = f"{INTEGRATION_ID}.ps1" if self.windows else f"{INTEGRATION_ID}.sh"
        return self.home() / "hooks" / name

    def state_path(self) -> Path:
        return self.home() / "integrations" / "state.json"

    def cli_installed(self, cli: str) -> bool:
        path = self.environ.get("PATH")
        return any(shutil.which(name, path=path) for name in BINARIES[cli])


def _asset(windows: bool) -> str:
    name = f"{INTEGRATION_ID}.ps1" if windows else f"{INTEGRATION_ID}.sh"
    return resources.files("snowpea_core.terminal.integrations").joinpath(
        "assets", name).read_text(encoding="utf-8")


def script_version(text: str) -> int | None:
    for line in text.splitlines()[:20]:
        body = line.lstrip("#/ ").strip()
        if body.startswith(VERSION_MARKER):
            try:
                return int(body[len(VERSION_MARKER):].strip())
            except ValueError:
                return None
    return None


def hook_command(env: Env, cli: str, action: str) -> str:
    """The exact command string registered for ``cli`` / ``action``."""
    path = str(env.script_path())
    if env.windows:
        if cli == "agy":
            # agy runs hook commands through its own shell; herdr found that only
            # -EncodedCommand survives its quoting on Windows.
            script = "& '{}' {} {}".format(path.replace("'", "''"), cli, action)
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            return f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"
        quoted = '"' + path.replace('"', '\\"') + '"'
        return f"powershell -NoProfile -ExecutionPolicy Bypass -File {quoted} {cli} {action}"
    quoted = "'" + path.replace("'", "'\"'\"'") + "'"
    return f"sh {quoted} {cli} {action}"


def our_commands(env: Env, cli: str) -> set[str]:
    return {hook_command(env, cli, action) for action in ALL_ACTIONS}


# -- state.json ------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_state(env: Env) -> dict[str, Any]:
    try:
        data = json.loads(env.state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", STATE_VERSION)
    if not isinstance(data.get("clis"), dict):
        data["clis"] = {}
    return data


def save_state(env: Env, state: dict[str, Any]) -> None:
    path = env.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fileio.write_text(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


# -- JSON hook editing -------------------------------------------------------------


def _handler(env: Env, cli: str, action: str) -> dict[str, Any]:
    return {"type": "command", "command": hook_command(env, cli, action),
            "timeout": HOOK_TIMEOUT_S}


def _group(env: Env, cli: str, matcher: str | None, action: str) -> dict[str, Any]:
    group: dict[str, Any] = {}
    if matcher is not None:
        group["matcher"] = matcher
    group["hooks"] = [_handler(env, cli, action)]
    return group


def _desired(env: Env, cli: str) -> list[tuple[str, Any]]:
    """``(event, entry)`` pairs the config should hold after install."""
    if cli == "claude":
        return [(e, _group(env, cli, m, a)) for e, m, a in CLAUDE_EVENTS]
    if cli == "codex":
        return [(e, _group(env, cli, m, a)) for e, m, a in CODEX_EVENTS]
    if cli == "cursor":
        return [(e, {"command": hook_command(env, cli, a)}) for e, a in CURSOR_EVENTS]
    return [(e, {"type": "command", "command": hook_command(env, cli, a),
                 "timeout": HOOK_TIMEOUT_S}) for e, a in AGY_EVENTS]


def _agy_block(env: Env) -> dict[str, Any]:
    return {event: [entry] for event, entry in _desired(env, "agy")}


def _entry_commands(cli: str, entry: Any) -> list[str]:
    """Commands an event-array entry runs (a group's handlers, or a flat handler)."""
    if not isinstance(entry, dict):
        return []
    if cli in ("claude", "codex"):
        hooks = entry.get("hooks")
        return [h.get("command") for h in hooks if isinstance(h, dict)
                and isinstance(h.get("command"), str)] if isinstance(hooks, list) else []
    command = entry.get("command")
    return [command] if isinstance(command, str) else []


@dataclass
class _Scan:
    present: set[str] = field(default_factory=set)  # events whose canonical entry is there
    ours: list[tuple[str, int, int | None]] = field(default_factory=list)  # (event, i, j)
    modified: bool = False
    extra: bool = False  # our command on an event / action we no longer register


def _scan(env: Env, cli: str, value: Any) -> _Scan:
    scan = _Scan()
    commands = our_commands(env, cli)
    if not isinstance(value, dict):
        return scan
    if cli == "agy":
        block = value.get(AGY_BLOCK)
        if block is None:
            return scan
        if block == _agy_block(env):
            scan.present = {event for event, _ in AGY_EVENTS}
        else:
            text = json.dumps(block)
            if any(json.dumps(c)[1:-1] in text for c in commands):
                scan.modified = True
            else:
                scan.extra = True
        scan.ours.append((AGY_BLOCK, -1, None))
        return scan
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        return scan
    desired = _desired(env, cli)
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            for j, command in enumerate(_entry_commands(cli, entry)):
                if command not in commands:
                    continue
                scan.ours.append((event, i, j if cli in ("claude", "codex") else None))
                wanted = [d for e, d in desired if e == event
                          and command in _entry_commands(cli, d)]
                if not wanted:
                    scan.extra = True
                elif entry == wanted[0]:
                    scan.present.add(event)
                else:
                    scan.modified = True
    return scan


def _strip_ours(env: Env, cli: str, text: str) -> str:
    """Remove every handler of ours from ``text`` (and the containers they empty)."""
    commands = our_commands(env, cli)
    if cli == "agy":
        value = jsonedit.loads(text)
        if isinstance(value, dict) and AGY_BLOCK in value:
            text = jsonedit.remove_member(text, (), AGY_BLOCK)
        return text
    while True:
        value = jsonedit.loads(text)
        hooks = value.get("hooks") if isinstance(value, dict) else None
        if not isinstance(hooks, dict):
            return text
        edit = None
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            for i, entry in enumerate(entries):
                if cli in ("claude", "codex") and isinstance(entry, dict):
                    handlers = entry.get("hooks")
                    if not isinstance(handlers, list):
                        continue
                    for j, handler in enumerate(handlers):
                        if isinstance(handler, dict) and handler.get("command") in commands:
                            edit = (event, i, j, len(handlers), len(entries))
                            break
                elif any(c in commands for c in _entry_commands(cli, entry)):
                    edit = (event, i, None, 1, len(entries))
                if edit:
                    break
            if edit:
                break
        if edit is None:
            return text
        event, i, j, n_handlers, n_entries = edit
        if j is not None and n_handlers > 1:
            text = jsonedit.remove_item(text, ("hooks", event, i, "hooks"), j)
        elif n_entries > 1:
            text = jsonedit.remove_item(text, ("hooks", event), i)
        else:
            text = jsonedit.remove_member(text, ("hooks",), event)


def _install_json(env: Env, cli: str, text: str, record: dict[str, Any]) -> str:
    """Return ``text`` with our canonical entries (removing stale ones first)."""
    value = jsonedit.loads(text)
    if not isinstance(value, dict):
        raise IntegrationError("the hook config is not a JSON object")
    scan = _scan(env, cli, value)
    desired = _desired(env, cli)
    if not scan.modified and not scan.extra and scan.present == {e for e, _ in desired}:
        return text  # already current: not one byte changes
    text = _strip_ours(env, cli, text)
    if cli == "agy":
        return jsonedit.insert_member(text, (), AGY_BLOCK, _agy_block(env))
    value = jsonedit.loads(text)
    if cli == "cursor" and "version" not in value:
        text = jsonedit.insert_member(text, (), "version", 1)
        record["addedVersionKey"] = True
        value = jsonedit.loads(text)
    if "hooks" not in value:
        text = jsonedit.insert_member(text, (), "hooks", {})
        record["createdHooks"] = True
    elif not isinstance(value["hooks"], dict):
        raise IntegrationError("`hooks` in the config is not an object")
    for event, entry in desired:
        hooks = jsonedit.loads(text)["hooks"]
        if event in hooks:
            if not isinstance(hooks[event], list):
                raise IntegrationError(f"`hooks.{event}` in the config is not a list")
            text = jsonedit.append_item(text, ("hooks", event), entry)
        else:
            text = jsonedit.insert_member(text, ("hooks",), event, [entry])
    return text


def _uninstall_json(env: Env, cli: str, text: str, record: Mapping[str, Any]) -> str:
    text = _strip_ours(env, cli, text)
    value = jsonedit.loads(text)
    if not isinstance(value, dict):
        return text
    if record.get("createdHooks") and value.get("hooks") == {}:
        text = jsonedit.remove_member(text, (), "hooks")
        value = jsonedit.loads(text)
    if cli == "cursor" and record.get("addedVersionKey") and value.get("version") == 1:
        text = jsonedit.remove_member(text, (), "version")
    return text


# -- per-CLI files -------------------------------------------------------------------


def config_files(env: Env, cli: str) -> list[Path]:
    base = env.config_dir(cli)
    if cli == "claude":
        return [base / "settings.json"]
    if cli == "codex":
        return [base / "hooks.json", base / "config.toml"]
    return [base / "hooks.json"]


def _stub(cli: str) -> str:
    return "{}\n"


# -- results -------------------------------------------------------------------------


@dataclass
class IntegrationRow:
    cli: str
    label: str
    status: str
    cliInstalled: bool
    configDir: str
    files: list[str]
    version: int = INTEGRATION_VERSION
    installedVersion: int | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def wire(self) -> dict[str, Any]:
        """The protocol's ``IntegrationRow`` (1.73.0)."""
        return {
            "cli": self.cli,
            "label": self.label,
            "state": self.status,
            "cliInstalled": self.cliInstalled,
            "version": None if self.installedVersion is None else str(self.installedVersion),
            "files": [{"path": path, "exists": os.path.lexists(path)} for path in self.files],
            "detail": self.detail,
        }


@dataclass
class ActionResult:
    cli: str
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    row: IntegrationRow | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "row"}
        out["row"] = self.row.as_dict() if self.row else None
        return out


def _check_cli(cli: str) -> str:
    if cli not in CLIS:
        raise IntegrationError(f"unknown CLI {cli!r}; expected one of {', '.join(CLIS)}")
    return cli


def _write(path: Path, before: str | None, after: str, record: dict[str, Any],
           result: ActionResult) -> None:
    if before == after:
        return
    files = record.setdefault("files", {})
    entry = files.setdefault(str(path), {"created": before is None})
    if before is not None and not entry.get("backup"):
        backup = fileio.backup_once(path, before)
        if backup is not None:
            entry["backup"] = str(backup)
            result.backups.append(str(backup))
    fileio.write_text(path, after)
    result.changed.append(str(path))


def _ensure_script(env: Env, result: ActionResult) -> None:
    path = env.script_path()
    fileio.check_writable(path)
    wanted = _asset(env.windows)
    current = fileio.read_text(path)
    if current == wanted:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fileio.write_text(path, wanted)
    if not env.windows:
        os.chmod(fileio.resolve_target(path), 0o755)
    result.changed.append(str(path))


def install(cli: str, env: Env | None = None) -> ActionResult:
    """Install (or bring up to date) the hook integration of ``cli``."""
    env = env or Env()
    _check_cli(cli)
    result = ActionResult(cli)
    base = env.config_dir(cli)
    if not base.is_dir():
        raise IntegrationError(
            f"{LABELS[cli]}'s config folder was not found at {base}; install and run "
            f"{LABELS[cli]} once first")
    files = config_files(env, cli)
    for path in files:
        fileio.check_writable(path)
    state = load_state(env)
    record: dict[str, Any] = dict(state["clis"].get(cli) or {})
    _ensure_script(env, result)

    hooks_path = files[0]
    before = fileio.read_text(hooks_path)
    # A missing or empty (whitespace-only) file starts as ``{}``.
    original = before if before is not None and before.strip() else _stub(cli)
    try:
        after = _install_json(env, cli, original, record)
    except jsonedit.JsonEditError as exc:
        raise IntegrationError(f"{hooks_path}: {exc}") from exc
    _write(hooks_path, before, after, record, result)
    if before is not None and not before.strip() and after != before:
        record["files"].setdefault(str(hooks_path), {})["blankOriginal"] = before

    if cli == "codex":
        toml_path = files[1]
        toml_before = fileio.read_text(toml_path)
        try:
            toml_after, change = tomledit.enable_hooks(toml_before or "")
        except tomledit.TomlEditError as exc:
            raise IntegrationError(f"{toml_path}: {exc}") from exc
        if change is not None:
            change["created"] = toml_before is None
            record["codexFeature"] = change
            _write(toml_path, toml_before, toml_after, record, result)

    record["version"] = INTEGRATION_VERSION
    record.setdefault("installedAt", _now())
    record["updatedAt"] = _now() if result.changed else record.get("updatedAt", _now())
    if result.changed or state["clis"].get(cli) != record:
        state["clis"][cli] = record
        save_state(env, state)
    result.row = status(cli, env)
    return result


def uninstall(cli: str, env: Env | None = None) -> ActionResult:
    """Remove exactly what :func:`install` added for ``cli``."""
    env = env or Env()
    _check_cli(cli)
    result = ActionResult(cli)
    state = load_state(env)
    record: dict[str, Any] = dict(state["clis"].get(cli) or {})
    file_records: dict[str, Any] = record.get("files") or {}
    files = config_files(env, cli)
    for path in files:
        fileio.check_writable(path)

    hooks_path = files[0]
    text = fileio.read_text(hooks_path)
    if text is not None and text.strip():
        try:
            after = _uninstall_json(env, cli, text, record)
        except jsonedit.JsonEditError as exc:
            raise IntegrationError(f"{hooks_path}: {exc}") from exc
        entry = file_records.get(str(hooks_path)) or {}
        back_to_stub = jsonedit.loads(after) == jsonedit.loads(_stub(cli))
        if back_to_stub and entry.get("created"):
            if fileio.remove_file(hooks_path):
                result.removed.append(str(hooks_path))
        elif back_to_stub and isinstance(entry.get("blankOriginal"), str):
            fileio.write_text(hooks_path, entry["blankOriginal"])
            result.changed.append(str(hooks_path))
        elif after != text:
            fileio.write_text(hooks_path, after)
            result.changed.append(str(hooks_path))

    if cli == "codex" and record.get("codexFeature"):
        toml_path = files[1]
        change = record["codexFeature"]
        toml_text = fileio.read_text(toml_path)
        if toml_text is not None:
            toml_after = tomledit.undo(toml_text, change)
            if change.get("created") and toml_after.strip() == "":
                if fileio.remove_file(toml_path):
                    result.removed.append(str(toml_path))
            elif toml_after != toml_text:
                fileio.write_text(toml_path, toml_after)
                result.changed.append(str(toml_path))

    if cli in state["clis"]:
        del state["clis"][cli]
    if not state["clis"]:
        script = env.script_path()
        if fileio.remove_file(script):
            result.removed.append(str(script))
    if state["clis"] or env.state_path().exists():
        save_state(env, state)
    result.row = status(cli, env)
    return result


def status(cli: str, env: Env | None = None) -> IntegrationRow:
    """``not-installed`` / ``current`` / ``outdated`` / ``modified`` for ``cli``."""
    env = env or Env()
    _check_cli(cli)
    state = load_state(env)
    record = state["clis"].get(cli) or {}
    files = config_files(env, cli)
    row = IntegrationRow(
        cli=cli, label=LABELS[cli], status=STATUS_NOT_INSTALLED,
        cliInstalled=env.cli_installed(cli), configDir=str(env.config_dir(cli)),
        files=[str(env.script_path()), *(str(p) for p in files)],
        installedVersion=record.get("version"))
    try:
        text = fileio.read_text(files[0])
        value = jsonedit.loads(text) if text is not None and text.strip() else {}
    except (OSError, jsonedit.JsonEditError) as exc:
        row.detail = f"{files[0]}: {exc}"
        value = {}
    scan = _scan(env, cli, value)
    if not scan.ours:
        if record:
            row.status = STATUS_MODIFIED
            row.detail = row.detail or "our hook entries were removed from the config"
        return row
    script = fileio.read_text(env.script_path())
    wanted_events = {e for e, _ in _desired(env, cli)}
    problems: list[tuple[str, str]] = []
    if scan.modified:
        problems.append((STATUS_MODIFIED, "a snowpea hook entry was edited"))
    if script is None:
        problems.append((STATUS_OUTDATED, "the hook script is missing"))
    elif script != _asset(env.windows):
        version = script_version(script)
        if version is not None and version < INTEGRATION_VERSION:
            problems.append((STATUS_OUTDATED, f"the hook script is version {version}"))
        else:
            problems.append((STATUS_MODIFIED, "the hook script was edited"))
    if scan.extra or (not scan.modified and scan.present != wanted_events):
        problems.append((STATUS_OUTDATED, "the registered events differ from this version's"))
    if record.get("version") is not None and record["version"] < INTEGRATION_VERSION:
        problems.append((STATUS_OUTDATED, f"installed by integration v{record['version']}"))
    if cli == "codex":
        try:
            enabled = tomledit.hooks_enabled(fileio.read_text(files[1]) or "")
        except (OSError, tomledit.TomlEditError):
            enabled = False
        if not enabled:
            problems.append((STATUS_MODIFIED, "Codex's [features] hooks is off"))
    if not problems:
        row.status = STATUS_CURRENT
        return row
    modified = [d for s, d in problems if s == STATUS_MODIFIED]
    row.status = STATUS_MODIFIED if modified else STATUS_OUTDATED
    row.detail = "; ".join(d for _, d in problems)
    return row


def status_all(env: Env | None = None) -> list[IntegrationRow]:
    env = env or Env()
    return [status(cli, env) for cli in CLIS]


def resolve_targets(name: str | None) -> list[str]:
    """``claude`` → ``["claude"]``; ``all`` / ``None`` → every CLI."""
    if not name or name == "all":
        return list(CLIS)
    return [_check_cli(name)]


#: For callers that want one function per verb.
ACTIONS: dict[str, Callable[..., Any]] = {"install": install, "uninstall": uninstall}
