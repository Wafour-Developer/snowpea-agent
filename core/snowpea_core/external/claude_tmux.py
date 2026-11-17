"""Claude Code in a shared tmux terminal (M19 WP6a, spike S5; Linux and macOS).

The CLI runs its own interactive UI in a tmux pane, so the person can
``tmux -L snowpea attach -t <session>`` from any terminal while snowpea follows
the same process:

* **reading** — the transcript (``projects/<enc>/<id>.jsonl``) is tailed and the
  status file (``sessions/<pid>.json``: ``idle`` / ``busy`` / ``waiting``)
  tells when a turn starts, waits for a permission answer and ends;
* **typing** — only while the status is ``idle`` (never into an open dialog:
  a digit would answer it), never text starting with ``/`` or ``!`` (they
  switch the CLI's input mode, even inside a paste), never control
  characters.  One line goes in with ``send-keys -l`` then ``Enter``; anything
  longer or multi-line with a bracketed paste (``load-buffer`` +
  ``paste-buffer -p``);
* **approvals** — answered by the person in the terminal (``approvals:
  person``); snowpea only shows that one is pending;
* **interrupt** — ``Escape`` to the pane while the status is ``busy`` (never
  ``C-c``: twice quits Claude).

tmux always runs with an argv list; pane ids, socket names and session names
are validated.  snowpea starts panes on its own server (``tmux -L snowpea``);
a pane the person started elsewhere is only adopted after the person allows
it, and is never killed by snowpea.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.external import proc
from snowpea_core.external.base import (
    ApprovalAsk,
    ChannelHost,
    ExternalChannel,
    LaunchSpec,
    unavailable,
)
from snowpea_core.external.claude import (
    TranscriptTail,
    read_status,
    status_time,
    tmux_field,
    transcript_path,
)
from snowpea_core.external.proc import ExternalError, UnrestrictedGrant
from snowpea_core.server import errors

if TYPE_CHECKING:
    from snowpea_core.external.claude import ClaudeAdapter

log = logging.getLogger("snowpea.external.claude.tmux")

#: ``resize-window`` / ``window-size`` and the rest of what S5 used.
TMUX_MIN_VERSION: tuple[int, int] = (2, 9)
PANE_RE = re.compile(r"^%\d{1,9}$")
SOCKET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
OWN_SESSION_RE = re.compile(r"^sp-[0-9a-f]{12}$")
_TMUX_VERSION_RE = re.compile(r"(\d+)\.(\d+)")
#: Longest prompt typed into a pane.
MAX_PROMPT_CHARS = 100_000
#: A single line up to this length goes in with ``send-keys -l``.
SEND_KEYS_MAX = 2_000
#: Seconds to see a typed prompt start a turn.
START_TIMEOUT_SEC = 15.0
#: Seconds after a keypress from an attached tmux client during which snowpea
#: does not type into the pane (the person is typing).
TYPING_QUIET_SEC = 3.0
#: ``session_busy`` ``data.reason`` of a refused send into a shared tmux pane: a
#: permission prompt is open there, the CLI is not idle, or a person typed a moment ago.
BUSY_TERMINAL_PROMPT = "terminal_prompt"
BUSY_TERMINAL_BUSY = "terminal_busy"
BUSY_TERMINAL_TYPING = "terminal_typing"
#: Window size of a pane snowpea starts.
WINDOW = ("200", "50")
_TRUST_MARKERS = ("quick safety check", "trust this folder", "do you trust")


def tmux_executable() -> str | None:
    found = shutil.which("tmux")
    if not found:
        return None
    with contextlib.suppress(OSError, RuntimeError):
        return str(Path(found).resolve(strict=True))
    return None


async def tmux_version_ok() -> bool:
    """tmux exists here and is :data:`TMUX_MIN_VERSION` or later."""
    if proc.IS_WINDOWS:
        return False
    exe = tmux_executable()
    if exe is None:
        return False
    try:
        child = await asyncio.create_subprocess_exec(
            exe, "-V", stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(child.communicate(), 10)
    except (OSError, TimeoutError):
        return False
    text = out.decode("utf-8", "replace").strip()
    found = _TMUX_VERSION_RE.search(text)
    if found is None:
        # ``tmux master`` / ``tmux next-3.5``: a development build is new enough.
        return text.startswith("tmux ")
    return (int(found.group(1)), int(found.group(2))) >= TMUX_MIN_VERSION


def validate_pane(pane: str) -> str:
    if not isinstance(pane, str) or not PANE_RE.fullmatch(pane):
        raise ExternalError(errors.INVALID_PARAMS, "invalid tmux pane id")
    return pane


def _socket_ok(path: str) -> bool:
    if not os.path.isabs(path):
        return False
    try:
        info = os.stat(path)
    except OSError:
        return False
    return stat.S_ISSOCK(info.st_mode) and (not hasattr(os, "getuid")
                                            or info.st_uid == os.getuid())


def tmux_socket_of(pid: int) -> str | None:
    """The tmux server socket a process lives under (its ``TMUX`` variable).

    Reads ``/proc/<pid>/environ`` on Linux and keeps only that one variable.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for entry in raw.split(b"\0"):
        if entry.startswith(b"TMUX="):
            value = entry[5:].decode("utf-8", "replace").split(",", 1)[0]
            return value if _socket_ok(value) else None
    return None


def default_sockets() -> list[str]:
    """The person's default tmux server socket (``$TMUX_TMPDIR`` or ``/tmp``)."""
    if not hasattr(os, "getuid"):
        return []
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    path = os.path.join(base, f"tmux-{os.getuid()}", "default")
    return [path] if _socket_ok(path) else []


def tty_of(pid: int) -> str | None:
    """The controlling terminal of ``pid`` as ``/dev/...``."""
    if sys.platform.startswith("linux"):
        with contextlib.suppress(OSError):
            target = os.readlink(f"/proc/{pid}/fd/0")
            return target if target.startswith("/dev/") else None
        return None
    try:
        ps = proc.trusted_tool("ps") or "/bin/ps"
        out = subprocess.run([ps, "-o", "tty=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out or out in ("?", "??"):
        return None
    return out if out.startswith("/dev/") else f"/dev/{out}"


class Tmux:
    """One tmux server, addressed by ``-L <name>`` or ``-S <path>``; argv only."""

    def __init__(self, *, socket_name: str | None = None, socket_path: str | None = None,
                 env: dict[str, str] | None = None) -> None:
        exe = tmux_executable()
        if exe is None or proc.IS_WINDOWS:
            raise unavailable("tmux is not installed on this machine")
        if socket_path is not None:
            if not _socket_ok(socket_path):
                raise ExternalError(errors.INVALID_PARAMS, "invalid tmux socket")
            self.base = [exe, "-S", socket_path]
            self.label = socket_path
        else:
            name = socket_name or "snowpea"
            if not SOCKET_NAME_RE.fullmatch(name):
                raise ExternalError(errors.INVALID_PARAMS, "invalid tmux socket name")
            self.base = [exe, "-L", name]
            self.label = name
        self.socket_name = socket_name if socket_path is None else None
        source = dict(env) if env is not None else proc.scrubbed_env()
        # A client started inside another tmux would otherwise talk to that one.
        for key in ("TMUX", "TMUX_PANE"):
            source.pop(key, None)
        self.env = source

    def attach_hint(self, session: str) -> str:
        if self.socket_name:
            return f"tmux -L {self.socket_name} attach -t {session}"
        return f"tmux -S {self.label} attach -t {session}"

    async def run(self, *args: str, data: bytes | None = None, timeout: float = 10.0,
                  check: bool = True, prefix: list[str] | None = None) -> tuple[int, str]:
        for arg in args:
            if not isinstance(arg, str) or "\x00" in arg:
                raise ExternalError(errors.INVALID_PARAMS, "invalid tmux argument")
        argv = [*(prefix or []), *self.base, *args]
        try:
            child = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
        except OSError as exc:
            raise unavailable(f"could not run tmux: {exc.strerror or exc}") from exc
        try:
            out, err = await asyncio.wait_for(child.communicate(data), timeout)
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                child.kill()
            with contextlib.suppress(Exception):
                await child.wait()
            raise unavailable(f"tmux {args[0] if args else ''} did not answer") from exc
        code = child.returncode if child.returncode is not None else -1
        if check and code != 0:
            detail = err.decode("utf-8", "replace").strip().splitlines()
            raise unavailable(
                f"tmux {args[0] if args else ''} failed" + (f": {detail[0][:200]}" if detail
                                                            else "")
            )
        return code, out.decode("utf-8", "replace")

    async def pane_info(self, pane: str) -> dict[str, str] | None:
        """``{pane, pid, tty, session, command, path}`` of a live pane, or ``None``."""
        validate_pane(pane)
        code, out = await self.run(
            "display-message", "-p", "-t", pane,
            "#{pane_id}\t#{pane_pid}\t#{pane_tty}\t#{session_name}\t#{pane_dead}"
            "\t#{pane_current_command}\t#{pane_current_path}",
            check=False,
        )
        if code != 0:
            return None
        parts = out.rstrip("\n").split("\t", 6)
        if len(parts) == 5:
            parts += ["", ""]
        if len(parts) != 7 or parts[0] != pane or parts[4] == "1":
            return None
        return {"pane": parts[0], "pid": parts[1], "tty": parts[2], "session": parts[3],
                "command": parts[5], "path": parts[6]}

    async def has_session(self, name: str) -> bool:
        code, _ = await self.run("has-session", "-t", f"={name}", check=False)
        return code == 0

    async def clients(self, name: str) -> list[str]:
        code, out = await self.run("list-clients", "-t", f"={name}", "-F", "#{client_tty}",
                                   check=False)
        return [line for line in out.splitlines() if line.strip()] if code == 0 else []

    async def last_client_activity(self, name: str) -> float | None:
        """Unix time of the newest keypress from a client attached to ``name``, if any."""
        code, out = await self.run("list-clients", "-t", f"={name}", "-F",
                                   "#{client_activity}", check=False)
        if code != 0:
            return None
        stamps = [int(line) for line in out.split() if line.strip().isdigit()]
        return float(max(stamps)) if stamps else None

    async def capture(self, pane: str) -> str:
        validate_pane(pane)
        code, out = await self.run("capture-pane", "-p", "-t", pane, check=False)
        return out if code == 0 else ""

    async def type_text(self, pane: str, text: str) -> None:
        """Type ``text`` into ``pane`` (no Enter)."""
        validate_pane(pane)
        if "\n" in text or "\t" in text or len(text) > SEND_KEYS_MAX:
            buffer = f"sp-{uuid.uuid4().hex[:12]}"
            await self.run("load-buffer", "-b", buffer, "-", data=text.encode("utf-8"))
            await self.run("paste-buffer", "-p", "-d", "-b", buffer, "-t", pane)
        else:
            await self.run("send-keys", "-t", pane, "-l", "--", text)

    async def key(self, pane: str, key: str) -> None:
        validate_pane(pane)
        if key not in ("Enter", "Escape"):
            raise ExternalError(errors.INVALID_PARAMS, f"refused tmux key {key!r}")
        await self.run("send-keys", "-t", pane, key)


def _scope_prefix() -> list[str]:
    """Run a new tmux server outside the daemon's systemd unit (S5 §7)."""
    if not sys.platform.startswith("linux") or not os.environ.get("INVOCATION_ID"):
        return []
    runner = proc.trusted_tool("systemd-run")
    return [runner, "--user", "--scope", "--quiet", "--collect"] if runner else []


def refuse_text(text: str) -> None:
    """Prompts the shared terminal cannot take.

    Leading whitespace, format characters (U+FEFF, U+200B, …) and controls are
    skipped before the ``/`` ``!`` check (security review L7), and a prompt
    that starts with a format character is refused outright: what a terminal
    shows first must be what it reads first.
    """
    if text[:1] and unicodedata.category(text[0]) == "Cf":
        raise ExternalError(
            errors.INVALID_PARAMS, "prompt starts with an invisible formatting character"
        )
    start = 0
    while start < len(text) and (
        text[start].isspace()
        or unicodedata.category(text[start]) in ("Cf", "Cc", "Zs", "Zl", "Zp")
    ):
        start += 1
    stripped = text[start:]
    if not stripped:
        raise ExternalError(errors.INVALID_PARAMS, "empty prompt")
    if stripped[0] in ("/", "!"):
        raise ExternalError(
            errors.INVALID_PARAMS,
            "a prompt starting with '/' or '!' switches Claude Code's input mode in the "
            "terminal; type it there yourself, or use a session snowpea runs",
        )
    if len(text) > MAX_PROMPT_CHARS:
        raise ExternalError(errors.INVALID_PARAMS, "prompt is too long for the terminal")
    if any((ord(ch) < 32 and ch not in "\n\t") or 127 <= ord(ch) <= 159 for ch in text):
        raise ExternalError(errors.INVALID_PARAMS,
                            "prompt contains control characters a terminal would act on")


class ClaudeTmuxChannel(ExternalChannel):
    """A Claude Code session in a tmux pane (host ``tmux``, approvals ``person``)."""

    host = "tmux"

    def __init__(self, spec: LaunchSpec, host: ChannelHost, adapter: ClaudeAdapter) -> None:
        super().__init__(spec, host)
        self.adapter = adapter
        self.server: Tmux | None = None
        self.pane: str | None = None
        self.session_name: str | None = None
        self.claude_pid: int | None = None
        #: The Claude Code process's start time (pid reuse check before each send).
        self.claude_start: str | None = None
        #: The mode the process really runs in when it differs from the one asked
        #: (an adopted pane started with a bypass flag); the manager reports it.
        self.actual_mode: str | None = None
        self.actual_native: str | None = None
        #: snowpea started this pane (and may stop it); adopted panes are the person's.
        self.owned = False
        self.closing = False
        self.dead = False
        self.started_mode: str | None = None
        self.started_native: str | None = None
        self.started_grant: UnrestrictedGrant | None = None
        self.turn_id: str | None = None
        self.turns_done = 0
        self.interrupting = False
        self.tail: TranscriptTail | None = None
        self.watcher: asyncio.Task[None] | None = None
        self._poll_lock = asyncio.Lock()
        self._sent_at_ms = 0.0
        self._prompts_before = 0
        self._turn_started = False
        self._last_status: str | None = None
        self._restarting = False

    # -- state ----------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return (not self.dead and self.claude_pid is not None
                and proc.pid_alive(self.claude_pid))

    def pids(self) -> set[int]:
        return {self.claude_pid} if self.alive and self.claude_pid is not None else set()

    def status(self) -> dict[str, Any] | None:
        return read_status(self.claude_pid) if self.claude_pid is not None else None

    # -- start ------------------------------------------------------------------
    async def start(self) -> None:
        if proc.IS_WINDOWS or not proc.tmux_supported():
            raise unavailable("tmux is not available on this machine")
        if not await tmux_version_ok():
            raise unavailable(
                f"the shared terminal needs tmux {'.'.join(map(str, TMUX_MIN_VERSION))} or later"
            )
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            await self.host_api.id_known(self.session_id)
        else:
            proc.validate_session_id(self.session_id)
        own = self.host_api.supervisor.pids()
        holder = self.adapter.holder(self.session_id, own) if self.spec.resume else None
        if holder is not None:
            await self._adopt(holder)
        else:
            await self._launch(resume=self.spec.resume)
        assert self.session_id is not None
        path = transcript_path(self.spec.cwd, self.session_id)
        start = path.stat().st_size if path.exists() else None
        self.tail = TranscriptTail(path, self.session_id, self.spec.cwd, start=start,
                                   builder=self.host_api.builder)
        self.watcher = asyncio.ensure_future(self._watch())

    async def _launch(self, *, resume: bool) -> None:
        assert self.session_id is not None
        server = Tmux(socket_name=self.adapter.tmux_socket, env=self.host_api.env)
        name = f"sp-{self.session_id.replace('-', '')[:12].lower()}"
        if not OWN_SESSION_RE.fullmatch(name):
            raise ExternalError(errors.INVALID_PARAMS, "invalid session id for tmux")
        if await server.has_session(name):
            raise unavailable(
                f"a tmux session {name} already exists on snowpea's tmux server; "
                f"close it first ({server.attach_hint(name)})"
            )
        session_args = ["--resume" if resume else "--session-id", self.session_id]
        argv = self.adapter.argv(self.spec, self.mode, self.grant, session_args,
                                 structured=False, native_mode=self.native_mode)
        prefix = _scope_prefix()
        _code, out = await server.run(
            "new-session", "-d", "-P", "-F", "#{session_name}\t#{pane_id}\t#{pane_pid}",
            "-s", name, "-x", WINDOW[0], "-y", WINDOW[1], "-c", self.spec.cwd, "--", *argv,
            prefix=prefix, timeout=20.0,
        )
        parts = out.strip().split("\t")
        if len(parts) != 3 or not PANE_RE.fullmatch(parts[1]) or not parts[2].isdigit():
            with contextlib.suppress(ExternalError):
                await server.run("kill-session", "-t", f"={name}", check=False)
            raise unavailable("tmux did not report the new pane")
        self.server = server
        self.session_name = name
        self.pane = parts[1]
        self.claude_pid = int(parts[2])
        self.claude_start = proc.process_start_time(self.claude_pid)
        self.owned = True
        self.dead = False
        self.started_mode = self.mode
        self.started_native = self.native_mode
        self.started_grant = self.grant
        self.tmux = {"session": name, "pane": self.pane}
        await self._wait_ready()

    async def _wait_ready(self) -> None:
        assert self.server is not None and self.pane is not None and self.claude_pid
        started = time.monotonic()
        deadline = started + self.adapter.ready_timeout
        last_screen = 0.0
        while True:
            data = read_status(self.claude_pid)
            if (data is not None and data.get("sessionId") == self.session_id
                    and data.get("status") == "idle"):
                return
            now = time.monotonic()
            if not proc.pid_alive(self.claude_pid):
                raise unavailable("Claude Code exited right after it started in the tmux pane")
            if now - started > 2.5 and now - last_screen > 1.0:
                last_screen = now
                screen = (await self.server.capture(self.pane)).lower()
                if any(marker in screen for marker in _TRUST_MARKERS):
                    raise unavailable(
                        "Claude Code asks whether to trust this folder. Open `claude` there "
                        "once in a terminal and answer the question, then try again"
                    )
            if now > deadline:
                raise unavailable("Claude Code did not become ready in the tmux pane")
            await asyncio.sleep(0.1)

    async def _adopt(self, holder: dict[str, Any]) -> None:
        """Type into a pane the person started, after the person allows it."""
        pane = tmux_field(holder.get("tmux"))
        try:
            pid = int(holder.get("pid"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            pid = 0
        if pane is None or pid <= 0:
            raise ExternalError(
                errors.EXTERNAL_VIEW_ONLY,
                "this Claude Code session is open in another terminal; it can be continued "
                "here once that one exits",
            )
        validate_pane(pane["pane"])
        sockets: list[str] = []
        found = tmux_socket_of(pid)
        if found:
            sockets.append(found)
        sockets += [path for path in default_sockets() if path not in sockets]
        server: Tmux | None = None
        info: dict[str, str] | None = None
        tty = tty_of(pid)
        for path in sockets:
            candidate = Tmux(socket_path=path, env=self.host_api.env)
            info = await candidate.pane_info(pane["pane"])
            if info is not None and (info["pid"] == str(pid) or (tty and info["tty"] == tty)):
                server = candidate
                break
        if server is None or info is None:
            raise ExternalError(
                errors.EXTERNAL_VIEW_ONLY,
                "this Claude Code session runs in a tmux pane snowpea cannot reach; "
                "continue it there",
            )
        # The process must be the pinned Claude Code (security review M5), not
        # whatever a status file in ~/.claude names.
        start_time = proc.process_start_time(pid)
        if start_time is None or not proc.process_runs(pid, self.spec.executable.path):
            raise ExternalError(
                errors.EXTERNAL_VIEW_ONLY,
                "the process in that tmux pane is not the Claude Code snowpea uses; "
                "continue it there",
            )
        bypass = any(proc.is_dangerous(arg) for arg in proc.process_argv(pid))
        # Person-only: never Jarvis, never the engine, never an unattended rule.
        answer = await self.host_api.ask_approval(ApprovalAsk(
            tool="tmux.adopt",
            # ApprovalRequest.args of tmux.adopt (1.53.0): these keys; paneCommand
            # and paneCwd (what tmux says runs there) added by the security review.
            args={
                "cli": "claude",
                "sessionId": self.session_id,
                "tmux": {"session": info["session"], "pane": info["pane"]},
                "cwd": str(self.spec.cwd),
                "paneCommand": info.get("command") or "",
                "paneCwd": info.get("path") or "",
                "unrestricted": bypass,
            },
            risk="high",
            note=("snowpea will type into this tmux pane, which you started yourself; "
                  "permission prompts stay yours to answer there"),
            person_only=True,
        ))
        if not answer.allow:
            raise ExternalError(errors.EXTERNAL_VIEW_ONLY,
                                "typing into that tmux pane was not allowed")
        self.server = server
        self.pane = info["pane"]
        self.session_name = info["session"]
        self.claude_pid = pid
        self.claude_start = start_time
        self.owned = False
        self.dead = False
        if bypass:
            # Started with a permission bypass: snowpea cannot gate it, whatever
            # mode was asked for (controlled: false; only the person sends).
            self.mode = "unrestricted"
            self.actual_mode = "unrestricted"
            self.native_mode = self.actual_native = "bypassPermissions"
        self.started_mode = self.mode
        self.started_native = self.native_mode
        self.started_grant = self.grant
        self.tmux = {"session": info["session"], "pane": info["pane"]}

    async def _verify_pane(self) -> None:
        """Right before typing: still the same Claude Code process in the same pane (M5)."""
        assert self.server is not None and self.pane is not None
        pid = self.claude_pid
        if (
            pid is None
            or (self.claude_start is not None and proc.process_start_time(pid) != self.claude_start)
            or not proc.process_runs(pid, self.spec.executable.path)
        ):
            raise unavailable("the process in the tmux pane changed; snowpea will not type into it")
        info = await self.server.pane_info(self.pane)
        if info is None:
            raise unavailable("the tmux pane is gone")
        if info.get("pid") != str(pid):
            tty = None if self.owned else tty_of(pid)
            if not tty or info.get("tty") != tty:
                raise unavailable("the tmux pane changed; snowpea will not type into it")

    # -- watching -----------------------------------------------------------------
    async def _poll(self) -> None:
        async with self._poll_lock:
            if self.tail is None:
                return
            items = await asyncio.to_thread(self.tail.poll)
        if items:
            await self.host_api.emit(items)

    def _started(self, data: dict[str, Any] | None) -> bool:
        if self._turn_started:
            return True
        assert self.tail is not None
        if self.tail.converter.prompts > self._prompts_before:
            return True
        return (data is not None and data.get("status") in ("busy", "waiting")
                and status_time(data) >= self._sent_at_ms)

    async def _watch(self) -> None:
        interval = self.adapter.poll_interval
        checked_pane = time.monotonic()
        try:
            while not self.closing:
                await asyncio.sleep(interval)
                if self._restarting:
                    continue
                gone = self.claude_pid is None or not proc.pid_alive(self.claude_pid)
                now = time.monotonic()
                if not gone and self.server is not None and self.pane and now - checked_pane > 2:
                    checked_pane = now
                    gone = await self.server.pane_info(self.pane) is None
                await self._poll()
                if gone:
                    if not self.closing:
                        self.dead = True
                        self.turn_id = None
                        await self.host_api.channel_died("the Claude Code pane closed")
                    return
                data = self.status()
                status = data.get("status") if data is not None else None
                if status == "waiting" and self._last_status != "waiting":
                    await self._announce_prompt()
                if self.turn_id is not None:
                    if self._started(data):
                        self._turn_started = True
                    if (self._turn_started and status == "idle" and data is not None
                            and status_time(data) >= self._sent_at_ms):
                        await self._finish_turn()
                self._last_status = status
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken watcher ends the channel visibly
            log.warning("watching the Claude Code pane failed", exc_info=True)
            if not self.closing:
                self.dead = True
                self.turn_id = None
                await self.host_api.channel_died("snowpea lost track of the Claude Code pane")

    async def _announce_prompt(self) -> None:
        assert self.tail is not None
        calls = self.tail.converter.open_calls
        raw_id, (name, _args) = next(reversed(calls.items()), (None, ("a tool", {})))
        hint = self.server.attach_hint(self.session_name or "") if self.server else "tmux attach"
        builder = self.host_api.builder
        builder.note(
            f"Claude Code is asking for permission to use {name}. Answer it in the terminal "
            f"({hint}).",
            key=f"tmux-wait:{self.session_id}:{raw_id or time.time()}",
        )
        await self.host_api.emit(builder.drain())

    async def _finish_turn(self) -> None:
        await asyncio.sleep(0.1)  # the last records land just around the idle switch
        await self._poll()
        builder = self.host_api.builder
        builder.flush()
        items = builder.drain()
        if items:
            await self.host_api.emit(items)
        reason = "interrupted" if self.interrupting else "complete"
        self.turn_id = None
        self.interrupting = False
        self._turn_started = False
        self.turns_done += 1
        await self.host_api.turn_finished(reason)

    # -- turns ------------------------------------------------------------------
    async def send(self, text: str, turn_id: str) -> None:
        refuse_text(text)
        if not self.alive:
            raise unavailable("the Claude Code pane is not running")
        if (self.started_mode != self.mode or self.started_native != self.native_mode
                or self.started_grant is not self.grant):
            await self._restart()
        assert self.server is not None and self.pane is not None and self.tail is not None
        data = self.status()
        status = data.get("status") if data is not None else None
        if status == "waiting":
            raise ExternalError(errors.SESSION_BUSY,
                                "a permission prompt is open in the terminal; answer it there "
                                "first", {"reason": BUSY_TERMINAL_PROMPT})
        if status != "idle":
            raise ExternalError(errors.SESSION_BUSY,
                                "Claude Code is busy in the terminal; wait for it or interrupt it",
                                {"reason": BUSY_TERMINAL_BUSY})
        if self.session_name is not None:
            # A person attached to the pane who typed a moment ago is mid-sentence:
            # never type over them (tmux reports each client's last keypress).
            active = await self.server.last_client_activity(self.session_name)
            if active is not None and time.time() - active < TYPING_QUIET_SEC:
                raise ExternalError(errors.SESSION_BUSY,
                                    "someone is typing in the terminal; try again in a moment",
                                    {"reason": BUSY_TERMINAL_TYPING})
        await self._poll()
        await self._verify_pane()
        self._prompts_before = self.tail.converter.prompts
        self.tail.converter.expect_prompt = text
        self._turn_started = False
        self.interrupting = False
        self._sent_at_ms = time.time() * 1000.0
        self.turn_id = turn_id
        try:
            await self.server.type_text(self.pane, text)
            await asyncio.sleep(0.15 if "\n" in text or len(text) > SEND_KEYS_MAX else 0.05)
            await self.server.key(self.pane, "Enter")
        except ExternalError:
            self.turn_id = None
            raise
        deadline = time.monotonic() + START_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self.turn_id != turn_id or self._started(self.status()):
                self._turn_started = self._turn_started or self.turn_id == turn_id
                return
            await asyncio.sleep(0.1)
        self.turn_id = None
        raise unavailable("Claude Code did not take the prompt in the terminal; check the pane")

    async def interrupt(self) -> None:
        """Escape while Claude is busy, whoever started the turn; nothing otherwise."""
        if self.server is None or self.pane is None or not self.alive:
            return
        data = self.status()
        status = data.get("status") if data is not None else None
        if status == "waiting":
            raise ExternalError(
                errors.EXTERNAL_VIEW_ONLY,
                "a permission prompt is open in the terminal; answer it there (Esc there "
                "declines it)",
            )
        if status == "busy":
            self.interrupting = True
            await self.server.key(self.pane, "Escape")

    async def set_mode(self, mode: str, grant: UnrestrictedGrant | None) -> None:
        if not self.owned and (mode != self.mode or self.native_mode != self.started_native):
            raise ExternalError(
                errors.EXTERNAL_VIEW_ONLY,
                "this pane was started outside snowpea; change its mode there (Shift+Tab)",
            )
        self.mode = mode
        self.grant = grant

    async def _restart(self) -> None:
        """Apply a mode change: start Claude again in a new pane on the same session."""
        if not self.owned:
            raise ExternalError(errors.EXTERNAL_VIEW_ONLY,
                                "this pane was started outside snowpea")
        assert self.session_id is not None
        self._restarting = True
        try:
            await self._stop_pane(force=True)
            resume = self.spec.resume or self.turns_done > 0 or transcript_path(
                self.spec.cwd, self.session_id).exists()
            await self._launch(resume=resume)
        finally:
            self._restarting = False

    # -- close ------------------------------------------------------------------
    async def _stop_pane(self, *, force: bool) -> None:
        server, name = self.server, self.session_name
        if not self.owned or server is None or not name:
            return
        with contextlib.suppress(ExternalError):
            if not force and await server.clients(name):
                # Someone is attached: leave their terminal alone.
                log.info("leaving tmux session %s running: a client is attached", name)
                return
            await server.run("kill-session", "-t", f"={name}", check=False)
        pid = self.claude_pid
        if pid is not None:
            deadline = time.monotonic() + 3.0
            while proc.pid_alive(pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        self.dead = True

    async def close(self) -> None:
        self.closing = True
        watcher = self.watcher
        self.watcher = None
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        await self._stop_pane(force=False)
        self.turn_id = None


__all__ = [
    "ClaudeTmuxChannel",
    "TMUX_MIN_VERSION",
    "Tmux",
    "default_sockets",
    "refuse_text",
    "tmux_executable",
    "tmux_socket_of",
    "tmux_version_ok",
    "tty_of",
    "validate_pane",
]
