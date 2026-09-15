"""Which model a vendor should *offer* before the user has picked one.

A preset's :attr:`~snowpea_core.providers.presets.VendorPreset.default_model`
is a string frozen into the build, so the setup wizard used to greet people
with the flagship of whichever month the release was cut.  This module is the
live answer instead, in three rungs (CORE-default-models):

1. **live** — the account's own catalog, when this machine has a credential
   for the vendor.  It reuses :func:`~snowpea_core.providers.models.resolve_models`
   and is capped at :data:`DEFAULT_MODEL_TIMEOUT`; a slow vendor falls through
   rather than holding up a screen.
2. **models.dev** — the newest chat-capable model the public catalog lists for
   the vendor, read from the same cache (and the same TTL) the curated rung
   already fills.
3. **preset** — the hand-kept string, which is now only reached offline on a
   cold machine.

:func:`newest_catalog_model` is the pure core of rung 2 and is what the tests
drive: given a models.dev document it returns one id, with no I/O anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from snowpea_core.providers import models as model_discovery
from snowpea_core.providers.presets import PRESETS_BY_VENDOR, VendorPreset

log = logging.getLogger("snowpea.providers.default_models")

#: Which rung answered.  These strings travel to the IDE over ``setup.catalog``.
DEFAULT_SOURCE_LIVE = "live"
DEFAULT_SOURCE_MODELS_DEV = "models.dev"
DEFAULT_SOURCE_PRESET = "preset"

#: Live discovery's whole budget.  The wizard's first screen is drawn from
#: this, so two seconds is the most a vendor may cost before it is skipped.
DEFAULT_MODEL_TIMEOUT = 2.0

#: How far behind the newest release a flagship may sit and still be preferred
#: to a cheaper sibling of the same generation.  Labs ship the small variant of
#: a family weeks after the big one (``glm-5.3`` then ``glm-5.3-flash``), and a
#: default that flips to the mini model for those weeks is the bug, not a fix.
FLAGSHIP_WINDOW_DAYS = 60

#: Words a lab spells into an id to mark a cheaper tier of one family.
#: models.dev has no tier field, so this is how "flagship" is detected at all.
TIER_WORDS: frozenset[str] = frozenset(
    {
        "air",
        "fast",
        "flash",
        "flashx",
        "haiku",
        "highspeed",
        "instant",
        "lite",
        "micro",
        "mini",
        "nano",
        "small",
        "tiny",
        "turbo",
    }
)

#: Ids that announce themselves as not-for-production.  ``status`` covers the
#: ones models.dev marks; the rest live in the id, as every lab spells them.
_UNSTABLE_RE = re.compile(
    r"(?:^|[-_./])(preview|exp|experimental|alpha|beta|rc\d*|draft|test)(?:[-_./\d]|$)",
    re.IGNORECASE,
)

#: Statuses that disqualify a model while a stable sibling exists.
_UNSTABLE_STATUS: frozenset[str] = frozenset({"deprecated", "beta"})

#: Vendors whose models.dev catalog is an aggregation rather than one lab's.
#: OpenRouter resells every lab, so "the newest id it lists" is whoever shipped
#: last — meaningless as *this vendor's* default; the preset has always pointed
#: at the Anthropic namespace, so the pick stays inside it.  Alibaba's catalog
#: carries third-party models (GLM, DeepSeek) beside Qwen's own.
MODELS_DEV_ID_PREFIX: dict[str, str] = {
    "openrouter": "anthropic/",
    "qwen": "qwen",
}


@dataclass(frozen=True)
class DefaultModel:
    """One vendor's default model, and which rung produced it."""

    vendor: str
    model: str
    source: str

    @property
    def detail(self) -> str:
        """The parenthetical a row prints after the model id."""
        return {
            DEFAULT_SOURCE_LIVE: "from your account",
            DEFAULT_SOURCE_MODELS_DEV: "from models.dev",
            DEFAULT_SOURCE_PRESET: "built-in default",
        }.get(self.source, self.source)

    @property
    def label(self) -> str:
        """``default: glm-5.3 (from models.dev)`` — the whole row suffix."""
        return f"default: {self.model} ({self.detail})"


# ---------------------------------------------------------------------------
# the pure models.dev pick
# ---------------------------------------------------------------------------


def _release_date(meta: Mapping[str, Any]) -> str:
    """``release_date`` as a sortable string, or ``""`` when it is absent."""
    raw = meta.get("release_date")
    if not isinstance(raw, str):
        return ""
    try:
        date.fromisoformat(raw.strip())
    except ValueError:
        return ""
    return raw.strip()


def _is_chat_model(meta: Mapping[str, Any]) -> bool:
    """True for a model an agent turn could actually use.

    Tool calling is the hard requirement — an agent without it is a chatbot —
    and text has to be both an input and an output modality, which is what
    keeps image and speech endpoints out of a *model* default.
    """
    if not meta.get("tool_call", False):
        return False
    modalities = meta.get("modalities")
    if not isinstance(modalities, Mapping):
        # Older payloads (and the tests' minimal ones) carry no modalities at
        # all; tool calling alone is enough to judge those.
        return True
    inputs = modalities.get("input")
    outputs = modalities.get("output")
    if not isinstance(inputs, list) or not isinstance(outputs, list):
        return True
    return "text" in inputs and "text" in outputs


def _is_stable(model_id: str, meta: Mapping[str, Any]) -> bool:
    """False for a deprecated, beta or preview id."""
    status = meta.get("status")
    if isinstance(status, str) and status.strip().lower() in _UNSTABLE_STATUS:
        return False
    return _UNSTABLE_RE.search(model_id) is None


def is_flagship(model_id: str) -> bool:
    """True when no segment of ``model_id`` names a cheaper tier of a family.

    ``glm-5.3`` is a flagship, ``glm-5.3-flash`` is not.  models.dev publishes
    no tier field, so the id is the only thing there is to read.
    """
    segments = re.split(r"[-_/.]", model_id.lower())
    return not any(segment in TIER_WORDS for segment in segments)


def _days_between(newer: str, older: str) -> int:
    """Whole days from ``older`` to ``newer``; a huge number when unknown."""
    if not newer or not older:
        return 10_000
    return (date.fromisoformat(newer) - date.fromisoformat(older)).days


def _vendor_models(vendor: str, catalog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The models.dev entries for ``vendor``, under whichever provider id hit."""
    prefix = MODELS_DEV_ID_PREFIX.get(vendor, "")
    for provider_id in model_discovery.MODELS_DEV_IDS.get(vendor, ()):
        entry = catalog.get(provider_id)
        models = entry.get("models") if isinstance(entry, Mapping) else None
        if not isinstance(models, Mapping):
            continue
        rows = {
            str(model_id): meta
            for model_id, meta in models.items()
            if isinstance(meta, Mapping) and str(model_id).startswith(prefix)
        }
        if rows:
            return rows
    return {}


def newest_catalog_model(vendor: str, catalog: Mapping[str, Any]) -> str | None:
    """The model ``vendor`` should default to according to ``catalog``.

    Pure: ``catalog`` is a models.dev document (``{provider: {"models": …}}``)
    and the answer is one id or ``None``.  The rule is the newest chat-capable
    release, with two corrections that the raw date alone gets wrong:

    * a deprecated or preview id only wins when no stable sibling exists, and
    * within :data:`FLAGSHIP_WINDOW_DAYS` of the newest release, a flagship
      beats a cheaper tier of the same generation (see :data:`TIER_WORDS`).
    """
    rows = _vendor_models(vendor, catalog)
    candidates = [
        (model_id, meta, _release_date(meta))
        for model_id, meta in rows.items()
        if _is_chat_model(meta)
    ]
    if not candidates:
        return None
    stable = [row for row in candidates if _is_stable(row[0], row[1])]
    pool = stable or candidates
    newest = max(released for _id, _meta, released in pool)
    flagships = [
        (model_id, released)
        for model_id, _meta, released in pool
        if is_flagship(model_id) and _days_between(newest, released) <= FLAGSHIP_WINDOW_DAYS
    ]
    ranked = flagships or [(model_id, released) for model_id, _meta, released in pool]
    # Newest first; the id breaks a same-day tie so the answer is stable across
    # runs (dict order in a downloaded JSON document is not something to trust).
    ranked.sort(key=lambda row: (row[1], row[0]), reverse=True)
    return ranked[0][0]


def rank_live_models(vendor: str, ids: list[str], catalog: Mapping[str, Any]) -> str | None:
    """The best of a *live* listing, using models.dev as the ordering key.

    ``/v1/models`` answers in no useful order — OpenAI's is effectively random
    — so an account's own list is ranked by the same rule as rung 2 whenever
    the public catalog describes those ids.  When it describes none of them
    (the Codex backend's slugs, a self-hosted server), the listing's own order
    stands: that one *is* meaningful, being priority-sorted by the backend.
    """
    if not ids:
        return None
    rows = _vendor_models(vendor, catalog)
    known = {model_id: rows[model_id] for model_id in ids if model_id in rows}
    if not known:
        return ids[0]
    return newest_catalog_model(vendor, _wrap(vendor, known)) or ids[0]


def _wrap(vendor: str, rows: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """``rows`` as a one-provider models.dev document for ``vendor``."""
    provider_ids = model_discovery.MODELS_DEV_IDS.get(vendor, ())
    provider_id = provider_ids[0] if provider_ids else vendor
    return {provider_id: {"models": dict(rows)}}


# ---------------------------------------------------------------------------
# the three-rung resolution
# ---------------------------------------------------------------------------


def preset_default(vendor: str, preset: VendorPreset | None = None) -> str:
    """The build's own string for ``vendor`` — rung 3, and never empty."""
    resolved = preset or PRESETS_BY_VENDOR.get(vendor)
    return resolved.default_model if resolved is not None else ""


def default_model_offline(
    vendor: str,
    *,
    preset: VendorPreset | None = None,
    home: Path | str | None = None,
) -> DefaultModel:
    """Rungs 2 and 3 with no I/O at all: the cached catalog, else the preset.

    ``setup.catalog`` and the wizard call this for every vendor they are not
    going to probe, so drawing eleven rows costs nothing.
    """
    catalog = model_discovery.models_dev_cached(home)
    picked = newest_catalog_model(vendor, catalog) if catalog else None
    if picked:
        return DefaultModel(vendor=vendor, model=picked, source=DEFAULT_SOURCE_MODELS_DEV)
    return DefaultModel(
        vendor=vendor, model=preset_default(vendor, preset), source=DEFAULT_SOURCE_PRESET
    )


async def models_dev_document(
    *,
    home: Path | str | None = None,
    timeout: float = DEFAULT_MODEL_TIMEOUT,
    transport: Any = None,
) -> Mapping[str, Any]:
    """The models.dev document, from the shared cache or the network.

    Honours ``SNOWPEA_MODELS_DEV=0`` exactly as the curated rung does, and
    never raises: an empty document simply means rung 2 has nothing to say.
    """
    try:
        document = await model_discovery.models_dev_document(
            home=home, timeout=timeout, transport=transport
        )
    except Exception as exc:  # noqa: BLE001 - a default is never worth an error
        log.debug("models.dev unavailable while picking a default: %s", exc)
        document = {}
    return document or model_discovery.models_dev_cached(home)


async def default_model_for(
    vendor: str,
    *,
    preset: VendorPreset | None = None,
    configured: bool = False,
    auth_method: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    credentials: dict[str, Any] | None = None,
    home: Path | str | None = None,
    timeout: float = DEFAULT_MODEL_TIMEOUT,
    transport: Any = None,
) -> DefaultModel:
    """``vendor``'s default model and where it came from — live, models.dev, preset.

    ``configured`` is what decides whether the live rung is attempted at all:
    asking a vendor we hold no credential for buys a guaranteed 401 and a
    delay.  Whatever happens — a timeout, a 500, a reshaped payload — this
    returns a usable id, because a wizard row with no default on it is worse
    than a slightly old one.
    """
    if configured:
        live = await _live_default(
            vendor,
            preset=preset,
            auth_method=auth_method,
            api_key=api_key,
            base_url=base_url,
            credentials=credentials,
            home=home,
            timeout=timeout,
            transport=transport,
        )
        if live is not None:
            return live
    catalog = await models_dev_document(home=home, timeout=timeout, transport=transport)
    picked = newest_catalog_model(vendor, catalog) if catalog else None
    if picked:
        return DefaultModel(vendor=vendor, model=picked, source=DEFAULT_SOURCE_MODELS_DEV)
    return DefaultModel(
        vendor=vendor, model=preset_default(vendor, preset), source=DEFAULT_SOURCE_PRESET
    )


async def _live_default(
    vendor: str,
    *,
    preset: VendorPreset | None,
    auth_method: str | None,
    api_key: str | None,
    base_url: str | None,
    credentials: dict[str, Any] | None,
    home: Path | str | None,
    timeout: float,
    transport: Any,
) -> DefaultModel | None:
    """Rung 1, or ``None`` when the account did not answer in time."""
    try:
        listing = await asyncio.wait_for(
            model_discovery.resolve_models(
                vendor,
                preset=preset,
                auth_method=auth_method,
                api_key=api_key,
                base_url=base_url,
                credentials=credentials,
                home=home,
                timeout=timeout,
                transport=transport,
            ),
            timeout,
        )
    except (TimeoutError, asyncio.CancelledError):
        log.debug("%s did not list its models within %.1fs; using the catalog", vendor, timeout)
        return None
    except Exception as exc:  # noqa: BLE001 - a default is never worth an error
        log.debug("could not ask %s for its models: %s", vendor, exc)
        return None
    if not listing.live or not listing.models:
        return None
    catalog = model_discovery.models_dev_cached(home)
    picked = rank_live_models(vendor, listing.models, catalog) or listing.models[0]
    return DefaultModel(vendor=vendor, model=picked, source=DEFAULT_SOURCE_LIVE)


__all__ = [
    "DEFAULT_MODEL_TIMEOUT",
    "DEFAULT_SOURCE_LIVE",
    "DEFAULT_SOURCE_MODELS_DEV",
    "DEFAULT_SOURCE_PRESET",
    "FLAGSHIP_WINDOW_DAYS",
    "MODELS_DEV_ID_PREFIX",
    "TIER_WORDS",
    "DefaultModel",
    "default_model_for",
    "default_model_offline",
    "is_flagship",
    "models_dev_document",
    "newest_catalog_model",
    "preset_default",
    "rank_live_models",
]
