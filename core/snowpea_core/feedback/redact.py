"""Masking for 개선하기 reports (protocol 1.59.0, capability ``improvementFeedback``).

A port of snowpea-browser's ``host/src/parent/feedback.ts`` (``redactString``,
``redactValue``, ``redactMessages``): the same rules, so a report from core and
one from the browser leave with the same things masked.  Secret-named keys,
key=value secrets, provider keys (OpenAI/Anthropic ``sk-``, AWS, GitHub,
Google, Slack, Stripe), JWTs, PEM private keys, bearer tokens, header lines and
header tuples, cookie jars and cookie headers, credential query parameters and
card numbers are masked; every string in ``secrets`` (the daemon token, the
provider keys core holds) is masked wherever it appears; a result marked
``meta.sensitive`` or of a vault call is withheld whole; one string is cut past
:data:`FIELD_CAP` and a ``data:`` URL past :data:`DATA_URL_CAP` becomes a marker.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("snowpea.feedback")

REDACTED = "[REDACTED]"
#: One string field (a tool result, page text) is cut past this.
FIELD_CAP = 200 * 1024
#: data: URLs longer than this become a marker.
DATA_URL_CAP = 2048
#: Deeper JSON in args or results goes as a JSON string (the server takes 256 levels).
MAX_DEPTH = 40
#: A secret shorter than this is not searched for verbatim (it would mask ordinary words).
MIN_SECRET = 6

_A = re.ASCII
#: Keys whose string value is always a secret (headers and credential fields).
SECRET_KEYS = re.compile(
    r"(authorization|proxy-authorization|cookie|set-cookie|x-api-key|api[-_]?key|apikey|"
    r"x-auth-token|password|passwd|passphrase|secret|client[-_]?secret|token|access[-_]?token|"
    r"refresh[-_]?token|id[-_]?token|session[-_]?token|private[-_]?key)",
    re.I | _A,
)
#: Keys whose whole value is a secret whatever its type (a cookie jar, a header list).
COOKIE_KEYS = re.compile(r"(cookie|cookies|set-cookie|set-cookie2)", re.I | _A)
#: A cookie object's own shape ({name, value, domain|path|…}): its value goes.
COOKIE_FIELDS = ("domain", "path", "httpOnly", "expires", "sameSite", "secure", "maxAge", "max-age")
#: Code that reads a secret from somewhere, not a secret.
SECRET_REF = re.compile(
    r"^(?:\$|os\.environ|os\.getenv|process\.env|import\.meta\.env|System\.getenv|getenv|env\.|"
    r"ENV\[|settings\.|config\.)",
    re.I | _A,
)
#: A vault call in a REPL script (vault.fill(…)) or a vault tool.
VAULT = re.compile(r"\bvault\s*\.\s*\w+\s*\(|^vault[._]", re.I | _A)


def _secret_value(value: str, quoted: bool) -> bool:
    """A key=value value worth masking: quoted 6+ chars, or a bare run with a digit or a
    symbol in it (or long), never a reference to where the secret lives."""
    if SECRET_REF.search(value) or "${" in value:
        return False
    if quoted:
        return True
    return bool(re.search(r"[\d!@#%^*+/=~-]", value, _A)) or len(value) >= 24


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_COOKIE_PAIR = r"[A-Za-z0-9_.!#$%&'*+^`|~-]{1,128}=[^;\s\"(){}<>,]*"
_COOKIE_HEADER = re.compile(rf"(?<![\w.=-]){_COOKIE_PAIR}(?:;[ \t]?{_COOKIE_PAIR})+", _A)


def _cookie_header(m: re.Match[str]) -> str:
    text = m.group(0)
    if any(
        re.search(r"=[A-Za-z0-9%._~+/=:-]{8,}", pair.strip(), _A) or "…" in pair
        for pair in text.split(";")
    ):
        return f"{REDACTED} (cookies)"
    return text


def _key_value(m: re.Match[str]) -> str:
    pre, quote, quoted, bare = m.group(1), m.group(2), m.group(3), m.group(4)
    value = quoted if quoted is not None else (bare or "")
    if _secret_value(value, quote is not None):
        q = quote or ""
        return f"{pre}{q}{REDACTED}{q}"
    return m.group(0)


def _card(m: re.Match[str]) -> str:
    text = m.group(0)
    return f"{REDACTED} (card)" if _luhn(re.sub(r"[ -]", "", text)) else text


def _data_url(m: re.Match[str]) -> str:
    text = m.group(0)
    if len(text) <= DATA_URL_CAP:
        return text
    return f"data:{m.group(1) or ''};[{len(text)} chars omitted]"


_Rep = str | Callable[[re.Match[str]], str]

#: [pattern, replacement], applied in order to every string that leaves.
PATTERNS: tuple[tuple[re.Pattern[str], _Rep], ...] = (
    # Header lines in logs / fetch dumps.
    (
        re.compile(
            r"^([ \t]*(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)"
            r"[ \t]*:)[^\r\n]*",
            re.I | re.M | _A,
        ),
        rf"\g<1> {REDACTED}",
    ),
    (re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}", _A), rf"\g<1> {REDACTED}"),
    # JWTs.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}", _A), REDACTED),
    # OpenAI / Anthropic / most "sk-" keys.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}", _A), REDACTED),
    # AWS access key ids and secrets.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", _A), REDACTED),
    (
        re.compile(
            r"(aws_secret_access_key|aws_session_token)(\s*[:=]\s*[\"']?)[A-Za-z0-9/+=]{16,}",
            re.I | _A,
        ),
        rf"\g<1>\g<2>{REDACTED}",
    ),
    # GitHub, Google, Slack, Stripe.
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b", _A), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b", _A), REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}", _A), REDACTED),
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b", _A), REDACTED),
    # PEM private keys.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", _A
        ),
        REDACTED,
    ),
    # key=value / "key": "value" credentials: only the value, only when it looks like a secret.
    (
        re.compile(
            r"((?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
            r"client[_-]?secret|password|passwd|secret|token)[\"']?\s*[:=]\s*)"
            r"(?:([\"'])([^\"'\r\n]{6,200})\2|([A-Za-z0-9._~+/=!@#%^*-]{6,})(?=$|[\s\"'&,;)}\]]))",
            re.I | re.M | _A,
        ),
        _key_value,
    ),
    # Cookie, Set-Cookie, Authorization… inside JSON text.
    (
        re.compile(
            r"(\"(?:cookie|set-cookie|set-cookie2|authorization|proxy-authorization|x-api-key)\"\s*[:,]\s*)"
            r"\"(?:[^\"\\]|\\.)*\"",
            re.I | _A,
        ),
        rf'\g<1>"{REDACTED}"',
    ),
    (
        re.compile(r"(\"cookies\"\s*:\s*)(?:\[[^\]]*\]|\"(?:[^\"\\]|\\.)*\")", re.I | _A),
        rf'\g<1>"{REDACTED}"',
    ),
    # A cookie object in JSON text without its key.
    (
        re.compile(
            r"(\"name\"\s*:\s*\"(?:[^\"\\]|\\.)*\"\s*,\s*\"value\"\s*:\s*)\"(?:[^\"\\]|\\.)*\""
            r"(?=[^{}]*\"(?:domain|path|httpOnly|expires|sameSite)\")",
            _A,
        ),
        rf'\g<1>"{REDACTED}"',
    ),
    # A cookie header string (document.cookie, a Cookie value outside any key).
    (_COOKIE_HEADER, _cookie_header),
    # Query values that grant access.
    (
        re.compile(
            r"([?&](?:code|sig|signature|x-amz-[a-z0-9-]+|key|api_key|apikey|auth|session|sessionid|"
            r"session_id|sid|token|access_token|id_token|refresh_token|client_secret|password)=)"
            r"[^&#\s\"'<>]+",
            re.I | _A,
        ),
        rf"\g<1>{REDACTED}",
    ),
    # Card numbers: Luhn-valid 13–19 digits, spaces or dashes allowed between them.
    (re.compile(r"(?<![\d.-])\d(?:[ -]?\d){12,18}(?!\d)", _A), _card),
    # data: URLs past the cap.
    (
        re.compile(
            r"data:([\w.+-]+/[\w.+-]+)?((?:;[\w-]+=[^;,]*)*)(;base64)?,[A-Za-z0-9+/=%._~-]+", _A
        ),
        _data_url,
    ),
)


@dataclass
class RedactStats:
    #: Strings something was masked in.
    strings: int = 0
    #: Strings cut past FIELD_CAP.
    truncated: int = 0
    #: Tool results withheld whole (sensitive or vault).
    withheld: int = 0


def _cap(text: str, stats: RedactStats) -> str:
    if len(text) <= FIELD_CAP:
        return text
    stats.truncated += 1
    return f"{text[:FIELD_CAP]}\n…[truncated: {len(text) - FIELD_CAP} more characters]"


def redact_string(text: str, secrets: Iterable[str] = (), stats: RedactStats | None = None) -> str:
    """``text`` with every secret masked and cut at :data:`FIELD_CAP`."""
    stats = stats if stats is not None else RedactStats()
    out = text
    for secret in secrets:
        if secret and len(secret) >= MIN_SECRET and secret in out:
            out = out.replace(secret, REDACTED)
    for pattern, rep in PATTERNS:
        out = pattern.sub(rep, out)
    if out != text:
        stats.strings += 1
    return _cap(out, stats)


def _secret_name(value: Any) -> bool:
    return isinstance(value, str) and bool(
        SECRET_KEYS.fullmatch(value) or COOKIE_KEYS.fullmatch(value)
    )


def redact_value(
    value: Any, secrets: tuple[str, ...], stats: RedactStats, key: str = "", depth: int = 0
) -> Any:
    """Any JSON value with secrets masked; ``None`` fields of objects are dropped."""
    if key and COOKIE_KEYS.fullmatch(key) and value != "" and value is not None:
        return REDACTED  # a cookie jar, whatever its shape
    if isinstance(value, str):
        if key and SECRET_KEYS.fullmatch(key) and value:
            return REDACTED
        return redact_string(value, secrets, stats)
    if isinstance(value, dict | list) and depth >= MAX_DEPTH:
        return redact_string(json.dumps(value, ensure_ascii=False), secrets, stats)
    if isinstance(value, list):
        # Header tuples (["set-cookie", "sid=…"], raw [name, value, …]):
        # the value after a secret name.
        return [
            REDACTED
            if i > 0 and _secret_name(value[i - 1]) and isinstance(item, str)
            else redact_value(item, secrets, stats, key, depth + 1)
            for i, item in enumerate(value)
        ]
    if isinstance(value, dict):
        name = value.get("name")
        hide = isinstance(value.get("value"), str) and (
            _secret_name(name) or (isinstance(name, str) and any(f in value for f in COOKIE_FIELDS))
        )
        out: dict[str, Any] = {}
        for k, item in value.items():
            if item is None:
                continue  # the server takes no nulls for optional fields
            out[str(k)] = (
                REDACTED
                if hide and k == "value"
                else redact_value(item, secrets, stats, str(k), depth + 1)
            )
        return out
    return value


WITHHELD = {"withheld": "sensitive result (vault, password or a protected field): not sent"}


def redact_messages(
    messages: list[dict[str, Any]], secrets: Iterable[str] = ()
) -> tuple[list[dict[str, Any]], RedactStats]:
    """Withhold what must never leave, then mask secrets in every string."""
    secret_list = tuple(s for s in secrets if isinstance(s, str) and len(s) >= MIN_SECRET)
    stats = RedactStats()
    out: list[dict[str, Any]] = []
    for message in messages:
        tool = message.get("tool")
        if isinstance(tool, dict) and "result" in tool:
            result = tool.get("result")
            meta = result.get("meta") if isinstance(result, dict) else None
            sensitive = isinstance(meta, dict) and meta.get("sensitive") is True
            args = tool.get("args")
            code = (
                args.get("code")
                if isinstance(args, dict) and isinstance(args.get("code"), str)
                else ""
            )
            vault = bool(VAULT.search(str(tool.get("name") or ""))) or bool(VAULT.search(code))
            if sensitive or vault:
                if vault and not sensitive:
                    log.warning(
                        "feedback: a vault call's result was not marked sensitive; withheld"
                    )
                stats.withheld += 1
                message = {**message, "tool": {**tool, "result": dict(WITHHELD)}}
        out.append(redact_value(message, secret_list, stats))
    return out, stats


__all__ = [
    "DATA_URL_CAP",
    "FIELD_CAP",
    "REDACTED",
    "RedactStats",
    "redact_messages",
    "redact_string",
    "redact_value",
]
