"""Process safety for external agent CLIs (M19 WP5).

Every adapter starts its CLI through this module and nothing else, so the
rules hold for all four:

* argv lists only, never a shell; the executable is resolved once to an
  absolute path and pinned (:class:`PinnedExecutable`);
* prompt text goes over stdin (:meth:`ExternalProcess.write_line`), never
  into argv; the one CLI that cannot read stdin gets it after ``--``
  (:func:`build_argv` ``prompt_arg``);
* session ids, model names and modes are checked against strict patterns;
* the working directory must be a registered snowpea project
  (:func:`validate_cwd`);
* the environment is scrubbed of the daemon token and snowpea's secrets
  (:func:`scrubbed_env`);
* at most ``jarvis.external.maxProcesses`` CLI processes run at once, each in
  its own process group, killed as a tree on interrupt, close and daemon
  shutdown, with pid files under ``$SNOWPEA_HOME/external/`` so a daemon that
  died is cleaned up after on the next start (:class:`ProcessSupervisor`);
* a permission-bypass flag reaches argv only through :func:`bypass_args`,
  which needs an :class:`UnrestrictedGrant`, which only
  :func:`grant_unrestricted` makes, which needs a :class:`PersonConfirmation`,
  which only :func:`confirm_person` makes from a human connection.  Every
  grant writes a ledger entry.  Any other argument that looks like a bypass
  is refused.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import functools
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.config.paths import utc_now
from snowpea_core.server import errors

log = logging.getLogger("snowpea.external.proc")

IS_WINDOWS = sys.platform == "win32"

#: The three ways snowpea runs an external session (protocol ``ExternalSessionMode``).
MODES: frozenset[str] = frozenset({"default", "readonly", "unrestricted"})

#: A CLI's own session/thread/chat id: uuids and uuidv7s in practice.  No
#: leading dash (it would read as a flag), no dots or slashes (paths).
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
#: A model id or alias: ``haiku``, ``gpt-5.3-codex``, ``openai/gpt-5``,
#: ``claude-opus-4@2026``.  No leading dash, no whitespace, no ``..``.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
#: Anything that reads like a permission bypass, as a flag or as a value
#: (``--dangerously-skip-permissions``, ``--yolo``, ``bypassPermissions``,
#: ``danger-full-access``, ``--approve-for-me``, ``isRunEverything``).
DANGEROUS_RE = re.compile(
    r"danger|yolo|bypass|skip[-_]?permission|approve[-_]?for[-_]?me|full[-_]?access"
    r"|run[-_]?everything",
    re.IGNORECASE,
)

#: Client kinds whose connection is a person at a screen (``system.hello``):
#: one list for the whole daemon, the delegation engine's
#: (:data:`snowpea_core.jarvis.delegation.HUMAN_CLIENT_KINDS`).  The desktop
#: app and the TUI send no ``clientKind``; a browser host, the remote
#: connector and unknown clients are not a person.
from snowpea_core.jarvis.delegation import HUMAN_CLIENT_KINDS  # noqa: E402

#: Environment variables a CLI child never inherits.  Every ``SNOWPEA_*``
#: variable (the daemon token, registry and studio keys, test switches) goes,
#: plus messenger bot credentials and the switches that silently change how
#: Claude Code behaves when the daemon itself was started from one.
SCRUB_PREFIXES: tuple[str, ...] = (
    "SNOWPEA_",
    "TELEGRAM_",
    "SLACK_",
    "DISCORD_",
    "MATRIX_",
    "WHATSAPP_",
    "KAKAO_",
    "LINE_CHANNEL_",
)
SCRUB_EXACT: frozenset[str] = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_SAFE_MODE",
        "CLAUDE_CODE_SSE_PORT",
    }
)

#: Environment variables a CLI child inherits (security review L3: an
#: allowlist, not a blocklist).  Each CLI's own config-dir variable is here
#: (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``); credentials are not.
ALLOW_EXACT: frozenset[str] = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "COLORTERM",
        "TZ", "TMPDIR", "TEMP", "TMP", "NO_COLOR", "FORCE_COLOR",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
        "NO_PROXY",
        "CLAUDE_CONFIG_DIR", "CODEX_HOME",
        "TMUX_TMPDIR", "__CF_USER_TEXT_ENCODING",
        # Windows: what a process needs to start and find its profile.
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA",
        "LOCALAPPDATA", "USERPROFILE", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "PROGRAMW6432", "COMMONPROGRAMFILES", "HOMEDRIVE", "HOMEPATH", "USERNAME",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    }
)
ALLOW_PREFIXES: tuple[str, ...] = ("LC_", "XDG_")
#: Proxy variables pass only without credentials in them (no ``user:pass@``).
PROXY_VARS: frozenset[str] = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"})
#: Test seam: prefixes the test suite's fake CLIs read (``FAKE_``); empty in production.
EXTRA_ALLOW_PREFIXES: list[str] = []
#: A variable name ``passEnv`` may list.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

#: Longest stdout line kept (bytes); a longer one is dropped whole.
MAX_LINE_BYTES = 16 * 1024 * 1024
#: Seconds between SIGTERM and SIGKILL when a process tree is stopped.
KILL_GRACE_SEC = 2.0
#: Seconds ``--version`` may take.
VERSION_TIMEOUT_SEC = 10.0
#: Short-lived helper processes (``agy models``, ``cursor-agent --list-models``,
#: ``claude auth status``, a token-free ``agy acp`` for a listing) that may run at
#: once; more wait for a slot.  They do not count against ``maxProcesses``.
MAX_HELPERS = 6


class ExternalError(Exception):
    """A refusal with a protocol error code; handlers turn it into an ``RpcError``."""

    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def validate_session_id(value: str) -> str:
    """A CLI session id, or ``invalid_params``."""
    if not isinstance(value, str) or not SESSION_ID_RE.fullmatch(value):
        raise ExternalError(errors.INVALID_PARAMS, "invalid external session id")
    return value


def validate_model(value: str | None) -> str | None:
    """A model id (or ``None``), or ``invalid_params``: never a flag, never a bypass."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not MODEL_RE.fullmatch(value)
        or ".." in value
        or DANGEROUS_RE.search(value)
    ):
        raise ExternalError(errors.INVALID_PARAMS, "invalid model name")
    return value


def validate_mode(value: str | None, allowed: Iterable[str] | None = None) -> str | None:
    """``default | readonly | unrestricted`` (or ``None``); ``allowed`` narrows it per CLI."""
    if value is None:
        return None
    if value not in MODES:
        raise ExternalError(errors.INVALID_PARAMS, f"unknown mode {value!r}")
    if allowed is not None and value not in set(allowed):
        raise ExternalError(errors.INVALID_PARAMS, f"this CLI does not offer mode {value!r}")
    return value


def is_dangerous(token: str) -> bool:
    """True for anything that reads like a permission bypass."""
    return bool(DANGEROUS_RE.search(token))


def assert_safe_args(
    args: Sequence[str],
    *,
    forbidden: Iterable[str] = (),
    forbidden_pairs: Iterable[tuple[str, str]] = (),
) -> None:
    """Refuse any argument that could bypass a permission, in any position.

    ``forbidden`` adds exact tokens a CLI treats as a bypass without saying so
    (cursor's ``-f``/``--force``); ``forbidden_pairs`` adds ``(flag, value)``
    combinations such as codex's ``("-a", "never")``.  ``--flag=value`` is
    checked as both halves.
    """
    banned = set(forbidden)
    pairs = {(flag, value.lower()) for flag, value in forbidden_pairs}
    previous: str | None = None
    for index, arg in enumerate(args):
        if not isinstance(arg, str):
            raise ExternalError(errors.INVALID_PARAMS, f"argument {index} is not a string")
        if "\x00" in arg:
            raise ExternalError(errors.INVALID_PARAMS, f"argument {index} contains NUL")
        parts = [arg]
        if arg.startswith("-") and "=" in arg:
            parts.extend(arg.split("=", 1))
        for part in parts:
            if is_dangerous(part) or part in banned:
                raise ExternalError(
                    errors.INVALID_PARAMS,
                    "refused a permission-bypass argument; unrestricted mode is chosen by a "
                    "person, never passed as an argument",
                )
        if arg.startswith("-") and "=" in arg:
            flag, value = arg.split("=", 1)
            if (flag, value.lower()) in pairs:
                raise ExternalError(errors.INVALID_PARAMS, f"refused {flag} {value}")
        if previous is not None and (previous, arg.lower()) in pairs:
            raise ExternalError(errors.INVALID_PARAMS, f"refused {previous} {arg}")
        previous = arg


def scan_payload(value: Any, *, path: str = "") -> None:
    """Refuse a structured request (JSON-RPC params) carrying a bypass value.

    For CLIs driven over a JSON protocol (codex app-server, agy ACP) the
    bypass is a value, not a flag; adapters run their outgoing params through
    this unless the values came from :func:`unrestricted_values`.
    """
    if isinstance(value, str):
        if is_dangerous(value):
            raise ExternalError(errors.INVALID_PARAMS, f"refused a bypass value at {path or '.'}")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and is_dangerous(key):
                raise ExternalError(errors.INVALID_PARAMS, f"refused a bypass key {key!r}")
            scan_payload(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            scan_payload(item, path=f"{path}[{index}]")


def _wide_root(path: Path) -> bool:
    """A folder too wide to stand for "everything under it" (``/``, home, above home)."""
    home = Path.home().resolve()
    return path.parent == path or path == home or path in home.parents


def validate_cwd(cwd: str | os.PathLike[str], roots: Iterable[str | os.PathLike[str]]) -> str:
    """The real path of ``cwd`` when it is a registered project root or inside one.

    A root that is a filesystem root or the home folder (or above it) only
    matches itself exactly.  Symbolic links are resolved first, so a link
    inside a project that points out of it is refused.
    """
    try:
        real = Path(cwd).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExternalError(errors.OUTSIDE_ROOT, "working directory does not exist") from exc
    if not real.is_dir():
        raise ExternalError(errors.OUTSIDE_ROOT, "working directory is not a folder")
    for root in roots:
        try:
            base = Path(root).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if real == base or (not _wide_root(base) and base in real.parents):
            return str(real)
    raise ExternalError(
        errors.OUTSIDE_ROOT, "working directory is not inside a registered snowpea project"
    )


def project_of(cwd: str, roots: Iterable[str]) -> str | None:
    """The most specific registered root containing ``cwd`` (no filesystem access)."""
    best: str | None = None
    try:
        target = Path(cwd)
    except (TypeError, ValueError):
        return None
    for root in roots:
        base = Path(root)
        if target == base or (not _wide_root(base) and base in target.parents):
            if best is None or len(str(base)) > len(best):
                best = str(base)
    return best


# ---------------------------------------------------------------------------
# person confirmation, unrestricted grants, ledger
# ---------------------------------------------------------------------------

#: Only this module mints confirmations, grants and bypass arguments.
_MINT = object()


@dataclass(frozen=True)
class PersonConfirmation:
    """Proof that a human connection made the current call (see :func:`confirm_person`)."""

    client_kind: str
    client_id: str | None
    surface: str
    ts: str
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise ExternalError(errors.JARVIS_PERSON_ONLY, "only a person may confirm this")


def _open_connection(conn: Any) -> bool:
    return (
        conn is not None
        and bool(getattr(conn, "authenticated", False))
        and not bool(getattr(conn, "closed", False))
    )


def is_human_connection(conn: Any) -> bool:
    """True for an authenticated, open connection a person sits behind.

    The same rule as the delegation engine's
    (:func:`snowpea_core.jarvis.delegation.person_refusal` with
    :meth:`~snowpea_core.jarvis.delegation.Caller.from_connection`): IDE, web,
    TUI, CLI, the desktop app and clients that send no ``clientKind``.
    """
    from snowpea_core.jarvis.delegation import Caller, person_refusal

    if not _open_connection(conn):
        return False
    return person_refusal(Caller.from_connection(conn), None, guarded=False) is None


def confirm_person(conn: Any, settings: Any = None) -> PersonConfirmation:
    """A :class:`PersonConfirmation` for ``conn``, or ``jarvis_person_only``.

    The delegation engine's person rule with ``guarded=True``: a remote web
    login (``RpcConnection.remote_login``) may not open unrestricted mode, as it
    may not hand guarded classes to Jarvis.  In-process callers (Jarvis,
    schedules) pass ``None`` and are refused; the gateway mints its own
    through :func:`person_confirmation_from_gateway`.
    """
    from snowpea_core.jarvis.delegation import Caller, person_refusal

    if not _open_connection(conn):
        raise ExternalError(
            errors.JARVIS_PERSON_ONLY,
            "only a person at the IDE, web or TUI may choose unrestricted mode",
        )
    refusal = person_refusal(Caller.from_connection(conn), settings, guarded=True)
    if refusal is not None:
        raise ExternalError(
            errors.JARVIS_PERSON_ONLY, f"only a person may choose unrestricted mode: {refusal}"
        )
    kind = str(getattr(conn, "client_kind", None) or "local")
    return PersonConfirmation(
        client_kind=kind,
        client_id=getattr(conn, "client_id", None),
        surface=kind,
        ts=utc_now(),
        _mint=_MINT,
    )


def person_confirmation_from_gateway(
    platform: str, *, bound_person: bool, settings: Any
) -> PersonConfirmation:
    """The messenger seam (M19 WP7): a confirmation for the person bound to a chat.

    The gateway calls this only after it checked the approver of the binding
    (``bound_person``).  Refused (``jarvis_person_only``) for anyone else and
    while ``jarvis.messenger.allowGrants`` is off — the same rule as a grant
    confirmed from a messenger.
    """
    from snowpea_core.jarvis.delegation import Caller, person_refusal

    name = str(platform or "").strip() or "messenger"
    refusal = person_refusal(
        Caller.messenger(name, bound_person=bound_person), settings, guarded=True
    )
    if refusal is not None:
        raise ExternalError(
            errors.JARVIS_PERSON_ONLY, f"only a person may choose unrestricted mode: {refusal}"
        )
    return PersonConfirmation(
        client_kind="messenger",
        client_id=name,
        surface=f"messenger:{name}",
        ts=utc_now(),
        _mint=_MINT,
    )


@dataclass(frozen=True)
class UnrestrictedGrant:
    """A person chose ``unrestricted`` for one session; the ledger has the entry."""

    cli: str
    session_id: str | None
    project: str | None
    action: str
    confirmation: PersonConfirmation
    ledger_id: str
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise ExternalError(errors.JARVIS_PERSON_ONLY, "unrestricted needs a person's grant")


#: ``callback(entry)`` that records one ledger entry (a ``JarvisDecision`` dict);
#: it may return the id the ledger gave the entry.
LedgerHook = Callable[[dict[str, Any]], Any]
_ledger_hook: LedgerHook | None = None


def set_ledger_hook(hook: LedgerHook | None) -> None:
    """Install the Jarvis ledger writer (WP2); ``None`` restores the default file."""
    global _ledger_hook
    _ledger_hook = hook


def default_ledger(home: Path) -> LedgerHook:
    """Append entries to ``$SNOWPEA_HOME/jarvis/ledger.jsonl`` (owner-only file)."""
    target = Path(home) / "jarvis" / "ledger.jsonl"

    def write(entry: dict[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(target.parent, 0o700)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return write


def grant_unrestricted(
    confirmation: PersonConfirmation,
    *,
    cli: str,
    session_id: str | None,
    project: str | None,
    action: str,
    home: Path,
    ledger: LedgerHook | None = None,
) -> UnrestrictedGrant:
    """Record the person's choice in the ledger and return the grant.

    ``action`` is ``create``, ``open`` or ``setMode``.  ``ledger`` is the
    daemon's writer (the delegation engine's ledger); without one the hook
    installed with :func:`set_ledger_hook`, else the default file.  The ledger
    write is part of the grant: if it fails, there is no grant.
    """
    if not isinstance(confirmation, PersonConfirmation) or confirmation._mint is not _MINT:
        raise ExternalError(errors.JARVIS_PERSON_ONLY, "unrestricted needs a person's grant")
    entry_id = f"jl-{uuid.uuid4().hex[:12]}"
    entry: dict[str, Any] = {
        "id": entry_id,
        "ts": utc_now(),
        "sessionId": None,
        "external": {"cli": cli, "id": session_id} if session_id else None,
        "project": project,
        "kind": "unrestricted",
        "importance": "high",
        "summary": (
            f"{cli} session {'created' if action == 'create' else 'switched'} to unrestricted "
            "mode: snowpea does not control what it runs"
        ),
        "decision": action,
        "decidedBy": "person",
        "reason": f"chosen on {confirmation.surface}",
        "by": confirmation.surface,
    }
    hook = ledger or _ledger_hook or default_ledger(home)
    try:
        written = hook(entry)
    except Exception as exc:  # noqa: BLE001 - no ledger entry, no grant
        log.warning("could not write the unrestricted ledger entry", exc_info=True)
        raise ExternalError(
            errors.INTERNAL, "could not record unrestricted mode in the ledger"
        ) from exc
    return UnrestrictedGrant(
        cli=cli,
        session_id=session_id,
        project=project,
        action=action,
        confirmation=confirmation,
        ledger_id=written if isinstance(written, str) and written else entry_id,
        _mint=_MINT,
    )


@dataclass(frozen=True)
class BypassArgs:
    """Bypass flags released by a grant; only :func:`build_argv` consumes them."""

    flags: tuple[str, ...]
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise ExternalError(errors.JARVIS_PERSON_ONLY, "bypass flags need a person's grant")


def _check_grant(grant: Any) -> UnrestrictedGrant:
    if not isinstance(grant, UnrestrictedGrant) or grant._mint is not _MINT:
        raise ExternalError(errors.JARVIS_PERSON_ONLY, "unrestricted needs a person's grant")
    return grant


def bypass_args(grant: UnrestrictedGrant, flags: Sequence[str]) -> BypassArgs:
    """The only way a bypass flag (``--dangerously-skip-permissions``, ``--yolo``) gets out."""
    _check_grant(grant)
    for flag in flags:
        if not isinstance(flag, str) or not flag.startswith("-") or "\x00" in flag:
            raise ExternalError(errors.INVALID_PARAMS, "a bypass flag must be a plain flag")
    return BypassArgs(flags=tuple(flags), _mint=_MINT)


def unrestricted_values(grant: UnrestrictedGrant, values: Mapping[str, Any]) -> dict[str, Any]:
    """Bypass *values* for a JSON protocol (codex ``approvalPolicy: never``), behind a grant."""
    _check_grant(grant)
    return dict(values)


# ---------------------------------------------------------------------------
# executables and argv
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PinnedExecutable:
    """An absolute, resolved executable path, fixed at discovery."""

    path: str
    version: str | None = None


def resolve_executable(name: str, override: str | None = None) -> str | None:
    """The real absolute path of ``override`` (absolute only) or of ``name`` on ``PATH``."""
    if override:
        candidate = Path(os.path.expanduser(override))
        if not candidate.is_absolute():
            raise ExternalError(errors.INVALID_PARAMS, "a CLI path override must be absolute")
        found: str | None = str(candidate)
    else:
        found = shutil.which(name)
    if not found:
        return None
    try:
        real = Path(found).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_file() or not os.access(real, os.X_OK):
        return None
    return str(real)


def build_argv(
    executable: PinnedExecutable,
    args: Sequence[str],
    *,
    bypass: BypassArgs | None = None,
    forbidden: Iterable[str] = (),
    forbidden_pairs: Iterable[tuple[str, str]] = (),
    prompt_arg: str | None = None,
) -> list[str]:
    """``[executable, *args, *bypass, "--", prompt]`` after every check.

    ``prompt_arg`` exists only for a CLI that cannot read its prompt from
    stdin (cursor-agent print mode, spike S4): the text goes after ``--`` so
    it can never be read as a flag.  Every other adapter writes the prompt to
    stdin.
    """
    if not isinstance(executable, PinnedExecutable) or not os.path.isabs(executable.path):
        raise ExternalError(errors.INTERNAL, "external CLI executable is not pinned")
    forbidden = tuple(forbidden)
    forbidden_pairs = tuple(forbidden_pairs)
    assert_safe_args(args, forbidden=forbidden, forbidden_pairs=forbidden_pairs)
    argv = [executable.path, *args]
    if bypass is not None:
        if not isinstance(bypass, BypassArgs) or bypass._mint is not _MINT:
            raise ExternalError(errors.JARVIS_PERSON_ONLY, "bypass flags need a person's grant")
        argv.extend(bypass.flags)
    if prompt_arg is not None:
        if "\x00" in prompt_arg:
            raise ExternalError(errors.INVALID_PARAMS, "prompt contains NUL")
        argv.extend(["--", prompt_arg])
    return argv


def _env_allowed(key: str, value: str, passed: frozenset[str]) -> bool:
    upper = key.upper()
    if key in SCRUB_EXACT or upper.startswith(SCRUB_PREFIXES):
        return False
    if upper in passed:
        return True
    if upper in PROXY_VARS:
        return "@" not in value  # a proxy URL with credentials stays behind
    return upper in ALLOW_EXACT or upper.startswith(ALLOW_PREFIXES + tuple(EXTRA_ALLOW_PREFIXES))


def scrubbed_env(
    extra: Mapping[str, str] | None = None,
    base: Mapping[str, str] | None = None,
    *,
    pass_env: Iterable[str] = (),
) -> dict[str, str]:
    """The allow-listed part of the daemon's environment, plus ``extra``.

    Only :data:`ALLOW_EXACT` / :data:`ALLOW_PREFIXES` (locale, terminal, home,
    temp, certificates, each CLI's config-dir variable) and credential-free
    proxies pass (security review L3); provider keys and tokens
    (``ANTHROPIC_API_KEY``, ``GITHUB_TOKEN``, ``AWS_*`` …) reach a CLI only
    when the person names them in ``jarvis.external.clis.<id>.passEnv``.
    """
    source = dict(os.environ if base is None else base)
    passed = frozenset(
        str(name).upper() for name in pass_env if isinstance(name, str) and ENV_NAME_RE.match(name)
    )
    env = {key: value for key, value in source.items() if _env_allowed(key, value, passed)}
    for key, value in (extra or {}).items():
        if key in SCRUB_EXACT or key.upper().startswith(SCRUB_PREFIXES):
            raise ExternalError(errors.INVALID_PARAMS, f"refused environment variable {key}")
        env[key] = value
    return env


async def probe_version(path: str, timeout: float = VERSION_TIMEOUT_SEC) -> str | None:
    """First line of ``<path> --version`` (no model call), or ``None``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            path,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=tempfile.gettempdir(),
            env=scrubbed_env(),
            **_group_kwargs(),
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _kill_tree(proc.pid, grace=0.5)
        with contextlib.suppress(Exception):
            await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    for line in out.decode("utf-8", "replace").splitlines():
        if line.strip():
            return line.strip()[:200]
    return None


def tmux_supported() -> bool:
    """tmux transport is possible here at all: not on Windows, tmux on PATH."""
    return not IS_WINDOWS and shutil.which("tmux") is not None


# ---------------------------------------------------------------------------
# process identity and tree kill
# ---------------------------------------------------------------------------


def _group_kwargs() -> dict[str, Any]:
    if IS_WINDOWS:
        # Its own group, and no console window even from a daemon run in a terminal.
        from snowpea_core.exec import winproc

        return {"creationflags": winproc.SPAWN_FLAGS}
    return {"start_new_session": True}


def pid_alive(pid: int) -> bool:
    """True while ``pid`` runs (never sends a signal on Windows)."""
    if pid <= 0:
        return False
    if IS_WINDOWS:  # pragma: no cover - Windows only
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_start_time(pid: int) -> str | None:
    """An identity stamp for ``pid`` (start time) so a reused pid is never killed."""
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        fields = raw[raw.rfind(")") + 2 :].split()
        return fields[19] if len(fields) > 19 else None
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return None
    ps = trusted_tool("ps")
    if ps is None:
        return None
    try:
        out = subprocess.run(
            [ps, "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def _children_linux(pid: int) -> list[int]:
    found: list[int] = []
    pending = [pid]
    by_parent: dict[int, list[int]] = collections.defaultdict(list)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fields = raw[raw.rfind(")") + 2 :].split()
        if len(fields) > 1 and fields[1].isdigit():
            by_parent[int(fields[1])].append(int(entry.name))
    while pending:
        current = pending.pop()
        for child in by_parent.get(current, []):
            if child not in found:
                found.append(child)
                pending.append(child)
    return found


def descendants(pid: int) -> list[int]:
    """Every process below ``pid`` still parented to it (Linux; empty elsewhere)."""
    if not sys.platform.startswith("linux"):
        return []
    try:
        return _children_linux(pid)
    except OSError:
        return []


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, sig)


def _signal_pid(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, sig)


def _own_group(pid: int, pgid: int | None) -> int | None:
    """The process group to signal for ``pid``, or ``None`` when there is none to signal.

    Only a group ``pid`` leads (or the one recorded at spawn for it) is
    signalled, and never the daemon's own group: a child that was not started
    in its own session (a helper another tool spawned, a CLI's grandchild)
    is stopped by pid and tree instead.
    """
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return None
    current = _safe_getpgid(pid)
    group = pgid if pgid is not None else current
    if group is None or group <= 1:
        return None
    with contextlib.suppress(OSError):
        if group == os.getpgrp():
            return None
    if pgid is None and group != pid:
        return None
    if current is not None and current != group:
        # The pid left that group (or was reused): its tree is signalled by pid.
        return None
    return group


async def _kill_tree(pid: int, *, pgid: int | None = None, grace: float = KILL_GRACE_SEC) -> None:
    """SIGTERM the group (when ``pid`` has its own), the pid and its tree; then SIGKILL.

    Works whether or not ``pid`` leads a process group: the pid itself and every
    descendant are always signalled by pid.
    """
    if IS_WINDOWS:  # pragma: no cover - Windows only
        # Exactly this pid's tree, no console window, taskkill from System32.
        from snowpea_core.exec import winproc

        with contextlib.suppress(OSError, subprocess.SubprocessError):
            await asyncio.to_thread(winproc.taskkill_tree, pid)
        return
    group = _own_group(pid, pgid)
    tree = descendants(pid)
    if group is not None:
        _signal_group(group, signal.SIGTERM)
    _signal_pid(pid, signal.SIGTERM)
    for child in tree:
        _signal_pid(child, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _running(pid) and not any(_running(child) for child in tree):
            break
        await asyncio.sleep(0.05)
    if group is not None:
        _signal_group(group, signal.SIGKILL)
    if _running(pid):
        _signal_pid(pid, signal.SIGKILL)
    for child in tree:
        if _running(child):
            _signal_pid(child, signal.SIGKILL)


def _running(pid: int) -> bool:
    """Alive and not a zombie waiting for its parent to reap it."""
    if not pid_alive(pid):
        return False
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        fields = raw[raw.rfind(")") + 2 :].split()
        return not (fields and fields[0] == "Z")
    return True


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------


class ExternalProcess:
    """One CLI child: line-oriented stdin/stdout, a bounded stderr tail, tree kill."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        cli: str,
        ref: str | None,
        supervisor: ProcessSupervisor | None,
        pid_file: Path | None,
    ) -> None:
        self.proc = proc
        self.pid = proc.pid
        self.pgid: int | None = None
        if not IS_WINDOWS:
            with contextlib.suppress(OSError):
                self.pgid = os.getpgid(proc.pid)
        self.cli = cli
        self.ref = ref
        self.started = time.monotonic()
        self.last_activity = self.started
        self.garbage_lines = 0
        self.oversized_lines = 0
        self._supervisor = supervisor
        self._pid_file = pid_file
        self._stderr: collections.deque[str] = collections.deque(maxlen=50)
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False
        if proc.stderr is not None:
            self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    # -- state ----------------------------------------------------------
    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    def stderr_tail(self, lines: int = 20) -> str:
        """The last stderr lines (never logged; shown only as an error detail)."""
        return "\n".join(list(self._stderr)[-lines:])

    async def _drain_stderr(self) -> None:
        stream = self.proc.stderr
        assert stream is not None
        buffer = b""
        try:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._stderr.append(line[:2000].decode("utf-8", "replace"))
                if len(buffer) > 65536:
                    self._stderr.append(buffer[:2000].decode("utf-8", "replace"))
                    buffer = b""
        except (asyncio.CancelledError, ConnectionError, OSError):
            return
        if buffer:
            self._stderr.append(buffer[:2000].decode("utf-8", "replace"))

    # -- stdin ----------------------------------------------------------
    async def write_line(self, message: Any) -> None:
        """Write one line (a JSON value, or a string as is) to stdin."""
        text = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
        if "\n" in text:
            raise ExternalError(errors.INVALID_PARAMS, "one stdin line may not contain a newline")
        await self.write_bytes((text + "\n").encode("utf-8"))

    async def write_bytes(self, data: bytes) -> None:
        stdin = self.proc.stdin
        if stdin is None or not self.alive or stdin.is_closing():
            raise ExternalError(errors.EXTERNAL_UNAVAILABLE, f"{self.cli} is not running")
        async with self._write_lock:
            try:
                stdin.write(data)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
                raise ExternalError(
                    errors.EXTERNAL_UNAVAILABLE, f"{self.cli} stopped reading its input"
                ) from exc
        self.last_activity = time.monotonic()

    async def close_stdin(self) -> None:
        stdin = self.proc.stdin
        if stdin is None or stdin.is_closing():
            return
        with contextlib.suppress(Exception):
            stdin.close()
            await stdin.wait_closed()

    # -- stdout ---------------------------------------------------------
    async def lines(self) -> AsyncIterator[str]:
        """Stdout split on ``\\n`` from raw reads (never ``readline`` + ``select``)."""
        stream = self.proc.stdout
        if stream is None:
            return
        buffer = bytearray()
        skipping = False
        while True:
            try:
                chunk = await stream.read(65536)
            except (ConnectionError, OSError):
                break
            if not chunk:
                break
            self.last_activity = time.monotonic()
            buffer.extend(chunk)
            while True:
                index = buffer.find(b"\n")
                if index < 0:
                    break
                line = bytes(buffer[:index])
                del buffer[: index + 1]
                if skipping:
                    skipping = False
                    continue
                yield line.decode("utf-8", "replace").rstrip("\r")
            if len(buffer) > MAX_LINE_BYTES:
                self.oversized_lines += 1
                buffer.clear()
                skipping = True
        if buffer and not skipping:
            yield bytes(buffer).decode("utf-8", "replace").rstrip("\r")

    async def json_lines(self) -> AsyncIterator[dict[str, Any]]:
        """Stdout lines that are JSON objects; anything else is counted, never logged."""
        async for line in self.lines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (ValueError, RecursionError):
                self.garbage_lines += 1
                continue
            if not isinstance(value, dict):
                self.garbage_lines += 1
                continue
            yield value

    # -- lifecycle ------------------------------------------------------
    async def wait(self) -> int:
        code = await self.proc.wait()
        await self._finish()
        return code

    async def terminate(self, grace: float = KILL_GRACE_SEC) -> None:
        """Close stdin, then stop the whole process tree."""
        await self.close_stdin()
        if self.alive:
            await _kill_tree(self.pid, pgid=self.pgid, grace=grace)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.proc.wait(), timeout=grace + 3)
        await self._finish()

    async def _finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._stderr_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=1)
            if not self._stderr_task.done():
                self._stderr_task.cancel()
        if self._pid_file is not None:
            with contextlib.suppress(OSError):
                self._pid_file.unlink()
        if self._supervisor is not None:
            self._supervisor._release(self)


class ProcessSupervisor:
    """Spawns CLI children within the cap and keeps their pid files."""

    def __init__(self, home: Path, max_processes: int | Callable[[], int] = 4) -> None:
        self.home = Path(home)
        self.dir = self.home / "external"
        self._max = max_processes
        self._live: dict[int, ExternalProcess] = {}
        self._reserved = 0
        #: Helper processes running now (see :meth:`spawn_helper`).
        self._helpers: dict[int, ExternalProcess] = {}
        #: Helpers started since this supervisor was made (introspection, tests).
        self.helpers_started = 0
        self._helper_slots: asyncio.Semaphore | None = None
        self._closed = False
        #: cli -> the executables snowpea pinned for it (discovery, spawns).  An
        #: orphan is killed only when it runs one of these, never by what its
        #: own pid record says (security review 3, N15).
        self._pinned: dict[str, set[str]] = {}

    def pin(self, cli: str, executable: str | None) -> None:
        """Remember ``executable`` as ``cli``'s configured, pinned binary."""
        if isinstance(executable, str) and os.path.isabs(executable):
            self._pinned.setdefault(cli, set()).add(executable)

    @property
    def max_processes(self) -> int:
        value = self._max() if callable(self._max) else self._max
        return max(1, int(value))

    def live(self) -> list[ExternalProcess]:
        return [proc for proc in self._live.values() if proc.alive]

    def pids(self) -> set[int]:
        """Pids of the CLI processes this daemon runs (to tell them from the person's)."""
        found = {proc.pid for proc in self._live.values() if proc.alive}
        found.update(proc.pid for proc in self._helpers.values() if proc.alive)
        return found

    def helpers(self) -> list[ExternalProcess]:
        """Helper processes running now."""
        return [proc for proc in self._helpers.values() if proc.alive]

    def _release(self, proc: ExternalProcess) -> None:
        self._live.pop(proc.pid, None)
        if self._helpers.get(proc.pid) is proc:
            self._helpers.pop(proc.pid, None)
            if self._helper_slots is not None:
                self._helper_slots.release()

    async def spawn_helper(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        cli: str,
        stdin: bool = False,
        stderr: bool = False,
    ) -> ExternalProcess:
        """Start a short-lived helper (no model call) the daemon still owns.

        Not counted against ``maxProcesses`` but bounded by :data:`MAX_HELPERS`
        (more wait), in its own process group, with a pid file so a daemon that
        died leaves none behind, and stopped by :meth:`shutdown`.  The caller
        stops it (``terminate``) or waits for it; either releases the slot.
        """
        if self._closed:
            raise ExternalError(errors.EXTERNAL_UNAVAILABLE, "snowpea is shutting down")
        if not argv or not os.path.isabs(argv[0]):
            raise ExternalError(errors.INTERNAL, "external CLI executable is not pinned")
        if self._helper_slots is None:
            self._helper_slots = asyncio.Semaphore(MAX_HELPERS)
        await self._helper_slots.acquire()
        try:
            raw = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE if stderr else asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=dict(env),
                **_group_kwargs(),
            )
        except OSError as exc:
            self._helper_slots.release()
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE, f"could not start {cli}: {exc.strerror or exc}"
            ) from exc
        except BaseException:
            self._helper_slots.release()
            raise
        pid_file = self._write_pid_file(raw.pid, cli, "helper", exe=argv[0])
        handle = ExternalProcess(raw, cli=cli, ref="helper", supervisor=self, pid_file=pid_file)
        self._helpers[raw.pid] = handle
        self.helpers_started += 1
        return handle

    async def run_helper(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        cli: str,
        timeout: float,
    ) -> tuple[int | None, bytes]:
        """Run a helper to the end: ``(returncode, stdout)``; ``(None, b"")`` on a timeout.

        A helper that overruns ``timeout`` is stopped with its whole tree.
        """
        try:
            handle = await self.spawn_helper(argv, cwd=cwd, env=env, cli=cli)
        except ExternalError:
            return None, b""
        try:
            out, _ = await asyncio.wait_for(handle.proc.communicate(), timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            await handle.terminate(grace=0.5)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return None, b""
        await handle.wait()
        return handle.returncode, out

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        cli: str,
        ref: str | None = None,
        stdin: bool = True,
    ) -> ExternalProcess:
        """Start ``argv`` (from :func:`build_argv`) in its own process group."""
        if self._closed:
            raise ExternalError(errors.EXTERNAL_UNAVAILABLE, "snowpea is shutting down")
        if not argv or not os.path.isabs(argv[0]):
            raise ExternalError(errors.INTERNAL, "external CLI executable is not pinned")
        if len(self.live()) + self._reserved >= self.max_processes:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                f"snowpea already runs {self.max_processes} external CLI processes "
                "(jarvis.external.maxProcesses); close one first",
            )
        self._reserved += 1
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=dict(env),
                **_group_kwargs(),
            )
        except OSError as exc:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE, f"could not start {cli}: {exc.strerror or exc}"
            ) from exc
        finally:
            self._reserved -= 1
        self.pin(cli, argv[0])
        pid_file = self._write_pid_file(proc.pid, cli, ref, exe=argv[0])
        handle = ExternalProcess(proc, cli=cli, ref=ref, supervisor=self, pid_file=pid_file)
        self._live[proc.pid] = handle
        return handle

    def _secure_dir(self) -> bool:
        """Create the pid-file directory 0700; False when it is not safely ours.

        A symlink, a non-directory or a directory another user owns is never
        used: its records could name any process (security review H1).
        """
        with contextlib.suppress(OSError):
            self.dir.parent.mkdir(parents=True, exist_ok=True)
            self.dir.mkdir(mode=0o700, exist_ok=True)
        try:
            info = os.lstat(self.dir)
        except OSError:
            return False
        if not stat.S_ISDIR(info.st_mode):
            return False
        if not IS_WINDOWS:
            if info.st_uid != os.getuid():
                return False
            if stat.S_IMODE(info.st_mode) & 0o077:
                try:
                    os.chmod(self.dir, 0o700)
                except OSError:
                    return False
        return True

    def _write_pid_file(
        self, pid: int, cli: str, ref: str | None, exe: str | None = None
    ) -> Path | None:
        record = {
            "pid": pid,
            "pgid": None if IS_WINDOWS else _safe_getpgid(pid),
            "startTime": process_start_time(pid),
            "exe": exe,
            "cli": cli,
            "ref": ref,
            "daemonPid": os.getpid(),
            "ts": utc_now(),
        }
        try:
            if not self._secure_dir():
                log.warning("the external pid-file directory is not safe; %s not recorded", cli)
                return None
            path = self.dir / f"{pid}.json"
            tmp = self.dir / f".{pid}.json.tmp"
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()  # unlink never follows a symlink
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(tmp, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(record))
            os.replace(tmp, path)
            return path
        except OSError:
            log.warning("could not write the pid file for %s", cli, exc_info=True)
            return None

    def reap_orphans(self) -> int:
        """Kill CLI processes a previous daemon left behind; returns how many.

        A record is trusted only when every check passes (security review H1):
        a regular file this user owns (no symlink) named after the recorded
        pid, a recorded start time and executable that match the live
        process, and a recording daemon that is gone (or is this daemon).
        Anything else is removed without a signal.
        """
        killed = 0
        if not self.dir.is_dir() or not self._secure_dir():
            return 0
        mine = self.pids()
        for path in sorted(self.dir.glob("*.json")):
            record = _read_pid_record(path)
            pid = _record_pid(path, record)
            if record is None or pid is None:
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if pid in mine:
                continue
            if self._same_process(pid, record, self._pinned):
                self._kill_now(pid, record.get("pgid"))
                killed += 1
                log.info("stopped an orphaned %s process (pid %d)", record.get("cli"), pid)
            with contextlib.suppress(OSError):
                path.unlink()
        return killed

    @staticmethod
    def _same_process(
        pid: int, record: Mapping[str, Any], pinned: Mapping[str, set[str]] | None = None
    ) -> bool:
        """The live ``pid`` is the CLI ``record`` names, by start time and by the
        executable snowpea pinned for ``record["cli"]`` (N15): the record's own
        ``exe`` is only a hint a forged file could fill with anything."""
        if not _killable_pid(pid) or not pid_alive(pid):
            return False
        daemon = record.get("daemonPid")
        if not isinstance(daemon, int) or isinstance(daemon, bool):
            return False
        if daemon != os.getpid() and pid_alive(daemon):
            return False  # another daemon still owns it
        recorded = record.get("startTime")
        if not isinstance(recorded, str) or not recorded:
            return False  # no identity stamp: never killed
        if process_start_time(pid) != recorded:
            return False
        cli = record.get("cli")
        expected = (pinned or {}).get(cli) if isinstance(cli, str) else None
        if not expected:
            return False  # nothing pinned for that CLI: never killed
        return any(process_runs(pid, exe, argv_words=2) for exe in sorted(expected))

    @staticmethod
    def _kill_now(pid: int, pgid: Any) -> None:
        if not _killable_pid(pid):
            return
        if IS_WINDOWS:  # pragma: no cover - Windows only
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
            return
        tree = descendants(pid)
        if _orphan_group(pid, pgid) is not None:
            _signal_group(pid, signal.SIGKILL)
        _signal_pid(pid, signal.SIGKILL)
        for child in tree:
            if _killable_pid(child):
                _signal_pid(child, signal.SIGKILL)

    async def shutdown(self, grace: float = KILL_GRACE_SEC) -> None:
        """Stop every child and helper this daemon runs, then sweep the pid files."""
        self._closed = True
        live = [*self._live.values(), *self._helpers.values()]
        await asyncio.gather(*(proc.terminate(grace) for proc in live), return_exceptions=True)
        self._live.clear()
        self._helpers.clear()
        self.reap_orphans()


def _safe_getpgid(pid: int) -> int | None:
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


#: Largest pid file read (a record is ~250 bytes).
MAX_PID_RECORD_BYTES = 4096


def _read_pid_record(path: Path) -> dict[str, Any] | None:
    """A pid record from a regular file this user owns, never through a symlink."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PID_RECORD_BYTES:
        return None
    if not IS_WINDOWS and info.st_uid != os.getuid():
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_PID_RECORD_BYTES + 1)
        record = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def _record_pid(path: Path, record: Mapping[str, Any] | None) -> int | None:
    """The record's pid when it is a real int that matches the file name."""
    if record is None:
        return None
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    if path.stem != str(pid):
        return None
    return pid if _killable_pid(pid) else None


def _killable_pid(pid: Any) -> bool:
    """Never init, a group wildcard, this daemon or its parent."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    return pid not in (os.getpid(), os.getppid())


def _orphan_group(pid: int, pgid: Any) -> int | None:
    """The group a reaped orphan leads, or ``None`` (then it is killed by pid only)."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1 or pgid != pid:
        return None
    with contextlib.suppress(OSError):
        if pgid == os.getpgrp():
            return None
    if _safe_getpgid(pid) != pgid:
        return None
    return pgid


def process_runs(pid: int, exe: str, argv_words: int = 3) -> bool:
    """True when ``pid`` runs ``exe`` (directly or as a script its interpreter runs).

    ``argv_words``: how many leading ``argv`` words may name it (a node-script
    CLI is ``node <script>``)."""
    want = os.path.realpath(exe)
    if sys.platform.startswith("linux"):
        with contextlib.suppress(OSError):
            if os.path.realpath(os.readlink(f"/proc/{pid}/exe")) == want:
                return True
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        head = [part.decode("utf-8", "replace")
                for part in raw.split(b"\x00")[:argv_words] if part]
        return any(
            part in (exe, want) or (os.path.isabs(part) and os.path.realpath(part) == want)
            for part in head
        )
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return False
    out = _ps_command(pid)
    return any(out.startswith(path) or f" {path}" in out for path in (exe, want))


def _ps_command(pid: int) -> str:
    ps = trusted_tool("ps")
    if ps is None:
        return ""
    try:
        return subprocess.run(
            [ps, "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def process_argv(pid: int) -> list[str]:
    """The command line of ``pid`` (Linux exactly; elsewhere split on spaces); [] if unknown."""
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return []
        return [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return []
    return _ps_command(pid).split()


#: Directories a system helper (``ps``, ``lsof``, ``systemd-run``) is taken
#: from: never a user-writable ``PATH`` entry (security review L5).
TRUSTED_TOOL_DIRS: tuple[str, ...] = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


@functools.lru_cache(maxsize=16)
def trusted_tool(name: str) -> str | None:
    """Absolute path of a system helper in :data:`TRUSTED_TOOL_DIRS`, or ``None``."""
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return None
    for folder in TRUSTED_TOOL_DIRS:
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


__all__ = [
    "BypassArgs",
    "DANGEROUS_RE",
    "ExternalError",
    "ExternalProcess",
    "HUMAN_CLIENT_KINDS",
    "LedgerHook",
    "MODES",
    "PersonConfirmation",
    "PinnedExecutable",
    "ProcessSupervisor",
    "UnrestrictedGrant",
    "assert_safe_args",
    "build_argv",
    "bypass_args",
    "confirm_person",
    "default_ledger",
    "descendants",
    "grant_unrestricted",
    "person_confirmation_from_gateway",
    "is_dangerous",
    "is_human_connection",
    "pid_alive",
    "probe_version",
    "process_start_time",
    "project_of",
    "resolve_executable",
    "scan_payload",
    "scrubbed_env",
    "set_ledger_hook",
    "tmux_supported",
    "unrestricted_values",
    "validate_cwd",
    "validate_mode",
    "validate_model",
    "validate_session_id",
]
