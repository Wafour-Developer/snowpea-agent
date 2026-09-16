"""Builders for ``session.event`` payloads.

Every event that leaves the daemon is constructed here so the shape is checked
against the pydantic model in ``server/protocol.py`` exactly once.  Callers get
back a ``(kind, payload_dict)`` pair ready for :meth:`EventHub.emit`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from snowpea_core.server.protocol import (
    SESSION_EVENT_MODELS,
    AudioSpoken,
    BackendChanged,
    CompactionEvent,
    CompactionStarted,
    ContextEvent,
    DiffEvent,
    ErrorEvent,
    JobDone,
    JobFailed,
    LoopSuspected,
    LspDiagnostics,
    MessageDelta,
    MessageDone,
    MessageReasoning,
    MessageUser,
    ModeChanged,
    ModelChanged,
    ToolCallEvent,
    ToolProgress,
    ToolResultEvent,
    TurnDequeued,
    TurnDone,
    TurnQueued,
    TurnStarted,
    UsageEvent,
)

Event = tuple[str, dict[str, Any]]


def _pack(model: Any) -> Event:
    data = model.model_dump(mode="json")
    kind = data.pop("kind")
    return kind, data


def message_user(
    text: str,
    attachments: Sequence[Any] | None = None,
    *,
    steered: bool = False,
) -> Event:
    """The prompt that opened this turn, as it entered the history.

    Emitted once per prompt, where the user message is appended — so a
    ``session.resume`` replays the question alongside the answer it produced.
    Each attachment contributes only its flavour and display name; the bytes
    live in the attachment store, not in the event log.
    """
    files = [
        {"kind": getattr(item, "kind", "file") or "file", "name": getattr(item, "name", "") or ""}
        for item in (attachments or [])
    ]
    return _pack(MessageUser(text=text, attachments=files, steered=steered))  # type: ignore[arg-type]


def message_delta(text: str) -> Event:
    return _pack(MessageDelta(text=text))


def message_reasoning(text: str, chars: int) -> Event:
    """The model is thinking; ``chars`` is the running total for the turn."""
    return _pack(MessageReasoning(text=text, chars=chars))


def message_done(
    text: str,
    role: str = "assistant",
    *,
    kind: Literal["", "interrupted"] = "",
    truncated: bool = False,
    continuations: int = 0,
) -> Event:
    event_kind, payload = _pack(
        MessageDone(  # type: ignore[arg-type]
            text=text,
            role=role,
            messageKind=kind,
            truncated=truncated,
            continuations=continuations,
        )
    )
    payload.pop("messageKind", None)
    if kind:
        payload["kind"] = kind
    return event_kind, payload


def tool_call(call_id: str, name: str, args: dict[str, Any]) -> Event:
    return _pack(ToolCallEvent(callId=call_id, name=name, args=args))


def tool_result(
    call_id: str, name: str, ok: bool, output: str = "", error: str | None = None
) -> Event:
    return _pack(ToolResultEvent(callId=call_id, name=name, ok=ok, output=output, error=error))


def tool_progress(
    call_id: str,
    name: str,
    *,
    stream: str = "stdout",
    chunk: str = "",
    seq: int = 0,
    truncated: bool = False,
) -> Event:
    """Output a still-running tool has produced so far (IDE-PROGRESS D2).

    Advisory: ``tool.result`` stays the authoritative record of the call.
    """
    return _pack(
        ToolProgress(  # type: ignore[arg-type]
            callId=call_id,
            name=name,
            stream=stream,
            chunk=chunk,
            seq=seq,
            truncated=truncated,
        )
    )


def diff(path: str, patch: str) -> Event:
    return _pack(DiffEvent(path=path, patch=patch))


def mode_changed(mode: str) -> Event:
    return _pack(ModeChanged(mode=mode))  # type: ignore[arg-type]


def model_changed(
    provider: str | None,
    model: str | None,
    effort: str | None = None,
    effort_source: str | None = None,
) -> Event:
    """This session is now talking to ``provider``/``model``.

    ``effort``/``effort_source`` ride along so a HUD showing "⚙ high" learns
    about a pin, a vendor rule, or a model change that altered the effective
    tier, without a second round trip (CORE-effort).
    """
    return _pack(
        ModelChanged(
            provider=provider,
            model=model,
            effort=effort,  # type: ignore[arg-type]
            effortSource=effort_source,  # type: ignore[arg-type]
        )
    )


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


def compaction_started(before: int, *, auto: bool = False) -> Event:
    """Compaction is about to run; ``compaction`` reports how it went."""
    return _pack(
        CompactionStarted(reason="auto" if auto else "manual", before=before)  # type: ignore[arg-type]
    )


def error(code: str, message: str) -> Event:
    return _pack(ErrorEvent(code=code, message=message))


def audio_spoken(
    path: str,
    mime: str = "audio/mpeg",
    provider: str = "",
    played: bool = False,
    voice: str | None = None,
    utterance: str = "reply",
) -> Event:
    return _pack(
        AudioSpoken(
            path=path,
            mime=mime,
            provider=provider,
            played=played,
            voice=voice,
            utterance=utterance,
        )
    )


def turn_started(turn_id: str, prompt: str | None = None, *, queued: bool = False) -> Event:
    """A turn began running, after any wait in the prompt queue."""
    return _pack(TurnStarted(turnId=turn_id, prompt=prompt, queued=queued))


def turn_queued(turn_id: str, position: int, queued: int) -> Event:
    """A prompt was accepted but parked behind the running turn."""
    return _pack(TurnQueued(turnId=turn_id, position=position, queued=queued))


def turn_dequeued(turn_id: str, reason: str = "started", queued: int = 0) -> Event:
    """A queued prompt started, was dropped, or was steered into the running turn."""
    return _pack(TurnDequeued(turnId=turn_id, reason=reason, queued=queued))  # type: ignore[arg-type]


def lsp_diagnostics(path: str, *, count: int, errors: int, warnings: int) -> Event:
    """One language server's verdict on one file (M13 contract §4)."""
    return _pack(LspDiagnostics(path=path, count=count, errors=errors, warnings=warnings))


def loop_suspected(tool: str, count: int) -> Event:
    """One tool call keeps repeating with the same arguments (CORE-repeat-guard)."""
    return _pack(LoopSuspected(tool=tool, count=count))


def turn_done(turn_id: str, reason: str = "complete", *, synthetic: bool = False) -> Event:
    """A turn ended.

    ``synthetic`` marks an event the daemon wrote on the turn's behalf to close
    one a crash or a restart left open (CORE-dangling-turns).
    """
    return _pack(
        TurnDone(turnId=turn_id, reason=reason, synthetic=synthetic)  # type: ignore[arg-type]
    )


def job_done(job_id: str, session_id: str | None, status: str = "ok", text: str = "") -> Event:
    """A scheduled job the *originating* thread created finished (CORE-session-kind)."""
    return _pack(JobDone(jobId=job_id, sessionId=session_id, status=status, text=text))


def job_failed(
    job_id: str, session_id: str | None, status: str = "error", text: str = ""
) -> Event:
    """A scheduled job the originating thread created ended without an answer."""
    return _pack(JobFailed(jobId=job_id, sessionId=session_id, status=status, text=text))


def validate(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Round-trip ``payload`` through the model registered for ``kind``."""
    model = SESSION_EVENT_MODELS.get(kind)
    if model is None:
        return payload
    data_in = {**payload, "kind": kind}
    message_kind = ""
    if kind == "message.done" and "kind" in payload:
        message_kind = str(payload["kind"])
        data_in["messageKind"] = message_kind
    data = model.model_validate(data_in).model_dump(mode="json")
    data.pop("kind", None)
    data.pop("messageKind", None)
    if message_kind:
        data["kind"] = message_kind
    return data


__all__ = [
    "Event",
    "backend_changed",
    "compaction",
    "compaction_started",
    "context",
    "diff",
    "error",
    "job_done",
    "job_failed",
    "loop_suspected",
    "message_delta",
    "message_done",
    "message_reasoning",
    "message_user",
    "mode_changed",
    "model_changed",
    "tool_call",
    "tool_progress",
    "tool_result",
    "turn_dequeued",
    "turn_done",
    "turn_queued",
    "turn_started",
    "usage",
    "validate",
]
