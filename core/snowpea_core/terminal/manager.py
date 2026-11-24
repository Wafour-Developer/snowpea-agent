"""TerminalManager: the daemon's terminals (M37 §2.1–§2.5).

Each terminal is a detached holder process (:mod:`.holder`) the manager talks
to over :class:`~.link.HolderLink`; the manager keeps the parsed screen
(:mod:`.screen`), the status machine (:mod:`.status`), the input gate
(:mod:`.input`) and the viewers (:mod:`.viewers`).

* :meth:`TerminalManager.create` takes an already resolved :class:`TerminalSpec`
  (argv, env, cwd); profiles and permission options are built elsewhere (T3).
* :meth:`TerminalManager.start` reconnects to every live holder after a daemon
  restart and rebuilds its screen from the holder's tail; a meta file whose
  holder is gone becomes an exited entry (it left an exit file) or a *lost* one.
* :meth:`TerminalManager.close` runs the kill sequence: HUP, TERM after 500 ms,
  KILL after another 500 ms, then up to 2 s for the exit.
* While no interactive viewer is attached, the manager answers the program's
  terminal queries itself (DA1/DA2, DSR, OSC 10/11, kitty, 7501).

Events go out through ``emit(method, params)`` (daemon-wide: ``terminal.status``,
``terminal.created``, ``terminal.exit``, ``terminal.title``, ``terminal.closed``)
and ``send(conn, method, params)`` (``terminal.data``, to one viewer only).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import signal as _signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.terminal import osc
from snowpea_core.terminal.input import GateRefused, InputGate
from snowpea_core.terminal.link import HolderError, HolderLink, spawn_holder, terminals_dir
from snowpea_core.terminal.screen import HEADLESS_COLS, HEADLESS_ROWS, Screen
from snowpea_core.terminal.status import HookReport, RulesLoader, StatusMachine, TerminalStatus
from snowpea_core.terminal.viewers import Delivery, ViewerSet

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
DEFAULT_MAX_TERMINALS = 16
#: Kill sequence (§2.1).
HUP_WAIT = 0.5
TERM_WAIT = 0.5
KILL_WAIT = 2.0
#: How often the screen rules and the blocker republish are looked at.
POLL_INTERVAL = 0.1
#: Reconnect attempts after a holder link drops while the holder still runs.
RECONNECT_TRIES = 5
#: Raw output kept per terminal to rebuild (reflow) the screen after a resize;
#: pyte replays about 1 MiB a second, and the holder's tail is 1 MiB too.
RAW_KEEP = 1024 * 1024
REFLOW_DEBOUNCE = 0.15
#: How long read / attach wait for a pending reflow before using the old screen.
REFLOW_WAIT = 5.0
TERMINAL_ID_RE = re.compile(r"^t[0-9a-f]{8}$")
#: A CLI's session id (as ``external.proc.SESSION_ID_RE``; kept here so this
#: module does not import the external package at load time).
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ANSI_RE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")

#: Wire error codes (existing ones: the protocol has no terminal-specific codes).
TERMINAL_LIMIT = errors.UNAVAILABLE
TERMINAL_EXITED = errors.UNAVAILABLE

Emit = Callable[[str, dict[str, Any]], Awaitable[None] | None]
Send = Callable[[Any, str, dict[str, Any]], Awaitable[None] | None]


class TerminalError(RpcError):
    pass


def _observe_only(action: str) -> TerminalError:
    """An observe-mode viewer may watch, never type or resize: a clear refusal."""
    return TerminalError(
        errors.UNAVAILABLE,
        f"this connection attached as an observer and cannot {action} the terminal; "
        "attach with mode interactive first",
        {"reason": "observe_only"},
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class TerminalSpec:
    """A resolved launch: what to run, where and how it is labelled."""

    argv: list[str]
    env: dict[str, str]
    cwd: str
    profile: str = "shell"
    title: str | None = None
    cols: int = HEADLESS_COLS
    rows: int = HEADLESS_ROWS
    labels: dict[str, str] = field(default_factory=dict)
    parent: str | None = None
    options: list[str] = field(default_factory=list)
    dangerous: bool = False
    #: ``{cli, sessionId?}`` for an external CLI terminal.
    external: dict[str, Any] | None = None
    snowpea_session_id: str | None = None
    #: The profile's submit key (a key name for :func:`.input.encode_key`).
    submit_key: str = "enter"
    #: Screen-rules id handed to the rules loader (``claude``, ``codex`` …).
    rules: str | None = None
    #: First input, sent through the input gate once the program settles.
    prompt: str | None = None

    def public(self) -> dict[str, Any]:
        """What the meta file keeps (never the environment)."""
        return {
            "argv": list(self.argv), "cwd": self.cwd, "profile": self.profile,
            "title": self.title, "labels": dict(self.labels), "parent": self.parent,
            "options": list(self.options), "dangerous": self.dangerous,
            "external": self.external, "snowpeaSessionId": self.snowpea_session_id,
            "submitKey": self.submit_key, "rules": self.rules,
        }

    @classmethod
    def from_meta(cls, meta: dict[str, Any]) -> TerminalSpec:
        spec = meta.get("spec") or {}
        return cls(
            argv=list(spec.get("argv") or []), env={}, cwd=str(spec.get("cwd") or ""),
            profile=str(spec.get("profile") or "shell"), title=spec.get("title"),
            labels=dict(spec.get("labels") or {}), parent=spec.get("parent"),
            options=list(spec.get("options") or []), dangerous=bool(spec.get("dangerous")),
            external=spec.get("external"), snowpea_session_id=spec.get("snowpeaSessionId"),
            submit_key=str(spec.get("submitKey") or "enter"), rules=spec.get("rules"),
        )


@dataclass
class _Waiter:
    event: asyncio.Event
    #: Literal text to wait for (never a regex: security review M5, no ReDoS on the loop).
    needle: str | None = None
    text: str = ""
    matched: str = ""


class Terminal:
    """One terminal as the daemon sees it."""

    def __init__(self, manager: TerminalManager, terminal_id: str, spec: TerminalSpec, *,
                 created_at: str, launched_at: float) -> None:
        self.manager = manager
        self.id = terminal_id
        self.spec = spec
        self.created_at = created_at
        self.screen = Screen(spec.cols, spec.rows)
        self.status = StatusMachine(
            rules=manager.rules_for(spec.rules), clock=manager.clock, launched_at=launched_at,
            on_change=self._status_changed,
        )
        self.viewers = ViewerSet(self.screen.repaint, clock=manager.clock)
        self.gate = InputGate(
            self._write_pty,
            state=lambda: self.status.state,
            turn=lambda: self.status.status.turn,
            last_output_at=lambda: self.last_output_at,
            bracketed_paste=lambda: self.screen.bracketed_paste,
            app_cursor=lambda: self.screen.app_cursor,
            kitty_flags=lambda: self.screen.kitty_flags,
            reports_status=lambda: self.status.reports_status,
            launched_at=launched_at,
            submit_key=spec.submit_key,
            clock=manager.clock,
        )
        self.gate.on_human_submit = self.status.on_human_submit
        self.link: HolderLink | None = None
        self.holder_pid: int | None = None
        self.holder_started: str | None = None
        self.pid: int | None = None
        self.socket: str = ""
        self.token: str = ""
        self.report_token: str = ""
        self.alive = False
        self.lost = False
        self.exit_code: int | None = None
        self.closing = False
        self.last_output_at = float("-inf")
        self.session_id: str | None = (spec.external or {}).get("sessionId")
        self.transcript_path: str | None = None
        #: The command a permission hook reported while blocked (security review H3).
        self.hook_command: str | None = None
        self._waiters: list[_Waiter] = []
        self._exited = asyncio.Event()
        #: Raw output (the last :data:`RAW_KEEP` bytes) for rebuilding after a resize.
        self.raw = bytearray()
        self.raw_total = 0
        self._reflow_gen = 0
        self._reflow_task: asyncio.Future[None] | None = None
        #: Started by this daemon and not yet connected (see :meth:`on_data`).
        self.fresh = False

    # -- info ------------------------------------------------------------------
    @property
    def title(self) -> str:
        return self.screen.title or self.spec.title or self.spec.profile

    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "terminalId": self.id,
            "profile": self.spec.profile,
            "title": self.title,
            "cwd": self.spec.cwd,
            "cols": self.screen.cols,
            "rows": self.screen.rows,
            "status": self.status.status.wire(),
            "alive": self.alive,
            "pid": self.pid if self.alive else None,
            "exitCode": self.exit_code,
            "parent": self.spec.parent,
            "labels": dict(self.spec.labels),
            "options": list(self.spec.options),
            "dangerous": self.spec.dangerous,
            "createdAt": self.created_at,
        }
        if self.lost:
            out["lost"] = True
            out["labels"]["snowpea.lost"] = "1"
        if self.spec.external:
            ref = {"cli": self.spec.external.get("cli")}
            if self.session_id:
                ref["sessionId"] = self.session_id
            out["external"] = ref
        if self.spec.snowpea_session_id:
            out["snowpeaSessionId"] = self.spec.snowpea_session_id
        return out

    def meta(self) -> dict[str, Any]:
        return {
            "id": self.id, "spec": self.spec.public(), "pid": self.holder_pid,
            "holderStarted": self.holder_started, "child": self.pid, "socket": self.socket,
            "token": self.token, "reportToken": self.report_token, "createdAt": self.created_at,
            "sessionId": self.session_id, "transcriptPath": self.transcript_path,
            "seen": self.status.status.seen,
        }

    # -- output ----------------------------------------------------------------
    def on_data(self, data: bytes, tail: bool) -> None:
        if tail and self.fresh:
            # The first tail of a terminal we just started is its first output:
            # treated as live (its start-up queries need answers).
            self.fresh = False
            if not data:
                return
        elif tail:
            self._replay(data)
            return
        now = self.manager.clock()
        self.last_output_at = now
        self.status.on_output(now)
        self._keep_raw(data)
        events = self.screen.feed(data)
        self._handle_events(events, live=True)
        self.manager.deliver(self, self.viewers.broadcast(data))
        if self._waiters:
            self._feed_waiters(data)

    def _replay(self, data: bytes) -> None:
        """A reattach: rebuild the screen from the holder's tail (no queries answered)."""
        self.screen = Screen(self.screen.cols, self.screen.rows)
        self.viewers._repaint = self.screen.repaint
        self.raw.clear()
        self._keep_raw(data)
        events = self.screen.feed(data)
        self._handle_events(events, live=False)
        for viewer in self.viewers:
            viewer.needs_repaint = True
            viewer.backlog.clear()
            viewer.backlog_bytes = 0
            self.manager.deliver(self, self.viewers._flush(viewer))

    def _handle_events(self, events: list[osc.Event], *, live: bool) -> None:
        for event in events:
            if isinstance(event, osc.StatusRecord):
                # The root record, or one naming this terminal itself (older
                # snowpea TUIs sent id=<terminalId>; security review H4).
                if event.root or event.id == self.id:
                    self.status.apply_osc(event.state, kind=event.kind, progress=event.progress,
                                          message=event.msg)
            elif isinstance(event, osc.TitleChange):
                if live:
                    self.manager.emit_soon("terminal.title", {"terminalId": self.id,
                                                              "title": self.title})
            elif isinstance(event, osc.Query) and live and not self.viewers.has_human:
                reply = osc.answer(event, cursor=self.screen.cursor,
                                   kitty_flags=self.screen.kitty_flags)
                if reply:
                    self._write_pty(reply)

    def _feed_waiters(self, data: bytes) -> None:
        text = _ANSI_RE.sub(b"", data).decode("utf-8", "replace")
        for waiter in self._waiters:
            if not waiter.needle or waiter.matched:
                continue
            window = waiter.text + text
            if waiter.needle in window:
                waiter.matched = waiter.needle
                waiter.event.set()
            # Keep only what a needle split across chunks could start with.
            waiter.text = window[-(len(waiter.needle) - 1):] if len(waiter.needle) > 1 else ""

    def on_exit(self, code: int | None, *, announce: bool = True) -> None:
        if self._exited.is_set():
            return
        self.alive = False
        self.exit_code = code
        self.status.on_exit(code)
        self._exited.set()
        if self.link is not None:
            self.link.bye()
        self._wake()
        if announce:
            self.manager.emit_soon("terminal.exit", {"terminalId": self.id, "exitCode": code})

    def _status_changed(self, status: TerminalStatus) -> None:
        self._wake()
        self.manager.emit_soon("terminal.status", {"terminalId": self.id,
                                                   "status": status.wire()})

    def _wake(self) -> None:
        for waiter in self._waiters:
            waiter.event.set()

    # -- input -----------------------------------------------------------------
    def _write_pty(self, data: bytes) -> None:
        if self.link is not None and self.alive:
            self.link.write(data)

    def apply_size(self) -> None:
        owner = self.viewers.size_owner()
        cols, rows = (owner.cols, owner.rows) if owner else (HEADLESS_COLS, HEADLESS_ROWS)
        assert cols and rows
        if (cols, rows) != (self.screen.cols, self.screen.rows):
            # pyte does not reflow: resize now (cheap), then rebuild the screen
            # from the raw output at the new size off the event loop.
            self.screen.resize(cols, rows)
            if self.link is not None and self.alive:
                self.link.resize(cols, rows)
            self._schedule_reflow()

    # -- reflow ----------------------------------------------------------------
    def _keep_raw(self, data: bytes) -> None:
        self.raw += data
        self.raw_total += len(data)
        if len(self.raw) > RAW_KEEP:
            del self.raw[: len(self.raw) - RAW_KEEP]

    def _schedule_reflow(self) -> None:
        self._reflow_gen += 1
        self._reflow_task = asyncio.ensure_future(self._reflow(self._reflow_gen))

    async def _reflow(self, gen: int) -> None:
        await asyncio.sleep(REFLOW_DEBOUNCE)  # a drag sends many sizes: rebuild the last
        if gen != self._reflow_gen:
            return
        cols, rows = self.screen.cols, self.screen.rows
        snapshot, upto = bytes(self.raw), self.raw_total
        rebuilt = await asyncio.to_thread(_rebuild_screen, snapshot, cols, rows)
        if gen != self._reflow_gen or (cols, rows) != (self.screen.cols, self.screen.rows):
            return
        missed = self.raw_total - upto
        if missed > len(self.raw):
            self._schedule_reflow()  # more output than we keep arrived meanwhile
            return
        if missed:
            rebuilt.feed(bytes(self.raw[-missed:]))
        if not rebuilt.title:
            rebuilt.title = self.screen.title
        self.screen = rebuilt
        self.viewers._repaint = rebuilt.repaint

    async def settled(self, timeout: float = REFLOW_WAIT) -> None:
        """Wait for a pending reflow (read and repaint then see the new size's lines)."""
        task = self._reflow_task
        while task is not None and not task.done():
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout)
            if task is self._reflow_task:
                break
            task = self._reflow_task


class TerminalManager:
    def __init__(
        self,
        home: Path | str,
        *,
        emit: Emit | None = None,
        send: Send | None = None,
        rules_loader: RulesLoader | None = None,
        max_terminals: int | Callable[[], int] = DEFAULT_MAX_TERMINALS,
        clock: Callable[[], float] = time.monotonic,
        holder_command: list[str] | None = None,
        report_address: str | Callable[[], str | None] | None = None,
    ) -> None:
        self.home = Path(home)
        self._emit = emit
        self._send = send
        self._rules_loader = rules_loader
        self._max = max_terminals
        self.clock = clock
        self._holder_command = holder_command
        #: How often a holder checks its terminal still exists (tests shorten it).
        self.holder_watch_interval: float | None = None
        self._report_address = report_address
        self._terminals: dict[str, Terminal] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Future[Any]] = set()
        self._lock = asyncio.Lock()
        self.started = False
        #: In-process listeners (Jarvis's intake, external pty sessions):
        #: ``listener(method, params)`` for every event, plus the in-process
        #: ``terminal.sessionId`` when a hook names the CLI's session.
        self.listeners: list[Callable[[str, dict[str, Any]], Any]] = []

    # -- plumbing ----------------------------------------------------------------
    def rules_for(self, rules_id: str | None) -> Any:
        if not rules_id or self._rules_loader is None:
            return None
        try:
            return self._rules_loader(rules_id)
        except Exception:  # noqa: BLE001 - a broken rule file must not stop a terminal
            log.warning("could not load screen rules %s", rules_id, exc_info=True)
            return None

    def set_rules_loader(self, loader: RulesLoader | None) -> None:
        self._rules_loader = loader

    @property
    def max_terminals(self) -> int:
        return int(self._max() if callable(self._max) else self._max)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def tell_listeners(self, method: str, params: dict[str, Any]) -> None:
        for listener in list(self.listeners):
            try:
                result = listener(method, params)
            except Exception:  # noqa: BLE001 - a listener must never fail the terminal
                log.debug("terminal listener failed on %s", method, exc_info=True)
                continue
            if asyncio.iscoroutine(result):
                self._spawn(self._guard(result))

    def emit_soon(self, method: str, params: dict[str, Any]) -> None:
        self.tell_listeners(method, params)
        if self._emit is None:
            return
        try:
            result = self._emit(method, params)
        except Exception:  # noqa: BLE001 - an event must never fail the terminal
            log.debug("terminal event %s failed", method, exc_info=True)
            return
        if asyncio.iscoroutine(result):
            self._spawn(self._guard(result))

    @staticmethod
    async def _guard(coro: Awaitable[Any]) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            log.debug("terminal event failed", exc_info=True)

    def deliver(self, term: Terminal, deliveries: list[Delivery]) -> None:
        if self._send is None:
            return
        for item in deliveries:
            if getattr(item.conn, "closed", False):
                continue
            try:
                result = self._send(item.conn, "terminal.data", item.params(term.id))
            except Exception:  # noqa: BLE001
                continue
            if asyncio.iscoroutine(result):
                self._spawn(self._guard(result))

    def _dir(self) -> Path:
        return terminals_dir(self.home)

    def _write_meta(self, term: Terminal) -> None:
        path = self._dir() / f"{term.id}.json"
        tmp = path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(term.meta(), handle)
        os.replace(tmp, path)

    def _remove_files(self, terminal_id: str, socket: str = "") -> None:
        remove_terminal_files(self._dir(), terminal_id, socket)

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        """Reconnect to live holders; turn dead ones into exited or lost entries."""
        if self.started:
            return
        self.started = True
        folder = self._dir()
        for path in sorted(folder.glob("t*.json")):
            if path.name.endswith(".exit.json") or not TERMINAL_ID_RE.match(path.stem):
                continue
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            try:
                await self._restore(meta)
            except Exception:  # noqa: BLE001 - one bad entry must not stop the rest
                log.warning("could not restore terminal %s", path.stem, exc_info=True)
        self._poll_task = asyncio.ensure_future(self._poll_loop())

    async def _restore(self, meta: dict[str, Any]) -> None:
        terminal_id = str(meta.get("id") or "")
        if not TERMINAL_ID_RE.match(terminal_id) or terminal_id in self._terminals:
            return
        spec = TerminalSpec.from_meta(meta)
        term = Terminal(self, terminal_id, spec, created_at=str(meta.get("createdAt") or ""),
                        launched_at=self.clock() - 3600)
        term.holder_pid = meta.get("pid") if isinstance(meta.get("pid"), int) else None
        term.holder_started = meta.get("holderStarted")
        term.pid = meta.get("child") if isinstance(meta.get("child"), int) else None
        term.socket = str(meta.get("socket") or "")
        term.token = str(meta.get("token") or "")
        term.report_token = str(meta.get("reportToken") or "")
        term.session_id = meta.get("sessionId") or term.session_id
        term.transcript_path = meta.get("transcriptPath")
        if meta.get("seen") is False:
            term.status._status = TerminalStatus(seen=False)
        self._terminals[terminal_id] = term
        if term.holder_pid and _pid_runs(term.holder_pid):  # the link proves the rest
            try:
                await self._connect(term)
                return
            except HolderError:
                log.info("terminal %s: holder did not answer", terminal_id)
        folder = self._dir()
        exit_file = folder / f"{terminal_id}.exit.json"
        tail_file = folder / f"{terminal_id}.tail"
        with contextlib.suppress(OSError):
            term._replay(tail_file.read_bytes())
        try:
            code = json.loads(exit_file.read_text(encoding="utf-8")).get("code")
        except (OSError, ValueError):
            # No holder and no exit record (a reboot, a killed holder): lost.
            # It can be resumed from its CLI session id (§2.8).
            term.lost = True
            term.on_exit(None, announce=False)
            return
        term.on_exit(code if isinstance(code, int) else None, announce=False)

    async def _connect(self, term: Terminal) -> None:
        link = HolderLink(term.socket, term.token, on_data=term.on_data, on_exit=term.on_exit,
                          on_close=lambda: self._link_closed(term))
        # Set first: the tail read inside connect() may already answer a query.
        term.link = link
        try:
            hello = await link.connect(tail=True)
        except HolderError:
            term.link = None
            raise
        if isinstance(hello.get("child"), int):
            term.pid = hello["child"]
        if not hello.get("alive", True):
            term.alive = True
            term.on_exit(hello.get("exitCode"))
        else:
            term.alive = True
            # The holder keeps the size it last had; make it ours.
            term.apply_size()
            link.resize(term.screen.cols, term.screen.rows)

    def _link_closed(self, term: Terminal) -> None:
        term.link = None
        if term.closing or not term.alive:
            return
        self._spawn(self._reconnect(term))

    async def _reconnect(self, term: Terminal) -> None:
        for attempt in range(RECONNECT_TRIES):
            await asyncio.sleep(0.2 * (attempt + 1))
            if term.closing or not term.alive or term.id not in self._terminals:
                return
            if not (term.holder_pid and _pid_runs(term.holder_pid)):
                break
            try:
                await self._connect(term)
                return
            except HolderError:
                continue
        exit_file = self._dir() / f"{term.id}.exit.json"
        try:
            code = json.loads(exit_file.read_text(encoding="utf-8")).get("code")
        except (OSError, ValueError):
            term.lost = True
            term.on_exit(None)
            return
        term.on_exit(code if isinstance(code, int) else None)

    async def shutdown(self) -> None:
        """Daemon stop: drop the links; holders and their programs keep running."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
            self._poll_task = None
        for term in list(self._terminals.values()):
            if term.alive:
                with contextlib.suppress(Exception):
                    self._write_meta(term)
            if term.link is not None:
                term.closing = True
                link, term.link = term.link, None
                await link.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._terminals.clear()
        self.started = False

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            now = self.clock()
            for term in list(self._terminals.values()):
                if not term.alive:
                    continue
                try:
                    term.status.poll(lambda t=term: (t.screen.text(), t.screen.title), now)
                except Exception:  # noqa: BLE001
                    log.debug("status poll failed for %s", term.id, exc_info=True)

    # -- create / list -----------------------------------------------------------
    def get(self, terminal_id: str) -> Terminal:
        term = self._terminals.get(terminal_id)
        if term is None:
            raise TerminalError(errors.NOT_FOUND, f"no terminal {terminal_id}")
        return term

    def list(self, *, parent: str | None = None, alive_only: bool = False) -> list[dict[str, Any]]:
        out = []
        for term in self._terminals.values():
            if parent is not None and term.spec.parent != parent:
                continue
            if alive_only and not term.alive:
                continue
            out.append(term.info())
        return out

    def terminals(self) -> list[Terminal]:
        return list(self._terminals.values())

    async def create(self, spec: TerminalSpec) -> dict[str, Any]:
        if not spec.argv:
            raise TerminalError(errors.INVALID_PARAMS, "nothing to run")
        if not Path(spec.cwd).is_dir():
            raise TerminalError(errors.INVALID_PARAMS, f"no such folder: {spec.cwd}")
        async with self._lock:
            alive = sum(1 for t in self._terminals.values() if t.alive)
            if alive >= self.max_terminals:
                raise TerminalError(
                    TERMINAL_LIMIT,
                    f"at most {self.max_terminals} terminals run at once (terminals.max)",
                )
            terminal_id = "t" + secrets.token_hex(4)
            while terminal_id in self._terminals:
                terminal_id = "t" + secrets.token_hex(4)
            term = Terminal(self, terminal_id, spec, created_at=_utc_now(),
                            launched_at=self.clock())
            term.report_token = secrets.token_urlsafe(24)
            env = dict(spec.env)
            env.setdefault("TERM", "xterm-256color")
            env.setdefault("COLORTERM", "truecolor")
            env["TERM_PROGRAM"] = "snowpea"
            env["SNOWPEA_TERMINAL_ID"] = terminal_id
            env["SNOWPEA_TERMINAL_TOKEN"] = term.report_token
            report = self._report_address() if callable(self._report_address) else (
                self._report_address
            )
            if report:
                env["SNOWPEA_TERMINAL_REPORT"] = report
            core_bin = _core_bin()
            if core_bin:
                # The hook script runs "$SNOWPEA_CORE_BIN" hook report ….
                env["SNOWPEA_CORE_BIN"] = core_bin
            self._terminals[terminal_id] = term
            try:
                self._write_meta(term)
                ready = await spawn_holder(
                    self.home, terminal_id, argv=spec.argv, env=env, cwd=spec.cwd,
                    cols=spec.cols, rows=spec.rows, command=self._holder_command,
                    watch_interval=self.holder_watch_interval,
                )
            except (HolderError, OSError, TimeoutError) as exc:
                self._terminals.pop(terminal_id, None)
                self._remove_files(terminal_id)
                raise TerminalError(
                    errors.UNAVAILABLE, f"could not start the terminal: {exc}"
                ) from exc
            term.holder_pid = ready["pid"]
            term.holder_started = _start_time(ready["pid"])
            term.pid = ready["child"]
            term.socket = ready["socket"]
            term.token = ready["token"]
            term.alive = True
            term.fresh = True
            self._write_meta(term)
            try:
                await self._connect(term)
            except HolderError as exc:
                term.alive = False
                term.lost = True
                raise TerminalError(errors.UNAVAILABLE, str(exc)) from exc
        info = term.info()
        self.emit_soon("terminal.created", {"terminal": info})
        if spec.prompt:
            self._spawn(self._first_prompt(term, spec.prompt))
        return info

    async def _first_prompt(self, term: Terminal, prompt: str) -> None:
        with contextlib.suppress(GateRefused):
            await term.gate.send(prompt)

    # -- viewers ---------------------------------------------------------------
    async def attach(self, terminal_id: str, conn: Any, mode: str = "interactive",
                     cols: int | None = None, rows: int | None = None) -> dict[str, Any]:
        term = self.get(terminal_id)
        if mode not in ("interactive", "observe"):
            raise TerminalError(errors.INVALID_PARAMS, f"unknown attach mode {mode}")
        viewer = term.viewers.attach(conn, mode, cols, rows)
        if mode == "interactive" and term.alive:
            term.apply_size()
        viewer.paused = True  # output during the wait is in the repaint below
        try:
            await term.settled()
            repaint = term.screen.repaint()
            seq = term.viewers.seq
        finally:
            viewer.paused = False
        return {
            "repaint": base64.b64encode(repaint).decode("ascii"),
            "seq": seq,
            "cols": term.screen.cols,
            "rows": term.screen.rows,
            "status": term.status.status.wire(),
            "title": term.title,
            "info": term.info(),
        }

    def detach(self, terminal_id: str, conn: Any) -> bool:
        term = self._terminals.get(terminal_id)
        if term is None:
            return False
        gone = term.viewers.detach(conn)
        if gone and term.alive:
            term.apply_size()
        return gone

    def detach_all(self, conn: Any) -> None:
        """A connection closed: it leaves every terminal it viewed (none is killed)."""
        for term in list(self._terminals.values()):
            if term.viewers.detach(conn) and term.alive:
                term.apply_size()

    async def write(self, terminal_id: str, conn: Any, data: bytes, *, human: bool = True) -> None:
        """A viewer's keys.  An observer may not type; a writer that never
        attached (a script) may, but takes no part in sizing."""
        term = self._alive(terminal_id)
        viewer = term.viewers.get(conn)
        if viewer is not None and not viewer.interactive:
            raise _observe_only("type into")
        if viewer is not None:
            owner_before = term.viewers.size_owner()
            viewer.touch()
            if term.viewers.size_owner() is not owner_before:
                term.apply_size()
        if human:
            await term.gate.human(data)
        else:
            # A viewer's terminal answering a query: straight through, no gate.
            term._write_pty(data)

    async def resize(self, terminal_id: str, conn: Any, cols: int, rows: int) -> dict[str, int]:
        term = self.get(terminal_id)
        if not term.viewers.resize(conn, cols, rows):
            viewer = term.viewers.get(conn)
            if viewer is not None and not viewer.interactive:
                raise _observe_only("resize")
            raise TerminalError(errors.INVALID_PARAMS, "attach before resizing")
        if term.alive:
            term.apply_size()
        return {"cols": term.screen.cols, "rows": term.screen.rows}

    def ack(self, terminal_id: str, conn: Any, nbytes: int) -> None:
        term = self.get(terminal_id)
        self.deliver(term, term.viewers.ack(conn, nbytes))

    # -- agents ----------------------------------------------------------------
    def _alive(self, terminal_id: str) -> Terminal:
        term = self.get(terminal_id)
        if not term.alive:
            raise TerminalError(TERMINAL_EXITED, f"terminal {terminal_id} has ended")
        return term

    async def settle(self, terminal_id: str) -> None:
        """Wait for a pending reflow of this terminal (see :meth:`Terminal.settled`)."""
        await self.get(terminal_id).settled()

    def read(self, terminal_id: str, lines: int = 60) -> dict[str, Any]:
        term = self.get(terminal_id)
        history = max(0, int(lines) - term.screen.rows)
        text = term.screen.text(history)
        rows = text.split("\n")
        if len(rows) > lines:
            text = "\n".join(rows[-lines:])
        return {"text": text, "status": term.status.status.wire(), "title": term.title,
                "alive": term.alive, "exitCode": term.exit_code}

    async def send(self, terminal_id: str, text: str, *, force: bool = False,
                   submit: bool = True) -> dict[str, Any]:
        term = self._alive(terminal_id)
        try:
            result = await term.gate.send(text, force=force, submit=submit)
        except GateRefused as exc:
            if exc.reason == "blocked":
                return {"accepted": False, "sent": False, "reason": "blocked"}
            raise TerminalError(errors.INVALID_PARAMS, str(exc)) from exc
        return {"accepted": result.accepted, "sent": True}

    async def keys(self, terminal_id: str, keys: list[str], *,
                   guard: Callable[[], None] | None = None) -> None:
        term = self._alive(terminal_id)
        try:
            await term.gate.keys(keys, guard=guard)
        except GateRefused as exc:
            raise TerminalError(errors.INVALID_PARAMS, str(exc)) from exc

    async def interrupt(self, terminal_id: str) -> None:
        term = self._alive(terminal_id)
        await term.gate.interrupt()

    async def wait(self, terminal_id: str, timeout: float = 60.0,
                   output: str | None = None) -> dict[str, Any]:
        # A literal substring, never a regex: a pattern from a client or a
        # prompt-injected Jarvis must not be able to stall the event loop (M5).
        needle = output or None
        term = self.get(terminal_id)
        start_turn = term.status.status.turn
        was_working = term.status.state == "working"
        waiter = _Waiter(asyncio.Event(), needle)
        term._waiters.append(waiter)
        deadline = self.clock() + float(timeout)
        try:
            while True:
                if not term.alive:
                    return self._wait_result(term, "exit")
                if waiter.matched:
                    return self._wait_result(term, "match", waiter.matched)
                state = term.status.state
                if state == "blocked":
                    return self._wait_result(term, "blocked")
                if (was_working and state != "working") or term.status.status.turn != start_turn:
                    return self._wait_result(term, "left_working")
                if state == "working":
                    was_working = True
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return self._wait_result(term, "timeout")
                waiter.event.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(waiter.event.wait(), min(remaining, 1.0))
        finally:
            with contextlib.suppress(ValueError):
                term._waiters.remove(waiter)

    def _wait_result(self, term: Terminal, reason: str, text: str = "") -> dict[str, Any]:
        return {"reason": reason, "status": term.status.status.wire(), "text": text}

    def seen(self, terminal_id: str) -> dict[str, Any]:
        term = self.get(terminal_id)
        term.status.mark_seen()
        with contextlib.suppress(OSError):
            if term.alive:
                self._write_meta(term)
        return term.status.status.wire()

    # -- hook reports ------------------------------------------------------------
    def check_report_token(self, terminal_id: str, token: str) -> Terminal:
        term = self._terminals.get(terminal_id)
        if (term is None or not term.report_token or not token
                or not hmac.compare_digest(term.report_token.encode(), str(token).encode())):
            raise TerminalError(errors.UNAUTHORIZED, "bad terminal token")
        return term

    def report(self, terminal_id: str, token: str, report: HookReport) -> dict[str, Any]:
        """``terminal.report`` from a CLI hook: token-checked, then ranked by the status machine."""
        term = self.check_report_token(terminal_id, token)
        return self._apply_report(term, report)

    # The report socket's sink (T2's ``terminal.report.ReportSink``): it checks
    # the token itself with :meth:`report_token`, then hands the report over.
    def report_token(self, terminal_id: str) -> str | None:
        term = self._terminals.get(terminal_id)
        return term.report_token if term is not None and term.report_token else None

    def apply_hook_report(self, terminal_id: str, report: Any) -> None:
        term = self._terminals.get(terminal_id)
        if term is not None:
            self._apply_report(term, report)

    def _apply_report(self, term: Terminal, report: Any) -> dict[str, Any]:
        changed = False
        session_id = getattr(report, "session_id", None)
        transcript = getattr(report, "transcript_path", None)
        # Anything in the terminal can send a report with its token (security
        # review M1): an id must look like one, and only the first is taken — a
        # Claude Code terminal already has the id snowpea gave it.
        if session_id and not _SESSION_ID_RE.match(str(session_id)):
            session_id = None
        if session_id and term.session_id and session_id != term.session_id:
            log.info("terminal %s: ignored a report naming another session", term.id)
            session_id = None
        if session_id and term.session_id is None:
            term.session_id = session_id
            changed = True
        if transcript and term.transcript_path is None and (session_id or term.session_id):
            term.transcript_path = transcript
            changed = True
        state = getattr(report, "state", None)
        if state is not None:
            # The command a permission hook named (H3); it holds only while blocked.
            command = getattr(report, "command", None)
            term.hook_command = (str(command)[:4000] if state == "blocked" and command
                                 else None)
        term.status.apply_hook_report(report)
        if changed and term.alive:
            with contextlib.suppress(OSError):
                self._write_meta(term)
        if changed and session_id and term.spec.external:
            self.tell_listeners("terminal.sessionId", {
                "terminalId": term.id, "cli": term.spec.external.get("cli"),
                "sessionId": session_id,
            })
        return term.status.status.wire()

    # -- close -------------------------------------------------------------------
    async def close(self, terminal_id: str, *, force: bool = False) -> dict[str, Any]:
        term = self.get(terminal_id)
        term.closing = True
        if term.alive and term.link is not None:
            steps = ([] if force else [("hup", HUP_WAIT)]) + [("term", TERM_WAIT),
                                                              ("kill", KILL_WAIT)]
            for name, wait in steps:
                term.link.signal(name)
                try:
                    await asyncio.wait_for(term._exited.wait(), wait)
                    break
                except TimeoutError:
                    continue
        if term.link is not None:
            term.link.bye()
            await term.link.drain()
            await term.link.close()
            term.link = None
        if term.holder_pid:
            # After BYE the holder leaves by itself; one still there is ours to end.
            for _ in range(10):
                if not _holder_alive(term.holder_pid, term.holder_started):
                    break
                await asyncio.sleep(0.05)
            else:
                # Signalled only when provably the same process (start time).
                log.warning("terminal %s: holder still running; ending it", term.id)
                _kill_pid(term.holder_pid)
        self._terminals.pop(terminal_id, None)
        self._remove_files(terminal_id, term.socket)
        exit_code = term.exit_code
        self.emit_soon("terminal.closed", {"terminalId": terminal_id})
        return {"terminalId": terminal_id, "exitCode": exit_code}

    async def close_all(self) -> None:
        for terminal_id in list(self._terminals):
            with contextlib.suppress(Exception):
                await self.close(terminal_id)


def _rebuild_screen(raw: bytes, cols: int, rows: int) -> Screen:
    """A fresh screen at ``cols`` x ``rows`` fed ``raw`` (runs on a worker thread)."""
    screen = Screen(cols, rows)
    screen.feed(raw)
    return screen



def remove_terminal_files(folder: Path, terminal_id: str, socket: str = "") -> None:
    """Delete one terminal's files (exact names built from a validated id only)."""
    if not TERMINAL_ID_RE.match(terminal_id):
        return
    for suffix in (".json", ".json.tmp", ".exit.json", ".tail", ".log", ".sock"):
        with contextlib.suppress(OSError):
            (folder / f"{terminal_id}{suffix}").unlink()
    if socket and not socket.startswith("tcp:") and socket.endswith(f"{terminal_id}.sock"):
        with contextlib.suppress(OSError):
            os.unlink(socket)


def close_all_holders(home: Path | str, *, timeout: float = 3.0) -> int:
    """End every terminal of ``home`` without a daemon or RPC; returns how many ran.

    Each live holder (pid checked against its recorded start time) gets
    SIGTERM, on which it runs the kill sequence on its program and exits;
    one still there after ``timeout`` is killed.  Every terminal's files go.
    For ``snowpea daemon stop --and-terminals`` and test cleanup — a normal
    daemon stop leaves terminals running.
    """
    folder = Path(home) / "terminals"
    if not folder.is_dir():
        return 0
    entries: list[tuple[str, int | None, str | None, str, str]] = []
    for path in sorted(folder.glob("t*.json")):
        if not TERMINAL_ID_RE.match(path.stem):
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        pid = meta.get("pid") if isinstance(meta.get("pid"), int) else None
        entries.append((path.stem, pid, meta.get("holderStarted"),
                        str(meta.get("socket") or ""), str(meta.get("token") or "")))
    verified = {pid: started for _, pid, started, _, _ in entries
                if pid and _holder_alive(pid, started)}
    for pid in verified:
        _terminate_pid(pid)
    # A holder whose identity cannot be proven by its start time (Windows has
    # none) is asked over its authenticated link instead: no blind signal.
    by_link = [(socket, token) for _, pid, _, socket, token in entries
               if pid and pid not in verified and socket and token and _pid_runs(pid)]
    ended_by_link = _end_over_links(by_link) if by_link else 0
    deadline = time.monotonic() + timeout
    left = dict(verified)
    while left and time.monotonic() < deadline:
        time.sleep(0.05)
        left = {pid: started for pid, started in left.items() if _holder_alive(pid, started)}
    for pid, started in left.items():
        if _holder_alive(pid, started):  # still the same process (security review L3)
            _kill_pid(pid)
    for terminal_id, _, _, socket, _ in entries:
        remove_terminal_files(folder, terminal_id, socket)
    return len(verified) + ended_by_link


def _end_over_links(targets: list[tuple[str, str]]) -> int:
    """Kill each terminal's program through its (token-proven) holder link; returns how many."""

    async def end_one(socket: str, token: str) -> bool:
        exited = asyncio.Event()
        link = HolderLink(socket, token, on_data=lambda _d, _t: None,
                          on_exit=lambda _c: exited.set())
        try:
            await link.connect(tail=False)
        except HolderError:
            return False
        try:
            for name, wait in (("hup", HUP_WAIT), ("term", TERM_WAIT), ("kill", KILL_WAIT)):
                link.signal(name)
                await link.drain()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(exited.wait(), wait)
                    break
            link.bye()  # the holder leaves at once instead of waiting for a core
            await link.drain()
        finally:
            await link.close()
        return True

    async def run() -> int:
        results = await asyncio.gather(*(end_one(s, t) for s, t in targets))
        return sum(1 for ok in results if ok)

    return asyncio.run(run())


def _terminate_pid(pid: int) -> None:
    if IS_WINDOWS:  # pragma: no cover - Windows only: no SIGTERM, end the tree
        _kill_pid(pid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, _signal.SIGTERM)


def _core_bin() -> str:
    try:
        from snowpea_core.terminal.report import (  # type: ignore[import-not-found]
            core_bin_for_env,
        )
    except ImportError:  # the hook integrations (T2) are not here
        return ""
    try:
        return str(core_bin_for_env() or "")
    except Exception:  # noqa: BLE001
        return ""


def _start_time(pid: int) -> str | None:
    from snowpea_core.external.proc import process_start_time

    with contextlib.suppress(Exception):
        return process_start_time(pid)
    return None


def _pid_runs(pid: int) -> bool:
    from snowpea_core.external.proc import pid_alive
    from snowpea_core.terminal.link import reap

    reap()  # a holder this daemon started and that exited is a zombie until reaped
    return pid_alive(pid)


def _holder_alive(pid: int, started: str | None) -> bool:
    """``pid`` runs and is provably the holder we started — safe to signal.

    Both start times must be known and equal (security review L2): an unknown
    start time counts as not alive, so a reused pid is never signalled.  (A
    connection needs no such check: the link's challenge proves the holder.)
    """
    if not started or not _pid_runs(pid):
        return False
    current = _start_time(pid)
    return current is not None and current == started


def _kill_pid(pid: int) -> None:
    if IS_WINDOWS:  # pragma: no cover - Windows only
        from snowpea_core.exec.winproc import taskkill_tree

        with contextlib.suppress(Exception):
            taskkill_tree(pid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, _signal.SIGKILL)
