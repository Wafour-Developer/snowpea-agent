"""Site memory (protocol 1.9.0): learned per-site maps for the browser host.

One entry per (browser profile, origin, page type): the page's structure and
how to find its controls, never field values or page text.  The host builds
and checks entries before it sends them; everything here is the second line
of defence, so a check refuses rather than strips: a silently "cleaned" entry
would hide a host bug that leaks data.

Entries are page-derived and untrusted.  Core stores and returns them; it
never puts them in a prompt.  Storage is the ``site_entries`` table in
``state.db`` (:mod:`snowpea_core.session.store`); the RPC surface is
:mod:`snowpea_core.server.site_handlers`.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from snowpea_core.util.redaction import (
    _BEARER,
    _SECRET_KEY_NAME,
    _WELL_KNOWN_VALUES,
    is_sensitive_key,
)

#: Serialized size of one stored entry, in UTF-8 bytes.
MAX_ENTRY_BYTES = 16 * 1024
MAX_ENTRIES_PER_ORIGIN = 30
MAX_ORIGINS_PER_PROFILE = 1000
#: An entry nobody verified for this long is dropped.
UNVERIFIED_DAYS = 90
#: A stale entry with this many failures and no success for STALE_DAYS is dropped.
STALE_FAILURES = 3
STALE_DAYS = 30


class SiteMemoryError(ValueError):
    """An entry or a request that site memory refuses; ``field`` names the culprit."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


# ---------------------------------------------------------------------------
# what an entry may not carry
# ---------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
#: International or grouped national numbers ("+1 555 123 4567",
#: "010-1234-5678", "(555) 123-4567"); a date such as 2026-10-06 is not one.
_PHONE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?\(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{4}\b")
_DIGIT_RUN = re.compile(r"\d{8,}")
#: ``password=…``-style pairs.  Only ``=``: redaction.py also treats
#: "password field" as a pair, which would refuse every login page summary.
_SECRET_PAIR = re.compile(rf"(?i)(?P<key>{_SECRET_KEY_NAME})\s*=\s*[^\s,;\]]+")
_AUTH_HEADER = re.compile(r"(?i)\bauthorization\s*:")
#: CSS that is really XPath, or a selector that reads an input's value.
_XPATH_LIKE = re.compile(r"(?i)^\s*(?:/|\./|\(\s*/|xpath\s*[=:])")
_VALUE_SELECTOR = re.compile(r"(?i)\[\s*value\s*[~|^$*]?=")


def _looks_sensitive(text: str) -> bool:
    if _EMAIL.search(text) or _PHONE.search(text) or _DIGIT_RUN.search(text):
        return True
    if _BEARER.search(text) or _WELL_KNOWN_VALUES.search(text) or _AUTH_HEADER.search(text):
        return True
    return any(is_sensitive_key(m.group("key")) for m in _SECRET_PAIR.finditer(text))


def _strings(value: Any, path: str) -> list[tuple[str, str]]:
    """Every string in ``value`` with its field path (``actions[0].locators[1].value``)."""
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        out: list[tuple[str, str]] = []
        for key, item in value.items():
            out.extend(_strings(item, f"{path}.{key}"))
        return out
    if isinstance(value, list):
        out = []
        for index, item in enumerate(value):
            out.extend(_strings(item, f"{path}[{index}]"))
        return out
    return []


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def normalize_origin(origin: str, field: str = "origin") -> str:
    """``scheme://host[:port]`` in lower case; anything else is refused."""
    raw = (origin or "").strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise SiteMemoryError(field, "not an origin") from exc
    if (
        parts.scheme.lower() not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise SiteMemoryError(field, "must be an http(s) origin such as https://example.com")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme.lower()}://{host}" + (f":{port}" if port is not None else "")


def check_entry(entry: dict[str, Any]) -> None:
    """Refuse what the wire schema cannot express (lengths and counts it does).

    ``entry`` is a ``SiteEntryInput`` dump.  Field paths in errors start with
    ``entry.`` so the host can point at the culprit.
    """
    pattern = entry["urlPattern"]
    if not pattern.startswith("/"):
        raise SiteMemoryError("entry.urlPattern", "must start with '/'")
    for index, action in enumerate(entry.get("actions") or []):
        where = f"entry.actions[{index}]"
        locators = action.get("locators") or []
        if all(loc["by"] == "css" for loc in locators):
            raise SiteMemoryError(f"{where}.locators", "needs at least one non-CSS locator")
        for li, loc in enumerate(locators):
            at = f"{where}.locators[{li}]"
            if loc["by"] == "role":
                if not loc.get("role") or loc.get("value") is not None:
                    raise SiteMemoryError(at, "a role locator takes role (and name), not value")
                continue
            if not loc.get("value") or loc.get("role") is not None or loc.get("name") is not None:
                raise SiteMemoryError(at, f"a {loc['by']} locator takes value only")
            if loc["by"] == "css":
                if _XPATH_LIKE.search(loc["value"]):
                    raise SiteMemoryError(f"{at}.value", "XPath is not a locator")
                if action.get("loginField") and _VALUE_SELECTOR.search(loc["value"]):
                    raise SiteMemoryError(
                        f"{at}.value", "a login field is stored by locators only, never a value"
                    )
    lv = entry.get("lastVerified")
    if lv is not None:
        parse_time(lv, "entry.lastVerified")
    # The fingerprint is a hash: digit runs in it are not personal data, so it
    # is checked for shape instead of content.
    fingerprint = entry.get("fingerprint")
    if fingerprint is not None and not re.fullmatch(r"[A-Za-z0-9:._+/=-]+", fingerprint):
        raise SiteMemoryError("entry.fingerprint", "must be a plain hash string")
    checked = {k: v for k, v in entry.items() if k not in ("fingerprint", "lastVerified")}
    for path, text in _strings(checked, "entry"):
        if _CONTROL.search(text):
            raise SiteMemoryError(path, "control characters and newlines are not allowed")
        if _looks_sensitive(text):
            raise SiteMemoryError(path, "looks like personal data or a credential")


def check_text(text: str, field: str) -> None:
    """The same content rule for a free string outside an entry (``site.mark`` detail)."""
    if _CONTROL.search(text):
        raise SiteMemoryError(field, "control characters and newlines are not allowed")
    if _looks_sensitive(text):
        raise SiteMemoryError(field, "looks like personal data or a credential")


def entry_size(entry: dict[str, Any]) -> int:
    return len(json.dumps(entry, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def check_size(entry: dict[str, Any]) -> None:
    if entry_size(entry) > MAX_ENTRY_BYTES:
        raise SiteMemoryError("entry", f"larger than {MAX_ENTRY_BYTES} bytes serialized")


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


def iso(moment: datetime) -> str:
    """The daemon's wire format (``config.paths.utc_now``), so rows compare as text."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str, field: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SiteMemoryError(field, "not an ISO-8601 time") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def retention_cutoffs(now: datetime) -> tuple[str, str]:
    """``(unverified_before, no_success_before)`` for the two retention rules."""
    return iso(now - timedelta(days=UNVERIFIED_DAYS)), iso(now - timedelta(days=STALE_DAYS))


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    # Before the query a "*" stays inside one path segment; in the query the
    # whole query is one segment, so there it may span anything.
    out: list[str] = []
    in_query = False
    for ch in pattern:
        if ch == "?":
            in_query = True
            out.append(re.escape(ch))
        elif ch == "*":
            out.append(".*" if in_query else "[^/]*")
        else:
            out.append(re.escape(ch))
    return re.compile("".join(out))


def pattern_matches(pattern: str, path: str) -> bool:
    """Whether ``path`` (with any query) matches ``pattern``.

    A pattern without a query part ignores the path's query, so
    ``/product/*`` matches ``/product/42?ref=home``.
    """
    path = path.split("#", 1)[0]
    if "?" not in pattern:
        path = path.split("?", 1)[0]
    return _pattern_regex(pattern).fullmatch(path) is not None


def specificity(pattern: str) -> tuple[int, int, int]:
    """Sort key: more literal characters, then fewer wildcards, then longer."""
    stars = pattern.count("*")
    return (len(pattern) - stars, -stars, len(pattern))


def order_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most specific urlPattern first; ties go to the most recently verified."""
    by_recent = sorted(entries, key=lambda e: str(e.get("lastVerified") or ""), reverse=True)
    return sorted(by_recent, key=lambda e: specificity(str(e["urlPattern"])), reverse=True)


__all__ = [
    "MAX_ENTRIES_PER_ORIGIN",
    "MAX_ENTRY_BYTES",
    "MAX_ORIGINS_PER_PROFILE",
    "STALE_DAYS",
    "STALE_FAILURES",
    "UNVERIFIED_DAYS",
    "SiteMemoryError",
    "check_entry",
    "check_size",
    "check_text",
    "entry_size",
    "iso",
    "normalize_origin",
    "order_entries",
    "parse_time",
    "pattern_matches",
    "retention_cutoffs",
    "specificity",
]
