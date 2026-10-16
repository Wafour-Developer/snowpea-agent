"""Typed flows and recipes in site memory (protocol 1.15.0, snowpea-browser addendum 24).

Flows gain typed steps (verb + action + intent, never a value), an ``expect``
and their own ``stats``; recipes ("readers") get a list of their own.  Core
refuses value selectors, XPath, attributes off the allowlist and any fill or
select on a login field, and ``site.markPart`` counts a use without touching
the entry's verification.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import aiohttp
import pytest
from _support import connect
from test_host_tools import Daemon, daemon, open_client  # noqa: F401
from test_site_memory import ORIGIN, A, code_of, entry, field_of, put

from snowpea_core.server import errors
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION
from snowpea_core.session import site_memory as sm

LOGIN = {
    "name": "username",
    "kind": "fill",
    "locators": [{"by": "label", "value": "User name"}],
    "loginField": True,
}
CLICK = {"name": "go", "kind": "click", "locators": [{"by": "text", "value": "Search"}]}
CHOICE = {"name": "sort", "kind": "select", "locators": [{"by": "label", "value": "Sort by"}]}


def recipe(name: str = "products", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "intent": "product cards of the search results",
        "item": "li.ProductUnit",
        "fields": [
            {"name": "name", "selector": ".name"},
            {"name": "price", "selector": "strong.price-value"},
            {"name": "link", "selector": "a.product-link", "attr": "href"},
            {"name": "whole", "selector": ""},
        ],
        "stats": {"hits": 3, "misses": 0, "day": 20742},
        "stale": False,
    }
    base.update(extra)
    return base


def typed_entry(**extra: Any) -> dict[str, Any]:
    page = entry()
    page["actions"] = [*page["actions"], CLICK, CHOICE, LOGIN]
    page["flows"] = [
        {
            "name": "search",
            "steps": [
                {"verb": "fill", "action": "search", "intent": "type the product to look for"},
                {"verb": "select", "action": "sort"},
                {"verb": "click", "action": "go", "intent": "run the search"},
                {"verb": "enter", "intent": "submit the focused field"},
                {"verb": "enter", "action": "search"},
            ],
            "expect": "page",
            "stats": {"ok": 3, "missed": 1, "lastRun": "2026-10-16T03:00:00.000Z"},
        },
        {"name": "legacy", "steps": ["fill search", "press Enter"]},
    ]
    page["recipes"] = [recipe()]
    page.update(extra)
    return page


def test_capability_and_version() -> None:
    assert "siteRecipes" in CAPABILITIES
    assert tuple(map(int, PROTOCOL_VERSION.split("."))) >= (1, 15, 0)


async def test_a_typed_entry_round_trips(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        sent = typed_entry()
        await put(client, sent)
        [got] = (await client.ok("site.get", {"origin": ORIGIN}))["entries"]
        steps = got["flows"][0]["steps"]
        assert [step["verb"] for step in steps] == ["fill", "select", "click", "enter", "enter"]
        assert steps[0]["intent"] == "type the product to look for"
        assert steps[3]["action"] is None and steps[4]["action"] == "search"
        assert got["flows"][0]["expect"] == "page"
        assert got["flows"][0]["stats"] == {
            "ok": 3, "missed": 1, "lastRun": "2026-10-16T03:00:00.000Z"
        }
        assert got["flows"][1]["steps"] == ["fill search", "press Enter"]
        [reader] = got["recipes"]
        assert reader["item"] == "li.ProductUnit" and reader["fields"][2]["attr"] == "href"
        assert reader["stats"] == {"hits": 3, "misses": 0, "day": 20742}
    finally:
        await client.stop()


FIRST_SELECTOR = "entry.recipes[0].fields[0].selector"
THIRTEEN_FIELDS = [{"name": f"f{i}", "selector": ".x"} for i in range(13)]


def _set(path: str, value: Any):  # type: ignore[no-untyped-def]
    def mutate(page: dict[str, Any]) -> None:
        target: Any = page
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[int(part)] if part.isdigit() else target[part]
        last = parts[-1]
        if last.isdigit():
            target[int(last)] = value
        else:
            target[last] = value

    return mutate


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (_set("recipes.0.item", "li[value]"), "entry.recipes[0].item"),
        (_set("recipes.0.fields.0.selector", 'input[value="x"]'), FIRST_SELECTOR),
        (_set("recipes.0.fields.0.selector", "a[VALUE^=a]"), FIRST_SELECTOR),
        (_set("recipes.0.item", "//li[@class='x']"), "entry.recipes[0].item"),
        (_set("recipes.0.fields.0.attr", "value"), "entry.recipes.0.fields.0.attr"),
        (_set("recipes.0.fields.0.attr", "onclick"), "entry.recipes.0.fields.0.attr"),
        (_set("flows.0.steps.0.action", "username"), "entry.flows[0].steps[0].action"),
        (
            _set("flows.0.steps.1", {"verb": "select", "action": "username"}),
            "entry.flows[0].steps[1].action",
        ),
        (_set("flows.0.steps.0.action", "nowhere"), "entry.flows[0].steps[0].action"),
        (_set("flows.0.steps.2", {"verb": "click"}), "entry.flows[0].steps[2].action"),
        (
            _set("flows.0.steps.0", {"verb": "fill", "action": "search", "text": "socks"}),
            "entry.flows.0.steps.0.SiteFlowStep.text",
        ),
        (_set("recipes", [recipe(), recipe()]), "entry.recipes[1].name"),
        (
            _set("recipes", [recipe(fields=[{"name": "a", "selector": ".a"}] * 2)]),
            "entry.recipes[0].fields[1].name",
        ),
        (_set("recipes", [recipe(f"r{i}") for i in range(9)]), "entry.recipes"),
        (
            _set("recipes", [recipe(fields=THIRTEEN_FIELDS)]),
            "entry.recipes.0.fields",
        ),
        (_set("flows.0.steps", [{"verb": "enter"}] * 17), "entry.flows.0.steps"),
        (_set("flows.0.stats", {"ok": -1, "missed": 0}), "entry.flows.0.stats.ok"),
        (_set("flows.0.stats", {"ok": 1.5, "missed": 0}), "entry.flows.0.stats.ok"),
        (_set("recipes.0.stats", {"hits": "3", "misses": 0}), "entry.recipes.0.stats.hits"),
    ],
)
async def test_refusals_name_the_field(
    http: aiohttp.ClientSession, daemon: Daemon, mutate: Any, field: str  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        page = copy.deepcopy(typed_entry())
        mutate(page)
        frame = await client.call("site.put", {"origin": ORIGIN, "entry": page})
        assert code_of(frame) == errors.INVALID_PARAMS
        found = field_of(frame)
        assert field in (found if isinstance(found, list) else [found]), found
        assert (await client.ok("site.get", {"origin": ORIGIN}))["entries"] == []
    finally:
        await client.stop()


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (_set("recipes.0.intent", "rows for jane.doe@example.com"), "entry.recipes[0].intent"),
        (_set("recipes.0.fields.0.selector", ".row-123456789"), FIRST_SELECTOR),
        (_set("recipes.0.item", "li\x07.x"), "entry.recipes[0].item"),
        (
            _set("flows.0.steps.0.intent", "log in with password=hunter2"),
            "entry.flows[0].steps[0].intent",
        ),
    ],
)
async def test_content_checks_reach_the_new_strings(
    http: aiohttp.ClientSession, daemon: Daemon, mutate: Any, field: str  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        page = copy.deepcopy(typed_entry())
        mutate(page)
        frame = await client.call("site.put", {"origin": ORIGIN, "entry": page})
        assert code_of(frame) == errors.INVALID_PARAMS and field_of(frame) == field
    finally:
        await client.stop()


def _sized(length: int) -> dict[str, Any]:
    """The typed entry plus 26 click actions of 4 text locators ``length`` chars long."""
    base = typed_entry()
    extra = [
        {
            "name": f"action-{i}",
            "kind": "click",
            "locators": [{"by": "text", "value": "x" * length} for _ in range(4)],
        }
        for i in range(26)
    ]
    return typed_entry(actions=[*base["actions"], *extra])


def _stored_size(page: dict[str, Any]) -> int:
    """What the put will store: the dump plus core's own fields (see site_put_handler)."""
    from snowpea_core.server.protocol import SiteEntryInput

    now = sm.iso(datetime.now(UTC))
    data = SiteEntryInput.model_validate(page).model_dump(mode="json")
    return sm.entry_size(
        {
            **data,
            "entryId": "e_" + "0" * 16,
            "origin": ORIGIN,
            "createdAt": now,
            "lastVerified": now,
            "successCount": 1,
            "failureCount": 0,
            "stale": False,
        }
    )


async def test_the_limit_is_24_kib(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    length = next(n for n in range(200, 0, -1) if _stored_size(_sized(n)) <= sm.MAX_ENTRY_BYTES)
    assert sm.MAX_ENTRY_BYTES - 200 < _stored_size(_sized(length)) <= 24 * 1024
    assert _stored_size(_sized(length + 1)) > 24 * 1024
    client = await open_client(http, daemon, client_id=A)
    try:
        await put(client, _sized(length))
        frame = await client.call(
            "site.put", {"origin": ORIGIN, "entry": _sized(length + 1) | {"pageType": "other"}}
        )
        assert code_of(frame) == errors.INVALID_PARAMS and field_of(frame) == "entry"
        assert "larger than 24576 bytes" in frame["error"]["message"]
    finally:
        await client.stop()


async def test_mark_part_counts_without_touching_verification(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    try:
        entry_id = (await put(client, typed_entry()))["entryId"]
        [before] = (await client.ok("site.get", {"origin": ORIGIN}))["entries"]

        async def mark(part: str, name: str, outcome: str) -> dict[str, Any]:
            params = {"origin": ORIGIN, "entryId": entry_id, "part": part, "name": name}
            return (await client.ok("site.markPart", {**params, "outcome": outcome}))["entry"]

        after = await mark("flow", "search", "ok")
        assert after["flows"][0]["stats"]["ok"] == 4
        assert after["flows"][0]["stats"]["lastRun"] != "2026-10-16T03:00:00.000Z"
        after = await mark("flow", "search", "miss")
        assert after["flows"][0]["stats"]["missed"] == 2
        after = await mark("flow", "legacy", "ok")
        assert after["flows"][1]["stats"]["ok"] == 1
        after = await mark("recipe", "products", "stale")
        assert after["recipes"][0]["stats"]["misses"] == 1 and after["recipes"][0]["stale"]
        after = await mark("recipe", "products", "ok")
        today = (datetime.now(UTC) - datetime(1970, 1, 1, tzinfo=UTC)).days
        assert after["recipes"][0]["stats"] == {"hits": 4, "misses": 1, "day": today}
        assert after["recipes"][0]["stale"] is False
        after = await mark("recipe", "products", "miss")
        assert after["recipes"][0]["stats"]["misses"] == 2 and not after["recipes"][0]["stale"]
        for key in ("successCount", "failureCount", "stale", "lastVerified"):
            assert after[key] == before[key], key
        assert sm.MAX_ENTRY_BYTES == 24 * 1024

        for params, code in (
            ({"part": "flow", "name": "nope", "outcome": "ok"}, errors.NOT_FOUND),
            ({"part": "recipe", "name": "nope", "outcome": "ok"}, errors.NOT_FOUND),
            ({"part": "flow", "name": "search", "outcome": "stale"}, errors.INVALID_PARAMS),
        ):
            frame = await client.call(
                "site.markPart", {"origin": ORIGIN, "entryId": entry_id, **params}
            )
            assert code_of(frame) == code, params
        frame = await client.call(
            "site.markPart",
            {"origin": ORIGIN, "entryId": "e_missing", "part": "flow", "name": "search",
             "outcome": "ok"},
        )
        assert code_of(frame) == errors.NOT_FOUND
    finally:
        await client.stop()


async def test_mark_part_follows_the_site_memory_rules(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    client = await open_client(http, daemon, client_id=A)
    ui = await connect(http, daemon)
    no_id = await open_client(http, daemon)
    try:
        entry_id = (await put(client, typed_entry()))["entryId"]
        params = {"origin": ORIGIN, "entryId": entry_id, "part": "flow", "name": "search",
                  "outcome": "ok"}
        assert code_of(await ui.call("site.markPart", params)) == errors.UNAUTHORIZED
        assert code_of(await no_id.call("site.markPart", params)) == errors.UNAUTHORIZED
        await client.ok(
            "settings.set", {"scope": "global", "patch": {"browser": {"siteMemory": False}}}
        )
        assert code_of(await client.call("site.markPart", params)) == errors.TOOL_INACTIVE
    finally:
        for c in (client, ui, no_id):
            await c.stop()


async def test_part_counts_are_not_verification_for_retention(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    from datetime import timedelta

    client = await open_client(http, daemon, client_id=A)
    try:
        old = sm.iso(datetime.now(UTC) - timedelta(days=91))
        entry_id = (await put(client, typed_entry(lastVerified=old)))["entryId"]
        # A put sets lastVerified from the host's clock (here 91 days ago); then
        # counting a recipe use must not keep the entry alive.
        frame = await client.call(
            "site.markPart",
            {"origin": ORIGIN, "entryId": entry_id, "part": "recipe", "name": "products",
             "outcome": "ok"},
        )
        assert code_of(frame) == errors.NOT_FOUND
        assert (await client.ok("site.get", {"origin": ORIGIN}))["entries"] == []
    finally:
        await client.stop()
