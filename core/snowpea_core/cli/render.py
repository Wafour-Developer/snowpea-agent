"""Rendering of ``session.event`` streams for the headless CLI.

Two renderers share one interface: :class:`PlainRenderer` writes the human
transcript described in plan §3.6 (assistant deltas streamed, tool calls as
one-line summaries) and :class:`JsonRenderer` writes JSON Lines.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

#: ``turn.done.reason`` → process exit code (contract §10, plan §3.6).
TURN_REASON_EXIT: dict[str, int] = {
    "complete": 0,
    "error": 1,
    "denied": 4,
    "interrupted": 5,
    "timeout": 5,
}

EXIT_OK = 0
EXIT_AGENT_FAILED = 1
EXIT_USAGE = 2
EXIT_NO_DAEMON = 3
EXIT_DENIED = 4
EXIT_TIMEOUT = 5

#: Error codes that mean "the turn was blocked by permissions" → exit 4.
DENIAL_CODES = frozenset({"mode_denied", "approval_denied", "approval_timeout"})

_MAX_ARG_CHARS = 60


def format_tokens(count: int) -> str:
    """``12345`` -> ``12.3k``; the compaction divider's units (CORE-context)."""
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}k"


def format_context(payload: dict[str, Any]) -> str:
    """``"12.3k / 200.0k (6.2%)"``; an unknown window renders as ``?``."""
    used = format_tokens(int(payload.get("used", 0) or 0))
    window = payload.get("window")
    if not window:
        return f"{used} / ?"
    percent = payload.get("percent")
    suffix = f" ({percent}%)" if percent is not None else ""
    return f"{used} / {format_tokens(int(window))}{suffix}"


def format_args(args: dict[str, Any]) -> str:
    """Render tool arguments as a short ``k=v, k=v`` summary."""
    parts: list[str] = []
    for key, value in args.items():
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
        text = " ".join(text.split())
        if len(text) > _MAX_ARG_CHARS:
            text = text[: _MAX_ARG_CHARS - 1] + "…"
        parts.append(f"{key}={text}")
    return ", ".join(parts)


class PlainRenderer:
    """Human-readable transcript on stdout; errors on stderr."""

    def __init__(self, out: TextIO | None = None, err: TextIO | None = None) -> None:
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        self._at_line_start = True

    # -- helpers -------------------------------------------------------
    def _write(self, text: str) -> None:
        if not text:
            return
        self.out.write(text)
        self.out.flush()
        self._at_line_start = text.endswith("\n")

    def _newline(self) -> None:
        if not self._at_line_start:
            self._write("\n")

    def _line(self, text: str) -> None:
        self._newline()
        self._write(text + "\n")

    # -- interface -----------------------------------------------------
    def event(self, event: dict[str, Any]) -> None:
        """Render one ``session.event`` notification body."""
        kind = str(event.get("kind", ""))
        payload = event.get("payload") or {}
        if kind == "message.delta":
            self._write(str(payload.get("text", "")))
        elif kind == "message.done":
            self._newline()
        elif kind == "tool.call":
            self._line(f"▶ {payload.get('name', '?')}({format_args(payload.get('args') or {})})")
        elif kind == "tool.result":
            name = payload.get("name", "?")
            if payload.get("ok"):
                self._line(f"✓ {name}")
            else:
                self._line(f"✗ {name}: {payload.get('error') or payload.get('output') or ''}")
        elif kind == "diff":
            self._line(f"± {payload.get('path', '?')}")
        elif kind == "mode.changed":
            self._line(f"· mode → {payload.get('mode', '?')}")
        elif kind == "subagent.spawn":
            self._line(f"· subagent {payload.get('name') or payload.get('agentId', '?')} started")
        elif kind == "subagent.done":
            self._line(f"· subagent {payload.get('agentId', '?')} finished")
        elif kind == "context":
            self._line(f"· context {format_context(payload)}")
        elif kind == "compaction":
            before = format_tokens(int(payload.get("before", 0) or 0))
            after = format_tokens(int(payload.get("after", 0) or 0))
            marker = ", auto" if payload.get("auto") else ""
            self._line(f"— compacted ({before} → {after} tokens{marker}) —")
        elif kind == "error":
            self._newline()
            self.err.write(f"✗ {payload.get('code', 'error')}: {payload.get('message', '')}\n")
            self.err.flush()

    def finish(
        self,
        exit_code: int,
        session_id: str | None,
        usage: dict[str, int],
        context: dict[str, Any] | None = None,
    ) -> None:
        """Terminate the transcript (no trailing summary in plain mode)."""
        del exit_code, session_id, usage, context
        self._newline()


class JsonRenderer:
    """One JSON object per line, then a final ``{"kind": "result", ...}``."""

    def __init__(self, out: TextIO | None = None) -> None:
        self.out = out if out is not None else sys.stdout

    def _emit(self, obj: dict[str, Any]) -> None:
        self.out.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        self.out.flush()

    def event(self, event: dict[str, Any]) -> None:
        self._emit(event)

    def finish(
        self,
        exit_code: int,
        session_id: str | None,
        usage: dict[str, int],
        context: dict[str, Any] | None = None,
    ) -> None:
        self._emit(
            {
                "kind": "result",
                "exitCode": exit_code,
                "sessionId": session_id,
                "usage": usage,
                # How full the window was when the turn ended (CORE-context);
                # null when the daemon never reported one.
                "context": context,
            }
        )


class TurnTracker:
    """Accumulates the bits of a turn the exit code depends on."""

    def __init__(self) -> None:
        self.usage: dict[str, int] = {"inputTokens": 0, "outputTokens": 0}
        self.denied = False
        self.reason: str | None = None
        self.turn_id: str | None = None
        #: Body of the most recent ``context`` event, reported by --json.
        self.context: dict[str, Any] | None = None

    def event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("kind", ""))
        payload = event.get("payload") or {}
        if kind == "usage":
            self.usage["inputTokens"] += int(payload.get("inputTokens", 0) or 0)
            self.usage["outputTokens"] += int(payload.get("outputTokens", 0) or 0)
        elif kind == "context":
            self.context = dict(payload)
        elif kind == "error" and str(payload.get("code", "")) in DENIAL_CODES:
            self.denied = True
        elif kind == "turn.done":
            self.reason = str(payload.get("reason", "complete"))
            self.turn_id = payload.get("turnId")

    @property
    def done(self) -> bool:
        return self.reason is not None

    def exit_code(self) -> int:
        """Map the finished turn onto an exit code."""
        if self.reason is None:
            return EXIT_AGENT_FAILED
        code = TURN_REASON_EXIT.get(self.reason, EXIT_AGENT_FAILED)
        if self.denied and code in (EXIT_OK, EXIT_AGENT_FAILED):
            return EXIT_DENIED
        return code


Renderer = PlainRenderer | JsonRenderer

__all__ = [
    "DENIAL_CODES",
    "EXIT_AGENT_FAILED",
    "EXIT_DENIED",
    "EXIT_NO_DAEMON",
    "EXIT_OK",
    "EXIT_TIMEOUT",
    "EXIT_USAGE",
    "TURN_REASON_EXIT",
    "JsonRenderer",
    "PlainRenderer",
    "Renderer",
    "TurnTracker",
    "format_args",
]
