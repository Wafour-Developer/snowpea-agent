"""What a chat shows *while* a turn runs: "typing…" and one progress line.

A messenger turn is silent between the prompt and the answer, and a turn that
calls six tools can be silent for minutes.  Two signals close that gap, both
optional and both feature-detected on the adapter (``typing`` / ``edit``,
see :class:`~snowpea_core.gateway.base.SupportsTyping`):

* a **keep-typing loop** that re-sends the platform's typing hint every few
  seconds, because every platform expires it after about five;
* a **single progress message** per turn, sent on the first ``tool.call`` and
  *edited in place* afterwards, so a long turn costs one message rather than
  one per tool.

Both pause while an approval or a question is waiting on the person: "typing"
next to a question nobody has answered is a lie, and the turn is not working.

An adapter without ``edit`` gets no progress message at all.  Editing is what
makes it one message; without it the same feature would be a stream of chat
spam, which is worse than silence.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

log = logging.getLogger("snowpea.gateway.activity")

#: Seconds between typing hints.  Telegram's lasts ~5s, Discord's ~10s.
TYPING_INTERVAL_SEC = 4.0
#: A slow platform must not stall the loop; the hint is cosmetic.
TYPING_CALL_TIMEOUT_SEC = 1.5
#: Floor between two edits of the progress message, so a turn that calls ten
#: fast tools does not spend its time rate-limited by the platform.
PROGRESS_EDIT_INTERVAL_SEC = 2.0
#: Argument keys worth showing beside a tool name, best first.
LABEL_KEYS: tuple[str, ...] = (
    "command",
    "path",
    "file_path",
    "pattern",
    "query",
    "url",
    "name",
    "prompt",
)
#: Longest argument hint shown in the progress line.
LABEL_MAX = 60


def tool_label(name: str, args: dict[str, Any]) -> str:
    """``shell echo hi`` — the tool plus one short argument hint.

    The first path-like or command-like argument only: a progress line is read
    at a glance, and the full arguments are in the transcript.
    """
    for key in LABEL_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            hint = " ".join(value.split())
            if len(hint) > LABEL_MAX:
                hint = hint[: LABEL_MAX - 1] + "…"
            return f"{name} {hint}"
    return name


def elapsed_text(seconds: float) -> str:
    """``42s`` / ``3m 07s`` — how long the turn took."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m {total % 60:02d}s"


class TurnActivity:
    """Typing hints and the progress message for one chat's current turn.

    One instance per :class:`~snowpea_core.gateway.router.GatewayConnection`;
    it is reset at every ``turn.started`` rather than recreated, so a pause
    left over from a resolved approval cannot outlive the turn that set it.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._task: asyncio.Task[None] | None = None
        self._paused = False
        self._running = False
        self._started = 0.0
        self._calls = 0
        self._message_id = ""
        self._label = ""
        self._shown = ""
        self._last_edit = 0.0

    # -- capabilities ---------------------------------------------------
    def _adapter(self) -> Any:
        return self.conn.router.adapter(self.conn.binding.id)

    def _typing_call(self) -> Any:
        if not self.conn.router.gateway_flag("typing"):
            return None
        return getattr(self._adapter(), "typing", None)

    def _edit_call(self) -> Any:
        if not self.conn.router.gateway_flag("progress"):
            return None
        return getattr(self._adapter(), "edit", None)

    def running_for(self) -> float | None:
        """Seconds this chat's turn has been running, or ``None`` when idle."""
        if not self._running or not self._started:
            return None
        return time.monotonic() - self._started

    # -- turn lifecycle -------------------------------------------------
    async def turn_started(self) -> None:
        """Reset the counters and start the keep-typing loop."""
        await self.cancel()
        self._running = True
        self._paused = False
        self._started = time.monotonic()
        self._calls = 0
        self._message_id = ""
        self._label = ""
        self._shown = ""
        self._last_edit = 0.0
        self._start_typing()

    async def tool_call(self, name: str, args: dict[str, Any]) -> None:
        """Count one tool call and show it, sending or editing one message."""
        if not self._running:
            return
        self._calls += 1
        self._label = tool_label(name, args)
        edit = self._edit_call()
        if edit is None:
            return
        if not self._message_id:
            self._message_id = await self.conn.router.send(
                self.conn.binding, self.conn.channel_id, f"⏳ {self._label}"
            )
            self._shown = self._label
            self._last_edit = time.monotonic()
            return
        if time.monotonic() - self._last_edit >= PROGRESS_EDIT_INTERVAL_SEC:
            await self._edit(f"⏳ {self._label}")

    async def turn_done(self, reason: str) -> None:
        """Stop typing and settle the progress message on a final line."""
        was_running = self._running
        self._running = False
        await self.cancel()
        if not was_running or not self._message_id:
            return
        mark = "✓" if reason == "complete" else "✗"
        plural = "" if self._calls == 1 else "s"
        took = elapsed_text(time.monotonic() - self._started)
        summary = f"{mark} {self._calls} tool call{plural} · {took}"
        if reason != "complete":
            summary = f"{summary} · {reason}"
        await self._edit(summary, force=True)

    # -- approvals and questions ----------------------------------------
    def pause(self) -> None:
        """Stop typing: the turn is waiting on a human, not working."""
        self._paused = True
        self._stop_typing()

    def resume(self) -> None:
        """Start typing again once the person has answered."""
        self._paused = False
        if self._running:
            self._start_typing()

    async def cancel(self) -> None:
        """Drop the typing loop; used on unsubscribe, stop and shutdown."""
        self._stop_typing()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- internals ------------------------------------------------------
    def _start_typing(self) -> None:
        if self._paused or self._typing_call() is None:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.ensure_future(self._keep_typing())

    def _stop_typing(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        self._task = None

    async def _keep_typing(self) -> None:
        while self._running and not self._paused:
            call = self._typing_call()
            if call is None:
                return
            try:
                await asyncio.wait_for(call(self.conn.channel_id), TYPING_CALL_TIMEOUT_SEC)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a cosmetic hint, never fatal
                log.debug("typing hint on %s failed: %s", self.conn.surface_id, exc)
            await asyncio.sleep(TYPING_INTERVAL_SEC)

    async def _edit(self, text: str, *, force: bool = False) -> None:
        edit = self._edit_call()
        if edit is None or not self._message_id or (text == self._shown and not force):
            return
        try:
            await edit(self.conn.channel_id, self._message_id, text)
        except Exception as exc:  # noqa: BLE001 - a dead edit must not end a turn
            log.debug("progress edit on %s failed: %s", self.conn.surface_id, exc)
            return
        self._shown = text
        self._last_edit = time.monotonic()


__all__ = [
    "LABEL_KEYS",
    "LABEL_MAX",
    "PROGRESS_EDIT_INTERVAL_SEC",
    "TYPING_CALL_TIMEOUT_SEC",
    "TYPING_INTERVAL_SEC",
    "TurnActivity",
    "elapsed_text",
    "tool_label",
]
