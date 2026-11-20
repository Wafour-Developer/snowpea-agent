"""snowpea web as the asking surface (owner's report 2026-11-20, feedback 9627f46c).

The web page asked nothing: core sends ``approval.request`` with a string id
(``"s1"``) and the web client answered numeric ids only, so a person-only ask
waited 52 minutes for the TUI.  The fix is in the web client; these tests pin
what it relies on from core:

* server->client request ids are strings, so a client must take them as they come;
* a loopback web page (``clientKind: "web"``, not the remote listener) is the
  person at this computer: its allow of a blocked call counts, like the desktop's;
* a remote web login's allow of a blocked call is refused, the request stays
  open and the page is told why (``refused.reason: "remote_blocked"``), so it can
  show the card again with Refuse only; its refusal counts.

Every command line below is DATA handed to an approval queue whose calls never
run anything.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from test_m24_person_override import Conn, _queue, _session

from snowpea_core.server.rpc import RpcConnection

BLOCKED_LINE = {"command": "rm -rf ~"}


async def test_server_requests_carry_string_ids() -> None:
    sent: list[dict[str, Any]] = []

    async def send(obj: dict[str, Any]) -> None:
        sent.append(obj)

    conn = RpcConnection(send, peer="127.0.0.1")
    task = asyncio.ensure_future(conn.call("approval.request", {"requestId": "ap-1"}, timeout=5))
    for _ in range(100):
        if sent:
            break
        await asyncio.sleep(0.01)
    assert isinstance(sent[0]["id"], str) and sent[0]["id"].startswith("s")
    conn.handle_response({"jsonrpc": "2.0", "id": sent[0]["id"], "result": {"decision": "deny"}})
    assert (await task)["decision"] == "deny"


async def test_a_loopback_web_page_allows_a_blocked_call_it_was_asked(tmp_path: Path) -> None:
    queue, _hub = _queue(tmp_path)
    origin = Conn("web", "web-1", reply={"decision": "allow", "scope": "once"})
    session = _session(tmp_path, origin_conn=origin, mode="accept")
    decision = await queue.request(session, "shell", dict(BLOCKED_LINE), timeout_sec=5,
                                   tags=("exec",))
    assert decision.allowed and decision.by == "origin"
    assert not [note for note in origin.notes if note[0] == "approval.pending"]


async def test_a_remote_web_login_is_told_why_and_may_still_refuse(tmp_path: Path) -> None:
    queue, _hub = _queue(tmp_path)
    origin = Conn("web", "web-r", reply={"decision": "allow", "scope": "once"}, remote=True)
    session = _session(tmp_path, origin_conn=origin, mode="accept")
    task = asyncio.ensure_future(queue.request(session, "shell", dict(BLOCKED_LINE),
                                               timeout_sec=30, tags=("exec",)))
    refused: dict[str, Any] | None = None
    for _ in range(200):
        refused = next((params for method, params in origin.notes
                        if method == "approval.pending" and params.get("refused")), None)
        if refused:
            break
        await asyncio.sleep(0.01)
    assert refused is not None, "the origin was not told its allow was refused"
    assert refused["refused"]["reason"] == "remote_blocked"
    assert refused["request"]["personOnly"] and refused["request"]["blocked"]
    assert not task.done()
    request_id = refused["request"]["requestId"]
    await queue.respond(request_id, "deny", by="web", conn=origin)
    assert not (await task).allowed
