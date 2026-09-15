"""Model discovery — one answer to "what can this account actually run?".

:func:`resolve_models` is that answer, and every surface uses it: the setup
wizard, ``provider.models``, ``snowpea provider models``, ``/model`` and the
TUI picker.  It tries four rungs in order and reports which one replied, so a
fallback can never pass for the vendor's own catalog:

1. **live** — the endpoint that *this account* reaches.  ``/v1/models`` for the
   OpenAI-compatible vendors, ``/v1beta/models`` for a Gemini API key,
   Anthropic's ``/v1/models``, Ollama's ``/api/tags`` for a local server with no
   OpenAI-compatible listing, and the Codex backend's per-account catalog for a
   ChatGPT subscription.  Google Code Assist is the one backend that publishes
   nothing (confirmed against google-gemini/gemini-cli, whose
   ``packages/core/src/code_assist`` holds static constants).
2. **settings** — ``providers.<vendor>.models``, or ``.oauth_models`` for an
   OAuth account, for a server whose listing lies or an account with early
   access to something unadvertised.
3. **cache** — the last good listing, under
   ``<SNOWPEA_HOME>/cache/models-<vendor>-<auth>.json``.
4. **curated** — the preset's hand-kept list merged with the public models.dev
   catalog, so a model released after this build still reaches the picker.

A preset's ``default_model`` is only a guess.  For ``local`` it is the
placeholder ``local-model``, which no vLLM / Ollama / LM Studio server
recognises, so sending it produces ``HTTP 404: The model 'local-model' does not
exist`` — which is why discovery exists at all.

Live answers are also cached in-process for :data:`CACHE_TTL_SEC` per
``(vendor, base_url)`` so the wizard, ``/model`` and the lazy auto-pick share
one round trip.  The structure is ported from hermes-agent (MIT):
``hermes_cli/codex_models.py`` and ``hermes_cli/models.py``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from snowpea_core.providers import replay
from snowpea_core.providers.base import ProviderError
from snowpea_core.providers.presets import PRESETS_BY_VENDOR, VendorPreset

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


#: Auth methods that reach a vendor's OAuth backend rather than its public API.
OAUTH_METHODS: frozenset[str] = frozenset({"chatgpt", "google_oauth", "google_adc"})


def oauth_models(vendor: str, auth_method: str | None) -> list[str] | None:
    """The static list an OAuth account must use, or ``None`` to go and ask.

    A ChatGPT or Google sign-in does not reach the vendor's ordinary API, and
    neither OAuth backend publishes a ``/models`` endpoint: Codex has none at
    all, and Code Assist answers for the *project*, not the account.  Asking
    anyway costs a guaranteed 401 right after a successful login (report §6.7
    A-P2-1), so the supported set is declared instead.
    """
    method = (auth_method or "").strip()
    if vendor == "openai" and method == "chatgpt":
        from snowpea_core.providers.codex_transport import CODEX_MODELS

        return list(CODEX_MODELS)
    if vendor == "gemini" and method == "google_oauth":
        from snowpea_core.providers.gemini_codeassist_transport import CODE_ASSIST_MODELS

        return list(CODE_ASSIST_MODELS)
    return None


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
    transport: Any = None,
    strict: bool = False,
) -> list[str]:
    """Model ids ``preset``'s endpoint offers, newest cache first.

    Raises :class:`ProviderError` (``internal``) when the server cannot be
    reached; callers that can carry on without a list catch it.

    ``strict`` stops the Anthropic path from quietly substituting the preset's
    static list for an endpoint that did not answer.  :func:`resolve_models`
    passes it so that a fallback is reported as a fallback — the static list is
    its own (curated) rung, and mislabelling it ``live`` is how a user ends up
    trusting a catalog nobody served.
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
        ids = await _list_anthropic(preset, api_key, resolved_base, timeout, transport, strict)
    elif preset.adapter == "gemini_native":
        ids = await _list_gemini(preset, api_key, resolved_base, timeout, transport)
    else:
        ids = await _list_openai(preset, api_key, resolved_base, timeout, transport)
    cache_put(preset.id, resolved_base, ids)
    return ids


def _fallback_base(preset: VendorPreset) -> str:
    return ANTHROPIC_BASE_URL if preset.adapter == "anthropic_native" else ""


async def _get_json(
    url: str,
    headers: dict[str, str],
    timeout: float,
    vendor: str,
    *,
    transport: Any = None,
) -> dict[str, Any]:
    """One JSON ``GET``.  ``transport`` is how the tests stand in for a vendor."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout, headers=headers, transport=transport
        ) as client:
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
    preset: VendorPreset, api_key: str | None, base_url: str, timeout: float, transport: Any = None
) -> list[str]:
    headers = {"accept": "application/json", **preset.extra_headers}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    payload = await _get_json(
        f"{base_url}/models", headers, timeout, preset.id, transport=transport
    )
    data = payload.get("data")
    rows = data if isinstance(data, list) else []
    return _dedupe([row.get("id", "") for row in rows if isinstance(row, dict)])


async def _list_gemini(
    preset: VendorPreset, api_key: str | None, base_url: str, timeout: float, transport: Any = None
) -> list[str]:
    headers = {"accept": "application/json"}
    if api_key:
        headers["x-goog-api-key"] = api_key
    payload = await _get_json(
        f"{base_url}/models", headers, timeout, preset.id, transport=transport
    )
    raw = payload.get("models")
    rows = raw if isinstance(raw, list) else []
    names = [str(row.get("name", "")) for row in rows if isinstance(row, dict)]
    return _dedupe([name.removeprefix("models/") for name in names])


async def _list_anthropic(
    preset: VendorPreset,
    api_key: str | None,
    base_url: str,
    timeout: float,
    transport: Any = None,
    strict: bool = False,
) -> list[str]:
    """The static preset list, extended by ``/v1/models`` when it answers."""
    static = list(preset.models)
    headers = {"accept": "application/json", "anthropic-version": ANTHROPIC_VERSION}
    if api_key:
        headers["x-api-key"] = api_key
    try:
        payload = await _get_json(
            f"{base_url}/v1/models", headers, timeout, preset.id, transport=transport
        )
    except ProviderError as exc:
        if strict:
            raise
        log.debug("anthropic model listing unavailable, using presets: %s", exc)
        return _dedupe(static)
    data = payload.get("data")
    rows = data if isinstance(data, list) else []
    live = [row.get("id", "") for row in rows if isinstance(row, dict)]
    return _dedupe([*live, *static])


# ---------------------------------------------------------------------------
# the resolution chain (one answer for every vendor and every auth method)
# ---------------------------------------------------------------------------

#: Which rung of :func:`resolve_models` produced a listing.
SOURCE_LIVE = "live"
SOURCE_SETTINGS = "settings"
SOURCE_CACHE = "cache"
SOURCE_CURATED = "curated"

#: The per-account Codex catalog.  A ChatGPT subscription does not serve
#: ``api.openai.com/v1/models``, but the Codex backend the subscription *does*
#: reach publishes its own list — the same one the Codex CLI shows.  Ported
#: from hermes-agent (MIT), ``hermes_cli/codex_models.py``.
CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
CODEX_CLIENT_VERSION = "1.0.0"

#: The public model catalog every vendor's curated list is enriched with.
MODELS_DEV_URL = "https://models.dev/api.json"
#: models.dev changes on release timescales; four hours is Hermes' figure too.
MODELS_DEV_TTL_SEC = 4 * 3600.0

#: ``vendor -> models.dev provider ids to try, in order``.  A tuple because the
#: public catalog has renamed providers more than once (zhipuai → zai) and a
#: miss must degrade to "no extras", never to a wrong vendor's models.
MODELS_DEV_IDS: dict[str, tuple[str, ...]] = {
    "anthropic": ("anthropic",),
    "openai": ("openai",),
    "openrouter": ("openrouter",),
    "gemini": ("google",),
    "xai": ("xai",),
    "glm": ("zai", "zhipuai", "z-ai"),
    "minimax": ("minimax",),
    "kimi": ("moonshotai", "kimi-for-coding", "moonshot"),
    "deepseek": ("deepseek",),
    "qwen": ("alibaba", "qwen"),
    # A self-hosted server serves whatever was loaded into it; no public
    # catalog can describe it.
    "local": (),
}

#: Ollama does not implement ``/v1/models``; this is the native equivalent.
OLLAMA_TAGS_PATH = "/api/tags"


@dataclass(frozen=True)
class ModelListing:
    """A model catalog plus where it came from.

    Every surface that shows models — the setup wizard, ``provider.models``,
    ``snowpea provider models`` and the TUI ``/model`` picker — renders this,
    so a curated fallback can never be mistaken for the vendor's own answer.
    """

    vendor: str
    auth: str
    models: list[str]
    source: str
    detail: str
    error: str | None = None

    @property
    def live(self) -> bool:
        return self.source == SOURCE_LIVE


def _auth_key(auth_method: str | None) -> str:
    """A filesystem-safe name for one authentication method."""
    text = (auth_method or "").strip().lower() or "api_key"
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in text)


def cache_path(home: Path | str, vendor: str, auth_method: str | None) -> Path:
    """``<SNOWPEA_HOME>/cache/models-<vendor>-<auth>.json``.

    Keyed by the auth method as well as the vendor because the two catalogs
    genuinely differ: an API key sees ``api.openai.com``'s models, a ChatGPT
    session sees the Codex backend's.
    """
    from snowpea_core.config.paths import Paths

    return Paths(home=Path(home)).cache_dir / f"models-{vendor}-{_auth_key(auth_method)}.json"


def read_cache_file(path: Path, *, max_age_sec: float | None = None) -> list[str]:
    """The remembered listing, or ``[]`` when there is none worth having.

    Age is advisory: an hour-old catalog beats an empty picker, so the cache is
    only consulted once the live call has already failed.  ``max_age_sec``
    exists for the models.dev cache, which *is* refreshed on a timer.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    if max_age_sec is not None:
        saved_at = payload.get("saved_at")
        if not isinstance(saved_at, int | float) or time.time() - float(saved_at) > max_age_sec:
            return []
    raw = payload.get("models")
    return _dedupe([str(item) for item in raw]) if isinstance(raw, list) else []


def write_cache_file(path: Path, ids: Sequence[str]) -> None:
    """Remember a *non-empty* listing; a failed write is never an error.

    Only good answers are stored: pinning an empty list would turn one flaky
    minute into a permanently empty picker.
    """
    if not ids:
        return
    payload = {"saved_at": time.time(), "models": list(ids)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - read-only home
        log.debug("could not cache the model listing at %s: %s", path, exc)


def override_models(config: dict[str, Any] | None, *, oauth: bool) -> list[str]:
    """``providers.<vendor>.models`` / ``.oauth_models`` from settings.json.

    Both are lists of model ids.  ``oauth_models`` is consulted only for an
    OAuth account, so one provider block can pin the Codex catalog without
    also pinning what an API key would see.  A value of the wrong shape is
    ignored with a warning rather than crashing a picker.
    """
    if not isinstance(config, dict):
        return []
    for field in ("oauth_models", "models") if oauth else ("models",):
        raw = config.get(field)
        if raw is None:
            continue
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            log.warning("providers.%s must be a list of model ids; ignoring it", field)
            continue
        ids = _dedupe(raw)
        if ids:
            return ids
    return []


def rejected_for_oauth(vendor: str, auth_method: str | None, model: str) -> bool:
    """True when ``model`` belongs to the vendor's API-key endpoint, not this backend.

    The Codex and Code Assist backends answer ``HTTP 400`` for an API-key model
    id (``gpt-4.1``), so those are dropped and the backend's own default used.
    Everything else is kept — including ids this build has never heard of.  The
    live per-account catalog is precisely where new and preview models appear
    first, and filtering the picker down to what this release happens to know
    is what left a subscriber staring at six ids their account had long since
    outgrown.
    """
    if not model or is_placeholder(model):
        return True
    if model in set(oauth_models(vendor, auth_method) or ()):
        return False
    preset = PRESETS_BY_VENDOR.get(vendor)
    return model in set(preset.models if preset else ())


def offline_models(
    vendor: str,
    auth_method: str | None,
    config: dict[str, Any] | None = None,
    *,
    home: Path | str | None = None,
) -> list[str]:
    """The best catalog available with no I/O: override, then cache, then curated.

    :func:`resolve_models` is the real answer, but ``provider.list`` is a
    synchronous surface that must not make eleven HTTP calls to render a table.
    It gets the same three lower rungs, so a list discovered live once is still
    the one it shows.
    """
    pinned = override_models(config, oauth=(auth_method or "") in OAUTH_METHODS)
    if pinned:
        return pinned
    if home is not None:
        remembered = read_cache_file(cache_path(home, vendor, auth_method))
        if remembered:
            return remembered
    return curated_models(vendor, auth_method)


def curated_models(vendor: str, auth_method: str | None) -> list[str]:
    """The hand-kept list for ``vendor``: the last word when nothing answers.

    OAuth accounts have their own: a ChatGPT session reaches the Codex backend
    and a Google sign-in reaches Code Assist, neither of which serves the
    vendor's public model set.
    """
    static = oauth_models(vendor, auth_method)
    if static is not None:
        return list(static)
    preset = PRESETS_BY_VENDOR.get(vendor)
    return [name for name in (preset.models if preset else ()) if not is_placeholder(name)]


# ---------------------------------------------------------------------------
# models.dev
# ---------------------------------------------------------------------------


def models_dev_enabled() -> bool:
    """``SNOWPEA_MODELS_DEV=0`` turns the public catalog off (tests, airgaps)."""
    return os.environ.get("SNOWPEA_MODELS_DEV", "1").strip() not in {"0", "false", "no"}


async def models_dev_catalog(
    vendor: str,
    *,
    home: Path | str | None = None,
    timeout: float = LIST_TIMEOUT,
    transport: Any = None,
) -> list[str]:
    """Tool-calling model ids models.dev lists for ``vendor``, or ``[]``.

    The catalog is shared by every vendor, so it is fetched once and cached in
    ``<SNOWPEA_HOME>/cache/models-dev.json`` for :data:`MODELS_DEV_TTL_SEC`.
    Anything that goes wrong — offline, a 502, a reshaped payload — returns
    ``[]``: this rung only ever *adds* models to a curated list.
    """
    candidates = MODELS_DEV_IDS.get(vendor, ())
    if not candidates or (transport is None and not models_dev_enabled()):
        return []
    catalog = await _models_dev_payload(home=home, timeout=timeout, transport=transport)
    for provider_id in candidates:
        entry = catalog.get(provider_id)
        models = entry.get("models") if isinstance(entry, dict) else None
        if not isinstance(models, dict):
            continue
        ids = [
            model_id
            for model_id, meta in models.items()
            if isinstance(meta, dict) and meta.get("tool_call", False)
        ]
        if ids:
            return _dedupe(ids)
    return []


async def _models_dev_payload(
    *, home: Path | str | None, timeout: float, transport: Any
) -> dict[str, Any]:
    """The whole models.dev document, from the disk cache when it is fresh."""
    from snowpea_core.config.paths import Paths

    path = Paths(home=Path(home)).cache_dir / "models-dev.json" if home is not None else None
    if path is not None:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict):
            saved_at = cached.get("saved_at")
            payload = cached.get("catalog")
            fresh = (
                isinstance(saved_at, int | float)
                and time.time() - float(saved_at) <= MODELS_DEV_TTL_SEC
            )
            if fresh and isinstance(payload, dict):
                return payload
    try:
        catalog = await _get_json(
            MODELS_DEV_URL,
            {"accept": "application/json"},
            timeout,
            "models.dev",
            transport=transport,
        )
    except ProviderError as exc:
        log.debug("models.dev unavailable: %s", exc)
        return {}
    if path is not None and catalog:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"saved_at": time.time(), "catalog": catalog}), encoding="utf-8"
            )
        except OSError:  # pragma: no cover - read-only home
            pass
    return catalog


# ---------------------------------------------------------------------------
# the live rungs
# ---------------------------------------------------------------------------


def _codex_headers(access_token: str) -> dict[str, str]:
    """``Authorization`` plus the account id the per-account catalog needs.

    Without ``ChatGPT-Account-Id`` the backend answers ``HTTP 200
    {"models": []}`` — an empty picker that looks like "this account has no
    models" rather than a missing header (hermes ``codex_models.py``).
    """
    from snowpea_core.providers import openai_oauth
    from snowpea_core.providers.codex_transport import USER_AGENT

    headers = {
        "authorization": f"Bearer {access_token}",
        "accept": "application/json",
        "user-agent": USER_AGENT,
    }
    account_id = openai_oauth.account_id_of(access_token)
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return headers


def codex_slugs(entries: Any) -> list[str]:
    """Visible slugs from a Codex ``models`` payload, lowest ``priority`` first.

    ``supported_in_api`` is deliberately *not* filtered on: it describes the
    public API, and the Codex backend accepts slugs marked false there (the
    research-preview models).  Entries are sorted by priority so the picker's
    first row is the one the account is meant to use.
    """
    ranked: list[tuple[int, int, str]] = []
    for index, item in enumerate(entries if isinstance(entries, list) else []):
        if not isinstance(item, dict):
            continue
        slug = item.get("slug") or item.get("id")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = item.get("visibility")
        if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, int | float) else 10_000
        ranked.append((rank, index, slug.strip()))
    ranked.sort()
    return _dedupe([slug for _rank, _index, slug in ranked])


async def codex_catalog(
    credentials: dict[str, Any] | None,
    *,
    timeout: float = LIST_TIMEOUT,
    transport: Any = None,
    on_credentials: Callable[[dict[str, Any]], None] | None = None,
) -> list[str]:
    """What the Codex backend lists for this ChatGPT account, or ``[]``.

    An access token that is expired (or about to be) is refreshed first, and
    the refreshed credentials are handed to ``on_credentials`` so they reach
    ``settings.json`` — otherwise the very next turn would pay for the same
    refresh again.
    """
    from snowpea_core.providers import openai_oauth

    resolved = openai_oauth.normalize_stored_credentials(credentials or {})
    if openai_oauth.is_expired(resolved) and resolved.get("refresh_token"):
        try:
            resolved = await openai_oauth.refresh_credentials(resolved)
        except Exception as exc:  # noqa: BLE001 - a listing never fails a login
            log.debug("could not refresh the ChatGPT session before listing: %s", exc)
        else:
            if on_credentials is not None:
                on_credentials(dict(resolved))
    access_token = str(resolved.get("access_token") or "")
    if not access_token:
        return []
    url = f"{CODEX_MODELS_URL}?client_version={CODEX_CLIENT_VERSION}"
    try:
        payload = await _get_json(
            url, _codex_headers(access_token), timeout, "openai", transport=transport
        )
    except ProviderError as exc:
        log.debug("codex model listing unavailable: %s", exc)
        return []
    return codex_slugs(payload.get("models"))


async def _list_ollama(base_url: str, timeout: float, transport: Any) -> list[str]:
    """``GET /api/tags`` — what Ollama serves instead of ``/v1/models``."""
    root = base_url.removesuffix("/v1").rstrip("/")
    payload = await _get_json(
        f"{root}{OLLAMA_TAGS_PATH}",
        {"accept": "application/json"},
        timeout,
        "local",
        transport=transport,
    )
    raw = payload.get("models")
    rows = raw if isinstance(raw, list) else []
    return _dedupe([str(row.get("name", "")) for row in rows if isinstance(row, dict)])


async def _live_catalog(
    vendor: str,
    *,
    preset: VendorPreset,
    auth_method: str | None,
    api_key: str | None,
    base_url: str | None,
    credentials: dict[str, Any] | None,
    timeout: float,
    transport: Any,
    refresh: bool,
    on_credentials: Callable[[dict[str, Any]], None] | None,
) -> tuple[list[str], str | None]:
    """One live listing attempt: ``(ids, error)``, never raising.

    Which endpoint that is depends on the *account*, not only the vendor: a
    ChatGPT subscription is served by the Codex backend, and a Google sign-in
    by Code Assist, which publishes no catalog at all (checked against
    google-gemini/gemini-cli ``packages/core/src/code_assist``: it ships static
    constants, so there is nothing to ask).
    """
    method = (auth_method or "").strip()
    if vendor == "openai" and method == "chatgpt":
        # A Codex fetch that fails is not an error the user must act on: the
        # curated list is the same set the backend accepts, so the fallback is
        # announced through ``detail`` and nothing is reported as broken.
        return await codex_catalog(
            credentials, timeout=timeout, transport=transport, on_credentials=on_credentials
        ), None
    if vendor == "gemini" and method in {"google_oauth", "google_adc"}:
        return [], None
    if not api_key and preset.key_required:
        return [], "no credential to list with"
    try:
        ids = await list_models(
            preset,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            refresh=refresh,
            transport=transport,
            strict=True,
        )
        error: str | None = None
    except ProviderError as exc:
        ids, error = [], str(exc)
    if ids or not preset.local_style:
        return ids, error
    # Ollama answers ``/api/tags``, not ``/v1/models``; a local server that
    # refused the OpenAI-compatible route is usually that one.  This applies to
    # every local-style vendor, named ones included — ``preset.local_style`` is
    # what says so, because the vendor id is now whatever the user called it.
    resolved_base = (base_url or preset.base_url or "").rstrip("/")
    if not resolved_base:
        return [], error
    try:
        return await _list_ollama(resolved_base, timeout, transport), None
    except ProviderError as exc:
        return [], error or str(exc)


def has_live_listing(vendor: str, auth_method: str | None) -> bool:
    """False for a backend that publishes no catalog at all.

    Only Google Code Assist is in that position: gemini-cli reaches it with
    static constants (``packages/core/src/code_assist`` has no listing call),
    so "curated" is the *correct* answer there rather than a degraded one.
    """
    return not (vendor == "gemini" and (auth_method or "") in {"google_oauth", "google_adc"})


def _describe(
    vendor: str, source: str, *, auth_method: str | None, preset: VendorPreset | None = None
) -> str:
    """The one line a picker prints above the list."""
    if preset is not None:
        label = preset.label
    else:
        label = PRESETS_BY_VENDOR[vendor].label if vendor in PRESETS_BY_VENDOR else vendor
    if vendor == "openai" and (auth_method or "") == "chatgpt":
        label = "ChatGPT (Codex)"
    elif vendor == "gemini" and (auth_method or "") in {"google_oauth", "google_adc"}:
        label = "Google Code Assist"
    unavailable = (
        f"the {label} listing is unavailable right now"
        if has_live_listing(vendor, auth_method)
        else f"{label} publishes no model listing"
    )
    live = (
        "from your ChatGPT account"
        if vendor == "openai" and (auth_method or "") == "chatgpt"
        else f"from the {label} API"
    )
    return {
        SOURCE_LIVE: live,
        SOURCE_SETTINGS: f"from providers.{vendor} in settings.json",
        SOURCE_CACHE: f"cached list — {unavailable}",
        SOURCE_CURATED: f"curated list — {unavailable}",
    }[source]


async def resolve_models(
    vendor: str,
    *,
    preset: VendorPreset | None = None,
    auth_method: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    credentials: dict[str, Any] | None = None,
    home: Path | str | None = None,
    refresh: bool = False,
    timeout: float = LIST_TIMEOUT,
    transport: Any = None,
    on_credentials: Callable[[dict[str, Any]], None] | None = None,
) -> ModelListing:
    """Every model ``vendor`` can serve this account, and where the list came from.

    One chain, the same for all eleven vendors and for every authentication
    method (M3 contract §2; the structure is hermes-agent's, MIT):

    1. **live** — the vendor's own endpoint: ``/v1/models`` for the
       OpenAI-compatible ones, ``/v1beta/models`` for Gemini, Anthropic's
       ``/v1/models``, Ollama's ``/api/tags``, and the Codex backend's
       per-account catalog for a ChatGPT subscription;
    2. **settings** — ``providers.<vendor>.models`` (or ``.oauth_models`` for
       an OAuth account), for a server whose listing lies or an account with
       early access to a model nothing advertises yet;
    3. **cache** — the last good answer, under
       ``<SNOWPEA_HOME>/cache/models-<vendor>-<auth>.json``, so a dropped
       network leaves the picker populated;
    4. **curated** — the preset's hand-kept list, merged with the public
       models.dev catalog so a model released after this version still shows up.

    Never raises: a picker with a stale list beats a traceback, and
    :attr:`ModelListing.source` is what tells the user which one they got.
    """
    resolved_preset = preset or PRESETS_BY_VENDOR.get(vendor)
    if resolved_preset is None:
        raise ProviderError("invalid_params", f"unknown provider vendor: {vendor}")
    oauth = (auth_method or "") in OAUTH_METHODS
    config = credentials if isinstance(credentials, dict) else {}
    path = cache_path(home, vendor, auth_method) if home is not None else None

    ids, error = await _live_catalog(
        vendor,
        preset=resolved_preset,
        auth_method=auth_method,
        api_key=api_key,
        base_url=base_url,
        credentials=config,
        timeout=timeout,
        transport=transport,
        refresh=refresh,
        on_credentials=on_credentials,
    )
    ids = [name for name in ids if not is_placeholder(name)]
    if ids:
        if path is not None:
            write_cache_file(path, ids)
        return ModelListing(
            vendor=vendor,
            auth=_auth_key(auth_method),
            models=ids,
            source=SOURCE_LIVE,
            detail=_describe(vendor, SOURCE_LIVE, auth_method=auth_method, preset=resolved_preset),
        )

    pinned = override_models(config, oauth=oauth)
    if pinned:
        return ModelListing(
            vendor=vendor,
            auth=_auth_key(auth_method),
            models=pinned,
            source=SOURCE_SETTINGS,
            detail=_describe(
                vendor, SOURCE_SETTINGS, auth_method=auth_method, preset=resolved_preset
            ),
            error=error,
        )

    if path is not None:
        remembered = read_cache_file(path)
        if remembered:
            return ModelListing(
                vendor=vendor,
                auth=_auth_key(auth_method),
                models=remembered,
                source=SOURCE_CACHE,
                detail=_describe(
                    vendor, SOURCE_CACHE, auth_method=auth_method, preset=resolved_preset
                ),
                error=error,
            )

    curated = curated_models(vendor, auth_method)
    extras = await models_dev_catalog(vendor, home=home, timeout=timeout, transport=transport)
    # Curated first: those ids are the ones this version is known to route
    # correctly, and models.dev is the long tail behind them.
    merged = _dedupe([*curated, *extras])
    return ModelListing(
        vendor=vendor,
        auth=_auth_key(auth_method),
        models=merged,
        source=SOURCE_CURATED,
        detail=_describe(vendor, SOURCE_CURATED, auth_method=auth_method, preset=resolved_preset),
        error=error,
    )


__all__ = [
    "ANTHROPIC_VERSION",
    "CACHE_TTL_SEC",
    "CODEX_MODELS_URL",
    "LIST_TIMEOUT",
    "MODELS_DEV_IDS",
    "MODELS_DEV_TTL_SEC",
    "MODELS_DEV_URL",
    "OAUTH_METHODS",
    "PLACEHOLDER_MODELS",
    "SOURCE_CACHE",
    "SOURCE_CURATED",
    "SOURCE_LIVE",
    "SOURCE_SETTINGS",
    "ModelListing",
    "cache_clear",
    "cache_get",
    "cache_path",
    "cache_put",
    "codex_catalog",
    "codex_slugs",
    "curated_models",
    "has_live_listing",
    "is_placeholder",
    "list_models",
    "models_dev_catalog",
    "models_dev_enabled",
    "offline_models",
    "oauth_models",
    "override_models",
    "read_cache_file",
    "rejected_for_oauth",
    "resolve_models",
    "write_cache_file",
]
