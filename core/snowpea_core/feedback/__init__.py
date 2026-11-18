"""개선하기 — a person sends one conversation to the Snowpea team (protocol 1.59.0).

Capability ``improvementFeedback``: ``feedback.preview`` builds the report the
way it would leave (redacted, fitted to the server's limits) and
``feedback.send`` uploads it (``server/feedback_handlers.py``).  The report is
schema 1 of snowpea-browser's ``docs/feedback-api.md`` with ``client.app``
``snowpea-core``; it is built from the session's stored events, the same source
the browser reads (``session.resume`` from seq 0).  Only a person's connection
reaches it (``person_gate.PERSON_METHODS``); it is never a tool.
"""

from __future__ import annotations

import gzip
import os
import platform
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from snowpea_core.feedback.redact import RedactStats, redact_messages, redact_string
from snowpea_core.feedback.report import (
    COMMENT_CAP,
    MAX_COMPRESSED,
    MAX_RAW,
    NOTE_CAP,
    ReportError,
    check_attachments,
    check_report,
    dumps,
    events_to_messages,
    fit_report,
)

SURFACES = ("desktop", "tui", "web", "ide", "cli", "messenger", "other")
_SURFACE_OF_CLIENT = {
    "tui": "tui",
    "ide": "ide",
    "web": "web",
    "cli": "cli",
    "desktop": "desktop",
    "app": "desktop",
}
_ENV_SECRET_SUFFIXES = ("_API_KEY", "_APIKEY", "_TOKEN", "_SECRET", "_KEY", "_PASSWORD")


class SessionMissing(LookupError):
    """No live or stored session has that id."""


def surface_of(conn: Any) -> str:
    """The report's ``client.surface`` for a connection that did not name one."""
    return _SURFACE_OF_CLIENT.get(str(getattr(conn, "client_kind", "") or ""), "other")


def known_secrets(core: Any) -> list[str]:
    """Strings that must never leave verbatim: the daemon token, every secret-named
    value in the settings (provider keys, messenger tokens…) and secret-named
    environment variables."""
    from snowpea_core.config.patch import SECRET_KEYS as SETTINGS_SECRET_KEYS
    from snowpea_core.feedback.redact import MIN_SECRET
    from snowpea_core.feedback.redact import SECRET_KEYS as REDACT_KEYS

    found: list[str] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, item in value.items():
                walk(item, str(k))
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif (
            isinstance(value, str)
            and key
            and (key in SETTINGS_SECRET_KEYS or REDACT_KEYS.fullmatch(key))
        ):
            found.append(value)

    token = getattr(core, "token", None)
    if isinstance(token, str):
        found.append(token)
    settings = getattr(core, "settings", None)
    if settings is not None:
        try:
            walk(settings.model_dump() if hasattr(settings, "model_dump") else settings)
        except Exception:  # noqa: BLE001 - a settings shape we cannot read adds nothing
            pass
    for name, value in os.environ.items():
        # Key-shaped values only: a long run without spaces (not "enabled", not a path list).
        if (
            name.upper().endswith(_ENV_SECRET_SUFFIXES)
            and len(value) >= 16
            and not any(c.isspace() for c in value)
        ):
            found.append(value)
    # Longest first, so a key that contains another is masked whole.
    unique = {s for s in found if isinstance(s, str) and len(s.strip()) >= MIN_SECRET}
    return sorted(unique, key=len, reverse=True)


def _short(value: Any, n: int) -> str | None:
    return value.strip()[:n] if isinstance(value, str) and value.strip() else None


def _os_name() -> str:
    name = platform.system().lower()
    return {"windows": "win32"}.get(name, name) or "unknown"


def _arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, machine
    ) or "unknown"


@dataclass
class Built:
    report: dict[str, Any]
    raw: bytes
    body: bytes
    stats: dict[str, int]


async def build_report(
    core: Any,
    session_id: str,
    *,
    comment: str = "",
    flagged: list[dict[str, Any]] | None = None,
    surface: str = "other",
    surface_version: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> Built:
    """The report for ``session_id``, redacted and fitted, plus its gzip body.

    :class:`SessionMissing` when no session has the id; :class:`ReportError`
    when the report cannot be sent (no messages, an unknown flagged id, too
    large, a bad attachment).
    """
    from snowpea_core.feedback.sender import install_id
    from snowpea_core.server.protocol import PROTOCOL_VERSION, SERVER_VERSION

    sessions = getattr(core, "sessions", None)
    live = sessions.get(session_id) if sessions is not None else None
    store = getattr(core, "store", None)
    row: dict[str, Any] | None = None
    if store is not None:
        try:
            row = await store.session(session_id)
        except Exception:  # noqa: BLE001 - the live session is enough
            row = None
    if live is None and row is None:
        raise SessionMissing(session_id)
    events = await store.events_after(session_id, 0) if store is not None else []

    secrets = known_secrets(core)
    messages, rstats = redact_messages(events_to_messages(events), secrets)
    if not messages:
        raise ReportError("this conversation has no messages yet", "sessionId")

    ids = {m["id"] for m in messages}
    flags: list[dict[str, Any]] = []
    seen: set[str] = set()
    unknown: list[str] = []
    for flag in flagged or []:
        mid = flag.get("messageId")
        if mid not in ids:
            unknown.append(str(mid))
            continue
        if mid in seen:
            continue
        seen.add(mid)
        note = flag.get("note")
        entry: dict[str, Any] = {"messageId": mid}
        if isinstance(note, str) and note.strip():
            entry["note"] = redact_string(note.strip(), secrets, rstats)[:NOTE_CAP]
        flags.append(entry)
    if unknown:
        raise ReportError(f"flagged: no message with id {', '.join(unknown[:5])}", "flagged")

    client: dict[str, Any] = {
        "app": "snowpea-core",
        "surface": surface if surface in SURFACES else "other",
        "version": SERVER_VERSION[:64],
        "os": _os_name()[:64],
        "arch": _arch()[:64],
        "coreVersion": SERVER_VERSION[:64],
        "protocolVersion": PROTOCOL_VERSION,
    }
    if surface_version and surface_version.strip():
        client["surfaceVersion"] = surface_version.strip()[:64]
    model = _short(getattr(live, "model", None), 200) or _short((row or {}).get("model"), 200)
    if model:
        client["model"] = model
    mode = _short(getattr(live, "mode", None), 64) or _short((row or {}).get("mode"), 64)
    if mode:
        client["permissionMode"] = mode
    locale = _short(getattr(live, "locale", None), 35)
    if locale:
        client["locale"] = locale

    session: dict[str, Any] = {"id": session_id[:200]}
    title = _short(getattr(live, "title", None), 1000) or _short((row or {}).get("title"), 1000)
    if title:
        session["title"] = redact_string(title, secrets, rstats)[:1000]
    created = _short((row or {}).get("created_at"), 64)
    if created:
        session["createdAt"] = created
    session["messages"] = messages

    report: dict[str, Any] = {
        "schema": 1,
        "kind": "improvement",
        "installId": install_id(core.paths.home),
        "createdAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "comment": redact_string(comment.strip(), secrets, rstats)[:COMMENT_CAP] if comment else "",
        "flagged": flags,
        "client": client,
        "session": session,
    }
    raw, body, dropped = fit_report(report)
    check_report(report)
    if attachments:
        check_attachments(attachments)
        report["attachments"] = [
            {
                "name": str(a.get("name") or "image")[:200],
                "mime": a["mime"],
                "bytesBase64": a["bytesBase64"],
            }
            for a in attachments
        ]
        # Attachments are never cut: the whole body must still fit.
        raw = dumps(report)
        body = gzip.compress(raw)
        if len(body) > MAX_COMPRESSED or len(raw) > MAX_RAW:
            raise ReportError(
                "the conversation and its attachments are too large to send",
                "attachments",
                too_large=True,
            )
    stats = _stats(report["session"]["messages"], rstats, len(raw), dropped)
    return Built(report=report, raw=raw, body=body, stats=stats)


def _stats(
    messages: list[dict[str, Any]], rstats: RedactStats, raw_bytes: int, dropped: int
) -> dict[str, int]:
    return {
        "messages": len(messages),
        "toolMessages": sum(1 for m in messages if m.get("role") == "tool"),
        "rawBytes": raw_bytes,
        "redactions": rstats.strings + rstats.withheld,
        "droppedResults": dropped,
    }


__all__ = ["Built", "SURFACES", "SessionMissing", "build_report", "known_secrets", "surface_of"]
