"""The tmux bridge: driving an external CLI that runs in the person's tmux pane (1.56.0).

Owner decision (2026-11-17): a session open in another terminal stays view
only, unless that terminal is a **tmux pane**.  Then snowpea drives the same
process through tmux: it types prompts, interrupts, switches the CLI's mode
with the CLI's own slash command or mode key, and brings permission prompts it
recognises into the approval queue.  Reading stays as before (the CLI's
transcript files).  A terminal that is not tmux stays view only.

How the pane is found (never from pane or transcript text): the pid that holds
the session (each adapter's liveness check) -> its controlling terminal
(``/proc/<pid>/stat`` ``tty_nr``; ``ps`` on macOS) -> the pane of a tmux
server whose ``pane_tty`` is that same device.  Servers looked at: the one
named in the process's own ``TMUX`` variable, then the sockets in the person's
tmux folder (``$TMUX_TMPDIR`` or ``/tmp``, ``tmux-<uid>/``) owned by the
person.  Only ``list-panes`` metadata is read to find it.

The hard guard runs before every single key delivery (:meth:`TerminalBridge.guard`):
the pane still exists and is not dead, its tty is still the one found, the CLI
pid is alive with the same start time and still has that tty as its
controlling terminal, its process group is the terminal's foreground group,
and tmux does not report a shell, pager or editor in the foreground.  Any
failure: nothing is sent, the bridge is lost (the session is view only again).
Text that reached a shell prompt would be run, so there is no "try anyway".

Text goes in only as a bracketed paste of a named buffer loaded from stdin
(``load-buffer -b <name> -``, ``paste-buffer -p -d``), then a separate
``send-keys Enter``.  Control characters other than newline and tab are
refused (escape above all), and so is a prompt starting with ``/`` or ``!``
(every CLI's command or shell prefix) — the mode table's own slash commands are
the only slash text snowpea types.  tmux runs with an argv list; nothing is
ever built into a shell command line.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.external import proc
from snowpea_core.external.claude_tmux import (
    PANE_RE,
    Tmux,
    _socket_ok,
    refuse_text,
    set_tmux_binary,
    tmux_executable,
    tmux_socket_of,
)
from snowpea_core.external.proc import ExternalError
from snowpea_core.server import errors

log = logging.getLogger("snowpea.external.tmux")

#: ``list-panes`` columns, tab separated.
LIST_FORMAT = (
    "#{pane_id}\t#{pane_tty}\t#{pane_pid}\t#{session_name}\t#{window_index}"
    "\t#{pane_current_command}\t#{pane_dead}"
)
#: Foreground programs that mean the CLI is not the one reading keys now.
SHELLS = frozenset({
    "sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "tcsh", "csh", "nu", "xonsh",
    "elvish", "pwsh", "ash", "busybox",
})
PAGERS_EDITORS = frozenset({
    "less", "more", "most", "man", "vi", "vim", "nvim", "view", "nano", "emacs", "pico",
    "micro", "hx", "helix", "kak", "joe", "ed", "mc", "tig", "lazygit",
})
#: Keys the bridge may send; anything else is refused before tmux runs.
KEYS = frozenset({"Enter", "Escape", "BTab", "y", "1"})
#: Longest pane snapshot handed out (characters).
SNAPSHOT_MAX = 8_000
#: Longest prompt text carried in an approval.
PROMPT_TEXT_MAX = 2_000
#: Mode-key presses tried before a switch is "unconfirmed".
MAX_CYCLE = 6
#: ``session_busy`` ``details.reason`` values of a refused send.
BUSY_TERMINAL_PROMPT = "terminal_prompt"
BUSY_TERMINAL_BUSY = "terminal_busy"
#: ``external_view_only`` ``details.reason`` when the guard refused.
GUARD_REASON = "terminal_guard"
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


# ---------------------------------------------------------------------------
# panes and processes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pane:
    """One tmux pane, as ``list-panes`` reports it."""

    socket: str
    pane: str
    tty: str
    pid: int
    session: str
    window: str
    command: str
    dead: bool = False

    def wire(self) -> dict[str, str]:
        return {"kind": "tmux", "pane": self.pane, "session": self.session,
                "window": self.window}


def parse_panes(socket: str, out: str) -> list[Pane]:
    panes: list[Pane] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 7 or not PANE_RE.fullmatch(parts[0]) or not parts[2].isdigit():
            continue
        if not parts[1].startswith("/dev/"):
            continue
        panes.append(Pane(socket=socket, pane=parts[0], tty=parts[1], pid=int(parts[2]),
                          session=parts[3][:200], window=parts[4][:20],
                          command=parts[5][:100], dead=parts[6] == "1"))
    return panes


@dataclass(frozen=True)
class ProcFacts:
    """What the guard needs to know about the CLI process."""

    start: str | None
    #: The controlling terminal's device number (``st_rdev`` of its tty), or None.
    tty_dev: int | None
    pgrp: int
    #: The terminal's foreground process group.
    tpgid: int


class Probe:
    """Reads process and tty facts; tests replace it (no real processes needed)."""

    def facts(self, pid: int) -> ProcFacts | None:
        if pid <= 0 or not proc.pid_alive(pid):
            return None
        if sys.platform.startswith("linux"):
            try:
                raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            fields = raw[raw.rfind(")") + 2:].split()
            try:
                pgrp, tty_nr, tpgid = int(fields[2]), int(fields[4]), int(fields[5])
            except (IndexError, ValueError):
                return None
            return ProcFacts(start=proc.process_start_time(pid),
                             tty_dev=tty_nr or None, pgrp=pgrp, tpgid=tpgid)
        ps = proc.trusted_tool("ps") or "/bin/ps"
        try:
            out = subprocess.run([ps, "-o", "tty=,pgid=,tpgid=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        parts = out.split()
        if len(parts) != 3 or parts[0] in ("?", "??"):
            return None
        tty = parts[0] if parts[0].startswith("/dev/") else f"/dev/{parts[0]}"
        try:
            return ProcFacts(start=proc.process_start_time(pid), tty_dev=self.tty_dev(tty),
                             pgrp=int(parts[1]), tpgid=int(parts[2]))
        except ValueError:
            return None

    def tty_dev(self, path: str) -> int | None:
        if not path.startswith("/dev/"):
            return None
        try:
            return os.stat(path).st_rdev
        except OSError:
            return None


PROBE = Probe()


def candidate_sockets(pid: int) -> list[str]:
    """tmux servers to look in for ``pid``'s pane, the process's own first."""
    found: list[str] = []
    own = tmux_socket_of(pid)
    if own:
        found.append(own)
    if hasattr(os, "getuid"):
        base = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
        with contextlib.suppress(OSError):
            for entry in sorted(base.iterdir()):
                path = str(entry)
                if path not in found and _socket_ok(path):
                    found.append(path)
    return found


async def list_panes(socket: str, env: Mapping[str, str] | None = None) -> list[Pane]:
    """Every pane of the server at ``socket`` (metadata only); [] when it does not answer."""
    try:
        server = Tmux(socket_path=socket, env=dict(env) if env is not None else None)
        code, out = await server.run("list-panes", "-a", "-F", LIST_FORMAT, check=False)
    except ExternalError:
        return []
    return parse_panes(socket, out) if code == 0 else []


async def find_pane(
    pid: int,
    *,
    sockets: Sequence[str] | None = None,
    probe: Probe = PROBE,
    env: Mapping[str, str] | None = None,
    cache: dict[str, list[Pane]] | None = None,
) -> Pane | None:
    """The live pane whose terminal is ``pid``'s controlling terminal, or ``None``."""
    if proc.IS_WINDOWS or tmux_executable() is None:
        return None
    facts = probe.facts(pid)
    if facts is None or facts.tty_dev is None:
        return None
    for socket in (sockets if sockets is not None else candidate_sockets(pid)):
        if cache is not None and socket in cache:
            panes = cache[socket]
        else:
            panes = await list_panes(socket, env)
            if cache is not None:
                cache[socket] = panes
        for pane in panes:
            if not pane.dead and probe.tty_dev(pane.tty) == facts.tty_dev:
                return pane
    return None


def clean_screen(text: str, limit: int = SNAPSHOT_MAX) -> str:
    """Pane text as plain, clipped text (untrusted: never acted on)."""
    text = _CONTROL_RE.sub("", text.replace("\r", ""))
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    out = "\n".join(lines)
    return out[-limit:] if len(out) > limit else out


# ---------------------------------------------------------------------------
# per-CLI profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptRule:
    """How one CLI's permission prompt looks on screen and which keys answer it."""

    question: re.Pattern[str]
    choice: re.Pattern[str]
    footer: re.Pattern[str]
    allow_key: str
    deny_key: str
    #: Only a person may answer it (the screen is the only source: Codex).
    person_only: bool


@dataclass(frozen=True)
class TerminalProfile:
    """What snowpea knows about one CLI's terminal UI (evidence in the module docs)."""

    cli: str
    #: The key that stops a running turn, or None when it is not known.
    interrupt: str | None
    #: ``(screen text, native mode id)`` shown while that mode is on.
    indicators: tuple[tuple[str, str], ...] = ()
    #: Text that, with no indicator, confirms the default mode.
    default_marker: str | None = None
    default_mode: str | None = None
    prompt: PromptRule | None = None
    #: The CLI says busy/idle reliably (else a send ends its turn once delivered).
    busy_signal: bool = True


PROFILES: dict[str, TerminalProfile] = {
    # Claude Code 2.1.326: status-line labels "accept edits on", "plan mode on",
    # "auto mode on" (default shows none), the "? for shortcuts" hint, Shift+Tab
    # cycles, /plan "Turn on plan mode", "esc to interrupt"; the permission
    # dialog "Do you want to … ? ❯ 1. Yes … Esc to cancel" (spike S5:
    # 1 = yes once, Esc = no; other numbers differ by tool).
    "claude": TerminalProfile(
        cli="claude",
        interrupt="Escape",
        indicators=(("accept edits on", "acceptEdits"), ("plan mode on", "plan"),
                    ("auto mode on", "auto")),
        default_marker="? for shortcuts",
        default_mode="default",
        prompt=PromptRule(
            question=re.compile(r"^\s*Do you want to .{1,300}\?\s*$"),
            choice=re.compile(r"^\s*(?:❯\s*)?1\.\s+Yes\s*$"),
            footer=re.compile(r"Esc to cancel"),
            allow_key="1", deny_key="Escape", person_only=False,
        ),
    ),
    # codex-cli 0.167.1: no mode label on screen and /permissions opens a picker
    # (no mode switching here); "esc to interrupt" (spike S5); the dialog
    # "Would you like to run the following command? … 1. Yes, proceed (y) …
    # (esc)" (y = once, Esc = no); the reason line is model-written, so only a
    # person answers it.
    "codex": TerminalProfile(
        cli="codex",
        interrupt="Escape",
        prompt=PromptRule(
            question=re.compile(r"^\s*Would you like to (?:run the following command|make the "
                                r"following edits|grant these permissions)\?\s*$"),
            choice=re.compile(r"^\s*(?:›\s*)?1\.\s+Yes, proceed \(y\)\s*$"),
            footer=re.compile(r"\(esc\)"),
            allow_key="y", deny_key="Escape", person_only=True,
        ),
    ),
    # agy 1.5.9: the status line "accept-edits · <model>" (spike S5 capture) and
    # the notices "Accept-edits mode: …", "Auto mode: …", "Plan mode: …"
    # "(shift+tab to cycle)"; "Press esc to interrupt generation."  Prompts were
    # never reached (spike S5): not recognised.  No busy signal.
    "agy": TerminalProfile(
        cli="agy",
        interrupt="Escape",
        indicators=(("accept-edits · ", "accept-edits"), ("Accept-edits mode:", "accept-edits"),
                    ("Auto mode:", "auto"), ("Plan mode:", "plan")),
        busy_signal=False,
    ),
    # cursor-agent 2026.11.13: "Plan (shift+tab to cycle)", "Ask (shift+tab to
    # cycle)" (agent shows neither).  It advertises "ctrl+c to stop", but C-c
    # can also quit a CLI, so snowpea does not send it: no interrupt.  Prompts
    # not recognised.
    "cursor": TerminalProfile(
        cli="cursor",
        interrupt=None,
        indicators=(("Plan (shift+tab to cycle)", "plan"), ("Ask (shift+tab to cycle)", "ask")),
    ),
}


def detect_mode(profile: TerminalProfile, screen: str) -> str | None:
    """The native mode the pane shows (its last lines), or ``None`` when it cannot tell."""
    lines = [line for line in clean_screen(screen).split("\n") if line.strip()][-8:]
    tail = "\n".join(lines)
    for text, mode in profile.indicators:
        if text in tail:
            return mode
    if profile.default_marker and profile.default_marker in tail:
        return profile.default_mode
    return None


@dataclass(frozen=True)
class TerminalPrompt:
    """A permission prompt recognised on the pane."""

    #: The prompt's lines, normalised: the same prompt has the same signature.
    signature: str
    #: Plain, clipped text for the person (untrusted).
    text: str
    question: str


def recognise(profile: TerminalProfile, screen: str) -> TerminalPrompt | None:
    """The permission prompt on screen, when it matches the CLI's rule exactly."""
    rule = profile.prompt
    if rule is None:
        return None
    lines = clean_screen(screen, limit=SNAPSHOT_MAX * 4).split("\n")
    for start in range(len(lines) - 1, -1, -1):
        if not rule.question.match(lines[start]):
            continue
        block = lines[start:start + 14]
        end = next((i for i, line in enumerate(block) if rule.footer.search(line)), None)
        if end is None:
            return None
        block = block[:end + 1]
        if not any(rule.choice.match(line) for line in block):
            return None
        text = "\n".join(line.rstrip() for line in block)
        signature = "\n".join(" ".join(line.split()) for line in block)
        return TerminalPrompt(signature=signature, text=text[:PROMPT_TEXT_MAX],
                              question=lines[start].strip()[:300])
    return None


# ---------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------


class TerminalBridge:
    """One CLI process in one tmux pane: the guard and every key that goes in."""

    def __init__(
        self,
        *,
        cli: str,
        pane: Pane,
        cli_pid: int,
        start: str | None,
        profile: TerminalProfile,
        probe: Probe = PROBE,
        env: Mapping[str, str] | None = None,
        audit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.cli = cli
        self.pane = pane
        self.cli_pid = cli_pid
        self.start = start
        self.profile = profile
        self.probe = probe
        self.server = Tmux(socket_path=pane.socket, env=dict(env) if env is not None else None)
        self.audit = audit
        #: Why the guard refused last (the bridge is lost then), or None.
        self.lost: str | None = None

    # -- the guard -----------------------------------------------------------
    def _refuse(self, reason: str) -> ExternalError:
        self.lost = reason
        log.info("tmux bridge %s %s refused: %s", self.cli, self.pane.pane, reason)
        return ExternalError(errors.EXTERNAL_VIEW_ONLY, reason, {"reason": GUARD_REASON})

    async def guard(self) -> Pane:
        """Raise unless the CLI still reads keys in the same pane (see module docs)."""
        if self.lost is not None:
            raise ExternalError(errors.EXTERNAL_VIEW_ONLY, self.lost, {"reason": GUARD_REASON})
        panes = await list_panes(self.pane.socket, self.server.env)
        now = next((p for p in panes if p.pane == self.pane.pane), None)
        if now is None or now.dead:
            raise self._refuse("the tmux pane is gone")
        if now.tty != self.pane.tty:
            raise self._refuse("the tmux pane's terminal changed")
        facts = self.probe.facts(self.cli_pid)
        if facts is None or (self.start is not None and facts.start != self.start):
            raise self._refuse("the CLI in the tmux pane exited")
        if facts.tty_dev is None or self.probe.tty_dev(now.tty) != facts.tty_dev:
            raise self._refuse("the CLI no longer runs in that tmux pane")
        if facts.tpgid != facts.pgrp:
            raise self._refuse("another program runs in the foreground of that tmux pane")
        name = os.path.basename(now.command.strip()).lower()
        if name in SHELLS or name in PAGERS_EDITORS:
            raise self._refuse(f"{name} runs in the foreground of that tmux pane, not the CLI")
        self.pane = now
        return now

    def _record(self, action: str, by: str, **extra: Any) -> None:
        entry = {"ts": time.time(), "cli": self.cli, "pane": self.pane.pane,
                 "socket": self.pane.socket, "action": action, "by": by, **extra}
        log.info("tmux bridge %s pane %s: %s by %s", self.cli, self.pane.pane, action, by)
        if self.audit is not None:
            with contextlib.suppress(Exception):
                self.audit(entry)

    # -- keys ---------------------------------------------------------------
    async def key(self, name: str, *, by: str = "snowpea") -> None:
        """One key from :data:`KEYS`, behind the guard."""
        if name not in KEYS:
            raise ExternalError(errors.INVALID_PARAMS, f"refused tmux key {name!r}")
        pane = await self.guard()
        await self.server.run("send-keys", "-t", pane.pane, name)
        self._record("key", by, key=name)

    async def send_text(self, text: str, *, by: str = "snowpea") -> None:
        """A prompt: bracketed paste of a buffer loaded from stdin, then Enter."""
        refuse_text(text)
        buffer = f"sp-{uuid.uuid4().hex[:12]}"
        await self.guard()
        await self.server.run("load-buffer", "-b", buffer, "-", data=text.encode("utf-8"))
        try:
            pane = await self.guard()
        except ExternalError:
            with contextlib.suppress(ExternalError):
                await self.server.run("delete-buffer", "-b", buffer, check=False)
            raise
        await self.server.run("paste-buffer", "-p", "-d", "-b", buffer, "-t", pane.pane)
        await asyncio.sleep(0.15)
        pane = await self.guard()
        await self.server.run("send-keys", "-t", pane.pane, "Enter")
        self._record("send", by, chars=len(text))

    async def slash(self, command: str, *, by: str = "snowpea") -> None:
        """A slash command from a mode table (never client text), then Enter."""
        if not re.fullmatch(r"/[a-z][a-z-]{0,30}", command):
            raise ExternalError(errors.INVALID_PARAMS, "invalid slash command")
        pane = await self.guard()
        await self.server.run("send-keys", "-t", pane.pane, "-l", "--", command)
        await asyncio.sleep(0.1)
        pane = await self.guard()
        await self.server.run("send-keys", "-t", pane.pane, "Enter")
        self._record("slash", by, command=command)

    async def capture(self) -> str:
        """The pane's visible text (only this pane, found from the process)."""
        code, out = await self.server.run("capture-pane", "-p", "-t", self.pane.pane,
                                          check=False)
        return out if code == 0 else ""

    async def snapshot(self) -> str:
        return clean_screen(await self.capture())

    # -- modes and prompts ----------------------------------------------------
    async def current_mode(self) -> str | None:
        return detect_mode(self.profile, await self.capture())

    async def switch_mode(self, target: str, terminal: Mapping[str, Any], *,
                          by: str = "person", settle: float = 0.4) -> bool:
        """Switch to ``target`` the table's way; True once the pane shows it."""
        slash = terminal.get("slash")
        if isinstance(slash, str):
            if await self.current_mode() == target:
                return True
            await self.slash(slash, by=by)
            await asyncio.sleep(settle)
            return await self.current_mode() == target
        if terminal.get("cycle"):
            for _ in range(MAX_CYCLE):
                if await self.current_mode() == target:
                    return True
                await self.key("BTab", by=by)
                await asyncio.sleep(settle)
            return await self.current_mode() == target
        raise ExternalError(errors.EXTERNAL_UNAVAILABLE, "this mode cannot be switched there")

    async def prompt(self) -> TerminalPrompt | None:
        return recognise(self.profile, await self.capture())

    async def answer(self, asked: TerminalPrompt, allow: bool, *, by: str) -> bool:
        """Answer ``asked`` with the CLI's key, only if it is still the prompt on screen."""
        rule = self.profile.prompt
        if rule is None:
            return False
        now = await self.prompt()
        if now is None or now.signature != asked.signature:
            log.info("tmux prompt changed before the answer; nothing sent")
            return False
        await self.key(rule.allow_key if allow else rule.deny_key, by=by)
        return True


@dataclass
class AuditLog:
    """Appends one JSON line per bridged key delivery (``external/tmux-sends.jsonl``, 0600)."""

    path: Path
    _lock: Any = field(default=None, repr=False)

    def __call__(self, entry: dict[str, Any]) -> None:
        import json

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


__all__ = [
    "BUSY_TERMINAL_BUSY",
    "BUSY_TERMINAL_PROMPT",
    "GUARD_REASON",
    "KEYS",
    "PROFILES",
    "AuditLog",
    "Pane",
    "Probe",
    "ProcFacts",
    "PromptRule",
    "TerminalBridge",
    "TerminalProfile",
    "TerminalPrompt",
    "candidate_sockets",
    "clean_screen",
    "detect_mode",
    "find_pane",
    "list_panes",
    "parse_panes",
    "recognise",
    "set_tmux_binary",
]
