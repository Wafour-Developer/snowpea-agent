"""Builders for ``session.event`` payloads.

Every event that leaves the daemon is constructed here so the shape is checked
against the pydantic model in ``server/protocol.py`` exactly once.  Callers get
back a ``(kind, payload_dict)`` pair ready for :meth:`EventHub.emit`.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.server.protocol import (
    SESSION_EVENT_MODELS,
    AudioSpoken,
    BackendChanged,
    CompactionEvent,
    ContextEvent,
    DiffEvent,
    ErrorEvent,
    MessageDelta,
    MessageDone,
    ModeChanged,
    ModelChanged,
    ToolCallEvent,
    ToolResultEvent,
    TurnDequeued,
    TurnDone,
    TurnQueued,
    UsageEvent,
)

Event = tuple[str, dict[str, Any]]


def _pack(model: Any) -> Event:
    data = model.model_dump(mode="json")
    kind = data.pop("kind")
    return kind, data


def message_delta(text: str) -> Event:
    return _pack(MessageDelta(text=text))


def message_done(text: str, role: str = "assistant") -> Event:
    return _pack(MessageDone(text=text, role=role))  # type: ignore[arg-type]


def tool_call(call_id: str, name: str, args: dict[str, Any]) -> Event:
    return _pack(ToolCallEvent(callId=call_id, name=name, args=args))


def tool_result(
    call_id: str, name: str, ok: bool, output: str = "", error: str | None = None
) -> Event:
    return _pack(ToolResultEvent(callId=call_id, name=name, ok=ok, output=output, error=error))


def diff(path: str, patch: str) -> Event:
    return _pack(DiffEvent(path=path, patch=patch))


def mode_changed(mode: str) -> Event:
    return _pack(ModeChanged(mode=mode))  # type: ignore[arg-type]


def model_changed(provider: str | None, model: str | None) -> Event:
    """This session is now talking to ``provider``/``model``."""
    return _pack(ModelChanged(provider=provider, model=model))


def backend_changed(backend: str) -> Event:
    return _pack(BackendChanged(backend=backend))  # type: ignore[arg-type]


def usage(input_tokens: int, output_tokens: int) -> Event:
    return _pack(UsageEvent(inputTokens=input_tokens, outputTokens=output_tokens))


def context(
    used: int,
    window: int | None,
    *,
    estimated: bool = True,
    model: str | None = None,
    provider: str | None = None,
) -> Event:
    """How full the context window is; ``percent`` is derived here (CORE-context)."""
    percent = round(used * 100.0 / window, 1) if window else None
    return _pack(
        ContextEvent(
            used=used,
            window=window,
            percent=percent,
            estimated=estimated,
            model=model,
            provider=provider,
        )
    )


def compaction(
    before: int, after: int, summary_chars: int, *, auto: bool = False, kept: int = 0
) -> Event:
    """The conversation was summarised and replaced (CORE-context)."""
    return _pack(
        CompactionEvent(
            before=before, after=after, summaryChars=summary_chars, auto=auto, kept=kept
        )
    )


def error(code: str, message: str) -> Event:
    return _pack(ErrorEvent(code=code, message=message))


def audio_spoken(
    path: str,
    mime: str = "audio/mpeg",
    provider: str = "",
    played: bool = False,
    voice: str | None = None,
) -> Event:
    return _pack(
        AudioSpoken(path=path, mime=mime, provider=provider, played=played, voice=voice)
    )


def turn_queued(turn_id: str, position: int, queued: int) -> Event:
    """A prompt was accepted but parked behind the running turn."""
    return _pack(TurnQueued(turnId=turn_id, position=position, queued=queued))


def turn_dequeued(turn_id: str, reason: str = "started", queued: int = 0) -> Event:
    """A queued prompt started running (``started``) or was flushed (``dropped``)."""
    return _pack(TurnDequeued(turnId=turn_id, reason=reason, queued=queued))  # type: ignore[arg-type]


def turn_done(turn_id: str, reason: str = "complete") -> Event:
    return _pack(TurnDone(turnId=turn_id, reason=reason))  # type: ignore[arg-type]


def validate(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Round-trip ``payload`` through the model registered for ``kind``."""
    model = SESSION_EVENT_MODELS.get(kind)
    if model is None:
        return payload
    data = model.model_validate({**payload, "kind": kind}).model_dump(mode="json")
    data.pop("kind", None)
    return data


__all__ = [
    "Event",
    "backend_changed",
    "compaction",
    "context",
    "diff",
    "error",
    "message_delta",
    "message_done",
    "mode_changed",
    "model_changed",
    "tool_call",
    "tool_result",
    "turn_dequeued",
    "turn_done",
    "turn_queued",
    "usage",
    "validate",
]
