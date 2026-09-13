"""Merging, masking and dotted-path helpers shared by settings writers.

``settings.set`` (RPC) and the ``settings_get`` / ``settings_set`` tools have to
agree on three things: how a patch merges onto the current document, which
fields are masked before anything leaves the daemon, and what ``search.provider``
means as a path.  They live here so both callers use the same rules.
"""

from __future__ import annotations

import json
from typing import Any

#: Field names masked in every settings payload that leaves the daemon.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apiKey",
        "token",
        "refresh_token",
        "refreshToken",
        "access_token",
        "accessToken",
        "id_token",
        "idToken",
        "oauth_token",
        "oauthToken",
        "password",
    }
)
MASK = "***"


def mask_secrets(value: Any) -> Any:
    """Recursively replace secret-named fields with ``"***"``.

    Never mutates ``value``; used only on responses, so the persisted file keeps
    the real credentials.
    """
    if isinstance(value, dict):
        return {
            key: (MASK if key in SECRET_KEYS and val is not None else mask_secrets(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into ``base``, recursing into nested dicts only.

    A list or scalar in the patch replaces the corresponding value wholesale; a
    dict merges key by key.

    **``null`` deletes.**  A merge alone cannot express a removal, so there was
    no way to delete a model profile, an agent assignment or a team through
    ``settings.set`` at all — the key simply survived every patch
    (CORE-model-assignment).  ``{"models": {"profiles": {"fast": null}}}``
    therefore removes ``fast``.

    This costs nothing for the scalar fields: every optional field in
    ``Settings`` and ``ProjectSettings`` defaults to ``None``, so deleting a key
    and setting it to ``null`` land on exactly the same validated document.
    Deleting a key that is not there is a no-op, not an error.
    """
    merged = dict(base)
    for key, value in patch.items():
        existing = merged.get(key)
        if value is None:
            merged.pop(key, None)
        elif isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def nest(key: str, value: Any) -> dict[str, Any]:
    """``"search.provider", "exa"`` -> ``{"search": {"provider": "exa"}}``."""
    parts = [part for part in key.split(".") if part]
    if not parts:
        raise ValueError("key must not be empty")
    patch: Any = value
    for part in reversed(parts):
        patch = {part: patch}
    return dict(patch)


def pluck(document: dict[str, Any], key: str) -> Any:
    """Read a dotted path out of ``document``; ``KeyError`` when it is absent."""
    node: Any = document
    for part in (p for p in key.split(".") if p):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(key)
        node = node[part]
    return node


def coerce(raw: Any) -> Any:
    """Turn a string argument into the JSON value it obviously is.

    ``"true"`` becomes ``True`` and ``"8"`` becomes ``8``, so a model that can
    only send strings still writes a boolean setting correctly.  Anything that
    is not valid JSON stays the string it was.
    """
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text:
        return raw
    try:
        return json.loads(text)
    except ValueError:
        return raw


__all__ = [
    "MASK",
    "SECRET_KEYS",
    "coerce",
    "deep_merge",
    "mask_secrets",
    "nest",
    "pluck",
]
