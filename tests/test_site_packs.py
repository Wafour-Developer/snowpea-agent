"""Addendum 27, C5: signed, read-only site-memory packs.

A plugin's ``sites/*.json`` with a detached Ed25519 ``.sig`` is a read-only
layer under the profile's learned entries: ``site.get`` returns a pack entry
when nothing learned has its pageType, a learned one shadows it, a bad
signature is ignored, and ``site.put`` never edits a pack entry.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from test_host_tools import Daemon, daemon, open_client  # noqa: F401
from test_site_memory import ORIGIN, code_of, entry, put

from snowpea_core.server import errors
from snowpea_core.session import site_packs

A = "snowpea-browser-A"


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setattr(site_packs, "PACK_KEYS", (public,))
    return private


def write_pack(
    daemon: Daemon,  # noqa: F811
    key: Ed25519PrivateKey | None,
    entries: list[dict[str, Any]],
    *,
    version: str = "2026.10.23",
    name: str = "example",
    source: str = "pack",
) -> Path:
    sites = Path(daemon.core.paths.home) / "plugins" / "snowpea-browser" / "sites"
    sites.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "packVersion": version,
            "origin": ORIGIN,
            "source": source,
            "publishedAt": "2026-10-20T00:00:00Z",
            "entries": entries,
        }
    ).encode()
    path = sites / f"{name}.json"
    path.write_bytes(body)
    signer = key or Ed25519PrivateKey.generate()
    path.with_name(path.name + ".sig").write_text(base64.b64encode(signer.sign(body)).decode())
    return path


async def _reload(daemon: Daemon) -> None:  # noqa: F811
    await daemon.core.skills.reload()


def test_the_release_key_is_the_browsers() -> None:
    import hashlib

    [release] = site_packs.PACK_KEYS
    assert hashlib.sha256(release).hexdigest().startswith("7ceb89aac03f6ed5")


async def test_a_pack_entry_serves_until_a_learned_one_shadows_it(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    key: Ed25519PrivateKey,
) -> None:
    write_pack(daemon, key, [entry(), entry("product", "/vp/products/*")])
    await _reload(daemon)
    client = await open_client(http, daemon, client_id=A)
    try:
        got = (await client.ok("site.get", {"origin": ORIGIN}))["entries"]
        assert {e["pageType"]: e["source"] for e in got} == {
            "search-results": "pack",
            "product": "pack",
        }
        assert {e["packVersion"] for e in got} == {"2026.10.23"}
        pack_id = next(e["entryId"] for e in got if e["pageType"] == "search-results")
        assert pack_id.startswith("p_")

        # A put for that pageType makes a learned entry; the pack stays as shipped.
        learned = await put(client, entry(summary="learned map"))
        assert learned["created"] is True and learned["entryId"].startswith("e_")
        got = (await client.ok("site.get", {"origin": ORIGIN}))["entries"]
        by_type = {e["pageType"]: e for e in got}
        assert by_type["search-results"]["source"] == "learned"
        assert by_type["search-results"]["summary"] == "learned map"
        assert by_type["product"]["source"] == "pack"
        assert site_packs.packs_for(daemon.core).entry(ORIGIN, pack_id)["summary"] != (
            "learned map"
        )

        # Marking a pack entry counts it but changes nothing stored.
        product = by_type["product"]["entryId"]
        marked = await client.ok(
            "site.mark", {"origin": ORIGIN, "entryId": product, "outcome": "ok"}
        )
        assert marked["entry"]["source"] == "pack" and marked["entry"]["successCount"] == 0
        [stats] = (await client.ok("site.stats", {}))["origins"]
        assert stats["locateOk"] == 1

        # site.list says how many of an origin's entries come from packs.
        [summary] = (await client.ok("site.list", {}))["sites"]
        assert summary["packEntries"] == 1

        # Forgetting the site forgets what was learned, not the pack.
        await client.ok("site.delete", {"origin": ORIGIN})
        got = (await client.ok("site.get", {"origin": ORIGIN}))["entries"]
        assert {e["source"] for e in got} == {"pack"} and len(got) == 2
    finally:
        await client.stop()


async def test_a_bad_signature_is_ignored_with_a_log_line(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    key: Ed25519PrivateKey,
    caplog: pytest.LogCaptureFixture,
) -> None:
    write_pack(daemon, None, [entry()], name="forged")
    unsigned = write_pack(daemon, key, [entry("product", "/p/*")], name="unsigned")
    unsigned.with_name(unsigned.name + ".sig").unlink()
    await _reload(daemon)
    client = await open_client(http, daemon, client_id=A)
    try:
        with caplog.at_level(logging.WARNING, logger="snowpea.site_packs"):
            got = (await client.ok("site.get", {"origin": ORIGIN}))["entries"]
        assert got == []
        assert "forged.json: missing or bad signature" in caplog.text
        assert "unsigned.json: missing or bad signature" in caplog.text
    finally:
        await client.stop()


async def test_a_newer_pack_version_replaces_the_older(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    key: Ed25519PrivateKey,
) -> None:
    write_pack(daemon, key, [entry(summary="old")], version="2026.10.9", name="a")
    write_pack(daemon, key, [entry(summary="new")], version="2026.10.23", name="b")
    await _reload(daemon)
    client = await open_client(http, daemon, client_id=A)
    try:
        [got] = (await client.ok("site.get", {"origin": ORIGIN}))["entries"]
        assert (got["summary"], got["packVersion"]) == ("new", "2026.10.23")
    finally:
        await client.stop()


async def test_a_pack_entry_with_a_value_is_refused(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    key: Ed25519PrivateKey,
) -> None:
    write_pack(daemon, key, [entry(summary="call 010-1234-5678"), entry("product", "/p/*")])
    await _reload(daemon)
    client = await open_client(http, daemon, client_id=A)
    try:
        got = (await client.ok("site.get", {"origin": ORIGIN}))["entries"]
        assert [e["pageType"] for e in got] == ["product"]
        frame = await client.call(
            "site.mark", {"origin": ORIGIN, "entryId": "p_0000000000000000", "outcome": "ok"}
        )
        assert code_of(frame) == errors.NOT_FOUND
    finally:
        await client.stop()
