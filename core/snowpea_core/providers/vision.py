"""Can this model see an image? — the chain that answers it.

:mod:`snowpea_core.providers.content` answers from the model *name*, against a
hand-kept allowlist.  That is the right default for a hosted catalog and the
wrong one for a self-hosted server: a vision model someone loaded under the
name ``flash-next-mtp`` matches no hint, so every image it was sent came back
as ``(this model cannot see images)`` while the server would have answered
perfectly well (CORE-vision).

So the name is one rung of four, not the whole answer:

1. **settings** — ``providers.<vendor>.vision`` for the whole server, or
   ``providers.<vendor>.models.<model>.vision`` for one model.  A person who
   knows what they loaded outranks every guess here.
2. **models.dev** — the public catalog's own card, when one is already on disk
   (``modalities.input`` containing ``image``).  Never fetched from here: this
   runs on the way into a request and must not add a round trip.
3. **the name** — :data:`~snowpea_core.providers.content.VISION_HINTS` and the
   vendors whose whole catalog takes images.
4. **try once** — for a local-style vendor only, and only when the rungs above
   said nothing: send the images, and if the server refuses the request,
   remember that and retry immediately with the text fallback so the turn still
   finishes.  A hosted vendor is never guessed at this way; its catalog is
   knowable, and a 400 there is a bill for nothing.

Rung 4 is what makes an unknown local model usable at all.  Its cost is one
refused request per model, once, and its memory is written to disk so a restart
does not pay it again.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("snowpea.providers.vision")

#: How long a learned answer stays good.  A server that was restarted with a
#: different model under the same name is the reason this expires at all.
TTL_SEC = 7 * 24 * 3600.0

#: Where the learned answers live, under ``<SNOWPEA_HOME>/cache``.
CACHE_FILE = "vision.json"

#: HTTP statuses that mean "this request was wrong", as opposed to "this server
#: is broken" (5xx) or "who are you" (401/403).  Only these are read as a
#: refusal of the image parts.
REJECT_STATUSES = frozenset({400, 415, 422})

#: Words that make a 400 a *vision* refusal rather than some other bad request.
#: A 400 about ``max_tokens`` must not be remembered as "this model is blind".
REJECT_PHRASES: tuple[str, ...] = (
    "image",
    "image_url",
    "multimodal",
    "vision",
    "content parts",
    "content type",
    "invalid content",
    "unsupported content",
    "expected a string",
    "must be a string",
)


def settings_override(config: Any, model: str | None) -> bool | None:
    """``providers.<vendor>.vision`` / ``.models.<model>.vision``, or ``None``.

    The per-model rule wins over the per-server one, the way a more specific
    rule always does.  ``providers.<vendor>.models`` is a *list* of model ids
    when it is pinning a catalog; only its object form carries capabilities,
    and both shapes are accepted so neither use has to know about the other.
    """
    if not isinstance(config, dict):
        return None
    models = config.get("models")
    if isinstance(models, dict) and model:
        card = models.get(model)
        if isinstance(card, dict) and isinstance(card.get("vision"), bool):
            return bool(card["vision"])
    value = config.get("vision")
    return bool(value) if isinstance(value, bool) else None


def from_models_dev(catalog: Any, provider_ids: tuple[str, ...], model: str | None) -> bool | None:
    """``modalities.input`` of ``model``'s card, or ``None`` when there is none.

    Takes the already-loaded catalog rather than fetching one: this is on the
    path into every request, and a network call there would cost more than the
    wrong answer it prevents.
    """
    if not isinstance(catalog, dict) or not model:
        return None
    for provider_id in provider_ids:
        entry = catalog.get(provider_id)
        models = entry.get("models") if isinstance(entry, dict) else None
        card = models.get(model) if isinstance(models, dict) else None
        if not isinstance(card, dict):
            continue
        modalities = card.get("modalities")
        inputs = modalities.get("input") if isinstance(modalities, dict) else None
        if isinstance(inputs, list):
            return any(str(item).lower() == "image" for item in inputs)
    return None


def is_rejection(status: int, detail: str) -> bool:
    """True when ``status``/``detail`` read as "I cannot take image parts".

    Deliberately narrow.  Remembering the wrong 400 would leave a model that
    can see perfectly well permanently downgraded to the text fallback, and
    nothing in the interface would explain why.
    """
    if status not in REJECT_STATUSES:
        return False
    text = (detail or "").lower()
    return any(phrase in text for phrase in REJECT_PHRASES)


class VisionMemory:
    """What the try-once rung has learned, keyed ``(vendor, base_url, model)``.

    Kept in memory and mirrored to ``<SNOWPEA_HOME>/cache/vision.json`` so the
    one refused request per model is paid once per week, not once per restart.
    """

    def __init__(self, home: Path | str | None = None) -> None:
        self.home = Path(home) if home is not None else None
        self._known: dict[tuple[str, str, str], tuple[float, bool]] = {}
        self._loaded = False

    # -- disk ----------------------------------------------------------
    def _path(self) -> Path | None:
        if self.home is None:
            return None
        from snowpea_core.config.paths import Paths

        return Paths(home=self.home).cache_dir / CACHE_FILE

    def _key(self, vendor: str, base_url: str, model: str) -> tuple[str, str, str]:
        return (vendor, (base_url or "").rstrip("/"), model or "")

    def load(self) -> None:
        """Read the file once; a broken one is ignored, not raised."""
        if self._loaded:
            return
        self._loaded = True
        path = self._path()
        if path is None:
            return
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(document, dict):
            return
        for raw, entry in document.items():
            try:
                parts = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(parts, list) or len(parts) != 3 or not isinstance(entry, dict):
                continue
            saved_at = entry.get("saved_at")
            value = entry.get("vision")
            if not isinstance(saved_at, int | float) or not isinstance(value, bool):
                continue
            if time.time() - float(saved_at) > TTL_SEC:
                continue
            self._known[(str(parts[0]), str(parts[1]), str(parts[2]))] = (float(saved_at), value)

    def save(self) -> None:
        path = self._path()
        if path is None:
            return
        # The key is a JSON list rather than a joined string: a base_url may
        # contain any separator one could pick, and a key that can collide is a
        # cache that can answer for the wrong server.
        document = {
            json.dumps(list(key)): {"saved_at": saved_at, "vision": value}
            for key, (saved_at, value) in self._known.items()
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(document), encoding="utf-8")
        except OSError:  # pragma: no cover - read-only home
            log.debug("could not persist the vision cache", exc_info=True)

    # -- lookup --------------------------------------------------------
    def get(self, vendor: str, base_url: str, model: str) -> bool | None:
        self.load()
        entry = self._known.get(self._key(vendor, base_url, model))
        if entry is None:
            return None
        saved_at, value = entry
        if time.time() - saved_at > TTL_SEC:
            self._known.pop(self._key(vendor, base_url, model), None)
            return None
        return value

    def remember(self, vendor: str, base_url: str, model: str, vision: bool) -> None:
        self.load()
        self._known[self._key(vendor, base_url, model)] = (time.time(), vision)
        self.save()

    def clear(self) -> None:
        self._known.clear()
        self._loaded = False


#: The process-wide memory.  ``ProviderRegistry`` points it at this machine's
#: home the moment it knows one.
MEMORY = VisionMemory()


__all__ = [
    "CACHE_FILE",
    "MEMORY",
    "REJECT_PHRASES",
    "REJECT_STATUSES",
    "TTL_SEC",
    "VisionMemory",
    "from_models_dev",
    "is_rejection",
    "settings_override",
]
