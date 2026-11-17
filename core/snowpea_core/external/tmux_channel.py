"""A session open in the person's tmux pane, driven through the tmux bridge (1.56.0).

The channel never starts, restarts or stops a process: the CLI is the
person's.  It tails the transcript as the "open elsewhere" view does, types
prompts through :class:`~snowpea_core.external.tmux.TerminalBridge` (the hard
guard before every key), and watches the pane for the permission prompts the
CLI's profile recognises.  When the CLI exits or the guard refuses, the
session goes back to view only (``ChannelHost.bridge_lost``).

Turns: a send starts a turn; it ends when the CLI was busy and is idle again
(Claude Code's status file, Codex's and Cursor's transcript writes), or after
``start_timeout`` when it never looked busy.  agy says nothing about busy, so a
send there ends its turn once the text is delivered.  A send while the CLI is
busy or while a prompt waits is refused (``session_busy``): no queueing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Literal

from snowpea_core.external import modes as native_modes
from snowpea_core.external.base import (
    ApprovalAsk,
    ChannelHost,
    ExternalAdapter,
    ExternalChannel,
    LaunchSpec,
    SessionInfo,
    unavailable,
)
from snowpea_core.external.proc import ExternalError, UnrestrictedGrant
from snowpea_core.external.tmux import (
    BUSY_TERMINAL_BUSY,
    BUSY_TERMINAL_PROMPT,
    TerminalBridge,
    TerminalPrompt,
    check_text,
    looks_like_dialog,
)
from snowpea_core.server import errors

log = logging.getLogger("snowpea.external.tmux")

ModeState = Literal["confirmed", "unconfirmed", "unknown"]
#: Bytes of a Claude transcript read back to find the call a prompt is about.
_TAIL_BYTES = 256 * 1024


def pending_claude_call(path: Path) -> tuple[str, dict[str, Any]] | None:
    """The last ``tool_use`` in a Claude transcript that has no ``tool_result`` yet."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict) or record.get("type") not in ("assistant", "user"):
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                name = block.get("name")
                args = block.get("input")
                calls[block["id"]] = (str(name or "tool")[:100],
                                      dict(args) if isinstance(args, dict) else {})
            elif block.get("type") == "tool_result":
                calls.pop(str(block.get("tool_use_id")), None)
    return next(reversed(calls.values()), None)


class TmuxBridgeChannel(ExternalChannel):
    """A CLI the person runs in a tmux pane, typed into through the bridge."""

    host = "tmux"

    def __init__(self, spec: LaunchSpec, host: ChannelHost, adapter: ExternalAdapter,
                 info: SessionInfo, bridge: TerminalBridge, *, poll_interval: float = 1.0,
                 start_timeout: float = 20.0) -> None:
        super().__init__(spec, host)
        self.adapter = adapter
        self.info = info
        self.bridge = bridge
        self.poll_interval = poll_interval
        self.start_timeout = start_timeout
        self.tmux = {"session": bridge.pane.session, "pane": bridge.pane.pane}
        self.mode_state: ModeState = "unknown"
        #: A recognised prompt being asked in the approval queue, and its task.
        self.asked: TerminalPrompt | None = None
        self.ask_task: asyncio.Task[None] | None = None
        #: The CLI waits for an answer the profile does not recognise.
        self.waiting = False
        #: A list or dialog a slash command left open (unrecognised: answered there).
        self.dialog = False
        #: The next send may be a single-line slash command (the local person only;
        #: the manager sets it per send).
        self.allow_slash = False
        #: Who the next keys are for (the manager sets it): person, jarvis, remote.
        self.actor = "person"
        self.turn_id: str | None = None
        self._busy_seen = False
        self._idle_ticks = 0
        self._sent_at = 0.0
        self._busy = False
        self.closing = False
        self.watcher: asyncio.Task[None] | None = None
        self.follower: asyncio.Task[None] | None = None

    # -- state ----------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return (not self.closing and self.bridge.lost is None
                and self.bridge.probe.facts(self.bridge.cli_pid) is not None)

    def pids(self) -> set[int]:
        # The CLI is the person's process: never one of snowpea's own.
        return set()

    def wire(self) -> dict[str, Any]:
        """``ExternalSessionRow.terminal``."""
        return {
            **self.bridge.pane.wire(),
            "modeState": self.mode_state,
            "prompt": (
                {"recognised": True, "text": self.asked.text} if self.asked is not None
                else {"recognised": False, "text": None} if self.waiting or self.dialog
                else None
            ),
        }

    async def _detect_mode(self) -> None:
        found = await self.bridge.current_mode()
        row = native_modes.find(self.adapter.table(), found) if found else None
        if row is None:
            self.mode_state = "unknown"
            return
        self.native_mode = row.id
        self.mode = row.legacy
        self.mode_state = "confirmed"

    # -- start ------------------------------------------------------------------
    async def start(self) -> None:
        await self.bridge.guard()
        await self._detect_mode()
        self.follower = asyncio.ensure_future(self._follow())
        self.watcher = asyncio.ensure_future(self._watch())

    async def _follow(self) -> None:
        try:
            async for items in self.adapter.follow(self.info, interval=self.poll_interval):
                if items:
                    await self.host_api.emit(items)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the view stops updating, the bridge stays
            log.debug("tailing the bridged session stopped", exc_info=True)

    async def _status(self) -> str | None:
        """``idle`` / ``busy`` / ``waiting`` where the CLI says so, else from liveness."""
        if self.adapter.id == "claude":
            from snowpea_core.external.claude import read_status

            data = read_status(self.bridge.cli_pid)
            status = data.get("status") if isinstance(data, dict) else None
            return status if isinstance(status, str) else None
        live = await self.adapter.liveness(self.info, own_pids=set())
        return "busy" if live.busy else "idle"

    async def _lose(self, reason: str) -> None:
        if self.closing:
            return
        self.closing = True
        if self.ask_task is not None:
            self.ask_task.cancel()
        if self.turn_id is not None:
            self.turn_id = None
            with contextlib.suppress(Exception):
                await self.host_api.turn_finished("error", reason)
        lost = getattr(self.host_api, "bridge_lost", None)
        if lost is not None:
            await lost(reason)
        else:
            await self.host_api.channel_died(reason)

    async def _watch(self) -> None:
        ticks = 0
        try:
            while not self.closing:
                await asyncio.sleep(self.poll_interval)
                ticks += 1
                facts = self.bridge.probe.facts(self.bridge.cli_pid)
                if facts is None or (self.bridge.start is not None
                                     and facts.start != self.bridge.start):
                    await self._lose("the CLI in the tmux pane exited")
                    return
                if self.bridge.lost is not None:
                    await self._lose(self.bridge.lost)
                    return
                if ticks % 2 == 0:
                    try:
                        await self.bridge.guard()
                    except ExternalError as exc:
                        await self._lose(exc.message)
                        return
                status = await self._status()
                self._busy = status in ("busy", "waiting")
                if self.dialog and not looks_like_dialog(await self.bridge.capture()):
                    self.dialog = False
                    note = getattr(self.host_api, "changed", None)
                    if note is not None:
                        await note()
                await self._prompts(status)
                self._turns(status)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken watcher drops the bridge visibly
            log.warning("watching the tmux pane failed", exc_info=True)
            await self._lose("snowpea lost track of the tmux pane")

    async def _prompts(self, status: str | None) -> None:
        rule = self.bridge.profile.prompt
        claude = self.adapter.id == "claude"
        look = (status == "waiting") if claude else (rule is not None and self._busy)
        changed = False
        if not look:
            if self.asked is not None or self.waiting:
                self._drop_ask()
                self.waiting = False
                changed = True
        else:
            prompt = await self.bridge.prompt() if rule is not None else None
            if prompt is None:
                if self.asked is not None:
                    self._drop_ask()
                    changed = True
                if claude and not self.waiting:
                    self.waiting = True
                    changed = True
            elif self.asked is None or prompt.signature != self.asked.signature:
                self._drop_ask()
                self.waiting = False
                self.asked = prompt
                self.ask_task = asyncio.ensure_future(self._ask(prompt))
                changed = True
        if changed:
            note = getattr(self.host_api, "changed", None)
            if note is not None:
                await note()

    def _drop_ask(self) -> None:
        task = self.ask_task
        self.ask_task = None
        self.asked = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _ask(self, prompt: TerminalPrompt) -> None:
        rule = self.bridge.profile.prompt
        assert rule is not None
        call = None
        if self.adapter.id == "claude" and self.session_id:
            from snowpea_core.external.claude import transcript_path

            call = await asyncio.to_thread(
                pending_claude_call, transcript_path(self.spec.cwd, self.session_id))
        tool, args = call if call is not None else ("terminal.prompt",
                                                     {"question": prompt.question})
        ask = ApprovalAsk(
            tool=tool,
            args=args,
            risk="high" if tool in ("Bash", "terminal.prompt") else "medium",
            note=(f"asked in the tmux pane {self.bridge.pane.pane}; the answer is sent there "
                  "as the CLI's key, only while the same question is still on screen"),
            person_only=rule.person_only or call is None,
            terminal={"pane": self.bridge.pane.pane, "text": prompt.text},
        )
        try:
            answer = await self.host_api.ask_approval(ask)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - nothing is answered then
            log.warning("terminal prompt bridge failed", exc_info=True)
            return
        try:
            await self.bridge.answer(prompt, answer.allow, by=answer.by or "person")
        except ExternalError as exc:
            log.info("terminal prompt not answered: %s", exc.message)
        finally:
            if self.asked is prompt:
                self.asked = None
                self.ask_task = None

    def _turns(self, status: str | None) -> None:
        if self.turn_id is None:
            return
        if self._busy:
            self._busy_seen = True
            self._idle_ticks = 0
            return
        if self._busy_seen:
            self._idle_ticks += 1
            if self._idle_ticks >= 2:
                self._end_turn("complete")
        elif asyncio.get_running_loop().time() - self._sent_at > self.start_timeout:
            self._end_turn("complete")

    def _end_turn(self, reason: str) -> None:
        self.turn_id = None
        self._busy_seen = False
        asyncio.ensure_future(self.host_api.turn_finished(reason))

    # -- turns ------------------------------------------------------------------
    async def precheck(self, text: str) -> None:
        """Refuse before anything is published: bad text, the guard, a busy CLI, a prompt."""
        check_text(text, slash_ok=self.allow_slash)
        await self.bridge.guard()
        if self.asked is not None or self.waiting or self.dialog:
            raise ExternalError(errors.SESSION_BUSY,
                                "a permission prompt is open in the terminal; answer it first",
                                {"reason": BUSY_TERMINAL_PROMPT})
        status = await self._status()
        if status == "waiting":
            raise ExternalError(errors.SESSION_BUSY,
                                "a permission prompt is open in the terminal; answer it first",
                                {"reason": BUSY_TERMINAL_PROMPT})
        if status == "busy":
            raise ExternalError(errors.SESSION_BUSY,
                                "the CLI is working in the terminal; wait for it or interrupt it",
                                {"reason": BUSY_TERMINAL_BUSY})

    async def send(self, text: str, turn_id: str) -> None:
        if self.allow_slash and text.lstrip().startswith("/"):
            # A slash command starts no model turn: settle, and say so when it
            # left a list or dialog open (answered in the terminal).
            await self.bridge.send_slash_text(text, by=self.actor)
            self.turn_id = turn_id
            await asyncio.sleep(0.8)
            if looks_like_dialog(await self.bridge.capture()):
                self.dialog = True
                note = getattr(self.host_api, "changed", None)
                if note is not None:
                    await note()
            await self._detect_mode()
            self._end_turn("complete")
            return
        await self.bridge.send_text(text, by=self.actor)
        self.turn_id = turn_id
        self._busy_seen = False
        self._idle_ticks = 0
        self._sent_at = asyncio.get_running_loop().time()
        if not self.bridge.profile.busy_signal:
            self._end_turn("complete")

    async def interrupt(self) -> None:
        key = self.bridge.profile.interrupt
        if key is None:
            raise unavailable("snowpea does not know this CLI's interrupt key; stop it there")
        if self.asked is not None or self.waiting:
            raise ExternalError(errors.EXTERNAL_VIEW_ONLY,
                                "a permission prompt is open in the terminal; answer it")
        if not self._busy and self.turn_id is None:
            return
        await self.bridge.key(key, by=self.actor)

    async def set_mode(self, mode: str, grant: UnrestrictedGrant | None) -> None:
        """Switch in the terminal now (the CLI's slash command or mode key), and check it."""
        row = native_modes.find(self.adapter.table(), self.native_mode or "")
        terminal = row.terminal if row is not None else None
        if row is None or not terminal:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                f"snowpea cannot switch {self.adapter.title} to that mode in the terminal; "
                "switch it there",
            )
        if self._busy or self.asked is not None or self.waiting or self.dialog:
            raise ExternalError(errors.SESSION_BUSY,
                                "the CLI is busy in the terminal; switch modes when it is idle",
                                {"reason": BUSY_TERMINAL_BUSY})
        confirmed = await self.bridge.switch_mode(row.id, terminal, by=self.actor)
        self.mode = row.legacy
        self.grant = grant
        self.mode_state = "confirmed" if confirmed else "unconfirmed"

    async def close(self) -> None:
        self.closing = True
        for task in (self.watcher, self.follower, self.ask_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        tasks = [t for t in (self.watcher, self.follower, self.ask_task)
                 if t is not None and t is not asyncio.current_task()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.turn_id = None


__all__ = ["TmuxBridgeChannel", "pending_claude_call"]
