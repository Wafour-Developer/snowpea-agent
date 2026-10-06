"""Site memory (protocol 1.9.0, snowpea-browser addendum 20).

A real daemon over ``/ws``: browser host clients (one per profile) teach and
read entries, a UI client lists and deletes them, and the store's retention,
caps and eviction are exercised through the same RPCs.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from _support import connect
from test_host_tools import Daemon, daemon, open_client  # noqa: F401

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    CAPABILITIES,
    PROTOCOL_VERSION,
    SiteGetParams,
    SitePutParams,
)
from snowpea_core.server.site_handlers import site_get_handler, site_put_handler
from snowpea_core.session import site_memory as sm

A = "snowpea-browser-A"
B = "snowpea-browser-B"
ORIGIN = "https://www.example.com"


def entry(page_type: str = "search-results", url_pattern: str = "/search?*", **extra: Any) -> dict:
    base: dict[str, Any] = {
        "pageType": page_type,
        "urlPattern": url_pattern,
        "summary": "Product search results with filters on the left",
        "landmarks": ["banner", "search", "main", "navigation:Filters"],
        "actions": [
            {
                "name": "search",
                "kind": "fill",
                "locators": [
                    {"by": "role", "role": "searchbox", "name": "Search"},
                    {"by": "label", "value": "Search"},
                    {"by": "testid", "value": "search-input"},
                    {"by": "css", "value": "input[name=q]"},
                ],
                "loginField": False,
            }
        ],
        "flows": [{"name": "search", "steps": ["fill search", "press Enter"]}],
        "pitfalls": ["cookie banner: click 'Accept all' first"],
        "fingerprint": "ax1:3f9a0c12345678",
    }
    base.update(extra)
    return base


def code_of(frame: dict[str, Any]) -> str:
    assert frame.get("error"), frame
    return str(frame["error"]["data"]["code"])


def field_of(frame: dict[str, Any]) -> Any:
    details = frame["error"]["data"].get("details")
    if isinstance(details, dict):
        return details.get("field")
    return [".".join(str(p) for p in item["loc"]) for item in details]


async def put(client: Any, page: dict[str, Any], origin: str = ORIGIN) -> dict[str, Any]:
    return await client.ok("site.put", {"origin": origin, "entry": page})


async def test_hello_advertises_site_memory_at_1_9_0(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        hello = await client.ok(
            "system.hello",
            {"token": daemon.token, "clientVersion": "t", "protocolVersion": PROTOCOL_VERSION},
        )
        assert "siteMemory" in hello["capabilities"]
        assert PROTOCOL_VERSION == "1.9.0" and "siteMemory" in CAPABILITIES
    finally:
        await client.stop()


async def test_put_creates_then_updates_by_origin_and_page_type(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        first = await put(client, entry())
        assert first["created"] is True and first["entryId"].startswith("e_")
        [stored] = (await client.ok("site.get", {"origin": ORIGIN}))["entries"]
        assert stored["successCount"] == 1 and stored["failureCount"] == 0
        assert stored["stale"] is False and stored["origin"] == ORIGIN
        created_at = stored["createdAt"]

        await client.ok(
            "site.mark", {"origin": ORIGIN, "entryId": first["entryId"], "outcome": "stale"}
        )
        again = await put(client, entry(summary="Search results, new layout", fingerprint="ax1:ff"))
        assert again == {"entryId": first["entryId"], "created": False}
        [updated] = (await client.ok("site.get", {"origin": ORIGIN}))["entries"]
        # Core keeps createdAt / counts / entryId; the put replaces the rest,
        # clears stale and counts as a success.
        assert updated["createdAt"] == created_at
        assert updated["summary"] == "Search results, new layout"
        assert updated["fingerprint"] == "ax1:ff"
        assert updated["successCount"] == 2 and updated["failureCount"] == 1
        assert updated["stale"] is False

        # The origin is normalised: the same site in other case is the same entry.
        third = await put(client, entry(), origin="HTTPS://WWW.Example.com/")
        assert third["created"] is False
    finally:
        await client.stop()


async def test_get_orders_most_specific_first_and_filters(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        await put(client, entry("any-page", "/*"))
        await put(client, entry("product", "/product/*"))
        await put(client, entry("product-reviews", "/product/*/reviews"))
        await put(client, entry("search-results", "/search?*"))

        got = await client.ok("site.get", {"origin": ORIGIN, "path": "/product/42/reviews"})
        assert [e["pageType"] for e in got["entries"]] == ["product-reviews"]
        got = await client.ok("site.get", {"origin": ORIGIN, "path": "/product/42?ref=home"})
        assert [e["pageType"] for e in got["entries"]] == ["product"]
        # "/*" ignores the query, so it matches too, after the more specific one.
        got = await client.ok("site.get", {"origin": ORIGIN, "path": "/search?q=shoes"})
        assert [e["pageType"] for e in got["entries"]] == ["search-results", "any-page"]
        got = await client.ok("site.get", {"origin": ORIGIN, "path": "/cart"})
        assert [e["pageType"] for e in got["entries"]] == ["any-page"]

        everything = await client.ok("site.get", {"origin": ORIGIN})
        assert [e["pageType"] for e in everything["entries"]] == [
            "product-reviews",
            "product",
            "search-results",
            "any-page",
        ]
        one = await client.ok("site.get", {"origin": ORIGIN, "pageType": "product"})
        assert [e["pageType"] for e in one["entries"]] == ["product"]
        assert (await client.ok("site.get", {"origin": "https://other.test"}))["entries"] == []
    finally:
        await client.stop()


async def test_mark_counts_successes_and_failures_and_flags_stale(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        entry_id = (await put(client, entry()))["entryId"]
        marked = await client.ok(
            "site.mark",
            {"origin": ORIGIN, "entryId": entry_id, "outcome": "stale", "detail": "no match"},
        )
        assert marked["entry"]["stale"] is True and marked["entry"]["failureCount"] == 1
        # A stale entry is still returned, flagged, so the caller can say so.
        [listed] = (await client.ok("site.get", {"origin": ORIGIN}))["entries"]
        assert listed["stale"] is True

        before = marked["entry"]["lastVerified"]
        ok = await client.ok("site.mark", {"origin": ORIGIN, "entryId": entry_id, "outcome": "ok"})
        assert ok["entry"]["stale"] is False
        assert ok["entry"]["successCount"] == 2 and ok["entry"]["failureCount"] == 1
        assert ok["entry"]["lastVerified"] >= before

        missing = await client.call(
            "site.mark", {"origin": ORIGIN, "entryId": "e_nope", "outcome": "ok"}
        )
        assert code_of(missing) == errors.NOT_FOUND
        wrong_origin = await client.call(
            "site.mark", {"origin": "https://other.test", "entryId": entry_id, "outcome": "ok"}
        )
        assert code_of(wrong_origin) == errors.NOT_FOUND
        bad = await client.call(
            "site.mark", {"origin": ORIGIN, "entryId": entry_id, "outcome": "maybe"}
        )
        assert code_of(bad) == errors.INVALID_PARAMS
    finally:
        await client.stop()


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        # XPath: not a locator kind, and not smuggled in as CSS either.
        (
            lambda e: e["actions"][0]["locators"].append({"by": "xpath", "value": "//input"}),
            "entry.actions.0.locators.4.by",
        ),
        (
            lambda e: e["actions"][0]["locators"].__setitem__(
                3, {"by": "css", "value": "//input[@name='q']"}
            ),
            "entry.actions[0].locators[3].value",
        ),
        # More than 6 locators.
        (
            lambda e: e["actions"][0]["locators"].extend(
                [{"by": "text", "value": f"Search {i}"} for i in range(3)]
            ),
            "entry.actions.0.locators",
        ),
        # Only CSS locators.
        (
            lambda e: e["actions"][0].__setitem__(
                "locators", [{"by": "css", "value": "input[name=q]"}]
            ),
            "entry.actions[0].locators",
        ),
        # A login field carries locators only, never a value.
        (
            lambda e: e["actions"][0].update(loginField=True, value="hunter2"),
            "entry.actions.0.value",
        ),
        (
            lambda e: e["actions"][0].update(
                loginField=True,
                locators=[
                    {"by": "label", "value": "Password"},
                    {"by": "css", "value": "input[value=hunter2]"},
                ],
            ),
            "entry.actions[0].locators[1].value",
        ),
        # Schema details.
        (lambda e: e.update(pageType="Search Results"), "entry.pageType"),
        (lambda e: e.update(urlPattern="search"), "entry.urlPattern"),
        (lambda e: e.update(summary="line one\nline two"), "entry.summary"),
        (lambda e: e.update(landmarks=["x"] * 21), "entry.landmarks"),
        (lambda e: e.update(entryId="e_mine"), "entry.entryId"),
        (
            lambda e: e["actions"][0]["locators"].__setitem__(0, {"by": "role", "value": "x"}),
            "entry.actions[0].locators[0]",
        ),
    ],
)
async def test_schema_rejections_name_the_field(
    http: aiohttp.ClientSession, daemon: Daemon, mutate: Any, field: str  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        page = copy.deepcopy(entry())
        mutate(page)
        frame = await client.call("site.put", {"origin": ORIGIN, "entry": page})
        assert code_of(frame) == errors.INVALID_PARAMS
        found = field_of(frame)
        assert field in (found if isinstance(found, list) else [found]), found
        assert (await client.ok("site.get", {"origin": ORIGIN}))["entries"] == []
    finally:
        await client.stop()


@pytest.mark.parametrize(
    "text",
    [
        "Signed in as jane.doe@example.com",
        "Call +1 555 123 4567 for help",
        "Support 010-1234-5678",
        "Order 123456789 placed",
        "use password=hunter2 to log in",
        "api_key=abcdef",
        "Bearer abcdefghijklmnop",
        "token sk-ant-abcdef123456",
    ],
)
async def test_personal_data_and_credentials_are_refused_not_stripped(
    http: aiohttp.ClientSession, daemon: Daemon, text: str  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        frame = await client.call("site.put", {"origin": ORIGIN, "entry": entry(pitfalls=[text])})
        assert code_of(frame) == errors.INVALID_PARAMS
        assert field_of(frame) == "entry.pitfalls[0]"
        # The refusal never echoes the value back.
        assert text not in frame["error"]["message"]
        assert (await client.ok("site.get", {"origin": ORIGIN}))["entries"] == []
        # Ordinary login-page words are fine.
        await put(
            client,
            entry(
                "login",
                "/login",
                summary="Login page with email and password fields",
                pitfalls=["2FA code arrives by SMS"],
            ),
        )
    finally:
        await client.stop()


async def test_an_oversized_entry_is_refused(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        actions = [
            {
                "name": f"action-{i}",
                "kind": "click",
                "locators": [{"by": "text", "value": "x" * 200} for _ in range(6)],
            }
            for i in range(30)
        ]
        frame = await client.call("site.put", {"origin": ORIGIN, "entry": entry(actions=actions)})
        assert code_of(frame) == errors.INVALID_PARAMS and field_of(frame) == "entry"
        assert sm.MAX_ENTRY_BYTES == 16 * 1024
    finally:
        await client.stop()


async def test_thirty_entries_per_origin(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        assert sm.MAX_ENTRIES_PER_ORIGIN == 30
        for i in range(30):
            await put(client, entry(f"page-{i}", f"/p{i}"))
        frame = await client.call(
            "site.put", {"origin": ORIGIN, "entry": entry("page-30", "/p30")}
        )
        assert code_of(frame) == errors.INVALID_PARAMS and field_of(frame) == "entry.pageType"
        # Replacing an existing page type still works on a full origin.
        assert (await put(client, entry("page-3", "/p3")))["created"] is False
        listed = await client.ok("site.list", {})
        assert listed["sites"][0]["entries"] == 30
    finally:
        await client.stop()


async def test_past_the_origin_cap_the_least_recently_verified_origin_goes(
    http: aiohttp.ClientSession, daemon: Daemon, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    assert sm.MAX_ORIGINS_PER_PROFILE == 1000
    monkeypatch.setattr(sm, "MAX_ORIGINS_PER_PROFILE", 3)
    client = await open_client(http, daemon, client_id=A)
    other = await open_client(http, daemon, client_id=B)
    try:
        now = datetime.now(UTC)
        for days, name in ((5, "old"), (1, "new"), (3, "mid")):
            when = sm.iso(now - timedelta(days=days))
            await put(client, entry(lastVerified=when), origin=f"https://{name}.test")
        await put(other, entry(), origin="https://b-only.test")
        await put(client, entry(), origin="https://fourth.test")

        sites = [s["origin"] for s in (await client.ok("site.list", {}))["sites"]]
        assert sites == ["https://fourth.test", "https://mid.test", "https://new.test"]
        # The cap is per profile: B's origin was neither counted nor evicted.
        b_sites = [s["origin"] for s in (await other.ok("site.list", {}))["sites"]]
        assert b_sites == ["https://b-only.test"]
    finally:
        await client.stop()
        await other.stop()


async def test_retention_drops_unverified_and_failing_stale_entries(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        now = datetime.now(UTC)
        await put(client, entry("old", "/old", lastVerified=sm.iso(now - timedelta(days=91))))
        await put(client, entry("recent", "/recent", lastVerified=sm.iso(now - timedelta(days=89))))
        failing = (await put(client, entry("failing", "/failing")))["entryId"]
        young = (await put(client, entry("young", "/young")))["entryId"]
        for entry_id in (failing, failing, failing, young, young, young):
            await client.ok(
                "site.mark", {"origin": ORIGIN, "entryId": entry_id, "outcome": "stale"}
            )
        store = daemon.core.store
        # "failing" last succeeded 31 days ago; "young" just now.
        store._execute(
            "UPDATE site_entries SET last_success = ? WHERE entry_id = ?",
            (sm.iso(now - timedelta(days=31)), failing),
        )
        got = await client.ok("site.get", {"origin": ORIGIN})
        names = [e["pageType"] for e in got["entries"]]
        assert sorted(names) == ["recent", "young"]
    finally:
        await client.stop()


async def test_who_may_call(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    host_a = await open_client(http, daemon, client_id=A)
    host_b = await open_client(http, daemon, client_id=B)
    no_id = await open_client(http, daemon)
    ui = await connect(http, daemon)
    try:
        entry_id = (await put(host_a, entry()))["entryId"]

        # Another profile sees nothing, cannot mark or delete A's entry.
        assert (await host_b.ok("site.get", {"origin": ORIGIN}))["entries"] == []
        frame = await host_b.call(
            "site.mark", {"origin": ORIGIN, "entryId": entry_id, "outcome": "stale"}
        )
        assert code_of(frame) == errors.NOT_FOUND
        assert (await host_b.ok("site.list", {}))["sites"] == []
        frame = await host_b.call("site.list", {"hostToolsFrom": A})
        assert code_of(frame) == errors.UNAUTHORIZED
        frame = await host_b.call("site.delete", {"origin": ORIGIN, "hostToolsFrom": A})
        assert code_of(frame) == errors.UNAUTHORIZED
        assert (await host_b.ok("site.delete", {"origin": ORIGIN}))["deleted"] == 0

        # The UI client may list and delete a named profile, never get/put/mark.
        for method, params in (
            ("site.get", {"origin": ORIGIN}),
            ("site.put", {"origin": ORIGIN, "entry": entry()}),
            ("site.mark", {"origin": ORIGIN, "entryId": entry_id, "outcome": "ok"}),
        ):
            assert code_of(await ui.call(method, params)) == errors.UNAUTHORIZED
            assert code_of(await no_id.call(method, params)) == errors.UNAUTHORIZED
        frame = await ui.call("site.list", {})
        assert code_of(frame) == errors.INVALID_PARAMS and field_of(frame) == "hostToolsFrom"
        listed = await ui.ok("site.list", {"hostToolsFrom": A})
        assert [(s["origin"], s["entries"], s["stale"]) for s in listed["sites"]] == [
            (ORIGIN, 1, 0)
        ]
        assert (await ui.ok("site.list", {"hostToolsFrom": B}))["sites"] == []
        deleted = await ui.ok(
            "site.delete", {"origin": ORIGIN, "entryId": entry_id, "hostToolsFrom": A}
        )
        assert deleted == {"deleted": 1}
        assert (await host_a.ok("site.get", {"origin": ORIGIN}))["entries"] == []
    finally:
        for client in (host_a, host_b, no_id, ui):
            await client.stop()


async def test_a_chat_gateway_is_refused(daemon: Daemon) -> None:  # noqa: F811
    gateway = SimpleNamespace(surface_id="gateway:telegram:1", client_kind="browser", client_id=A)
    for handler, params in (
        (site_get_handler, SiteGetParams(origin=ORIGIN)),
        (site_put_handler, SitePutParams.model_validate({"origin": ORIGIN, "entry": entry()})),
    ):
        with pytest.raises(RpcError) as caught:
            await handler(gateway, params, daemon.core)  # type: ignore[arg-type]
        assert caught.value.code == errors.UNAUTHORIZED


async def test_list_pages_and_delete_all_of_an_origin(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        for name in ("a", "b", "c"):
            await put(client, entry(), origin=f"https://{name}.test")
            await put(client, entry("login", "/login"), origin=f"https://{name}.test")
        b_ids = [
            e["entryId"]
            for e in (await client.ok("site.get", {"origin": "https://b.test"}))["entries"]
        ]
        await client.ok(
            "site.mark", {"origin": "https://b.test", "entryId": b_ids[0], "outcome": "stale"}
        )
        await client.ok(
            "site.mark", {"origin": "https://b.test", "entryId": b_ids[1], "outcome": "ok"}
        )
        first = await client.ok("site.list", {"limit": 2})
        row_b = first["sites"][1]
        assert row_b["pageTypes"] == ["login", "search-results"]
        # Two puts and one 'ok' mark; one 'stale' mark.
        assert (row_b["successCount"], row_b["failureCount"], row_b["stale"]) == (3, 1, 1)
        row_a = first["sites"][0]
        assert (row_a["successCount"], row_a["failureCount"], row_a["stale"]) == (2, 0, 0)
        assert [s["origin"] for s in first["sites"]] == ["https://a.test", "https://b.test"]
        second = await client.ok("site.list", {"limit": 2, "cursor": first["cursor"]})
        assert [s["origin"] for s in second["sites"]] == ["https://c.test"]
        assert second.get("cursor") is None
        assert (await client.ok("site.delete", {"origin": "https://b.test"}))["deleted"] == 2
        only = await client.ok("site.list", {"origin": "https://b.test"})
        assert only["sites"] == []
        bad = await client.call("site.get", {"origin": "https://a.test/path"})
        assert code_of(bad) == errors.INVALID_PARAMS and field_of(bad) == "origin"
    finally:
        await client.stop()


def test_entries_persist_in_state_db(tmp_path: Path) -> None:
    from snowpea_core.session.store import Store

    store = Store(tmp_path / "state.db")
    try:
        names = {r["name"] for r in store._query("PRAGMA table_info(site_entries)")}
        assert {"profile", "origin", "page_type", "entry_json", "last_verified"} <= names
    finally:
        store.close()
    # Reopening an existing state.db adds nothing twice.
    Store(tmp_path / "state.db").close()


async def test_turning_site_memory_off_keeps_only_list_and_delete(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        assert daemon.core.settings.browser.siteMemory is True  # on by default
        entry_id = (await put(client, entry()))["entryId"]
        await client.ok(
            "settings.set", {"scope": "global", "patch": {"browser": {"siteMemory": False}}}
        )
        assert daemon.core.settings.browser.siteMemory is False
        for method, params in (
            ("site.get", {"origin": ORIGIN}),
            ("site.put", {"origin": ORIGIN, "entry": entry("login", "/login")}),
            ("site.mark", {"origin": ORIGIN, "entryId": entry_id, "outcome": "ok"}),
        ):
            frame = await client.call(method, params)
            assert code_of(frame) == errors.TOOL_INACTIVE, method
            assert frame["error"]["message"] == "site memory is turned off"
        # The settings UI can still see and clear what was remembered.
        listed = await client.ok("site.list", {})
        assert [(s["origin"], s["entries"]) for s in listed["sites"]] == [(ORIGIN, 1)]
        assert (await client.ok("site.delete", {"origin": ORIGIN}))["deleted"] == 1

        await client.ok(
            "settings.set", {"scope": "global", "patch": {"browser": {"siteMemory": True}}}
        )
        assert (await put(client, entry()))["created"] is True
        assert len((await client.ok("site.get", {"origin": ORIGIN}))["entries"]) == 1
    finally:
        await client.stop()
