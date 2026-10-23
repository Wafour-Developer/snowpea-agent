"""What user memory refuses to store (addendum 27, C6).

A remembered note goes back into every later prompt through the digest, so a
password or a phone number saved once is repeated to the model for good.
``save_memory``, the auto-remember nudge and ``memory.write`` all ask
:func:`sensitive_kind` first and refuse rather than strip: a note with its
value cut out can say the opposite of what was meant, and a refusal tells the
model what to save instead.  Site memory has its own, stricter check
(:mod:`snowpea_core.session.site_memory`).

The answer names the kind of value found, never the value itself.
"""

from __future__ import annotations

import re

from snowpea_core.util.redaction import (
    _BEARER,
    _SECRET_KEY_NAME,
    _WELL_KNOWN_VALUES,
    is_sensitive_key,
)

_SECRET_PAIR = re.compile(rf"(?i)(?P<key>{_SECRET_KEY_NAME})\s*[=:]\s*\S+")
_AUTH_HEADER = re.compile(r"(?i)\bauthorization\s*:\s*\S+")
#: A long run of key-like characters with both letters and digits in it.
_OPAQUE_KEY = re.compile(
    r"(?<![A-Za-z0-9_-])(?=[A-Za-z_-]*\d)(?=[\d_-]*[A-Za-z])[A-Za-z0-9_-]{32,}"
)
#: 주민등록번호: YYMMDD-GNNNNNN, with or without the dash.
_RRN = re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\s?-?\s?[1-8]\d{6}(?!\d)")
#: 13 to 19 digits, possibly grouped by spaces or dashes; checked with Luhn.
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
#: International or grouped national numbers ("+82 10 1234 5678",
#: "010-1234-5678", "02-123-4567"); a date such as 2026-10-06 is not one.
_PHONE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?\(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{4}\b")
#: A Korean mobile number written without separators (01012345678).
_KR_MOBILE = re.compile(r"(?<!\d)01[016789]\d{7,8}(?!\d)")
_DIGIT_RUN = re.compile(r"\d{8,}")


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def sensitive_kind(text: str) -> str | None:
    """What kind of value in ``text`` memory must not keep, or ``None``.

    The kinds read as a noun phrase: "a password or key", "a phone number".
    """
    if _BEARER.search(text) or _AUTH_HEADER.search(text) or _WELL_KNOWN_VALUES.search(text):
        return "an access token"
    if any(is_sensitive_key(m.group("key")) for m in _SECRET_PAIR.finditer(text)):
        return "a password or key"
    if _OPAQUE_KEY.search(text):
        return "a key or token"
    if _RRN.search(text):
        return "a resident registration number"
    for match in _CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return "a card number"
    if _EMAIL.search(text):
        return "an email address"
    if _PHONE.search(text) or _KR_MOBILE.search(text):
        return "a phone number"
    if _DIGIT_RUN.search(text):
        return "a long number"
    return None


def refusal(kind: str) -> str:
    """The message a refused save answers with; it never quotes the text."""
    return f"not saved: it contains what looks like {kind}; save the preference without the value"


__all__ = ["refusal", "sensitive_kind"]
