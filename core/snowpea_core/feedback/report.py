"""Build an improvement report (schema 1) from a session's stored events.

The mapping is snowpea-browser's ``eventsToMessages`` (host/src/parent/feedback.ts):
the same events become the same messages with the same ids (``m_<seq>`` of the
event that started the message), so a report from core and one from the browser
read alike in the admin console.  :func:`fit_report` gzips the report and drops
the largest tool results (then args, then long texts) until it fits the
server's limits; :func:`check_report` enforces the rest of the server contract
(docs/feedback-api.md in snowpea-browser) before anything is sent.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

#: The server takes 8 MB compressed; stay a little under.
MAX_COMPRESSED = 8 * 1024 * 1024 - 64 * 1024
#: …and 40 MB once decompressed.
MAX_RAW = 38 * 1024 * 1024
MAX_MESSAGES = 20_000
COMMENT_CAP = 20_000
NOTE_CAP = 4000
MAX_FLAGGED = 1000
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
#: Message ids the server refuses (they would shadow an object's own keys).
FORBIDDEN_IDS = frozenset({"__proto__", "constructor", "prototype"})
#: An attachment's mime type and the signature its bytes must start with.
IMAGE_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}

#: Events that say nothing a reviewer needs (streaming fragments are folded into their message).
SKIPPED = frozenset(
    {
        "message.delta",
        "tool.progress",
        "usage",
        "context",
        "turn.started",
        "turn.queued",
        "turn.dequeued",
        "subagent.update",
        "lsp.diagnostics",
        "audio.spoken",
        "command.progress",
        "team.task.update",
        "checkpoint.updated",
        "compaction.started",
    }
)


class ReportError(Exception):
    """The report cannot be built or would be refused; ``field`` names what."""

    def __init__(self, message: str, field: str = "", *, too_large: bool = False) -> None:
        super().__init__(message)
        self.field = field
        self.too_large = too_large


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def internal_error_id(payload: dict[str, Any]) -> str:
    """An internal error's id (``details.errorId``, or ``internal error (id …)``); "" when none."""
    details = payload.get("details")
    if (
        isinstance(details, dict)
        and isinstance(details.get("errorId"), str)
        and details["errorId"].strip()
    ):
        return details["errorId"].strip()[:64]
    match = re.search(r"internal error \(id ([^)\s]{1,64})\)", _str(payload.get("message")), re.I)
    return match.group(1) if match else ""


_LOCAL_PATH = re.compile(r"^(?:/|~[\\/]|[A-Za-z]:[\\/]|\\\\|file:)")


def local_basename(value: str) -> str:
    """An absolute local path (POSIX, ~, Windows drive or UNC, file:) → its basename."""
    if not _LOCAL_PATH.search(value):
        return value
    parts = re.split(r"[\\/]", re.sub(r"[\\/]+$", "", value))
    return parts[-1] or value if parts else value


def _attachment_meta(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    out = {
        k: (local_basename(v) if isinstance(v, str) and k != "url" else v) for k, v in item.items()
    }
    if isinstance(out.get("url"), str) and re.match(r"^file:", out["url"], re.I):
        out["url"] = local_basename(out["url"])
    return out


def _msg_id(seq: int) -> str:
    return f"m_{seq}"


def events_to_messages(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stored events (``kind``, ``seq``, ``ts``, ``payload``) → schema-1 messages, in order.

    Tool calls and results are paired by callId into one ``tool`` message;
    reasoning and streamed text go with the assistant message they belong to;
    errors, turn ends, mode/model switches and subagents become ``event``
    messages; everything else not in :data:`SKIPPED` is kept as an event with
    its payload.
    """
    out: list[dict[str, Any]] = []
    tools: dict[str, tuple[dict[str, Any], float | None]] = {}
    state: dict[str, Any] = {
        "reasoning": "",
        "reasoning_seq": None,
        "deltas": "",
        "delta_seq": None,
        "ts": "",
    }

    def reset() -> None:
        state.update(reasoning="", reasoning_seq=None, deltas="", delta_seq=None)

    def flush() -> None:
        if not state["reasoning"] and not state["deltas"]:
            return
        seq = state["delta_seq"] if state["delta_seq"] is not None else state["reasoning_seq"]
        message: dict[str, Any] = {
            "id": _msg_id(seq),
            "seq": seq,
            "role": "assistant",
            "ts": state["ts"],
        }
        if state["deltas"]:
            message["text"] = state["deltas"]
        if state["reasoning"]:
            message["reasoning"] = state["reasoning"]
        if not state["deltas"]:
            message["meta"] = {"reasoningOnly": True}
        out.append(message)
        reset()

    def event(ev: dict[str, Any], text: str, meta: dict[str, Any] | None = None) -> None:
        out.append(
            {
                "id": _msg_id(ev["seq"]),
                "seq": ev["seq"],
                "role": "event",
                "ts": ev.get("ts") or "",
                "text": text,
                "meta": {"kind": ev.get("kind"), **(meta or {})},
            }
        )

    ordered = sorted(
        (e for e in events if isinstance(e, dict) and isinstance(e.get("seq"), int)),
        key=lambda e: e["seq"],
    )
    for ev in ordered:
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        kind = _str(ev.get("kind"))
        state["ts"] = ev.get("ts") or ""
        if kind == "message.user":
            flush()
            meta: dict[str, Any] = {}
            attachments = p.get("attachments")
            if isinstance(attachments, list) and attachments:
                meta["attachments"] = [_attachment_meta(a) for a in attachments]
            if p.get("expansion"):
                meta["expansion"] = p["expansion"]
            if p.get("steered") is True:
                meta["steered"] = True
            message = {
                "id": _msg_id(ev["seq"]),
                "seq": ev["seq"],
                "role": "user",
                "ts": state["ts"],
                "text": _str(p.get("text")),
            }
            if meta:
                message["meta"] = meta
            out.append(message)
        elif kind == "message.reasoning":
            if state["reasoning_seq"] is None:
                state["reasoning_seq"] = ev["seq"]
            state["reasoning"] += _str(p.get("text"))
        elif kind == "message.delta":
            if state["delta_seq"] is None:
                state["delta_seq"] = ev["seq"]
            state["deltas"] += _str(p.get("text"))
        elif kind == "message.done":
            if p.get("role") == "user":
                continue
            seq = next(
                s for s in (state["reasoning_seq"], state["delta_seq"], ev["seq"]) if s is not None
            )
            meta = {}
            if p.get("truncated") is True:
                meta["truncated"] = True
            if p.get("messageKind"):
                meta["messageKind"] = p["messageKind"]
            message = {
                "id": _msg_id(seq),
                "seq": seq,
                "role": "system" if p.get("role") == "system" else "assistant",
                "ts": state["ts"],
                "text": _str(p.get("text")) or state["deltas"],
            }
            if state["reasoning"]:
                message["reasoning"] = state["reasoning"]
            if meta:
                message["meta"] = meta
            out.append(message)
            reset()
        elif kind == "tool.call":
            flush()
            call_id = _str(p.get("callId"))
            if call_id and call_id in tools:
                continue
            tool: dict[str, Any] = {"name": _str(p.get("name")) or "tool"}
            if call_id:
                tool["callId"] = call_id
            tool["args"] = p.get("args") if p.get("args") is not None else {}
            tool["status"] = "running"
            message = {
                "id": _msg_id(ev["seq"]),
                "seq": ev["seq"],
                "role": "tool",
                "ts": state["ts"],
                "tool": tool,
            }
            out.append(message)
            if call_id:
                tools[call_id] = (message, _parse_ts(ev.get("ts")))
        elif kind == "tool.result":
            call_id = _str(p.get("callId"))
            entry = tools.get(call_id) if call_id else None
            if entry is None:
                # A result with no call in the store (it started before the replay window).
                flush()
                tool = {"name": _str(p.get("name")) or "tool"}
                if call_id:
                    tool["callId"] = call_id
                message = {
                    "id": _msg_id(ev["seq"]),
                    "seq": ev["seq"],
                    "role": "tool",
                    "ts": state["ts"],
                    "tool": tool,
                }
                out.append(message)
                entry = (message, None)
            tool = entry[0]["tool"]
            tool["status"] = "error" if p.get("ok") is False else "ok"
            result: dict[str, Any] = {}
            if isinstance(p.get("output"), str):
                result["output"] = p["output"]
            if isinstance(p.get("error"), str) and p["error"]:
                result["error"] = p["error"]
            # Images are references to core's store: only their shape goes.
            content = p.get("content")
            if isinstance(content, list):
                blocks = [
                    {"type": "image", "mediaType": _str(b.get("mediaType")) or "image/png"}
                    if isinstance(b, dict) and b.get("type") == "image"
                    else {
                        "type": _str(b.get("type") if isinstance(b, dict) else None) or "text",
                        "text": _str(b.get("text") if isinstance(b, dict) else None),
                    }
                    for b in content
                ]
                if any(b["type"] == "image" or b.get("text") for b in blocks):
                    result["content"] = blocks
            if isinstance(p.get("meta"), dict):
                result["meta"] = p["meta"]
            tool["result"] = result
            started, ended = entry[1], _parse_ts(ev.get("ts"))
            if started is not None and ended is not None and ended >= started:
                tool["durationMs"] = int(round((ended - started) * 1000))
        elif kind == "error":
            flush()
            error_id = internal_error_id(p)
            meta = {"code": _str(p.get("code"))}
            if error_id:
                meta["errorId"] = error_id
            event(ev, f"{_str(p.get('code')) or 'error'}: {_str(p.get('message'))}", meta)
        elif kind == "turn.done":
            flush()
            reason = _str(p.get("reason")) or "complete"
            if reason != "complete" or p.get("synthetic") is True:
                meta = {"reason": reason}
                if p.get("synthetic") is True:
                    meta["synthetic"] = True
                event(ev, f"turn ended: {reason}", meta)
        elif kind == "mode.changed":
            event(ev, f"mode: {_str(p.get('mode'))}", {"mode": _str(p.get("mode"))})
        elif kind == "model.changed":
            meta = {"model": _str(p.get("model"))}
            if isinstance(p.get("provider"), str):
                meta["provider"] = p["provider"]
            event(ev, f"model: {_str(p.get('model'))}", meta)
        elif kind == "subagent.spawn":
            flush()
            meta = {"agentId": _str(p.get("agentId"))}
            if isinstance(p.get("task"), str):
                meta["task"] = p["task"]
            label = _str(p.get("title")) or _str(p.get("name")) or _str(p.get("task"))
            event(ev, f"subagent started: {label}", meta)
        elif kind == "subagent.done":
            flush()
            status = _str(p.get("status")) or ("error" if p.get("ok") is False else "done")
            meta = {"agentId": _str(p.get("agentId"))}
            if isinstance(p.get("reason"), str):
                meta["reason"] = p["reason"]
            if isinstance(p.get("rounds"), int | float) and not isinstance(p.get("rounds"), bool):
                meta["rounds"] = p["rounds"]
            if isinstance(p.get("deniedTools"), list):
                meta["deniedTools"] = p["deniedTools"]
            event(ev, f"subagent {status}: {_str(p.get('summary')) or _str(p.get('result'))}", meta)
        elif kind in SKIPPED:
            continue
        else:
            # compaction, loop.suspected, hook.continue, diff, plan/todos, jobs, …:
            # kept with their payload.
            event(ev, kind, {"payload": p})
    flush()
    return _unique_ids(out)


def _unique_ids(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ids are unique within a report (the server refuses a repeat)."""
    seen: set[str] = set()
    for message in messages:
        base = str(message["id"])
        candidate, n = base, 1
        while candidate in seen or candidate in FORBIDDEN_IDS:
            n += 1
            candidate = f"{base}_{n}"
        message["id"] = candidate
        seen.add(candidate)
    return messages


def dumps(report: dict[str, Any]) -> bytes:
    return json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def fit_report(
    report: dict[str, Any], max_compressed: int = MAX_COMPRESSED, max_raw: int = MAX_RAW
) -> tuple[bytes, bytes, int]:
    """Gzip ``report`` (changed in place to fit); returns (raw JSON, gzip body, fields dropped).

    A chat over :data:`MAX_MESSAGES` keeps its start and end with a gap
    message between; then the largest tool results, args and long texts are
    cut until the body fits.  :class:`ReportError` (``too_large``) when even
    that is not enough.
    """
    session = report["session"]
    messages = session["messages"]
    if len(messages) > MAX_MESSAGES:
        head = messages[: MAX_MESSAGES // 2]
        tail = messages[-(MAX_MESSAGES // 2 - 1) :]
        gap = {
            "id": "m_gap",
            "role": "event",
            "text": f"{len(messages) - len(head) - len(tail)} messages left out (too many)",
        }
        session["messages"] = [*head, gap, *tail]
        kept = {m["id"] for m in session["messages"]}
        report["flagged"] = [f for f in report["flagged"] if f["messageId"] in kept]

    slots: list[tuple[float, int, Any]] = []

    def drop_field(holder: dict[str, Any], key: str, size: int) -> Any:
        def run() -> None:
            holder[key] = {"dropped": f"{size} characters left out to fit the size limit"}

        return run

    def cut_text(holder: dict[str, Any], key: str, text: str) -> Any:
        def run() -> None:
            holder[key] = (
                f"{text[:4096]}\n…[{len(text) - 4096} characters left out to fit the size limit]"
            )

        return run

    for message in session["messages"]:
        tool = message.get("tool")
        if isinstance(tool, dict):
            if "result" in tool:
                size = len(json.dumps(tool["result"], ensure_ascii=False))
                slots.append((size, size - 60, drop_field(tool, "result", size)))
            if "args" in tool:
                size = len(json.dumps(tool["args"], ensure_ascii=False))
                slots.append((size / 2, size - 60, drop_field(tool, "args", size)))
        for key in ("text", "reasoning"):
            text = message.get(key)
            if isinstance(text, str) and len(text) > 4096:
                slots.append((len(text) / 4, len(text) - 4160, cut_text(message, key, text)))
    slots.sort(key=lambda slot: slot[0], reverse=True)

    dropped = 0
    while True:
        raw = dumps(report)
        body = gzip.compress(raw)
        fits = len(body) <= max_compressed and len(raw) <= max_raw
        if fits or dropped >= len(slots):
            if not fits:
                raise ReportError(
                    "the conversation is too large to send even without tool results",
                    "session",
                    too_large=True,
                )
            return raw, body, dropped
        over = max((len(body) - max_compressed) * (len(raw) / len(body)), len(raw) - max_raw, 1)
        freed = 0
        while dropped < len(slots) and freed < over * 1.1:
            freed += max(slots[dropped][1], 1)
            slots[dropped][2]()
            dropped += 1


def check_attachments(attachments: list[dict[str, Any]]) -> None:
    """The server's attachment rules: images only, at most 10, 5 MB decoded in total,
    standard base64 without a data-URL prefix, bytes that start with the format's signature."""
    if len(attachments) > MAX_ATTACHMENTS:
        raise ReportError(f"at most {MAX_ATTACHMENTS} attachments", "attachments")
    total = 0
    for i, item in enumerate(attachments):
        mime = item.get("mime")
        signatures = IMAGE_SIGNATURES.get(mime) if isinstance(mime, str) else None
        if signatures is None:
            raise ReportError(
                f"attachments[{i}].mime: must be one of {', '.join(IMAGE_SIGNATURES)}",
                "attachments",
            )
        try:
            data = base64.b64decode(str(item.get("bytesBase64") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ReportError(
                f"attachments[{i}].bytesBase64: not standard base64", "attachments"
            ) from exc
        if (
            not data
            or not any(data.startswith(sig) for sig in signatures)
            or (mime == "image/webp" and data[8:12] != b"WEBP")
        ):
            raise ReportError(f"attachments[{i}]: the bytes are not a {mime} image", "attachments")
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise ReportError("attachments: at most 5 MB in total", "attachments")


def check_report(report: dict[str, Any]) -> None:
    """The rest of the server's validation, so a refusal is ours, not a 400 after the upload."""
    messages = report["session"]["messages"]
    if not 1 <= len(messages) <= MAX_MESSAGES:
        raise ReportError(
            "the conversation has no messages yet"
            if not messages
            else f"at most {MAX_MESSAGES} messages",
            "session",
        )
    ids: set[str] = set()
    for i, message in enumerate(messages):
        mid = message.get("id")
        if (
            not isinstance(mid, str)
            or not 1 <= len(mid) <= 200
            or mid in FORBIDDEN_IDS
            or mid in ids
        ):
            raise ReportError(
                f"session.messages[{i}].id: missing, repeated or not allowed", "session"
            )
        ids.add(mid)
    if len(report["comment"]) > COMMENT_CAP:
        raise ReportError(f"comment: at most {COMMENT_CAP} characters", "comment")
    flagged = report["flagged"]
    if len(flagged) > MAX_FLAGGED:
        raise ReportError(f"flagged: at most {MAX_FLAGGED}", "flagged")
    for i, flag in enumerate(flagged):
        if flag.get("messageId") not in ids:
            raise ReportError(f"flagged[{i}].messageId: no such message", "flagged")
        if len(flag.get("note", "")) > NOTE_CAP:
            raise ReportError(f"flagged[{i}].note: at most {NOTE_CAP} characters", "flagged")
    check_attachments(report.get("attachments") or [])


__all__ = [
    "COMMENT_CAP",
    "FORBIDDEN_IDS",
    "MAX_COMPRESSED",
    "MAX_MESSAGES",
    "MAX_RAW",
    "NOTE_CAP",
    "ReportError",
    "check_attachments",
    "check_report",
    "dumps",
    "events_to_messages",
    "fit_report",
    "local_basename",
]
