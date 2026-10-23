"""Addendum 27, C3: honest site counts and ``site.stats``.

Every ``site.put`` used to add to ``successCount``, so a map that never
worked looked used.  Now only ``site.mark ok`` does, and ``site.stats`` counts
each put, mark and markPart per origin and day for the profile that made it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from _support import connect
from test_host_tools import Daemon, daemon, open_client  # noqa: F401
from test_site_memory import ORIGIN, entry, put
from test_site_recipes import recipe

from snowpea_core.server import errors

pytestmark = pytest.mark.asyncio

A = "snowpea-browser-A"
B = "snowpea-browser-B"


def _recipe_entry() -> dict:
    return entry(recipes=[recipe("price")])


async def test_stats_count_every_call_kind(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id=A)
    try:
        entry_id = (await put(host, _recipe_entry()))["entryId"]
        await put(host, _recipe_entry())
        mark = {"origin": ORIGIN, "entryId": entry_id}
        await host.ok("site.mark", {**mark, "outcome": "ok"})
        await host.ok("site.mark", {**mark, "outcome": "ok"})
        await host.ok("site.mark", {**mark, "outcome": "stale"})
        part = {**mark, "part": "flow", "name": "search"}
        await host.ok("site.markPart", {**part, "outcome": "ok"})
        await host.ok("site.markPart", {**part, "outcome": "miss"})
        recipe = {**mark, "part": "recipe", "name": "price"}
        await host.ok("site.markPart", {**recipe, "outcome": "ok"})
        await host.ok("site.markPart", {**recipe, "outcome": "miss"})
        await host.ok("site.markPart", {**recipe, "outcome": "stale"})

        [stats] = (await host.ok("site.stats", {}))["origins"]
        assert stats == {
            "origin": ORIGIN,
            "locateOk": 2,
            "locateMiss": 1,
            "runOk": 1,
            "runMiss": 1,
            "readOk": 1,
            "readMiss": 2,
            "puts": 2,
            "staleMarks": 2,
        }
        # The entry's own count is the two 'ok' marks, not the puts.
        [stored] = (await host.ok("site.get", {"origin": ORIGIN}))["entries"]
        assert stored["successCount"] == 2

        # Another profile sees none of it.
        other = await open_client(http, daemon, client_id=B)
        try:
            assert (await other.ok("site.stats", {}))["origins"] == []
        finally:
            await other.stop()

        # Forgetting the site forgets its counts.
        await host.ok("site.delete", {"origin": ORIGIN})
        assert (await host.ok("site.stats", {}))["origins"] == []
    finally:
        await host.stop()


async def test_days_limits_the_window(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
) -> None:
    store = daemon.core.store
    today = datetime.now(UTC).date()
    await store.site_count(A, ORIGIN, (today - timedelta(days=40)).isoformat(), "puts")
    await store.site_count(A, ORIGIN, (today - timedelta(days=6)).isoformat(), "puts")
    await store.site_count(A, ORIGIN, today.isoformat(), "puts", "locateOk")
    await store.site_count(A, "https://other.test", today.isoformat(), "puts")
    host = await open_client(http, daemon, client_id=A)
    try:
        month = (await host.ok("site.stats", {"origin": ORIGIN}))["origins"]
        assert [(s["origin"], s["puts"], s["locateOk"]) for s in month] == [(ORIGIN, 2, 1)]
        week = (await host.ok("site.stats", {"origin": ORIGIN, "days": 7}))["origins"]
        assert week[0]["puts"] == 2
        today_only = (await host.ok("site.stats", {"origin": ORIGIN, "days": 1}))["origins"]
        assert today_only[0]["puts"] == 1
        everything = (await host.ok("site.stats", {"days": 90}))["origins"]
        assert [(s["origin"], s["puts"]) for s in everything] == [
            ("https://other.test", 1),
            (ORIGIN, 3),
        ]
    finally:
        await host.stop()


async def test_only_browser_clients_read_stats(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
) -> None:
    plain = await connect(http, daemon)
    try:
        frame = await plain.call("site.stats", {})
        assert frame["error"]["data"]["code"] == errors.UNAUTHORIZED
    finally:
        await plain.stop()
