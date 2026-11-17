"""0.2.91: approvals and questions are kept with the conversation's events.

``approval.asked`` / ``approval.answered`` and ``question.asked`` /
``question.answered`` are stored session events (``SESSION_EVENT_KINDS``), so
an export of ``session.events`` shows which approvals were asked, where, and
how and by whom they ended.  A ``secret`` / ``config`` call's arguments and a
secret question's answer are never stored.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.server.protocol import (
    SESSION_EVENT_KINDS,
    QuestionItem,
    QuestionOption,
)
from snowpea_core.session import events as event_builders
from snowpea_core.session.questions import QuestionQueue


class _Conn:
    authenticated = True
    closed = False
    remote_login = False
    client_id = None

    def __init__(self, kind: str = "tui", reply: Any = None, delay: float = 0.0) -> None:
        self.client_kind = kind
        self.reply = reply
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None):
        self.calls.append(params)
        if self.reply is None:
            await asyncio.Event().wait()  # nobody at this card
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.reply

    async def notify(self, *_: Any, **__: Any) -> None:
        return None


class _Hub:
    """Records what would be stored; ``emit_event`` validates like the real hub."""

    def __init__(self, session: Any) -> None:
        self.sessions = SimpleNamespace(get={session.id: session}.get)
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit_event(self, session_id: str, event: tuple[str, dict[str, Any]]) -> None:
        kind, payload = event
        self.events.append((kind, event_builders.validate(kind, payload)))

    async def notify(self, *_: Any, **__: Any) -> None:
        return None

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [payload for k, payload in self.events if k == kind]


def _session(origin: Any) -> Any:
    return SimpleNamespace(id="s-1", origin_conn=origin, asker_conn=None, workdir="/tmp",
                           kind="chat", host_tools_from=None, owned_by=None,
                           parent_session_id=None)


def _approvals(session: Any, timeout: int = 300) -> tuple[ApprovalQueue, _Hub]:
    queue = ApprovalQueue(Settings.model_validate({"approvals": {"timeoutSec": timeout}}))
    hub = _Hub(session)
    queue.hub = hub  # type: ignore[assignment]
    return queue, hub


def test_the_kinds_are_stored_session_events() -> None:
    for kind in ("approval.asked", "approval.answered", "question.asked", "question.answered"):
        assert kind in SESSION_EVENT_KINDS
    # The kinds older clients know are where they were.
    assert SESSION_EVENT_KINDS[:3] == ("message.user", "message.delta", "message.reasoning")


@pytest.mark.asyncio
async def test_a_persons_deny_is_recorded_as_the_origins() -> None:
    conn = _Conn("tui", reply={"decision": "deny", "scope": "once", "reason": "not now"})
    session = _session(conn)
    queue, hub = _approvals(session)
    await queue.request(session, "shell", {"command": "rm build.log"}, risk="high")
    [asked] = hub.of("approval.asked")
    [answered] = hub.of("approval.answered")
    assert [k for k, _ in hub.events] == ["approval.asked", "approval.answered"]
    assert asked == {
        "requestId": asked["requestId"], "tool": "shell", "risk": "high",
        "summary": "rm build.log", "argsWithheld": False, "reasonCode": None,
        "unchecked": False, "personOnly": False, "route": None, "askedConn": "tui",
    }
    assert answered["requestId"] == asked["requestId"]
    assert answered["decision"] == "deny" and answered["by"] == "origin"
    assert answered["scope"] is None and answered["reason"] == "not now"
    assert answered["waitedMs"] >= 0


@pytest.mark.asyncio
async def test_a_client_timeout_is_recorded_as_origin_timeout() -> None:
    conn = _Conn("ide", reply={"decision": "deny", "scope": "once"}, delay=1.05)
    session = _session(conn)
    queue, hub = _approvals(session, timeout=2)
    await queue.request(session, "write_file", {"path": "a.txt"}, timeout_sec=2)
    [asked] = hub.of("approval.asked")
    [answered] = hub.of("approval.answered")
    assert asked["summary"] == "a.txt" and asked["askedConn"] == "ide"
    assert answered["by"] == "origin-timeout"
    assert answered["reason"].startswith("nobody answered on the desktop app within")
    assert answered["waitedMs"] >= 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("by", ["jarvis", "jarvis:auto"])
async def test_a_jarvis_decision_is_recorded(by: str) -> None:
    session = _session(None)  # the shared queue: Jarvis answers with approval.respond
    queue, hub = _approvals(session)
    task = asyncio.ensure_future(queue.request(session, "shell", {"command": "ls"}))
    for _ in range(300):
        if queue.unattended():
            break
        await asyncio.sleep(0.01)
    await queue.respond(queue.unattended()[0].requestId, "allow", "once", by=by)
    assert (await asyncio.wait_for(task, 5)).allowed
    [asked] = hub.of("approval.asked")
    [answered] = hub.of("approval.answered")
    assert asked["askedConn"] == "none"
    assert answered["by"] == by and answered["decision"] == "allow"
    assert answered["scope"] == "once"


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["secret", "config"])
async def test_secret_and_config_arguments_are_withheld(tag: str) -> None:
    conn = _Conn("tui", reply={"decision": "allow", "scope": "once"})
    session = _session(conn)
    queue, hub = _approvals(session)
    await queue.request(session, "read_file", {"path": "/home/u/.ssh/id_ed25519"},
                        tags=(tag,), cacheable=False)
    [asked] = hub.of("approval.asked")
    assert asked["summary"] == "arguments withheld" and asked["argsWithheld"] is True
    assert "id_ed25519" not in repr(hub.events)


@pytest.mark.asyncio
async def test_a_cache_hit_asks_nobody_and_records_nothing() -> None:
    conn = _Conn("tui", reply={"decision": "allow", "scope": "session"})
    session = _session(conn)
    queue, hub = _approvals(session)
    await queue.request(session, "write_file", {"path": "a.txt"})
    await queue.request(session, "write_file", {"path": "b.txt"})
    assert len(conn.calls) == 1
    assert [k for k, _ in hub.events] == ["approval.asked", "approval.answered"]


def _qqueue(session: Any) -> tuple[QuestionQueue, _Hub]:
    queue = QuestionQueue(Settings())
    hub = _Hub(session)
    queue.hub = hub  # type: ignore[assignment]
    return queue, hub


@pytest.mark.asyncio
async def test_questions_are_recorded_and_a_secret_answer_is_not() -> None:
    conn = _Conn("tui", reply={"answers": [{"selected": ["blue"]}, {"text": "hunter2"}]})
    session = _session(conn)
    queue, hub = _qqueue(session)
    await queue.ask(session, [
        QuestionItem(header="Colour", question="Which colour?",
                     options=[QuestionOption(label="blue"), QuestionOption(label="red")]),
        QuestionItem(question="The deploy password?", options=[], secret=True),
    ])
    [asked] = hub.of("question.asked")
    [answered] = hub.of("question.answered")
    assert asked["askedConn"] == "tui"
    assert asked["questions"][0] == {"header": "Colour", "question": "Which colour?",
                                     "options": ["blue", "red"], "secret": False}
    assert asked["questions"][1]["secret"] is True
    assert answered["by"] == "origin"
    assert answered["answers"][0]["selected"] == ["blue"]
    assert answered["answers"][1] == {"selected": [], "text": None, "withheld": True,
                                      "declined": False, "timedOut": False}
    assert "hunter2" not in repr(hub.events)


# -- end to end: stored and replayed ------------------------------------------------


async def test_the_record_is_stored_and_replayed(
    gateway_env: None, http: Any, tmp_path: Any
) -> None:
    from _support import connect, make_daemon

    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = await make_daemon(tmp_path / "home", {"approvals": {"timeoutSec": 10}})
    try:
        client = await connect(http, daemon, approval_mode="ignore")
        session_id = (await client.ok(
            "session.create", {"workdir": str(workdir), "mode": "accept"}))["sessionId"]
        turn = await client.ok("session.prompt", {"sessionId": session_id,
                                                  "text": "deploy the thing"})
        deadline = asyncio.get_running_loop().time() + 10
        while not client.approval_requests:
            assert asyncio.get_running_loop().time() < deadline, "no approval.request"
            await asyncio.sleep(0.02)
        request_id = client.approval_requests[0]["requestId"]
        await client.ok("approval.respond",
                        {"requestId": request_id, "decision": "deny", "scope": "once"})
        assert await client.wait_turn(turn["turnId"]) == "complete"
        events = (await client.ok("session.resume",
                                  {"sessionId": session_id, "afterSeq": 0}))["events"]
        kinds = [e["kind"] for e in events]
        asked = next(e for e in events if e["kind"] == "approval.asked")
        answered = next(e for e in events if e["kind"] == "approval.answered")
        assert kinds.index("approval.asked") < kinds.index("approval.answered")
        assert asked["payload"]["requestId"] == request_id
        assert asked["payload"]["tool"] == "shell"
        assert answered["payload"]["decision"] == "deny"
        await client.stop()
    finally:
        await daemon.stop()
