"""Model discovery — ask a vendor which models it actually serves.

A preset's ``default_model`` is a *guess*.  For ``local`` it is the placeholder
``local-model``, which no vLLM / Ollama / LM Studio server ever recognises, so
sending it produces ``HTTP 404: The model 'local-model' does not exist``.  This
module asks the server instead:

``openai_compat``
    ``GET {base_url}/models`` -> ``data[].id``
``gemini_native``
    ``GET {base_url}/models`` (the base URL already ends in ``/v1beta``)
    -> ``models[].name`` with the ``models/`` prefix stripped
``anthropic_native``
    ``GET {base_url}/v1/models`` merged with the preset's static list, which is
    also the answer when the endpoint is unavailable.

Results are cached for :data:`CACHE_TTL_SEC` per ``(vendor, base_url)`` so the
wizard, ``/model`` and the lazy auto-pick share one round trip.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from snowpea_core.providers import replay
from snowpea_core.providers.base import ProviderError
from snowpea_core.providers.presets import VendorPreset

log = logging.getLogger("snowpea.providers.models")

#: Model ids that mean "nobody chose one yet" — never send these to a server.
PLACEHOLDER_MODELS: frozenset[str] = frozenset({"", "local-model", "default", "unset"})

#: Discovery must never stall a prompt: five seconds, then give up.
LIST_TIMEOUT = 5.0

#: How long one ``(vendor, base_url)`` answer stays fresh.
CACHE_TTL_SEC = 600.0

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

#: ``(vendor, base_url)`` -> ``(expires_at, model_ids)``.
_CACHE: dict[tuple[str, str], tuple[float, list[str]]] = {}


def is_placeholder(model: str | None) -> bool:
    """True when ``model`` is a stand-in rather than a real model id."""
    return (model or "").strip().lower() in PLACEHOLDER_MODELS


def cache_clear() -> None:
    """Drop every cached listing (tests, and ``/model --refresh``)."""
    _CACHE.clear()


def cache_get(vendor: str, base_url: str) -> list[str] | None:
    entry = _CACHE.get((vendor, base_url))
    if entry is None:
        return None
    expires_at, ids = entry
    if expires_at < time.monotonic():
        _CACHE.pop((vendor, base_url), None)
        return None
    return list(ids)


def cache_put(vendor: str, base_url: str, ids: list[str]) -> None:
    _CACHE[(vendor, base_url)] = (time.monotonic() + CACHE_TTL_SEC, list(ids))


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------


async def list_models(
    preset: VendorPreset,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = LIST_TIMEOUT,
    refresh: bool = False,
) -> list[str]:
    """Model ids ``preset``'s endpoint offers, newest cache first.

    Raises :class:`ProviderError` (``internal``) when the server cannot be
    reached; callers that can carry on without a list catch it.
    """
    resolved_base = (base_url or preset.base_url or _fallback_base(preset)).rstrip("/")
    if not resolved_base:
        raise ProviderError("invalid_params", f"{preset.id}: no base_url configured")
    if not refresh:
        cached = cache_get(preset.id, resolved_base)
        if cached is not None:
            return cached
    if replay.is_replay():
        # Replay fixtures record chat exchanges, not model listings; the static
        # preset list keeps the matrix test's request order intact.
        ids = list(preset.models)
        cache_put(preset.id, resolved_base, ids)
        return ids

    if preset.adapter == "anthropic_native":
        ids = await _list_anthropic(preset, api_key, resolved_base, timeout)
    elif preset.adapter == "gemini_native":
        ids = await _list_gemini(preset, api_key, resolved_base, timeout)
    else:
        ids = await _list_openai(preset, api_key, resolved_base, timeout)
    cache_put(preset.id, resolved_base, ids)
    return ids


def _fallback_base(preset: VendorPreset) -> str:
    return ANTHROPIC_BASE_URL if preset.adapter == "anthropic_native" else ""


async def _get_json(
    url: str, headers: dict[str, str], timeout: float, vendor: str
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await client.get(url)
            if response.status_code >= 400:
                detail = response.text[:200]
                raise ProviderError(
                    "internal", f"{vendor}: HTTP {response.status_code} from {url}: {detail}"
                )
            payload = response.json()
    except ProviderError:
        raise
    except httpx.HTTPError as exc:
        raise ProviderError("internal", f"{vendor}: {type(exc).__name__}: {exc}") from exc
    except ValueError as exc:
        raise ProviderError("internal", f"{vendor}: {url} did not return JSON: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _dedupe(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in ids:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


async def _list_openai(
    preset: VendorPreset, api_key: str | None, base_url: str, timeout: float
) -> list[str]:
    headers = {"accept": "application/json", **preset.extra_headers}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    payload = await _get_json(f"{base_url}/models", headers, timeout, preset.id)
    data = payload.get("data")
    rows = data if isinstance(data, list) else []
    return _dedupe([row.get("id", "") for row in rows if isinstance(row, dict)])


async def _list_gemini(
    preset: VendorPreset, api_key: str | None, base_url: str, timeout: float
) -> list[str]:
    headers = {"accept": "application/json"}
    if api_key:
        headers["x-goog-api-key"] = api_key
    payload = await _get_json(f"{base_url}/models", headers, timeout, preset.id)
    raw = payload.get("models")
    rows = raw if isinstance(raw, list) else []
    names = [str(row.get("name", "")) for row in rows if isinstance(row, dict)]
    return _dedupe([name.removeprefix("models/") for name in names])


async def _list_anthropic(
    preset: VendorPreset, api_key: str | None, base_url: str, timeout: float
) -> list[str]:
    """The static preset list, extended by ``/v1/models`` when it answers."""
    static = list(preset.models)
    headers = {"accept": "application/json", "anthropic-version": ANTHROPIC_VERSION}
    if api_key:
        headers["x-api-key"] = api_key
    try:
        payload = await _get_json(f"{base_url}/v1/models", headers, timeout, preset.id)
    except ProviderError as exc:
        log.debug("anthropic model listing unavailable, using presets: %s", exc)
        return _dedupe(static)
    data = payload.get("data")
    rows = data if isinstance(data, list) else []
    live = [row.get("id", "") for row in rows if isinstance(row, dict)]
    return _dedupe([*live, *static])


__all__ = [
    "ANTHROPIC_VERSION",
    "CACHE_TTL_SEC",
    "LIST_TIMEOUT",
    "PLACEHOLDER_MODELS",
    "cache_clear",
    "cache_get",
    "cache_put",
    "is_placeholder",
    "list_models",
]
