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
import json
import logging
import os
import re
import shutil
import signal
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

#: Client kinds whose connection is a person at a screen (``system.hello``).
#: ``browser`` (the browser host) and connections that did not say are not.
HUMAN_CLIENT_KINDS: frozenset[str] = frozenset({"ide", "desktop", "tui", "cli", "web"})

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

#: Longest stdout line kept (bytes); a longer one is dropped whole.
MAX_LINE_BYTES = 16 * 1024 * 1024
#: Seconds between SIGTERM and SIGKILL when a process tree is stopped.
KILL_GRACE_SEC = 2.0
#: Seconds ``--version`` may take.
VERSION_TIMEOUT_SEC = 10.0


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


def is_human_connection(conn: Any) -> bool:
    """True for an authenticated, open IDE / desktop / TUI / CLI / web connection."""
    return (
        conn is not None
        and bool(getattr(conn, "authenticated", False))
        and not bool(getattr(conn, "closed", False))
        and getattr(conn, "client_kind", None) in HUMAN_CLIENT_KINDS
    )


def confirm_person(conn: Any) -> PersonConfirmation:
    """A :class:`PersonConfirmation` for ``conn``, or ``jarvis_person_only``.

    In-process callers (Jarvis, schedules, the gateway) pass ``None`` and are
    refused: they ask the person through a person-only approval instead.
    """
    if not is_human_connection(conn):
        raise ExternalError(
            errors.JARVIS_PERSON_ONLY,
            "only a person at the IDE, web or TUI may choose unrestricted mode",
        )
    kind = str(getattr(conn, "client_kind", ""))
    return PersonConfirmation(
        client_kind=kind,
        client_id=getattr(conn, "client_id", None),
        surface=kind,
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


#: ``callback(entry)`` that records one ledger entry (a ``JarvisDecision`` dict).
LedgerHook = Callable[[dict[str, Any]], None]
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
) -> UnrestrictedGrant:
    """Record the person's choice in the ledger and return the grant.

    ``action`` is ``create``, ``open`` or ``setMode``.  The ledger write is
    part of the grant: if it fails, there is no grant.
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
    }
    hook = _ledger_hook or default_ledger(home)
    try:
        hook(entry)
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
        ledger_id=entry_id,
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


def scrubbed_env(
    extra: Mapping[str, str] | None = None, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The daemon's environment minus snowpea secrets, plus ``extra``."""
    source = dict(os.environ if base is None else base)
    env = {
        key: value
        for key, value in source.items()
        if key not in SCRUB_EXACT and not key.upper().startswith(SCRUB_PREFIXES)
    }
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
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
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
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
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


async def _kill_tree(pid: int, *, pgid: int | None = None, grace: float = KILL_GRACE_SEC) -> None:
    """SIGTERM the group and the tree, wait ``grace``, then SIGKILL what is left."""
    if IS_WINDOWS:  # pragma: no cover - Windows only
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=15,
            )
        return
    group = pgid if pgid is not None else pid
    tree = descendants(pid)
    _signal_group(group, signal.SIGTERM)
    for child in tree:
        _signal_pid(child, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(pid) and not any(pid_alive(child) for child in tree):
            break
        await asyncio.sleep(0.05)
    _signal_group(group, signal.SIGKILL)
    for child in tree:
        if pid_alive(child):
            _signal_pid(child, signal.SIGKILL)


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
            except ValueError:
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

    @property
    def max_processes(self) -> int:
        value = self._max() if callable(self._max) else self._max
        return max(1, int(value))

    def live(self) -> list[ExternalProcess]:
        return [proc for proc in self._live.values() if proc.alive]

    def pids(self) -> set[int]:
        """Pids of the CLI processes this daemon runs (to tell them from the person's)."""
        return {proc.pid for proc in self._live.values() if proc.alive}

    def _release(self, proc: ExternalProcess) -> None:
        self._live.pop(proc.pid, None)

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
        pid_file = self._write_pid_file(proc.pid, cli, ref)
        handle = ExternalProcess(proc, cli=cli, ref=ref, supervisor=self, pid_file=pid_file)
        self._live[proc.pid] = handle
        return handle

    def _write_pid_file(self, pid: int, cli: str, ref: str | None) -> Path | None:
        record = {
            "pid": pid,
            "pgid": None if IS_WINDOWS else _safe_getpgid(pid),
            "startTime": process_start_time(pid),
            "cli": cli,
            "ref": ref,
            "daemonPid": os.getpid(),
            "ts": utc_now(),
        }
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / f"{pid}.json"
            tmp = self.dir / f".{pid}.json.tmp"
            tmp.write_text(json.dumps(record), encoding="utf-8")
            os.replace(tmp, path)
            return path
        except OSError:
            log.warning("could not write the pid file for %s", cli, exc_info=True)
            return None

    def reap_orphans(self) -> int:
        """Kill CLI processes a previous daemon left behind; returns how many."""
        killed = 0
        if not self.dir.is_dir():
            return 0
        mine = self.pids()
        for path in sorted(self.dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                pid = int(record["pid"])
            except (OSError, ValueError, KeyError, TypeError):
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if pid in mine:
                continue
            if self._same_process(pid, record):
                self._kill_now(pid, record.get("pgid"))
                killed += 1
                log.info("stopped an orphaned %s process (pid %d)", record.get("cli"), pid)
            with contextlib.suppress(OSError):
                path.unlink()
        return killed

    @staticmethod
    def _same_process(pid: int, record: Mapping[str, Any]) -> bool:
        if pid == os.getpid() or not pid_alive(pid):
            return False
        recorded = record.get("startTime")
        if recorded is not None:
            return process_start_time(pid) == recorded
        pgid = record.get("pgid")
        return pgid is not None and _safe_getpgid(pid) == pgid

    @staticmethod
    def _kill_now(pid: int, pgid: Any) -> None:
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
        group = int(pgid) if isinstance(pgid, int) else pid
        _signal_group(group, signal.SIGKILL)
        _signal_pid(pid, signal.SIGKILL)
        for child in tree:
            _signal_pid(child, signal.SIGKILL)

    async def shutdown(self, grace: float = KILL_GRACE_SEC) -> None:
        """Stop every child this daemon runs, then sweep the pid files."""
        live = list(self._live.values())
        await asyncio.gather(*(proc.terminate(grace) for proc in live), return_exceptions=True)
        self._live.clear()
        self.reap_orphans()


def _safe_getpgid(pid: int) -> int | None:
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return None
    try:
        return os.getpgid(pid)
    except OSError:
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
