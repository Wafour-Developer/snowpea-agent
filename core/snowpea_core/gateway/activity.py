"""What a chat shows *while* a turn runs: "typing…" and one progress line.

A messenger turn is silent between the prompt and the answer, and a turn that
calls six tools can be silent for minutes.  Two signals close that gap, both
optional and both feature-detected on the adapter (``typing`` / ``edit``,
see :class:`~snowpea_core.gateway.base.SupportsTyping`):

* a **keep-typing loop** that re-sends the platform's typing hint every few
  seconds, because every platform expires it after about five;
* a **single progress message** per turn, sent on the first ``tool.call`` and
  *edited in place* afterwards, so a long turn costs one message rather than
  one per tool.  Fast tool results are coalesced into a trailing edit after the
  platform-safe edit floor, so the chat still sees evidence before ``turn.done``.

Both pause while an approval or a question is waiting on the person: "typing"
next to a question nobody has answered is a lie, and the turn is not working.

An adapter without ``edit`` gets no progress message at all.  Editing is what
makes it one message; without it the same feature would be a stream of chat
spam, which is worse than silence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from snowpea_core.util.redaction import is_sensitive_key, redact_text, redact_value

log = logging.getLogger("snowpea.gateway.activity")

#: Seconds between typing hints.  Telegram's lasts ~5s, Discord's ~10s.
TYPING_INTERVAL_SEC = 4.0
#: A slow platform must not stall the loop; the hint is cosmetic.
TYPING_CALL_TIMEOUT_SEC = 1.5
#: Floor between two edits of the progress message, so a turn that calls ten
#: fast tools does not spend its time rate-limited by the platform.
PROGRESS_EDIT_INTERVAL_SEC = 1.0
#: Telegram groups count edits against a tighter shared channel budget.
GROUP_PROGRESS_EDIT_INTERVAL_SEC = 3.0
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
#: Longest structured progress body.  It must fit comfortably inside every
#: adapter's edited-message limit while still leaving room for final answers.
DETAIL_MAX = 1800
#: How many recent tool/subagent facts one edited progress message keeps.
DETAIL_ITEMS = 8
#: Longest one-line value rendered in the messenger progress message.
VALUE_MAX = 260
#: Max preview lines kept when rendering stdout/stderr in messenger progress.
VALUE_MAX_LINES = 5


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


def _clip(text: str, limit: int = VALUE_MAX) -> str:
    """Single-line ``text`` clipped for a phone-sized progress message."""
    single = " ".join(str(text or "").split())
    return single if len(single) <= limit else single[: limit - 1] + "…"


def _clip_multiline(text: str, limit: int = VALUE_MAX) -> str:
    """Bound ``text`` while preserving useful stdout/stderr line breaks."""
    raw_lines = str(text or "").splitlines() or [str(text or "")]
    if len(raw_lines) > VALUE_MAX_LINES:
        head = raw_lines[: VALUE_MAX_LINES - 2]
        raw_lines = [*head, "…", raw_lines[-1]]
    lines: list[str] = []
    for raw in raw_lines:
        line = raw.rstrip()
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        lines.append(line)
    return "\n".join(lines)


def _redact_text(text: str) -> str:
    """Hide obvious secret assignments inside free-form tool text."""
    return redact_text(text)


def _redact_value(key: str, value: Any) -> Any:
    """Return a chat-safe, bounded version of one argument/result value."""
    if is_sensitive_key(key):
        return redact_value(key, value)
    if isinstance(value, dict):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value("", item) for item in value[:6]]
    return redact_value(key, value)


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
        return _clip(_redact_text(text))
    text = str(value)
    redacted = _redact_text(text)
    if "\n" in redacted:
        return _clip_multiline(redacted)
    return _clip(redacted)


def _detail_lines(title: str, fields: list[tuple[str, Any]]) -> str:
    lines = [_redact_text(title)]
    for key, value in fields:
        value = _redact_value(str(key), value)
        if value is None or value == "":
            continue
        rendered = _format_value(value)
        if "\n" in rendered:
            lines.append(f"  {key}:")
            lines.extend(f"    {line}" for line in rendered.splitlines())
        else:
            lines.append(f"  {key}: {rendered}")
    return "\n".join(lines)


def _compose_render(headline: str, details: list[str], hidden: bool) -> str:
    parts = [headline]
    if hidden:
        parts.append("… earlier progress hidden …")
    parts.extend(details)
    return "\n\n".join(part for part in parts if part)


def _detail_budget(headline: str, *, hidden: bool) -> int:
    reserved = len(headline)
    if hidden:
        reserved += len("\n\n… earlier progress hidden …")
    reserved += len("\n\n")
    return max(0, DETAIL_MAX - reserved)


def _bound_detail_block(block: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if len(block) <= budget:
        return block
    lines = block.splitlines() or [block]
    heading = _clip(lines[0], min(VALUE_MAX, budget))
    if len(heading) >= budget:
        return heading[: max(0, budget - 1)] + ("…" if budget else "")

    notice = "  … detail truncated …"
    body_budget = budget - len(heading) - len("\n")
    if body_budget <= 0:
        return heading[:budget]
    if body_budget <= len(notice):
        return f"{heading}\n{notice[:body_budget]}"

    evidence_budget = body_budget - len(notice) - len("\n")
    rest = "\n".join(lines[1:])
    if evidence_budget <= 0 or not rest:
        return f"{heading}\n{notice}"[:budget]
    if evidence_budget <= 5:
        evidence = rest[-evidence_budget:]
    else:
        head_budget = max(1, evidence_budget // 2 - 2)
        tail_budget = max(1, evidence_budget - head_budget - 3)
        evidence = f"{rest[:head_budget]}…{rest[-tail_budget:]}"
    return f"{heading}\n{notice}\n{evidence}"[:budget]


class TurnActivity:
    """Typing hints and the progress message for one chat's current turn.

    One instance per :class:`~snowpea_core.gateway.router.GatewayConnection`;
    it is reset at every ``turn.started`` rather than recreated, so a pause
    left over from a resolved approval cannot outlive the turn that set it.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._task: asyncio.Task[None] | None = None
        self._trailing_task: asyncio.Task[None] | None = None
        self._paused = False
        self._running = False
        self._started = 0.0
        self._calls = 0
        self._message_id = ""
        self._posted = False
        self._label = ""
        self._shown = ""
        self._last_edit = 0.0
        self._details: list[str] = []
        self._pending_headline = ""
        self._edit_backoff_until = 0.0

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
        self._posted = False
        self._label = ""
        self._shown = ""
        self._last_edit = 0.0
        self._details = []
        self._pending_headline = ""
        self._edit_backoff_until = 0.0
        self._start_typing()

    async def tool_call(self, name: str, args: dict[str, Any]) -> None:
        """Count one tool call and show it, sending or editing one message."""
        if not self._running:
            return
        self._calls += 1
        safe_args = {str(key): _redact_value(str(key), value) for key, value in args.items()}
        self._label = tool_label(name, safe_args)
        self._remember_detail(
            _detail_lines(
                f"Tool call #{self._calls}: {name}",
                [(key, value) for key, value in safe_args.items()],
            )
        )
        edit = self._edit_call()
        if edit is None:
            return
        if not self._posted:
            # Once per turn, whatever comes back.  A send that names no message
            # — Slack answering a slash command, or a platform that failed —
            # leaves nothing to edit, and retrying would spam the chat.
            await self._post_or_edit(f"⏳ {self._label}")
            return
        await self._post_or_edit(f"⏳ {self._label}")

    async def tool_result(self, name: str, payload: dict[str, Any]) -> None:
        """Show a bounded, redacted result for the last tool call."""
        if not self._running:
            return
        ok = bool(payload.get("ok"))
        mark = "✓" if ok else "✗"
        raw_meta = payload.get("meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        if meta.get("sensitive"):
            result_text = "<sensitive output hidden>"
        else:
            result_text = str(payload.get("output") or payload.get("error") or "")
            if not result_text:
                content = payload.get("content")
                if isinstance(content, list):
                    text_blocks = [
                        str(block.get("text") or "")
                        for block in content
                        if isinstance(block, dict) and block.get("text")
                    ]
                    result_text = "\n".join(text_blocks)
            result_text = _redact_text(result_text)
        self._remember_detail(
            _detail_lines(
                f"Tool result {mark}: {name}",
                [
                    ("call", payload.get("callId")),
                    ("output" if ok else "error", result_text or "(no output)"),
                ],
            )
        )
        await self._post_or_edit(f"⏳ {self._label or name}")

    async def subagent_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Show readable subagent progress in the same edited progress message."""
        if not self._running:
            return
        name = str(payload.get("name") or "subagent")
        title = str(payload.get("title") or "").strip()
        label = f"{name} · {title}" if title else name
        status = str(payload.get("status") or "")
        if kind == "subagent.spawn":
            heading = f"Subagent started: {label}"
            fields = [
                ("agent", payload.get("agentId")),
                ("session", payload.get("sessionId")),
                ("task", payload.get("task")),
                ("status", status),
            ]
        elif kind == "subagent.update":
            heading = f"Subagent update: {label}"
            fields = [
                ("agent", payload.get("agentId")),
                ("session", payload.get("sessionId")),
                ("status", status),
                ("last", payload.get("lastText") or payload.get("text")),
            ]
        else:
            ok = bool(payload.get("ok", status not in {"error", "interrupted"}))
            mark = "✓" if ok else "✗"
            heading = f"Subagent {mark} {label}"
            budget = int(payload.get("budget") or 0)
            rounds = int(payload.get("rounds") or 0)
            fields = [
                ("agent", payload.get("agentId")),
                ("session", payload.get("sessionId")),
                ("reason", payload.get("reason")),
                ("rounds", f"{rounds}/{budget}" if budget else rounds),
                ("summary", payload.get("summary") or payload.get("result")),
            ]
        self._remember_detail(_detail_lines(heading, fields))
        await self._post_or_edit(f"⏳ {self._label or label}")

    async def subagent_tool_call(
        self, agent_label: str, tool_name: str, args: dict[str, Any]
    ) -> None:
        """Show a child session's real tool call without counting it as the parent's."""
        if not self._running:
            return
        safe_args = {str(key): _redact_value(str(key), value) for key, value in args.items()}
        label = tool_label(tool_name, safe_args)
        self._remember_detail(
            _detail_lines(
                f"Subagent tool call: {agent_label} → {tool_name}",
                [(key, value) for key, value in safe_args.items()],
            )
        )
        await self._post_or_edit(f"⏳ {self._label or label}")

    async def subagent_tool_result(
        self, agent_label: str, tool_name: str, payload: dict[str, Any]
    ) -> None:
        """Show a child session's real tool result, bounded and redacted."""
        if not self._running:
            return
        ok = bool(payload.get("ok"))
        mark = "✓" if ok else "✗"
        raw_meta = payload.get("meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        if meta.get("sensitive"):
            result_text = "<sensitive output hidden>"
        else:
            result_text = str(payload.get("output") or payload.get("error") or "")
            result_text = _redact_text(result_text)
        self._remember_detail(
            _detail_lines(
                f"Subagent tool result {mark}: {agent_label} → {tool_name}",
                [
                    ("call", payload.get("callId")),
                    ("output" if ok else "error", result_text or "(no output)"),
                ],
            )
        )
        await self._post_or_edit(f"⏳ {self._label or tool_name}")

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
        await self._edit(self._render(summary), force=True)

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
        trailing, self._trailing_task = self._trailing_task, None
        self._pending_headline = ""
        if trailing is not None and not trailing.done():
            trailing.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await trailing

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

    async def _post_or_edit(self, headline: str, *, force: bool = False) -> None:
        edit = self._edit_call()
        if edit is None:
            return
        body = self._render(headline)
        if not self._posted:
            self._posted = True
            self._message_id = await self.conn.router.send(
                self.conn.binding, self.conn.channel_id, body
            )
            self._shown = body
            self._last_edit = time.monotonic()
            return
        if self._trailing_task is not None and not self._trailing_task.done() and not force:
            # The deferred refresh may already be inside a slow platform call.
            # Coalesce instead of cancelling it and starting a concurrent edit.
            self._pending_headline = headline
            return
        now = time.monotonic()
        if now < self._edit_backoff_until and not force:
            self._pending_headline = headline
            self._schedule_trailing_edit()
            return
        if now - self._last_edit >= self._progress_edit_interval() or force:
            self._cancel_trailing_edit()
            await self._edit(body, force=force)
            return
        self._pending_headline = headline
        self._schedule_trailing_edit()

    async def _edit(self, text: str, *, force: bool = False) -> None:
        edit = self._edit_call()
        if edit is None or not self._message_id or (text == self._shown and not force):
            return
        if time.monotonic() < self._edit_backoff_until:
            self._pending_headline = text.splitlines()[0] if text else self._pending_headline
            self._schedule_trailing_edit()
            return
        try:
            await edit(self.conn.channel_id, self._message_id, text)
        except Exception as exc:  # noqa: BLE001 - a dead edit must not end a turn
            retry_after = getattr(exc, "retry_after", None)
            if isinstance(retry_after, (int, float)) and retry_after > 0:
                self._edit_backoff_until = time.monotonic() + float(retry_after)
                self._pending_headline = text.splitlines()[0] if text else self._pending_headline
                self._schedule_trailing_edit()
            log.debug("progress edit on %s failed: %s", self.conn.surface_id, exc)
            return
        self._shown = text
        self._last_edit = time.monotonic()
        self._edit_backoff_until = 0.0

    def _cancel_trailing_edit(self) -> None:
        task = self._trailing_task
        if task is not None and not task.done():
            task.cancel()
        self._trailing_task = None
        self._pending_headline = ""

    def _schedule_trailing_edit(self) -> None:
        if not self._posted or not self._message_id:
            return
        if self._trailing_task is not None and not self._trailing_task.done():
            return
        elapsed = time.monotonic() - self._last_edit
        delay = max(
            0.0,
            self._progress_edit_interval() - elapsed,
            self._edit_backoff_until - time.monotonic(),
        )
        self._trailing_task = asyncio.ensure_future(self._trailing_edit_after(delay))

    def _progress_edit_interval(self) -> float:
        if getattr(self.conn.binding, "platform", "") == "telegram":
            chat_type = str(getattr(self.conn, "chat_type", "") or "").lower()
            if chat_type in {"group", "supergroup", "channel"}:
                return GROUP_PROGRESS_EDIT_INTERVAL_SEC
        return PROGRESS_EDIT_INTERVAL_SEC

    async def _trailing_edit_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if not self._posted or not self._message_id:
                return
            if not self._running and not self._pending_headline:
                return
            headline = self._pending_headline
            if not headline:
                return
            self._pending_headline = ""
            await self._edit(self._render(headline))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - progress is cosmetic
            log.debug("trailing progress edit on %s failed: %s", self.conn.surface_id, exc)
        finally:
            current = asyncio.current_task()
            if self._trailing_task is current:
                self._trailing_task = None
                # An event may arrive while the platform edit is in flight.
                # Give that newer evidence its own coalesced trailing refresh.
                if self._pending_headline and (self._running or self._posted):
                    self._schedule_trailing_edit()

    def _remember_detail(self, text: str) -> None:
        self._details.append(text)
        if len(self._details) > DETAIL_ITEMS:
            self._details = self._details[-DETAIL_ITEMS:]

    def _render(self, headline: str) -> str:
        headline = _clip(_redact_text(headline), min(VALUE_MAX, DETAIL_MAX))
        if not self._details:
            return headline[:DETAIL_MAX]

        selected: list[str] = []
        truncated = False
        for detail in reversed(self._details):
            trial = [_bound_detail_block(detail, DETAIL_MAX), *selected]
            hidden = len(trial) < len(self._details)
            body = _compose_render(headline, trial, hidden or truncated)
            if len(body) <= DETAIL_MAX:
                selected = trial
                continue
            if selected:
                break
            budget = _detail_budget(headline, hidden=True)
            selected = [_bound_detail_block(detail, budget)]
            truncated = True
            break

        hidden = len(selected) < len(self._details) or truncated
        body = _compose_render(headline, selected, hidden)
        if len(body) <= DETAIL_MAX:
            return body

        budget = _detail_budget(headline, hidden=True)
        selected = [_bound_detail_block(selected[-1], budget)] if selected else []
        return _compose_render(headline, selected, hidden=True)[:DETAIL_MAX]


__all__ = [
    "LABEL_KEYS",
    "LABEL_MAX",
    "DETAIL_ITEMS",
    "DETAIL_MAX",
    "GROUP_PROGRESS_EDIT_INTERVAL_SEC",
    "PROGRESS_EDIT_INTERVAL_SEC",
    "TYPING_CALL_TIMEOUT_SEC",
    "TYPING_INTERVAL_SEC",
    "TurnActivity",
    "elapsed_text",
    "tool_label",
]
