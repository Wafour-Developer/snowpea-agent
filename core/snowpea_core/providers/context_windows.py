"""How many tokens one model's context window holds (CORE-context).

The HUD shows ``used / window``, and auto-compaction fires at a percentage of
the window, so both need a number for the session's model.  The sources:

1. ``settings.providers.<vendor>.context_window`` — an explicit override, which
   is the escape hatch for a model nothing else describes.
2. a self-hosted (local-style) server, asked: vLLM/SGLang put ``max_model_len``
   on every ``GET /v1/models`` row, llama.cpp-style servers ``meta.n_ctx`` (or
   ``/props``), LM Studio ``loaded_context_length``, TGI ``/info``, and Ollama
   ``/api/ps`` (the loaded ``num_ctx``) then ``/api/show``.  Cached for
   :data:`CACHE_TTL_SEC`; a trained length (``n_ctx_train``) only when no
   probe found the served one.
3. for a hosted vendor, the smaller of :data:`STATIC_WINDOWS` (longest prefix)
   and the models.dev catalog's exact ``limit.context``.

An unknown model yields ``None``, which every surface renders as ``?`` rather
than guessing — a wrong window silently truncates or silently never compacts.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from snowpea_core.providers.presets import VendorPreset

log = logging.getLogger("snowpea.providers.context")

#: Discovery must never stall a prompt.
LOOKUP_TIMEOUT = 5.0

#: How long one ``(vendor, base_url, model)`` answer stays fresh, matching the
#: model listing's TTL so ``/model`` and the HUD agree after a refresh.
CACHE_TTL_SEC = 600.0

K = 1024

#: ``model id prefix -> window in tokens``.  Longest prefix wins, so
#: ``gpt-4.1-mini`` can differ from ``gpt-4.1`` without reordering anything.
#:
#: Sources are each vendor's published model card as of 2026-05; a model that
#: advertises a larger window only under a beta header is listed at its
#: default, because that is what an ordinary request gets.
STATIC_WINDOWS: dict[str, int] = {
    # -- Anthropic ---------------------------------------------------------
    # Every generally available claude-* model is 200k by default; the Claude 5
    # family ships a 1M window as standard (models.dev, 2026-09).
    "claude-": 200 * 1000,
    "claude-sonnet-5": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    # -- OpenAI ------------------------------------------------------------
    "gpt-6": 1_050_000,
    "gpt-5.6": 1_050_000,
    "gpt-5": 400 * 1000,
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128 * 1000,
    "gpt-4-turbo": 128 * 1000,
    "gpt-4": 8192,
    "gpt-3.5-turbo": 16385,
    "o4-mini": 200 * 1000,
    "o3": 200 * 1000,
    "o1": 200 * 1000,
    # -- Google Gemini -----------------------------------------------------
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
    "gemini-2.0": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-": 1_048_576,
    # -- xAI ---------------------------------------------------------------
    "grok-4.6": 500 * 1000,
    "grok-4.5": 500 * 1000,
    "grok-4": 256 * 1000,
    "grok-3": 131072,
    "grok-2": 131072,
    "grok-": 131072,
    # -- Zhipu GLM ---------------------------------------------------------
    "glm-5": 1_000_000,
    "glm-4.6": 200 * 1000,
    "glm-4.5": 128 * 1000,
    "glm-4": 128 * 1000,
    # -- MiniMax -----------------------------------------------------------
    "minimax-m3": 1_048_576,
    "minimax-m2": 204800,
    "minimax-text": 1_000_000,
    "abab": 245760,
    # -- Moonshot Kimi -----------------------------------------------------
    "kimi-k3": 1_048_576,
    "kimi-k2": 256 * 1000,
    "kimi-": 128 * 1000,
    "moonshot-v1-128k": 128 * 1000,
    "moonshot-v1-32k": 32 * 1000,
    "moonshot-v1-8k": 8 * 1000,
    # -- DeepSeek ----------------------------------------------------------
    "deepseek-v4": 1_000_000,
    "deepseek-chat": 128 * 1000,
    "deepseek-reasoner": 128 * 1000,
    "deepseek-": 128 * 1000,
    # -- Qwen --------------------------------------------------------------
    "qwen3.8": 1_000_000,
    "qwen3-max": 262144,
    "qwen3-coder": 262144,
    "qwen3": 131072,
    "qwen-max": 32768,
    "qwen-plus": 131072,
    "qwen-turbo": 1_000_000,
    "qwen": 131072,
    # -- Meta Model API ----------------------------------------------------
    "muse-spark": 1_048_576,
}

#: ``model id prefix -> largest ``max_tokens`` the vendor accepts``.  Same
#: longest-prefix matching as :data:`STATIC_WINDOWS`, and the same rule for a
#: model that is not listed: ``None``, which means "do not clamp".
#:
#: Only models whose ceiling is *below* the default budget really matter — a
#: request over the cap is a 400, not a truncation — so the table stays short
#: and only carries numbers taken from a published model card
#: (CORE-reasoning-budget).
MAX_OUTPUT_TOKENS: dict[str, int] = {
    # -- OpenAI ------------------------------------------------------------
    "gpt-4o": 16384,
    "gpt-4-turbo": 4096,
    "gpt-4": 8192,
    "gpt-3.5-turbo": 4096,
    # -- Google Gemini -----------------------------------------------------
    "gemini-1.5": 8192,
    "gemini-2.0": 8192,
    # -- DeepSeek ----------------------------------------------------------
    "deepseek-chat": 8192,
    # -- Moonshot Kimi -----------------------------------------------------
    "moonshot-v1-8k": 4096,
}

#: OpenRouter ids are ``<vendor>/<model>``; the slug after the slash is looked
#: up in :data:`STATIC_WINDOWS` with the vendor's own dots restored
#: (``claude-sonnet-4.5`` -> ``claude-sonnet-4-5`` is *not* needed, because the
#: table matches on the ``claude-`` prefix).
OPENROUTER_SEPARATOR = "/"

#: ``(vendor, base_url, model)`` -> ``(expires_at, window | None)``.
_CACHE: dict[tuple[str, str, str], tuple[float, int | None]] = {}

#: What a server last SAID, kept past the TTL.  The TTL only decides when to ask
#: again; it must never turn a known 262k back into the static table's 131k.  A
#: long session used to do exactly that: the entry expired after ten minutes and
#: the turn-ending ``context`` event (which may not await a lookup) fell through
#: to the family default, so the HUD halved and auto-compaction fired early.
_LAST_KNOWN: dict[tuple[str, str, str], int] = {}

#: A failed lookup is retried soon; a transient timeout should not pin the
#: fallback for the full TTL.
NEGATIVE_TTL_SEC = 60.0


def cache_clear() -> None:
    """Drop every discovered window (tests, and ``/model --refresh``)."""
    _CACHE.clear()
    _LAST_KNOWN.clear()


def last_known(vendor: str, base_url: str, model: str) -> int | None:
    """The window a server reported for this model, however long ago."""
    return _LAST_KNOWN.get((vendor, base_url, model))


def remember_discovered(vendor: str, base_url: str, model: str, window: int) -> None:
    """Record a server's own answer: fresh for the TTL, remembered after it."""
    _LAST_KNOWN[(vendor, base_url, model)] = window
    cache_put(vendor, base_url, model, window)


def remember_failure(vendor: str, base_url: str, model: str, fallback: int | None) -> None:
    """Record that the server could not be asked; ``fallback`` is served meanwhile."""
    _CACHE[(vendor, base_url, model)] = (time.monotonic() + NEGATIVE_TTL_SEC, fallback)


def cache_get(vendor: str, base_url: str, model: str) -> tuple[bool, int | None]:
    """``(hit, window)`` — ``hit`` is False when nothing fresh is cached."""
    entry = _CACHE.get((vendor, base_url, model))
    if entry is None:
        return False, None
    expires_at, window = entry
    if expires_at < time.monotonic():
        _CACHE.pop((vendor, base_url, model), None)
        return False, None
    return True, window


def cache_put(vendor: str, base_url: str, model: str, window: int | None) -> None:
    _CACHE[(vendor, base_url, model)] = (time.monotonic() + CACHE_TTL_SEC, window)


# ---------------------------------------------------------------------------
# static lookup
# ---------------------------------------------------------------------------


def _longest_prefix(table: dict[str, int], model: str | None) -> int | None:
    """Look ``model`` up in a prefix table, else ``None``.

    Matching is case-insensitive, ignores an OpenRouter ``vendor/`` prefix and
    any ``:free`` / ``@version`` suffix, and prefers the longest key that the
    id starts with.
    """
    name = (model or "").strip().lower()
    if not name:
        return None
    candidates = [name]
    if OPENROUTER_SEPARATOR in name:
        candidates.append(name.rsplit(OPENROUTER_SEPARATOR, 1)[1])
    for candidate in candidates:
        trimmed = candidate.split(":", 1)[0].split("@", 1)[0]
        exact = table.get(trimmed)
        if exact is not None:
            return exact
        best: int | None = None
        best_len = -1
        for prefix, value in table.items():
            if trimmed.startswith(prefix) and len(prefix) > best_len:
                best, best_len = value, len(prefix)
        if best is not None:
            return best
    return None


def static_window(model: str | None) -> int | None:
    """Window for ``model`` from :data:`STATIC_WINDOWS`, else ``None``."""
    return _longest_prefix(STATIC_WINDOWS, model)


def max_output_tokens(model: str | None) -> int | None:
    """Largest ``max_tokens`` ``model`` accepts, or ``None`` when unknown."""
    return _longest_prefix(MAX_OUTPUT_TOKENS, model)


def clamp_output_tokens(model: str | None, requested: int) -> int:
    """``requested``, lowered to whatever ``model`` actually accepts.

    A budget is a wish: asking a model for more output than its ceiling is
    rejected outright by most vendors, so the setting is clamped here rather
    than turning a long answer into an HTTP 400.
    """
    cap = max_output_tokens(model)
    if cap is None:
        return max(1, requested)
    return max(1, min(requested, cap))


def preset_window(preset: VendorPreset, model: str | None = None) -> int | None:
    """Static window for ``model`` (default: the preset's own default model)."""
    return static_window(model or preset.default_model)


# ---------------------------------------------------------------------------
# live discovery (local vendors only)
# ---------------------------------------------------------------------------


def _as_window(value: Any) -> int | None:
    """A positive int from whatever JSON shape a server used, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            return None
        return number if number > 0 else None
    return None


#: Keys a server may put the window under, most specific first.
_WINDOW_KEYS = (
    # LM Studio: what the model is loaded with, before what it could take.
    "loaded_context_length",
    "max_model_len",
    "context_length",
    "max_context_length",
    "context_window",
    # llama.cpp-style servers (and Strata) put the served window in
    # ``meta.n_ctx``.
    "n_ctx",
    "max_position_embeddings",
)

#: What a model was trained for, not what this server gives it: only when no
#: probe found the served window (llama.cpp ``meta.n_ctx_train``).
_TRAINED_KEYS = ("n_ctx_train",)


def window_from_row(row: dict[str, Any], keys: tuple[str, ...] = _WINDOW_KEYS) -> int | None:
    """Window declared on one ``/v1/models`` row, else ``None``."""
    for source in (row, row.get("meta"), row.get("default_generation_settings")):
        if not isinstance(source, dict):
            continue
        for key in keys:
            window = _as_window(source.get(key))
            if window is not None:
                return window
    return None


def trained_window(row: dict[str, Any]) -> int | None:
    """The trained length a row declares, the last thing to go by."""
    return window_from_row(row, _TRAINED_KEYS)


def window_from_ollama_show(payload: dict[str, Any]) -> int | None:
    """Window from an Ollama ``/api/show`` body.

    Ollama namespaces the value by architecture (``llama.context_length``,
    ``qwen3.context_length``, …) inside ``model_info``, so the family is not
    known in advance; any ``*.context_length`` counts.
    """
    info = payload.get("model_info")
    if isinstance(info, dict):
        for key, value in info.items():
            if str(key).endswith("context_length"):
                window = _as_window(value)
                if window is not None:
                    return window
    return window_from_row(payload)


async def _get_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await client.get(url)
            if response.status_code >= 400:
                return None
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("context window lookup failed for %s: %s", url, exc)
        return None
    return payload if isinstance(payload, dict) else None


async def _post_json(
    url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await client.post(url, json=body)
            if response.status_code >= 400:
                return None
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("context window lookup failed for %s: %s", url, exc)
        return None
    return payload if isinstance(payload, dict) else None


async def discover_window(
    base_url: str,
    model: str,
    *,
    api_key: str | None = None,
    timeout: float = LOOKUP_TIMEOUT,
) -> int | None:
    """Ask a local OpenAI-compatible server how long ``model``'s context is.

    Tries ``GET {base_url}/models`` first (vLLM and LM Studio both annotate the
    rows) and falls back to Ollama's ``POST {root}/api/show``.  Every failure
    — unreachable, 404, no such field — is ``None``, never an exception: a
    missing window degrades the HUD, it must not break a prompt.
    """
    base = base_url.rstrip("/")
    if not base or not model:
        return None
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    root = base.removesuffix("/v1")
    trained: int | None = None

    payload = await _get_json(f"{base}/models", headers, timeout)
    if payload is not None:
        data = payload.get("data")
        rows = [row for row in (data if isinstance(data, list) else []) if isinstance(row, dict)]
        # A single-model server need not echo the alias the client uses.
        mine = [row for row in rows if str(row.get("id", "")) == model] or (
            rows if len(rows) == 1 else []
        )
        for row in mine:
            window = window_from_row(row)
            if window is not None:
                return window
            trained = trained or trained_window(row)

    # Each server software states the window it was started with somewhere of
    # its own; a 404 from the others costs one quick round trip, once per TTL.
    for probe in (
        _llamacpp_props,  # llama.cpp / ik_llama: GET /props
        _lmstudio_model,  # LM Studio: GET /api/v0/models/{model}
        _tgi_info,  # Hugging Face TGI: GET /info
        _ollama_window,  # Ollama: POST /api/ps, then /api/show
    ):
        window = await probe(root, model, headers, timeout)
        if window is not None:
            return window
    return trained


async def _llamacpp_props(
    root: str, model: str, headers: dict[str, str], timeout: float
) -> int | None:
    props = await _get_json(f"{root}/props", headers, timeout)
    return window_from_row(props) if props is not None else None


async def _lmstudio_model(
    root: str, model: str, headers: dict[str, str], timeout: float
) -> int | None:
    row = await _get_json(f"{root}/api/v0/models/{model}", headers, timeout)
    return window_from_row(row) if row is not None else None


async def _tgi_info(root: str, model: str, headers: dict[str, str], timeout: float) -> int | None:
    info = await _get_json(f"{root}/info", headers, timeout)
    if info is None:
        return None
    return _as_window(info.get("max_total_tokens")) or _as_window(info.get("max_input_tokens"))


async def _ollama_window(
    root: str, model: str, headers: dict[str, str], timeout: float
) -> int | None:
    # /api/ps reports the context a loaded model actually runs with
    # (num_ctx); /api/show only knows what the model could take.
    running = await _get_json(f"{root}/api/ps", headers, timeout)
    for row in (running or {}).get("models") or []:
        if isinstance(row, dict) and model in (row.get("name"), row.get("model")):
            window = _as_window(row.get("context_length"))
            if window is not None:
                return window
    show = await _post_json(f"{root}/api/show", {"model": model}, headers, timeout)
    return window_from_ollama_show(show) if show is not None else None


# ---------------------------------------------------------------------------
# the public catalog (hosted vendors)
# ---------------------------------------------------------------------------

#: The parsed models.dev document, keyed by the cache file's mtime: the
#: window is looked up on every turn and the file is large.
_CATALOG: tuple[str, int, dict[str, Any]] | None = None


def _catalog(home: Path | str | None) -> dict[str, Any]:
    global _CATALOG
    if home is None:
        return {}
    from snowpea_core.config.paths import Paths
    from snowpea_core.providers.models import models_dev_cached

    path = Paths(home=Path(home)).cache_dir / "models-dev.json"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _CATALOG is None or _CATALOG[0] != str(path) or _CATALOG[1] != stamp:
        _CATALOG = (str(path), stamp, models_dev_cached(home))
    return _CATALOG[2]


def catalog_window(vendor: str, model: str | None, home: Path | str | None) -> int | None:
    """``limit.context`` models.dev lists for exactly this vendor's ``model``."""
    from snowpea_core.providers.models import MODELS_DEV_IDS

    name = (model or "").strip()
    if not name:
        return None
    catalog = _catalog(home)
    for provider_id in MODELS_DEV_IDS.get(vendor, ()):
        entry = catalog.get(provider_id)
        models = entry.get("models") if isinstance(entry, dict) else None
        meta = models.get(name) if isinstance(models, dict) else None
        limit = meta.get("limit") if isinstance(meta, dict) else None
        if isinstance(limit, dict):
            window = _as_window(limit.get("context"))
            if window is not None:
                return window
    return None


__all__ = [
    "CACHE_TTL_SEC",
    "LOOKUP_TIMEOUT",
    "MAX_OUTPUT_TOKENS",
    "STATIC_WINDOWS",
    "cache_clear",
    "cache_get",
    "cache_put",
    "catalog_window",
    "clamp_output_tokens",
    "discover_window",
    "last_known",
    "remember_discovered",
    "remember_failure",
    "max_output_tokens",
    "preset_window",
    "static_window",
    "trained_window",
    "window_from_ollama_show",
    "window_from_row",
]
