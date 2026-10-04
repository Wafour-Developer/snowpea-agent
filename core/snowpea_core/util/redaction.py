"""Shared masking for secrets that can reach history or chat progress."""

from __future__ import annotations

import re
from typing import Any

TEXT_REDACTION = "***"
VALUE_REDACTION = "<redacted>"

_SENSITIVE_KEY_MARKERS = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "credentials",
    "auth",
    "authorization",
    "cookie",
)
_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_SECRET_KEY_NAME = (
    r"[A-Za-z0-9_-]*(?:key|token|secret|password|passwd|credentials?|auth|authorization|cookie)"
    r"[A-Za-z0-9_-]*"
)
_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?P<key>{_SECRET_KEY_NAME})"
    r"(?P<sep>\s*(?:=|:)\s*|\s+)"
    r"(?P<value>[^\s,;]+)"
)
_AUTHORIZATION_HEADER = re.compile(r"(?im)^(\s*authorization\s*:\s*).+$")
_INLINE_AUTHORIZATION_BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*)bearer\s+[^\s,;]+")
_JSON_PAIR = re.compile(
    rf'(?i)(?P<prefix>"(?P<key>{_SECRET_KEY_NAME})"\s*:\s*)'
    r'"(?:\\.|[^"\\])*"'
)
_BEARER = re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._~+/=-]{8,})")
_WELL_KNOWN_VALUES = re.compile(
    r"(?ix)"
    r"\bsk-(?:ant-)?[A-Za-z0-9][A-Za-z0-9._-]{3,}\b"
    r"|\bgh[opsu]_[A-Za-z0-9_]{8,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{8,}\b"
    r"|\bxox[abpr]-[A-Za-z0-9-]{8,}\b"
    r"|\bAKIA[0-9A-Z]{8,20}\b"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)


def is_sensitive_key(key: str) -> bool:
    """Whether an argument/header/env key conventionally names a credential."""
    lowered = str(key or "").strip().lower()
    if not lowered:
        return False
    parts = [part for part in re.split(r"[_\-\s]+", lowered) if part]
    if any(part in _SENSITIVE_KEY_MARKERS for part in parts):
        return True
    for marker in _SENSITIVE_KEY_MARKERS:
        if lowered.endswith(marker):
            return True
        if marker != "key" and marker in lowered:
            return True
    return "key" in lowered and "api" in lowered


def redact_text(text: str, *, replacement: str = TEXT_REDACTION) -> str:
    """Mask secret-looking keys and common token values in free-form text."""

    def assignment(match: re.Match[str]) -> str:
        key = match.group("key")
        if not is_sensitive_key(key):
            return match.group(0)
        return f"{key}{match.group('sep')}{replacement}"

    def json_pair(match: re.Match[str]) -> str:
        if not is_sensitive_key(match.group("key")):
            return match.group(0)
        return f'{match.group("prefix")}"{replacement}"'

    masked = _JSON_PAIR.sub(json_pair, str(text or ""))
    masked = _AUTHORIZATION_HEADER.sub(lambda m: f"{m.group(1)}{replacement}", masked)
    masked = _INLINE_AUTHORIZATION_BEARER.sub(lambda m: f"{m.group(1)}{replacement}", masked)
    masked = _SECRET_ASSIGNMENT.sub(assignment, masked)
    masked = _BEARER.sub(lambda m: f"{m.group(1)} {replacement}", masked)
    return _WELL_KNOWN_VALUES.sub(replacement, masked)


def redact_value(key: str, value: Any) -> Any:
    """Return a redacted copy of structured data for display/persistence."""
    if is_sensitive_key(key):
        return VALUE_REDACTION
    if isinstance(value, dict):
        return {str(k): redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value("", item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


__all__ = ["VALUE_REDACTION", "is_sensitive_key", "redact_text", "redact_value"]
