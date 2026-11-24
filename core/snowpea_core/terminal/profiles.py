"""Terminal profiles and how each is launched (M37 §2.6, §2.9).

Profiles: ``shell``, ``snowpea`` (the snowpea TUI attached to a daemon
session) and the four external CLIs ``claude``, ``codex``, ``agy`` and
``cursor``.  ``terminal.profiles`` lists them (:func:`list_profiles`) and
``terminal.create`` turns a request into a
:class:`~snowpea_core.terminal.manager.TerminalSpec` (:func:`build_spec`).

**Permission options** come from each CLI's mode table
(:mod:`snowpea_core.external.modes`), never from a separate list: every row
the installed CLI offers, other than the CLI's own default, is one option.
Flags were checked against the installed CLIs' ``--help`` (Claude Code 2.1,
Codex 0.170, agy 1.5.9, cursor-agent 2026.11); Codex's ``untrusted`` approval
policy and ``--full-auto`` are gone in 0.170, so they are not offered.

* A *dangerous* option (a row with a bypass flag: Claude Code
  ``--dangerously-skip-permissions``, Codex
  ``--dangerously-bypass-approvals-and-sandbox``, agy
  ``--dangerously-skip-permissions``, cursor ``--yolo``) needs
  ``confirmDangerous`` from the person, then the existing grant path:
  :func:`proc.confirm_person` → :func:`proc.grant_unrestricted` (a ledger
  entry) → :func:`proc.bypass_args`.  It is refused from Jarvis or any tool
  (no connection), while ``web.remote.maxMode`` is below ``auto``, and over
  the web tunnel without the terminal unlock.
* A *person-only* row that is not a bypass (Claude Code ``acceptEdits`` /
  ``auto`` / ``dontAsk``, agy ``accept-edits`` / ``auto``, Codex ``default``)
  takes the same grant (a ledger entry), as ``external.create`` does, but no
  ``confirmDangerous``.
* Every other argument still goes through :func:`proc.assert_safe_args` with
  the adapter's forbidden flags; only the grant's :class:`proc.BypassArgs`
  lets those flags out.

**Launch.**  A CLI runs inside the person's interactive shell, by the
absolute path discovery resolved (an override only once the person
confirmed it): ``[$SHELL, "-ic", '"$0" "$@"; exec $SHELL -i', <cli>, args…]``,
so PATH, nvm and the like hold, and the pane drops back to a shell when the
CLI exits (``terminals.keepShell``).  On Windows the CLI is started directly,
under ``cmd /k`` while ``keepShell`` is on.

**Environment.**  The person's environment minus every ``SNOWPEA_*`` variable
(the daemon token among them), messenger credentials and the switches that
change a nested Claude Code; the terminal manager adds ``TERM``,
``SNOWPEA_TERMINAL_ID``, ``SNOWPEA_TERMINAL_TOKEN`` and
``SNOWPEA_TERMINAL_REPORT``.  The snowpea profile finds its daemon through the
discovery file of ``--home``; the daemon token is never in its argv or env.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import sys
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.external import proc
from snowpea_core.external.modes import NativeMode
from snowpea_core.external.proc import ExternalError
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.terminal.manager import TerminalSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

IS_WINDOWS = sys.platform == "win32"

PROFILE_ORDER: tuple[str, ...] = ("shell", "snowpea", "claude", "codex", "agy", "cursor")
CLI_PROFILES: tuple[str, ...] = ("claude", "codex", "agy", "cursor")
#: Shells whose ``-c`` takes ``"$0" "$@"`` as written.
POSIX_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "ksh", "mksh", "yash"})

#: Who started a terminal from Jarvis's tools.
JARVIS_PARENT = "jarvis"


@dataclass(frozen=True)
class ProfileDef:
    id: str
    label: str
    #: Key names (``terminal/input.py``): submit, interrupt and stop.
    submit_key: str = "enter"
    interrupt_key: str = "escape"
    stop_keys: tuple[str, ...] = ("ctrl+c",)
    #: Keys that answer a permission prompt shown by this CLI (Jarvis, §2.11);
    #: empty: Jarvis never answers one here.
    approve_keys: tuple[str, ...] = ()
    deny_keys: tuple[str, ...] = ()
    rules: str | None = None
    integration: str | None = None
    resumable: bool = False


PROFILES: dict[str, ProfileDef] = {
    "shell": ProfileDef("shell", "Shell", interrupt_key="ctrl+c"),
    "snowpea": ProfileDef("snowpea", "snowpea", interrupt_key="escape"),
    # Claude Code: "1. Yes" is the first choice; Esc answers no.
    "claude": ProfileDef("claude", "Claude Code", approve_keys=("1",), deny_keys=("escape",),
                         rules="claude", integration="claude", resumable=True),
    # Codex: "Yes, proceed (y)"; "No, and tell Codex what to do (esc)".
    "codex": ProfileDef("codex", "Codex", approve_keys=("y",), deny_keys=("escape",),
                        rules="codex", integration="codex", resumable=True),
    "agy": ProfileDef("agy", "Antigravity (agy)", rules="agy", integration="agy",
                      resumable=True),
    "cursor": ProfileDef("cursor", "Cursor agent", rules="cursor", integration="cursor",
                         resumable=True),
}

#: Codex's full-access row has no flag of its own in the table (it is an
#: app-server policy there); in a terminal it is this flag.
CODEX_BYPASS = ("--dangerously-bypass-approvals-and-sandbox",)
#: Codex rows as terminal flags (Codex 0.170 ``--help``): ``-a`` takes only
#: ``on-request`` or ``never`` now, so ``untrusted`` has no flag.
CODEX_ARGS: dict[str, tuple[str, ...]] = {
    "read-only": ("--sandbox=read-only",),
    "default": ("--sandbox=workspace-write", "--ask-for-approval=on-request"),
}


@dataclass(frozen=True)
class OptionDef:
    """One permission option: a native mode row as terminal flags."""

    id: str
    label: str
    args: tuple[str, ...]
    dangerous: bool
    #: Chosen only by a person, through a grant (a ledger entry).
    person_only: bool
    #: Options of one group exclude each other (all permission modes are one).
    group: str = "mode"

    def wire(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "args": list(self.args),
                "dangerous": self.dangerous}


def option_of(cli: str, row: NativeMode) -> OptionDef | None:
    """``row`` as terminal flags, or ``None`` (the CLI's default, or no flag for it)."""
    launch = dict(row.launch or {})
    bypass = tuple(launch.get("bypass") or ())
    if cli == "codex":
        if row.legacy == "unrestricted":
            bypass = CODEX_BYPASS
        args = bypass or CODEX_ARGS.get(row.id, ())
    elif bypass:
        args = bypass
    elif cli == "claude":
        mode = launch.get("permissionMode")
        args = (f"--permission-mode={mode}",) if mode and mode != "default" else ()
    elif cli == "agy":
        mode = launch.get("acpMode")
        args = (f"--mode={mode}",) if mode and mode != "default" else ()
    else:
        args = tuple(f"{a}={b}" for a, b in _pairs(launch.get("args") or ()))
    if not args:
        return None
    return OptionDef(row.id, row.label, tuple(args), dangerous=bool(bypass),
                     person_only=bool(row.person_only or bypass))


def _pairs(args: Sequence[str]) -> list[tuple[str, str]]:
    items = list(args)
    return [(items[i], items[i + 1]) for i in range(0, len(items) - 1, 2)]


def options_for(cli: str, rows: Iterable[NativeMode]) -> list[OptionDef]:
    out: list[OptionDef] = []
    for row in rows:
        option = option_of(cli, row)
        if option is not None and all(o.id != option.id for o in out):
            out.append(option)
    return out


# ---------------------------------------------------------------------------
# what is installed
# ---------------------------------------------------------------------------


@dataclass
class CliState:
    """A CLI as discovery found it: its confirmed path and the modes it offers."""

    path: str | None
    rows: tuple[NativeMode, ...] = ()
    problems: list[str] = field(default_factory=list)
    forbidden: tuple[str, ...] = ()
    forbidden_pairs: tuple[tuple[str, str], ...] = ()


async def cli_state(core: Core, cli: str) -> CliState:
    """Discovery of ``cli`` (the external registry; a path override only once confirmed)."""
    from snowpea_core.server.external_handlers import ensure_manager

    manager = ensure_manager(core)
    adapter = manager.registry.get(cli)
    if adapter is None:
        return CliState(None, problems=[f"{cli} is not supported by this snowpea core"])
    try:
        found = await manager.discover(cli)
    except Exception as exc:  # noqa: BLE001 - not installed, as far as a profile goes
        return CliState(None, adapter.native_modes(None), [str(exc)])
    path = None
    if found is not None and found.installed and found.executable is not None:
        path = found.executable.path
    return CliState(
        path,
        adapter.native_modes(found),
        list(found.problems) if found is not None else [],
        tuple(adapter.forbidden_args),
        tuple(adapter.forbidden_pairs),
    )


def snowpea_bin() -> str | None:
    """The ``snowpea`` launcher of this install (next to this Python first)."""
    name = "snowpea.exe" if IS_WINDOWS else "snowpea"
    beside = Path(sys.executable).parent / name
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    found = shutil.which("snowpea")
    return str(Path(found).resolve()) if found else None


def shell_path() -> str:
    return os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"


def shell_argv() -> list[str]:
    from snowpea_core.server.terminal_handlers import shell_argv as base

    return base()


# ---------------------------------------------------------------------------
# environment and launch
# ---------------------------------------------------------------------------


def terminal_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The person's environment minus snowpea's secrets (every ``SNOWPEA_*``)."""
    source = dict(os.environ if base is None else base)
    return {
        key: value for key, value in source.items()
        if not key.upper().startswith(proc.SCRUB_PREFIXES) and key not in proc.SCRUB_EXACT
    }


def launch_argv(argv: Sequence[str], *, keep_shell: bool, shell: str | None = None) -> list[str]:
    """``argv`` run inside the person's interactive shell (§2.6)."""
    argv = [str(a) for a in argv]
    if IS_WINDOWS:  # pragma: no cover - Windows only
        if keep_shell:
            return [os.environ.get("COMSPEC") or "cmd.exe", "/k", *argv]
        return argv
    user_shell = shell or shell_path()
    wrapper = user_shell if Path(user_shell).name in POSIX_SHELLS else (
        shutil.which("bash") or "/bin/sh"
    )
    flags = "-lic" if sys.platform == "darwin" else "-ic"
    script = (f'"$0" "$@"; exec {shlex.quote(user_shell)} -i' if keep_shell
              else 'exec "$0" "$@"')
    return [wrapper, flags, script, *argv]


def _keep_shell(core: Core) -> bool:
    return bool(getattr(getattr(core.settings, "terminals", None), "keepShell", True))


# ---------------------------------------------------------------------------
# the dangerous-option gate
# ---------------------------------------------------------------------------


def _tunnel_refusal(core: Core, conn: Any) -> str | None:
    """``terminal_locked`` / ``max_mode`` for a tunnel connection (T4's gate when present)."""
    try:
        from snowpea_core.server.tunnel import (  # type: ignore[import-not-found]
            require_terminal_unlock,
        )
    except ImportError:  # no tunnel in this core: nothing comes over one
        return "terminal_locked" if getattr(conn, "via_tunnel", False) else None
    try:
        require_terminal_unlock(conn, dangerous=True, core=core)
    except RpcError as exc:
        reason = exc.data.get("reason") if isinstance(exc.data, dict) else None
        return "max_mode" if reason == "tunnel_dangerous" else "terminal_locked"
    return None


def _max_mode(core: Core) -> str:
    remote = getattr(getattr(core.settings, "web", None), "remote", None)
    return str(getattr(remote, "maxMode", "auto") or "auto")


def dangerous_refusal(core: Core, conn: Any) -> str | None:
    """Why ``conn`` may not start a dangerous option now (``TerminalProfile.dangerousRefusal``).

    ``person_only``: no person's local connection (Jarvis, a tool, a browser,
    a remote web login); ``terminal_locked``: over the web tunnel without the
    unlock; ``max_mode``: ``web.remote.maxMode`` is below ``auto``.  ``None``:
    allowed, with ``confirmDangerous``.
    """
    if conn is None:
        return "person_only"
    tunnel = _tunnel_refusal(core, conn)
    if tunnel is not None:
        return tunnel
    if _max_mode(core) != "auto":
        return "max_mode"
    try:
        proc.confirm_person(conn, core.settings)
    except ExternalError:
        return "person_only"
    return None


_REFUSALS = {
    "person_only": "only the person, at a local IDE, TUI or web page, may start a terminal "
                   "with a dangerous option; Jarvis and tools never can",
    "terminal_locked": "this connection comes through the web tunnel: unlock terminals first "
                       "(web.tunnel.unlock)",
    "max_mode": "dangerous options are off while web.remote.maxMode is below auto",
}


def _refuse_dangerous(reason: str) -> RpcError:
    code = errors.JARVIS_PERSON_ONLY if reason == "person_only" else errors.UNAVAILABLE
    return RpcError(code, _REFUSALS[reason], {"reason": reason})


# ---------------------------------------------------------------------------
# terminal.profiles
# ---------------------------------------------------------------------------


async def list_profiles(core: Core, conn: Any) -> list[dict[str, Any]]:
    refusal = dangerous_refusal(core, conn)
    out: list[dict[str, Any]] = []
    for profile_id in PROFILE_ORDER:
        profile = PROFILES[profile_id]
        row: dict[str, Any] = {
            "id": profile_id, "label": profile.label, "installed": False, "path": None,
            "resumable": profile.resumable, "integration": profile.integration,
            "options": [], "dangerousRefusal": None,
        }
        if profile_id == "shell":
            argv = shell_argv()
            row.update(installed=True, path=argv[0])
        elif profile_id == "snowpea":
            path = snowpea_bin()
            row.update(installed=path is not None, path=path, resumable=True)
        else:
            state = await cli_state(core, profile_id)
            options = options_for(profile_id, state.rows)
            row.update(installed=state.path is not None, path=state.path,
                       options=[o.wire() for o in options])
            if any(o.dangerous for o in options):
                row["dangerousRefusal"] = refusal
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# terminal.create
# ---------------------------------------------------------------------------


def existing(core: Core, params: Any) -> dict[str, Any] | None:
    """A live terminal ``terminal.create`` reuses instead of starting one (§2.9).

    ``{profile: "snowpea", snowpeaSessionId}`` with a terminal of that session
    still running returns that terminal.
    """
    if params.profile != "snowpea" or not params.snowpeaSessionId:
        return None
    manager = getattr(core, "terminals", None)
    if manager is None:
        return None
    for term in manager.terminals():
        if term.alive and term.spec.snowpea_session_id == params.snowpeaSessionId:
            return term.info()  # type: ignore[no-any-return]
    return None


def _choose(cli: str, ids: Sequence[str], offered: Sequence[OptionDef]) -> list[OptionDef]:
    by_id = {o.id: o for o in offered}
    chosen: list[OptionDef] = []
    groups: set[str] = set()
    for option_id in ids:
        option = by_id.get(option_id)
        if option is None:
            raise RpcError(errors.INVALID_PARAMS,
                           f"{cli} has no option {option_id!r} (see terminal.profiles)")
        if option.group in groups:
            raise RpcError(errors.INVALID_PARAMS,
                           f"choose one permission option for {cli}, not several")
        groups.add(option.group)
        chosen.append(option)
    return chosen


async def _root_of(core: Core, cwd: str) -> str | None:
    from snowpea_core.server.external_handlers import _roots

    roots = [str(Path(r).resolve()) for r in await _roots(core)]
    return proc.project_of(cwd, roots)


async def _snowpea_spec(core: Core, conn: Any, params: Any, cwd: str) -> TerminalSpec:
    if params.options or params.confirmDangerous:
        raise RpcError(errors.INVALID_PARAMS, "the snowpea profile has no options")
    path = snowpea_bin()
    if path is None:
        raise RpcError(errors.UNAVAILABLE, "the snowpea command was not found on this machine")
    argv = [path, "--home", str(core.paths.home)]
    session_id = params.snowpeaSessionId
    if session_id:
        from snowpea_core.server.external_handlers import _workdir_fn

        workdir = await _workdir_fn(core)(session_id)
        if workdir is None:
            raise RpcError(errors.NOT_FOUND, f"no session {session_id}")
        cwd = workdir
        argv += ["--session", session_id]
    argv += ["--cwd", cwd]
    return TerminalSpec(
        argv=launch_argv(argv, keep_shell=_keep_shell(core)), env=terminal_env(), cwd=cwd,
        profile="snowpea", snowpea_session_id=session_id, title=params.title,
    )


async def build_spec(core: Core, conn: Any, params: Any, cwd: str) -> TerminalSpec:
    """``terminal.create`` (and ``external.open|create {transport: "pty"}``) → a launch."""
    profile = params.profile
    if profile not in PROFILES:
        raise RpcError(errors.INVALID_PARAMS, f"unknown profile {profile!r}")
    if profile == "shell":
        if params.options or params.confirmDangerous:
            raise RpcError(errors.INVALID_PARAMS, "the shell profile has no options")
        return TerminalSpec(argv=shell_argv(), env=terminal_env(), cwd=cwd, profile="shell")
    if profile == "snowpea":
        return await _snowpea_spec(core, conn, params, cwd)
    return await _cli_spec(core, conn, params, cwd)


async def _cli_spec(core: Core, conn: Any, params: Any, cwd: str) -> TerminalSpec:
    cli = params.profile
    definition = PROFILES[cli]
    state = await cli_state(core, cli)
    if state.path is None:
        raise RpcError(errors.EXTERNAL_UNAVAILABLE,
                       f"{definition.label} was not found on this machine",
                       {"reason": "not_installed", "problems": state.problems[:3]})
    chosen = _choose(cli, list(params.options or []), options_for(cli, state.rows))
    resume = getattr(params, "resumeSessionId", None)
    model = getattr(params, "model", None)
    try:
        if resume:
            proc.validate_session_id(resume)
        proc.validate_model(model)
    except ExternalError as exc:
        raise RpcError(exc.code, exc.message) from exc
    root = await _root_of(core, cwd)
    # The CLI loads the project's own settings and hooks: the person trusts it once.
    from snowpea_core.server.external_handlers import ensure_manager

    try:
        ensure_manager(core)._require_trust(root, cwd, conn, bool(getattr(params, "trust", False)))
    except ExternalError as exc:
        raise RpcError(exc.code, exc.message, exc.details) from exc
    dangerous = any(o.dangerous for o in chosen)
    granted = [o for o in chosen if o.person_only]
    plain = [o for o in chosen if not o.person_only]
    grant = None
    if dangerous:
        if not params.confirmDangerous:
            raise RpcError(errors.INVALID_PARAMS,
                           "a dangerous option needs confirmDangerous after the person "
                           "confirmed the risk", {"reason": "confirm_dangerous"})
        refusal = dangerous_refusal(core, conn)
        if refusal is not None:
            raise _refuse_dangerous(refusal)
    if granted:
        grant = _grant(core, conn, cli, resume, root, granted)
    session_id = resume
    lead: list[str] = []
    tail: list[str] = []
    if cli == "claude":
        if resume:
            tail = ["--resume", resume]
        else:
            # Known from the start, so the terminal links to its session at once.
            session_id = str(uuid.uuid4())
            tail = ["--session-id", session_id]
    elif cli == "codex":
        if resume:
            lead, tail = ["resume"], [resume]
    elif cli == "agy":
        if resume:
            tail = ["--conversation", resume]
    elif cli == "cursor" and resume:
        tail = ["--resume", resume]
    middle = [a for o in plain for a in o.args] + (["--model", model] if model else [])
    flags = [a for o in granted for a in o.args]
    try:
        executable = proc.PinnedExecutable(state.path)
        proc.build_argv(executable, [*lead, *middle, *tail], forbidden=state.forbidden,
                        forbidden_pairs=state.forbidden_pairs)
        released = proc.bypass_args(grant, flags).flags if grant is not None else ()
    except ExternalError as exc:
        raise RpcError(exc.code, exc.message) from exc
    argv = [state.path, *lead, *middle, *released, *tail]
    labels: dict[str, str] = {}
    if grant is not None:
        labels["snowpea.grant"] = grant.ledger_id
    return TerminalSpec(
        argv=launch_argv(argv, keep_shell=_keep_shell(core)), env=terminal_env(), cwd=cwd,
        profile=cli, options=[o.id for o in chosen], dangerous=dangerous,
        external={"cli": cli, "sessionId": session_id} if session_id else {"cli": cli},
        submit_key=definition.submit_key, rules=definition.rules, labels=labels,
        title=params.title,
    )


def _grant(core: Core, conn: Any, cli: str, session_id: str | None, root: str | None,
           options: Sequence[OptionDef]) -> Any:
    """The person's grant for person-only options (a ledger entry), or a refusal."""
    from snowpea_core.server.external_handlers import _ledger_fn

    try:
        confirmation = proc.confirm_person(conn, core.settings)
    except ExternalError as exc:
        raise RpcError(exc.code, exc.message, {"reason": "person_only"}) from exc
    dangerous = any(o.dangerous for o in options)
    label = ", ".join(o.label for o in options)
    try:
        return proc.grant_unrestricted(
            confirmation, cli=cli, session_id=session_id, project=root,
            action="open" if session_id else "create", home=core.paths.home,
            ledger=_ledger_fn(core),
            mode_label=None if dangerous else f"{label} (terminal)",
        )
    except ExternalError as exc:
        raise RpcError(exc.code, exc.message) from exc


def profile_def(profile_id: str) -> ProfileDef | None:
    return PROFILES.get(profile_id)


def resumable_session(info: dict[str, Any]) -> tuple[str, str] | None:
    """``(cli, sessionId)`` a lost or ended CLI terminal can be resumed from (§2.8)."""
    external = info.get("external") or {}
    cli, session_id = external.get("cli"), external.get("sessionId")
    if cli in CLI_PROFILES and session_id:
        with contextlib.suppress(ExternalError):
            proc.validate_session_id(session_id)
            return str(cli), str(session_id)
    return None


__all__ = [
    "CLI_PROFILES",
    "JARVIS_PARENT",
    "PROFILES",
    "PROFILE_ORDER",
    "CliState",
    "OptionDef",
    "ProfileDef",
    "build_spec",
    "cli_state",
    "dangerous_refusal",
    "existing",
    "launch_argv",
    "list_profiles",
    "option_of",
    "options_for",
    "profile_def",
    "resumable_session",
    "snowpea_bin",
    "terminal_env",
]
