"""Signed, read-only site-memory packs (snowpea-browser addendum 27, C5).

A plugin ships curated site maps as ``<plugin>/sites/<name>.json``, each with
a detached ``<name>.json.sig``: base64 of the Ed25519 signature over the JSON
file's exact bytes, made with the browser's release-feed key (the same
signing as ``latest.json.sig``).  A file whose signature does not verify
against :data:`PACK_KEYS` is ignored with a log line.

The JSON is one origin's pack::

    {"kind": "site-pack", "packVersion": "2026.10.23",
     "origin": "https://www.example.com",
     "source": "pack", "publishedAt": "2026-10-23T00:00:00Z",
     "entries": [<SiteEntryInput>, ...]}

``kind`` must be ``"site-pack"``, so nothing else the key signs (the update
feed) reads as a pack.  ``source`` is ``"pack"`` (curated, the default) or ``"shared"`` (opt-in maps
published through the same signed channel).  Every entry passes the same
checks as ``site.put``.  Pack entries are never stored: ``site.get`` lays
them under the profile's learned entries, a learned entry wins for its
pageType, retention never sees them, and ``site.put`` only writes learned
ones.  Two packs for one origin: the higher ``packVersion`` replaces the
other (on a tie, the later path).  The directories are looked at again at
most every :data:`RESCAN_SEC` seconds, or at once when the plugin set changes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from snowpea_core.server.protocol import SiteEntryInput
from snowpea_core.session import site_memory as sm

log = logging.getLogger("snowpea.site_packs")

#: The release feed's Ed25519 public key (snowpea-browser
#: ``update_signing_key.h``; SHA-256 fingerprint 7ceb89aa…3443fb6e7).
PACK_KEYS: tuple[bytes, ...] = (
    bytes.fromhex("b74892dd6cdf6599fbfdaf151eeed31f27664d8a603f671796aa0f98ead7f040"),
)

#: A pack file larger than this is not read.
MAX_PACK_BYTES = 1024 * 1024

PACK_SOURCES = ("pack", "shared")

#: How often ``site.get`` may look for changed pack files.
RESCAN_SEC = 2.0

_VERSION_PART = re.compile(r"\d+|[^\d]+")


def entry_id(origin: str, page_type: str) -> str:
    """A pack entry's id: stable across versions, never a learned ``e_`` id."""
    digest = hashlib.sha256(f"{origin}\n{page_type}".encode()).hexdigest()[:16]
    return f"p_{digest}"


def is_pack_id(value: str) -> bool:
    return value.startswith("p_")


def version_key(version: str) -> tuple[Any, ...]:
    """``"2026.10.9"`` < ``"2026.10.23"``: numbers compare as numbers."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in _VERSION_PART.findall(version)
    )


def verify(data: bytes, signature: bytes, keys: Iterable[bytes]) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    for key in keys:
        try:
            Ed25519PublicKey.from_public_bytes(key).verify(signature, data)
        except (InvalidSignature, ValueError):
            continue
        return True
    return False


def _read_signature(path: Path) -> bytes | None:
    try:
        return base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
    except (OSError, UnicodeDecodeError, binascii.Error):
        return None


def load_pack(path: Path, keys: Iterable[bytes]) -> tuple[str, str, list[dict[str, Any]]] | None:
    """``(origin, packVersion, entries)`` of one signed pack file, or ``None``."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_PACK_BYTES + 1)
        mtime = path.stat().st_mtime
    except OSError as exc:
        log.warning("site pack %s: unreadable (%s); ignored", path, exc)
        return None
    if len(data) > MAX_PACK_BYTES:
        log.warning("site pack %s: larger than %d bytes; ignored", path, MAX_PACK_BYTES)
        return None
    signature = _read_signature(path.with_name(path.name + ".sig"))
    if signature is None or not verify(data, signature, keys):
        log.warning("site pack %s: missing or bad signature; ignored", path)
        return None
    try:
        pack = json.loads(data)
        if pack.get("kind") != "site-pack":
            raise ValueError('kind is not "site-pack"')
        origin = sm.normalize_origin(str(pack["origin"]))
        version = str(pack["packVersion"]).strip()
        source = str(pack.get("source") or "pack")
        raw_entries = pack.get("entries")
    except (ValueError, KeyError, TypeError, AttributeError, sm.SiteMemoryError) as exc:
        log.warning("site pack %s: not a pack (%s); ignored", path, exc)
        return None
    if not version or len(version) > 40 or source not in PACK_SOURCES:
        log.warning("site pack %s: bad packVersion or source; ignored", path)
        return None
    if not isinstance(raw_entries, list):
        log.warning("site pack %s: entries is not a list; ignored", path)
        return None
    published = str(pack.get("publishedAt") or "")
    try:
        when = sm.iso(min(sm.parse_time(published, "publishedAt"), datetime.now(UTC)))
    except sm.SiteMemoryError:
        when = sm.iso(datetime.fromtimestamp(mtime, UTC))
    entries: list[dict[str, Any]] = []
    page_types: set[str] = set()
    for index, raw in enumerate(raw_entries[: sm.MAX_ENTRIES_PER_ORIGIN]):
        try:
            entry = SiteEntryInput.model_validate(raw).model_dump(mode="json")
            sm.check_entry(entry)
        except (ValidationError, sm.SiteMemoryError) as exc:
            log.warning("site pack %s: entry %d refused (%s); skipped", path, index, exc)
            continue
        if entry["pageType"] in page_types:
            continue
        page_types.add(entry["pageType"])
        entry.update(
            {
                "entryId": entry_id(origin, entry["pageType"]),
                "origin": origin,
                "createdAt": when,
                "lastVerified": when,
                "successCount": 0,
                "failureCount": 0,
                "stale": False,
                "source": source,
                "packVersion": version,
            }
        )
        try:
            sm.check_size(entry)
        except sm.SiteMemoryError as exc:
            log.warning("site pack %s: entry %d refused (%s); skipped", path, index, exc)
            continue
        entries.append(entry)
    return origin, version, entries


class SitePacks:
    """The pack layer: every signed pack under the given ``sites`` directories.

    :meth:`refresh` rereads only when a file was added, removed or changed.
    """

    def __init__(self, keys: Iterable[bytes] | None = None) -> None:
        self.keys = tuple(PACK_KEYS if keys is None else keys)
        self._stamp: tuple[tuple[str, int, int], ...] | None = None
        self._dirs: tuple[str, ...] | None = None
        self._checked = 0.0
        self._by_origin: dict[str, tuple[str, list[dict[str, Any]]]] = {}

    @staticmethod
    def _files(directories: Iterable[Path]) -> list[Path]:
        found: list[Path] = []
        for directory in directories:
            try:
                found.extend(sorted(directory.glob("*.json")))
            except OSError:
                continue
        return found

    def refresh(self, directories: Iterable[Path]) -> None:
        directories = list(directories)
        dirs = tuple(str(directory) for directory in directories)
        now = time.monotonic()
        if dirs == self._dirs and now - self._checked < RESCAN_SEC:
            return
        self._dirs, self._checked = dirs, now
        files = self._files(directories)
        stamp: list[tuple[str, int, int]] = []
        for path in files:
            for part in (path, path.with_name(path.name + ".sig")):
                try:
                    info = part.stat()
                except OSError:
                    stamp.append((str(part), -1, -1))
                    continue
                stamp.append((str(part), info.st_mtime_ns, info.st_size))
        if tuple(stamp) == self._stamp:
            return
        by_origin: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        # Sorted by path, so on a packVersion tie the later path wins whatever
        # order the plugins load in.
        for path in sorted(files, key=str):
            loaded = load_pack(path, self.keys)
            if loaded is None:
                continue
            origin, version, entries = loaded
            held = by_origin.get(origin)
            if held is None or version_key(version) >= version_key(held[0]):
                by_origin[origin] = (version, entries)
        self._by_origin = by_origin
        self._stamp = tuple(stamp)
        if by_origin:
            log.info("site packs: %d origin%s", len(by_origin), "" if len(by_origin) == 1 else "s")

    def entries(self, origin: str) -> list[dict[str, Any]]:
        held = self._by_origin.get(origin)
        return [dict(entry) for entry in held[1]] if held else []

    def entry(self, origin: str, entry_id_: str) -> dict[str, Any] | None:
        return next((e for e in self.entries(origin) if e["entryId"] == entry_id_), None)


def plugin_site_dirs(core: Any) -> list[Path]:
    """``<plugin>/sites`` of every plugin the skill loader has loaded."""
    loader = getattr(core, "skills", None)
    plugins = getattr(loader, "plugins", None) or []
    return [Path(plugin.root) / "sites" for plugin in plugins]


def packs_for(core: Any) -> SitePacks:
    """The daemon's pack layer, refreshed from the loaded plugins."""
    packs = getattr(core, "site_packs", None)
    if packs is None:
        packs = SitePacks()
        core.site_packs = packs
    packs.refresh(plugin_site_dirs(core))
    return packs


__all__ = [
    "PACK_KEYS",
    "SitePacks",
    "entry_id",
    "is_pack_id",
    "load_pack",
    "packs_for",
    "plugin_site_dirs",
    "verify",
    "version_key",
]
