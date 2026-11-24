"""Does the vendor accept this key?  One cheap authenticated call per vendor family.

Before 1.75.0 a provider counted as set up the moment *any* string was stored
as its key: ``provider.list`` said ``configured``, the model picker filled up
from the curated list, and the first chat then failed with a raw ``HTTP 401``.
:func:`verify_credentials` asks the vendor instead, with a short timeout:

* OpenAI-compatible vendors (Qwen/DashScope, DeepSeek, xAI, Kimi, GLM, MiniMax,
  Meta, local servers, ...): ``GET {base}/models`` with the key;
* OpenRouter, whose ``/models`` is public: ``GET {base}/key``;
* Anthropic: ``GET /v1/models?limit=1``; Gemini: ``GET {base}/models?pageSize=1``;
* a ``/models`` that is missing (404/405) or refused without saying the key is
  bad (a 403 for a key without the "list models" scope): a one-token chat
  completion with the vendor's model.

The answer is one of :data:`STATES`.  ``unreachable`` (network, timeout, 5xx)
never blocks a save — the key is simply not known to be bad.  A 402/429
(no credit, rate limit) means the vendor *recognised* the key, so it is ``ok``
with ``reason: "quota"``.

Answers are cached per credential fingerprint (a hash of vendor, endpoint and
key — never the key itself) for :data:`TTL_SEC`; an ``unreachable`` answer for
much less.  ``SNOWPEA_KEY_VERIFY=0`` turns the network check off (the test
suite sets it; tests that exercise it point a vendor at a local fake server).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from snowpea_core.providers.presets import VendorPreset

VerifyState = Literal["ok", "invalid", "unreachable", "unchecked"]
STATES: tuple[str, ...] = ("ok", "invalid", "unreachable", "unchecked")

#: Seconds one verification may take in all (``/models`` plus a chat fallback).
VERIFY_TIMEOUT = 8.0
#: How long an ``ok`` / ``invalid`` answer is reused.
TTL_SEC = 6 * 3600.0
#: How long an ``unreachable`` answer is reused: the network comes back.
UNREACHABLE_TTL_SEC = 300.0
#: The longest ``provider.list`` waits for checks it started before answering.
LIST_WAIT_SEC = 1.5

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

#: Phrases vendors use for "this key is not valid", lower-cased.  DashScope and
#: OpenAI say ``invalid_api_key`` / "Incorrect API key provided", Gemini
#: ``API_KEY_INVALID`` / "API key not valid", Anthropic ``authentication_error``,
#: MiniMax answers HTTP 200 with ``base_resp.status_code`` 1004 "login fail".
_INVALID_MARKERS: tuple[str, ...] = (
    "invalid_api_key",
    "invalid api key",
    "incorrect api key",
    "api key not valid",
    "api_key_invalid",
    "invalid x-api-key",
    "authentication_error",
    "authentication fails",
    "authentication failed",
    "invalid authentication",
    "invalid_authentication",
    "invalid token",
    "invalid_token",
    "api key is invalid",
    "invalid api-key",
    "invalid apikey",
    "unauthorized",
    "login fail",
    "no auth credentials",
    "missing authentication",
)
#: A 403 that is about *where* the caller is, not the key.
_REGION_MARKERS: tuple[str, ...] = ("country", "region", "territory", "location is not supported")
#: A 403 that is about what the key may do, not whether it is a key.
_SCOPE_MARKERS: tuple[str, ...] = (
    "missing scopes",
    "insufficient permissions",
    "permission_error",
    "does not have permission",
    "permission denied",
)
#: MiniMax's ``base_resp.status_code`` values for a bad key.
_MINIMAX_INVALID_CODES = frozenset({1004, 2049})


@dataclass(frozen=True)
class VerifyResult:
    """What the vendor said about the stored credential."""

    state: VerifyState
    message: str = ""
    #: Unix time of the check; ``None`` when nothing was asked.
    checked_at: float | None = None
    #: ``auth`` | ``quota`` | ``network`` | ``timeout`` | ``server`` | ``no_key``
    #: | ``oauth`` | ``disabled`` | ``pending`` | ``""``.
    reason: str = ""

    def wire(self) -> dict[str, Any]:
        """The ``ProviderVerify`` shape (1.75.0)."""
        out: dict[str, Any] = {"state": self.state, "message": self.message}
        out["checkedAt"] = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.checked_at))
            if self.checked_at is not None
            else None
        )
        out["reason"] = self.reason or None
        return out


def enabled() -> bool:
    """``SNOWPEA_KEY_VERIFY=0`` turns the network check off."""
    return os.environ.get("SNOWPEA_KEY_VERIFY", "1").strip().lower() not in {"0", "false", "no"}


def fingerprint(vendor: str, base_url: str | None, api_key: str | None) -> str:
    """A cache key for one credential at one endpoint; the key is only hashed."""
    raw = f"{vendor}\0{(base_url or '').rstrip('/')}\0{api_key or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

#: ``fingerprint -> (expires_at monotonic, result)``.
_CACHE: dict[str, tuple[float, VerifyResult]] = {}
#: Checks running now, so two callers share one round trip.
_INFLIGHT: dict[str, asyncio.Task[VerifyResult]] = {}


def cache_clear() -> None:
    _CACHE.clear()
    _INFLIGHT.clear()


def cancel_inflight() -> None:
    """Cancel every running check (daemon stop)."""
    for task in list(_INFLIGHT.values()):
        task.cancel()
    _INFLIGHT.clear()


def cached(fp: str) -> VerifyResult | None:
    entry = _CACHE.get(fp)
    if entry is None:
        return None
    expires_at, result = entry
    if expires_at < time.monotonic():
        _CACHE.pop(fp, None)
        return None
    return result


def remember(fp: str, result: VerifyResult) -> None:
    """Keep a definite answer; ``unchecked`` is never cached."""
    if result.state == "unchecked":
        return
    ttl = UNREACHABLE_TTL_SEC if result.state == "unreachable" else TTL_SEC
    _CACHE[fp] = (time.monotonic() + ttl, result)


def inflight(fp: str) -> asyncio.Task[VerifyResult] | None:
    task = _INFLIGHT.get(fp)
    return task if task is not None and not task.done() else None


def track(fp: str, task: asyncio.Task[VerifyResult]) -> None:
    _INFLIGHT[fp] = task

    def _done(_task: asyncio.Task[VerifyResult]) -> None:
        if _INFLIGHT.get(fp) is _task:
            _INFLIGHT.pop(fp, None)

    task.add_done_callback(_done)


# ---------------------------------------------------------------------------
# message handling
# ---------------------------------------------------------------------------

_KEYISH = re.compile(r"[A-Za-z0-9_\-\.\*]{10,}")


def redact(text: str, api_key: str | None) -> str:
    """``text`` with the key, and anything that looks like part of it, removed.

    Vendors echo a masked key back ("Incorrect API key provided: sk-ab***yz");
    a token sharing the key's first six or last four characters goes too.
    """
    out = text or ""
    key = (api_key or "").strip()
    if not key:
        return out
    out = out.replace(key, "[redacted]")
    head, tail = key[:6], key[-4:]

    def scrub(match: re.Match[str]) -> str:
        token = match.group(0)
        if len(key) >= 8 and (token.startswith(head) or token.endswith(tail) or key in token):
            return "[redacted]"
        return token

    return _KEYISH.sub(scrub, out)


def _vendor_message(body: str) -> str:
    """The human sentence in a vendor's error body, else the body's start."""
    try:
        payload = json.loads(body)
    except ValueError:
        return body.strip()[:300]
    candidates: list[Any] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            candidates += [error.get("message"), error.get("code"), error.get("type")]
        candidates += [error, payload.get("message"), payload.get("detail"), payload.get("msg")]
        base = payload.get("base_resp")
        if isinstance(base, dict):
            candidates.append(base.get("status_msg"))
    elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return _vendor_message(json.dumps(payload[0]))
    for item in candidates:
        if isinstance(item, str) and item.strip():
            return item.strip()[:300]
    return body.strip()[:300]


#: Markers too loose to read a *chat* failure by: "unauthorized" is also how
#: a vendor words a model or a feature the account may not use.
_LOOSE_MARKERS = frozenset({"unauthorized", "invalid token", "invalid_token"})


def says_invalid_key(text: str, *, strict: bool = False) -> bool:
    """True when ``text`` (a status line or a body) names a rejected key.

    ``strict`` leaves out the loose markers; the key check itself, which
    reads its own probe's answer, may use them.
    """
    low = (text or "").lower()
    return any(
        marker in low
        for marker in _INVALID_MARKERS
        if not (strict and marker in _LOOSE_MARKERS)
    )


def _minimax_invalid(body: str) -> bool:
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    base = payload.get("base_resp") if isinstance(payload, dict) else None
    code = base.get("status_code") if isinstance(base, dict) else None
    return isinstance(code, int) and code in _MINIMAX_INVALID_CODES


#: ``HTTP 401`` in a provider failure, as every adapter words it.
_AUTH_STATUS = re.compile(r"\bHTTP 401\b|AuthenticationError|\b401 Unauthorized\b")


def is_auth_failure(text: str) -> bool:
    """True when a chat or listing failure says the credential was refused.

    A 401 from any adapter, or a body that names a bad key (Gemini answers
    400 ``API_KEY_INVALID``).  A 403 counts only with such words: it is just as
    often a region, a scope or a model the account may not use.
    """
    return bool(_AUTH_STATUS.search(text or "")) or says_invalid_key(text, strict=True)


def rejected_message(label: str) -> str:
    """The one line a person sees when a turn needs a key the vendor refused."""
    return f"API key for {label} was rejected — update it in Settings → Providers"


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Answer:
    status: int
    body: str


async def _request(
    client: httpx.AsyncClient, method: str, url: str, *, json_body: Any = None
) -> _Answer:
    if method == "POST":
        response = await client.post(url, json=json_body)
    else:
        response = await client.get(url)
    return _Answer(response.status_code, response.text[:4000])


def _classify(answer: _Answer, api_key: str | None, *, chat: bool) -> VerifyResult | None:
    """A verdict for one answer, or ``None`` when the next probe should decide."""
    now = time.time()
    body = answer.body
    message = redact(_vendor_message(body), api_key) if body else ""
    status = answer.status
    if 200 <= status < 300:
        if _minimax_invalid(body):
            return VerifyResult("invalid", message or "the key was rejected", now, "auth")
        return VerifyResult("ok", "", now)
    if status == 401:
        return VerifyResult("invalid", message or f"HTTP {status}", now, "auth")
    if says_invalid_key(body) and status in (400, 403, 404):
        return VerifyResult("invalid", message or f"HTTP {status}", now, "auth")
    if status in (402, 429):
        return VerifyResult("ok", message or f"HTTP {status}", now, "quota")
    if status == 403:
        low = body.lower()
        if any(marker in low for marker in _REGION_MARKERS):
            return VerifyResult("unreachable", message or f"HTTP {status}", now, "server")
        if chat or any(marker in low for marker in _SCOPE_MARKERS):
            # The vendor knew the key and declined the request: valid key.
            return VerifyResult("ok", message, now)
        # Neither a bad key nor a known reason: the chat probe decides.
        return None
    if status >= 500:
        return VerifyResult("unreachable", message or f"HTTP {status}", now, "server")
    if chat:
        # 400/404/422 on a one-token chat: authentication already passed (a
        # wrong model id, a parameter this server does not take).
        return VerifyResult("ok", message, now)
    if status in (404, 405):
        return None
    return VerifyResult("unreachable", message or f"HTTP {status}", now, "server")


def _chat_body(model: str) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}


async def verify_credentials(
    preset: VendorPreset,
    *,
    api_key: str | None,
    base_url: str | None,
    model: str | None = None,
    timeout: float = VERIFY_TIMEOUT,
    transport: Any = None,
) -> VerifyResult:
    """Ask the vendor whether it accepts ``api_key``; never raises."""
    if not enabled() and transport is None:
        return VerifyResult("unchecked", "", None, "disabled")
    if not api_key and preset.key_required:
        return VerifyResult("unchecked", "no API key is stored", None, "no_key")
    headers: dict[str, str] = {"accept": "application/json"}
    if preset.adapter == "anthropic_native":
        root = (base_url or ANTHROPIC_BASE_URL).rstrip("/")
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if api_key:
            headers["x-api-key"] = api_key
        probes = [("GET", f"{root}/v1/models?limit=1", None)]
    elif preset.adapter == "gemini_native":
        root = (base_url or preset.base_url or "").rstrip("/")
        if api_key:
            headers["x-goog-api-key"] = api_key
        probes = [("GET", f"{root}/models?pageSize=1", None)]
    else:
        root = (base_url or preset.base_url or "").rstrip("/")
        headers.update(preset.extra_headers)
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        first = f"{root}/key" if preset.id == "openrouter" else f"{root}/models"
        probes = [("GET", first, None)]
        from snowpea_core.providers.models import is_placeholder

        chat_model = (model or preset.default_model or "").strip()
        if chat_model and not is_placeholder(chat_model):
            probes.append(("POST", f"{root}/chat/completions", _chat_body(chat_model)))
    if not root:
        return VerifyResult("unchecked", "no endpoint is configured", None, "no_key")

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=timeout, headers=headers, transport=transport, follow_redirects=False
        ) as client:
            for method, url, body in probes:
                left = timeout - (time.monotonic() - started)
                if left <= 0:
                    raise httpx.TimeoutException("verification timed out")
                answer = await asyncio.wait_for(
                    _request(client, method, url, json_body=body), timeout=left
                )
                verdict = _classify(answer, api_key, chat=method == "POST")
                if verdict is not None:
                    return verdict
    except (TimeoutError, httpx.TimeoutException):
        return VerifyResult(
            "unreachable", "the vendor did not answer in time", time.time(), "timeout"
        )
    except httpx.HTTPError as exc:
        text = redact(f"{type(exc).__name__}: {exc}", api_key)
        return VerifyResult("unreachable", text[:300], time.time(), "network")
    return VerifyResult(
        "unreachable", "the vendor gave no answer that says whether the key works",
        time.time(), "server",
    )


__all__ = [
    "LIST_WAIT_SEC",
    "STATES",
    "TTL_SEC",
    "VERIFY_TIMEOUT",
    "VerifyResult",
    "VerifyState",
    "cache_clear",
    "cached",
    "cancel_inflight",
    "enabled",
    "fingerprint",
    "inflight",
    "is_auth_failure",
    "redact",
    "rejected_message",
    "remember",
    "says_invalid_key",
    "track",
    "verify_credentials",
]
