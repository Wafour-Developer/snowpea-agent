"""Addendum 27, C2: a browser client records whether a turn did what was asked.

``turn.outcome`` is stored as a session event, so ``session.resume`` and
``session.events`` return it; it is refused for another client's session and
when its reason holds a value memory would refuse.
"""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest
from _support import connect
from test_host_tools import Daemon, daemon, new_session, open_client  # noqa: F401

from snowpea_core.server import errors

pytestmark = pytest.mark.asyncio

A = "snowpea-browser-A"
B = "snowpea-browser-B"


async def test_an_outcome_is_stored_and_replayed(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    tmp_path: Path,
) -> None:
    host = await open_client(http, daemon, client_id=A)
    try:
        session_id = await new_session(host, tmp_path / "w", hostToolsFrom=A)
        sent = await host.ok(
            "turn.outcome",
            {
                "sessionId": session_id,
                "turnId": "t-1",
                "outcome": "failed",
                "by": "host",
                "reason": "the checkout button never appeared",
            },
        )
        later = await host.ok(
            "turn.outcome",
            {"sessionId": session_id, "turnId": "t-1", "outcome": "corrected", "by": "user"},
        )
        assert later["seq"] > sent["seq"]

        resumed = await host.ok("session.resume", {"sessionId": session_id})
        outcomes = [e for e in resumed["events"] if e["kind"] == "turn.outcome"]
        assert [(e["payload"]["outcome"], e["payload"]["by"]) for e in outcomes] == [
            ("failed", "host"),
            ("corrected", "user"),
        ]
        assert outcomes[0]["payload"]["reason"] == "the checkout button never appeared"

        # A closed session takes one too, after its last event.
        await daemon.core.sessions.close(session_id)
        closed = await host.ok(
            "turn.outcome",
            {"sessionId": session_id, "turnId": "t-1", "outcome": "abandoned", "by": "user"},
        )
        read = await host.ok("session.events", {"sessionId": session_id})
        assert read["events"][-1]["kind"] == "turn.outcome"
        assert read["events"][-1]["seq"] == closed["seq"] == read["lastSeq"]
    finally:
        await host.stop()


async def test_only_the_owning_browser_client_may_judge(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    tmp_path: Path,
) -> None:
    host = await open_client(http, daemon, client_id=A)
    other = await open_client(http, daemon, client_id=B)
    plain = await connect(http, daemon)
    try:
        session_id = await new_session(host, tmp_path / "w")
        params = {"sessionId": session_id, "turnId": "t-1", "outcome": "ok", "by": "user"}
        for client in (other, plain):
            frame = await client.call("turn.outcome", params)
            assert frame["error"]["data"]["code"] == errors.UNAUTHORIZED
        missing = await host.call("turn.outcome", {**params, "sessionId": "s-nope"})
        assert missing["error"]["data"]["code"] == errors.NOT_FOUND
    finally:
        await plain.stop()
        await other.stop()
        await host.stop()


async def test_a_reason_with_personal_data_is_refused(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    tmp_path: Path,
) -> None:
    host = await open_client(http, daemon, client_id=A)
    try:
        session_id = await new_session(host, tmp_path / "w")
        frame = await host.call(
            "turn.outcome",
            {
                "sessionId": session_id,
                "turnId": "t-1",
                "outcome": "failed",
                "by": "host",
                "reason": "could not call 010-1234-5678",
            },
        )
        assert frame["error"]["data"]["code"] == errors.INVALID_PARAMS
        assert "phone number" in frame["error"]["message"]
        assert "010-1234-5678" not in frame["error"]["message"]
        events = await host.ok("session.events", {"sessionId": session_id})
        assert not [e for e in events["events"] if e["kind"] == "turn.outcome"]
    finally:
        await host.stop()
