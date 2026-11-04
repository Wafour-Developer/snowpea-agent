"""R2 (1.38.0): typed site playbooks, stored apart from page entries."""

from __future__ import annotations

import copy
from typing import Any

import aiohttp
import pytest
from _support import connect  # noqa: F401
from test_host_tools import Daemon, daemon, open_client  # noqa: F401
from test_site_memory import ORIGIN, code_of, entry, field_of, put

from snowpea_core.server import errors
from snowpea_core.server.protocol import CAPABILITIES

pytestmark = pytest.mark.asyncio

A = "snowpea-browser-A"
B = "snowpea-browser-B"


def playbook(name: str = "write-post", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "goal": "write and schedule a blog post",
        "intents": ["publish post", "예약 발행"],
        "start": "/postwrite",
        "skills": ["tistory"],
        "slots": [
            {"name": "title", "kind": "text"},
            {"name": "body", "kind": "html"},
            {"name": "images", "kind": "files", "optional": True},
        ],
        "segments": [
            {
                "name": "title",
                "steps": [{"do": "fill", "action": "search", "slot": "title"}],
                "checks": [{"kind": "fieldEquals", "action": "search", "slot": "title"}],
            },
            {
                "name": "publish",
                "steps": [{"do": "click", "action": "search", "consequential": "publish"}],
                "checks": [{"kind": "present", "action": "search"}],
            },
        ],
        "pitfalls": [{"text": "the editor is in the top document", "seen": 2, "source": "host"}],
        "state": "candidate",
        "mac": "hmac-sha256:" + "A1b2C3d4" * 6,
    }
    body.update(extra)
    return body


async def pb_put(host: Any, body: dict[str, Any], origin: str = ORIGIN) -> dict[str, Any]:
    return await host.ok("site.playbook.put", {"origin": origin, "playbook": body})


async def pb_get(host: Any, origin: str = ORIGIN, **extra: Any) -> list[dict[str, Any]]:
    return (await host.ok("site.playbook.get", {"origin": origin, **extra}))["playbooks"]


async def test_capability_and_a_round_trip(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    assert "sitePlaybooks" in CAPABILITIES
    host = await open_client(http, daemon, client_id=A)
    try:
        await put(host, entry())  # the action the steps name
        stored = await pb_put(host, playbook())
        assert stored["created"] is True
        pb = stored["playbook"]
        assert pb["id"].startswith("pb_") and pb["version"] == 1 and pb["prev"] is None
        assert pb["mac"].startswith("hmac-sha256:")
        [got] = await pb_get(host)
        assert got == pb
        assert await pb_get(host, name="nope") == []
    finally:
        await host.stop()


async def test_a_structural_change_keeps_the_old_version_one_deep(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id=A)
    try:
        first = (await pb_put(host, playbook()))["playbook"]
        same = (await pb_put(host, playbook(goal="same steps, new words")))["playbook"]
        assert same["version"] == 1 and same["id"] == first["id"] and same["prev"] is None
        changed = playbook()
        changed["segments"] = changed["segments"][:1]
        second = (await pb_put(host, changed))["playbook"]
        assert second["version"] == 2 and len(second["prev"]["segments"]) == 2
        changed["slots"] = changed["slots"][:2]
        third = (await pb_put(host, changed))["playbook"]
        assert third["version"] == 3 and len(third["prev"]["slots"]) == 3
        assert third["prev"].get("prev") is None  # one deep
    finally:
        await host.stop()


async def test_user_settings_survive_a_relearn(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id=A)
    try:
        pb = (await pb_put(host, playbook()))["playbook"]
        await host.ok(
            "site.playbook.edit",
            {"origin": ORIGIN, "playbookId": pb["id"], "op": "alwaysAsk", "value": True},
        )
        await host.ok(
            "site.playbook.edit", {"origin": ORIGIN, "playbookId": pb["id"], "op": "acknowledge"}
        )
        again = (await pb_put(host, playbook(state="verified")))["playbook"]
        assert again["alwaysAsk"] is True and again["acked"] is True
        assert again["state"] == "verified"
    finally:
        await host.stop()


async def test_values_and_secrets_are_refused(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id=A)
    try:
        for bad, field in (
            (playbook(goal="mail me at kim@example.com"), "playbook.goal"),
            (playbook(intents=["call 010-1234-5678"]), "playbook.intents[0]"),
            (playbook(pitfalls=[{"text": "password=hunter2"}]), "playbook.pitfalls[0].text"),
            (playbook(mac="a b"), "playbook.mac"),
        ):
            frame = await host.call("site.playbook.put", {"origin": ORIGIN, "playbook": bad})
            assert code_of(frame) == errors.INVALID_PARAMS, bad
            assert field_of(frame) == field or "mac" in field, (field_of(frame), field)
        # Unknown keys (a "value", a locator) are not part of the schema.
        extra = playbook()
        extra["segments"][0]["steps"][0]["value"] = "hello"
        frame = await host.call("site.playbook.put", {"origin": ORIGIN, "playbook": extra})
        assert code_of(frame) == errors.INVALID_PARAMS
        assert await pb_get(host) == []
    finally:
        await host.stop()


async def test_a_login_field_is_never_a_step_and_a_slot_must_exist(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id=A)
    try:
        page = entry()
        page["actions"][0]["loginField"] = True
        await put(host, page)
        frame = await host.call("site.playbook.put", {"origin": ORIGIN, "playbook": playbook()})
        assert code_of(frame) == errors.INVALID_PARAMS
        assert field_of(frame).endswith(".action")
        await host.ok("site.delete", {"origin": ORIGIN})
        bad = playbook()
        bad["segments"][0]["steps"][0]["slot"] = "nowhere"
        frame = await host.call("site.playbook.put", {"origin": ORIGIN, "playbook": bad})
        assert field_of(frame).endswith(".slot")
    finally:
        await host.stop()


async def test_limits(http: aiohttp.ClientSession, daemon: Daemon) -> None:  # noqa: F811
    host = await open_client(http, daemon, client_id=A)
    try:
        for index in range(10):
            await pb_put(host, playbook(f"pb-{index}"))
        frame = await host.call(
            "site.playbook.put", {"origin": ORIGIN, "playbook": playbook("one-more")}
        )
        assert code_of(frame) == errors.INVALID_PARAMS
        # An existing name still updates when the origin is full.
        assert (await pb_put(host, playbook("pb-3", goal="changed")))["created"] is False
        # Too many steps in a segment: the schema says so.
        wide = playbook("wide")
        wide["segments"][0]["steps"] = [{"do": "click"}] * 17
        frame = await host.call("site.playbook.put", {"origin": "https://a.test", "playbook": wide})
        assert code_of(frame) == errors.INVALID_PARAMS
    finally:
        await host.stop()


async def test_marks_keep_ten_runs_and_count_for_stats(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id=A)
    try:
        pb = (await pb_put(host, playbook()))["playbook"]
        mark = {"origin": ORIGIN, "playbookId": pb["id"], "done": 2, "total": 2}
        for _ in range(11):
            await host.ok("site.playbook.mark", {**mark, "outcome": "ok", "rounds": 9})
        miss = await host.ok(
            "site.playbook.mark", {**mark, "outcome": "partial", "done": 1}
        )
        history = miss["playbook"]["history"]
        assert len(history) == 10 and history[-1]["result"] == "partial"
        assert miss["playbook"]["verifiedAt"] is not None
        bad = await host.call("site.playbook.mark", {**mark, "outcome": "ok", "done": 3})
        assert code_of(bad) == errors.INVALID_PARAMS
        gone = await host.call(
            "site.playbook.mark", {**mark, "playbookId": "pb_missing", "outcome": "ok"}
        )
        assert code_of(gone) == errors.NOT_FOUND
        [stats] = (await host.ok("site.stats", {}))["origins"]
        assert (stats["playbookPuts"], stats["playbookOk"], stats["playbookMiss"]) == (1, 11, 1)
    finally:
        await host.stop()


async def test_edits_rename_retire_restore_remove(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id=A)
    try:
        pb = (await pb_put(host, playbook()))["playbook"]
        await pb_put(host, playbook("other"))

        async def edit(**args: Any) -> dict[str, Any]:
            return (
                await host.ok(
                    "site.playbook.edit", {"origin": ORIGIN, "playbookId": pb["id"], **args}
                )
            )["playbook"]

        assert (await edit(op="rename", name="blog-post"))["name"] == "blog-post"
        assert [p["name"] for p in await pb_get(host)] == ["blog-post", "other"]
        clash = await host.call(
            "site.playbook.edit",
            {"origin": ORIGIN, "playbookId": pb["id"], "op": "rename", "name": "other"},
        )
        assert code_of(clash) == errors.INVALID_PARAMS
        assert (await edit(op="retire"))["state"] == "retired"
        assert (await edit(op="unretire"))["state"] == "candidate"
        removed = await edit(op="removeSegment", segment="publish")
        assert [s["name"] for s in removed["segments"]] == ["title"] and removed["version"] == 2
        assert len((await edit(op="removePitfall", index=0))["pitfalls"]) == 0
        restored = await edit(op="restore")
        assert [s["name"] for s in restored["segments"]] == ["title", "publish"]
        assert restored["version"] == 3 and restored["state"] == "candidate"
        nothing = await host.call(
            "site.playbook.edit",
            {"origin": ORIGIN, "playbookId": pb["id"], "op": "removeSegment", "segment": "zzz"},
        )
        assert code_of(nothing) == errors.INVALID_PARAMS
    finally:
        await host.stop()


async def test_profiles_are_apart_and_delete_forgets(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id=A)
    other = await open_client(http, daemon, client_id=B)
    try:
        pb = (await pb_put(host, playbook()))["playbook"]
        assert await pb_get(other) == []
        denied = await other.call(
            "site.playbook.edit", {"origin": ORIGIN, "playbookId": pb["id"], "op": "retire"}
        )
        assert code_of(denied) == errors.NOT_FOUND
        sites = (await host.ok("site.list", {}))["sites"]
        assert sites == []  # a playbook alone does not make a listed site
        await put(host, entry())
        [site] = (await host.ok("site.list", {}))["sites"]
        assert site["playbooks"] == 1
        assert (await host.ok("site.playbook.delete", {"origin": ORIGIN, "playbookId": pb["id"]}))[
            "deleted"
        ] == 1
        await pb_put(host, playbook())
        await host.ok("site.delete", {"origin": ORIGIN})
        assert await pb_get(host) == []
    finally:
        await host.stop()
        await other.stop()


async def test_the_body_is_copied_not_shared() -> None:
    from datetime import UTC, datetime

    from snowpea_core.session import site_playbooks as sp

    data = copy.deepcopy(playbook())
    built = sp.build_put(data, None, ORIGIN, datetime.now(UTC))
    built["segments"].clear()
    assert data["segments"]


async def test_review_findings_stay_closed(
    http: aiohttp.ClientSession, daemon: Daemon  # noqa: F811
) -> None:
    host = await open_client(http, daemon, client_id=A)
    try:
        page = entry()
        page["actions"][0]["loginField"] = True
        page["actions"][0]["name"] = "password"
        other = {**copy.deepcopy(page["actions"][0]), "name": "go", "loginField": False}
        page["actions"].append(other)
        await put(host, page)

        async def refused(body: dict[str, Any]) -> str:
            frame = await host.call("site.playbook.put", {"origin": ORIGIN, "playbook": body})
            assert code_of(frame) == errors.INVALID_PARAMS, body
            return str(field_of(frame))

        # a login field through `via`
        via = playbook()
        via["segments"][0]["steps"] = [{"do": "fill", "via": "password"}]
        assert (await refused(via)).endswith(".via")
        # another host, a query with values, an id in a path
        for url in ("//evil.example/x", "/\\evil.example/x", "/search?q=john+smith"):
            bad = playbook()
            bad["segments"][0]["steps"] = [{"do": "goto", "url": url}]
            assert (await refused(bad)).endswith(".url"), url
        bad = playbook()
        bad["segments"][0]["page"] = "/users/jane.doe/drafts/12345678"
        await refused(bad)
        evil = playbook(start="https://evil.example/inbox?from=jane")
        assert await refused(evil) == "playbook.start"
        # a covert slot in the digest
        assert await refused(playbook(mac="password=hunter2")) == "playbook.mac"
        assert await refused(playbook(mac="01012345678")) == "playbook.mac"
        # a long measure word
        long = playbook()
        long["segments"][0]["checks"][0]["measure"] = ["x" * 3000]
        await refused(long)

        # the row with its previous version stays within twice the budget
        await host.ok("site.delete", {"origin": ORIGIN})
        await put(host, entry())
        fat = playbook()
        fat["pitfalls"] = [{"text": "a" * 195} for _ in range(12)]
        fat["intents"] = ["b" * 60 for _ in range(8)]
        await pb_put(host, fat)
        pb = (await pb_get(host))[0]
        assert pb["id"]

        # a rename is filtered like a put; a restore re-checks login fields
        for name in ("01012345678",):
            frame = await host.call(
                "site.playbook.edit",
                {"origin": ORIGIN, "playbookId": pb["id"], "op": "rename", "name": name},
            )
            assert code_of(frame) == errors.INVALID_PARAMS
        base = playbook("restorable")
        stored = (await pb_put(host, base))["playbook"]
        slim = playbook("restorable")
        slim["segments"] = slim["segments"][:1]
        await pb_put(host, slim)
        flagged = entry()
        flagged["actions"][0]["loginField"] = True
        await put(host, flagged)
        frame = await host.call(
            "site.playbook.edit", {"origin": ORIGIN, "playbookId": stored["id"], "op": "restore"}
        )
        assert code_of(frame) == errors.INVALID_PARAMS and field_of(frame).endswith(".action")
    finally:
        await host.stop()


async def test_a_failing_playbook_is_not_kept_alive_by_failed_marks() -> None:
    from datetime import UTC, datetime, timedelta

    from snowpea_core.session import site_playbooks as sp

    now = datetime.now(UTC)
    built = sp.build_put(playbook(), None, ORIGIN, now - timedelta(days=100))
    stamp = built["updatedAt"]
    sp.append_run(built, "failed", 0, 2, 9, now)
    assert built["updatedAt"] == stamp and len(built["history"]) == 1
    sp.append_run(built, "ok", 2, 2, 9, now)
    assert built["updatedAt"] != stamp


async def test_playbook_origins_are_bounded(
    http: aiohttp.ClientSession, daemon: Daemon, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from snowpea_core.session import site_memory as sm

    monkeypatch.setattr(sm, "MAX_ORIGINS_PER_PROFILE", 2)
    host = await open_client(http, daemon, client_id=A)
    try:
        await pb_put(host, playbook(), "https://one.test")
        await pb_put(host, playbook(), "https://two.test")
        frame = await host.call(
            "site.playbook.put", {"origin": "https://three.test", "playbook": playbook()}
        )
        assert code_of(frame) == errors.INVALID_PARAMS
        assert (await pb_put(host, playbook("more"), "https://one.test"))["created"] is True
    finally:
        await host.stop()
