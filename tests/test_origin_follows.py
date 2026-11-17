"""0.2.91: approval and question cards go to the surface the person is prompting from.

Field report (browser -26, core 0.2.89): a session's ``origin_conn`` was
another client (the desktop app, attached while the browser was away); every
``write_file`` approval went there, nobody looked, and its card answered a bare
deny after its own 300 s window, which core read as the person's "no".

Now the person's connection whose words start (or steer) a turn is that turn's
asker (``person_gate.asker_for``); a client's own timeout is ``origin-timeout``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent import loop as agent_loop
from snowpea_core.config.settings import Settings
from snowpea_core.permissions.approval_queue import ApprovalQueue, _Pending
from snowpea_core.server.errors import RpcError
from snowpea_core.server.person_gate import asker_for, snapshot
from snowpea_core.server.protocol import ApprovalRequest, QuestionItem, QuestionRequest
from snowpea_core.session import questions as question_mod
from snowpea_core.session.questions import QuestionQueue
from snowpea_core.session.session import asking_conn, client_timed_out


class _Conn:
    """A connection that records what it was asked and answers when told to."""

    authenticated = True

    def __init__(self, kind: str | None = "ide", *, remote: bool = False,
                 client_id: str | None = None, reply: Any = None, delay: float = 0.0) -> None:
        self.client_kind = kind
        self.remote_login = remote
        self.client_id = client_id
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.reply = reply
        self.delay = delay
        self.release = asyncio.Event() if reply is None else None

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None):
        self.calls.append((method, params))
        if self.release is not None:
            await self.release.wait()
            return {"decision": "allow", "scope": "once"}
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.reply

    async def notify(self, method: str, params: dict[str, Any], **_: Any) -> None:
        self.notes.append((method, params))


class _Hub:
    def __init__(self, sessions: dict[str, Any]) -> None:
        self.sessions = SimpleNamespace(get=sessions.get)
        self.sent: list[tuple[str, dict[str, Any], Any]] = []

    async def notify(self, method: str, params: dict[str, Any], exclude: Any = None) -> None:
        self.sent.append((method, params, exclude))


def _session(origin: Any, sid: str = "s-1", **extra: Any) -> Any:
    fields: dict[str, Any] = {
        "id": sid, "origin_conn": origin, "asker_conn": None, "workdir": "/tmp",
        "kind": "chat", "host_tools_from": None, "origin_client_id": None,
        "parent_session_id": None, "owned_by": None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def _approvals(session: Any, timeout: int = 300) -> tuple[ApprovalQueue, _Hub]:
    queue = ApprovalQueue(Settings.model_validate({"approvals": {"timeoutSec": timeout}}))
    hub = _Hub({session.id: session})
    queue.hub = hub  # type: ignore[assignment]
    return queue, hub


def _core(queue: Any = None, questions: Any = None) -> Any:
    return SimpleNamespace(approvals=queue, questions=questions)


def _prompted(session: Any, conn: Any, actor: str | None = None) -> None:
    """What ``session.prompt`` / ``command.run`` leave behind once their turn starts."""
    agent_loop.note_asker(_core(), session, snapshot(conn, actor, origin="human"),
                          starts_turn=True)


async def _until(predicate: Any, limit: float = 3.0) -> None:
    for _ in range(int(limit / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


# -- end to end: two clients on one daemon --------------------------------------------


async def test_the_daemon_asks_the_client_that_prompted(
    gateway_env: None, http: Any, tmp_path: Any
) -> None:
    from _support import connect, make_daemon

    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = await make_daemon(tmp_path / "home", {"approvals": {"timeoutSec": 10}})
    try:
        first = await connect(http, daemon, approval_mode="ignore")
        second = await connect(http, daemon, approval_mode="ignore")
        session_id = (await first.ok(
            "session.create", {"workdir": str(workdir), "mode": "accept"}))["sessionId"]
        # The second client attaches while the first is still open: the first
        # stays the origin, as before...
        await second.ok("session.resume", {"sessionId": session_id})
        # ...but the second is where the person types now.
        turn = await second.ok("session.prompt", {"sessionId": session_id,
                                                  "text": "deploy the thing"})
        deadline = asyncio.get_running_loop().time() + 10
        while not second.approval_requests:
            assert asyncio.get_running_loop().time() < deadline, "no approval.request"
            await asyncio.sleep(0.02)
        assert first.approval_requests == []
        request_id = second.approval_requests[0]["requestId"]
        await second.ok("approval.respond",
                        {"requestId": request_id, "decision": "deny", "scope": "once"})
        assert await second.wait_turn(turn["turnId"]) == "complete"
        await first.stop()
        await second.stop()
    finally:
        await daemon.stop()


# -- who may become the asker -------------------------------------------------------


def test_people_become_the_asker_and_nobody_else() -> None:
    session = _session(_Conn("ide"))
    assert asker_for(session, snapshot(_Conn("tui"))) is not None
    assert asker_for(session, snapshot(_Conn("ide", remote=True))) is not None
    assert asker_for(session, snapshot(_Conn("connector"))) is None
    assert asker_for(session, snapshot(None, caller="messenger")) is None
    assert asker_for(session, None) is None
    closed = _Conn("tui")
    who = snapshot(closed)
    closed.closed = True
    assert asker_for(session, who) is None
    # A browser: only the one that hosts (or opened) the session.
    mine = _Conn("browser", client_id="b-1")
    assert asker_for(session, snapshot(mine)) is None
    assert asker_for(_session(None, host_tools_from="b-1"), snapshot(mine)) is mine
    assert asker_for(_session(None, origin_client_id="b-1"), snapshot(mine)) is mine
    assert asker_for(_session(None, host_tools_from="b-2"), snapshot(mine)) is None
    # A browser-owned job's run keeps its origin.
    assert asker_for(_session(None, owned_by="b-1"), snapshot(_Conn("tui"))) is None


def test_a_turn_nobody_typed_clears_the_asker() -> None:
    a, b = _Conn("ide"), _Conn("tui")
    session = _session(a)
    _prompted(session, b)
    assert session.asker_conn is b
    agent_loop.note_asker(_core(), session, None, starts_turn=True)  # Jarvis / a schedule
    assert session.asker_conn is None and asking_conn(session) is a
    _prompted(session, b)
    agent_loop.note_asker(_core(), session, snapshot(_Conn("connector")))  # a steer
    assert session.asker_conn is b


# -- approvals ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_approval_goes_to_the_surface_that_prompted() -> None:
    a, b = _Conn("ide"), _Conn("browser", client_id="b-1")
    session = _session(a, host_tools_from="b-1")  # A attached last; B is the browser
    _prompted(session, b)
    queue, _ = _approvals(session)
    task = asyncio.ensure_future(queue.request(session, "write_file", {"path": "x"}))
    await _until(lambda: b.calls)
    assert [m for m, _ in b.calls] == ["approval.request"] and not a.calls
    assert queue.list(conn=b) and not queue.list(conn=a)
    b.release.set()
    decision = await asyncio.wait_for(task, 5)
    assert decision.allowed and decision.by == "origin"


@pytest.mark.asyncio
async def test_a_closed_asker_falls_back_to_the_origin_then_to_everyone() -> None:
    a, b = _Conn("ide"), _Conn("tui")
    session = _session(a)
    _prompted(session, b)
    b.closed = True
    queue, hub = _approvals(session, timeout=1)
    task = asyncio.ensure_future(queue.request(session, "write_file", {"path": "x"}))
    await _until(lambda: a.calls)
    a.release.set()
    assert (await asyncio.wait_for(task, 5)).allowed
    # Both gone: the shared queue, answerable from any surface.
    a.closed = True
    task = asyncio.ensure_future(queue.request(session, "write_file", {"path": "y"}))
    await _until(lambda: queue.unattended())
    assert any(method == "approval.pending" for method, _, _ in hub.sent)
    request = queue.unattended()[0]
    await queue.respond(request.requestId, "allow", conn=_Conn("ide"))
    assert (await asyncio.wait_for(task, 5)).allowed


@pytest.mark.asyncio
async def test_a_connector_prompt_leaves_the_approval_with_the_origin() -> None:
    a, b = _Conn("ide"), _Conn("connector")
    session = _session(a)
    _prompted(session, b)
    queue, _ = _approvals(session)
    task = asyncio.ensure_future(queue.request(session, "write_file", {"path": "x"}))
    await _until(lambda: a.calls)
    assert not b.calls
    a.release.set()
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_a_remote_login_is_asked_but_keeps_its_limits() -> None:
    a, b = _Conn("ide"), _Conn("ide", remote=True, reply={"decision": "allow", "scope": "once"})
    session = _session(a)
    _prompted(session, b)
    queue, _ = _approvals(session, timeout=1)
    # A guarded person-only grant: a remote web login's allow is no answer.
    decision = await asyncio.wait_for(queue.request(
        session, "jarvis_delegation", {"allow": ["exec"]}, person_only=True, timeout_sec=1), 8)
    assert [m for m, _ in b.calls] == ["approval.request"] and not a.calls
    assert any(m == "approval.pending" and p.get("refused") for m, p in b.notes)
    assert not decision.allowed and decision.by == "timeout"


@pytest.mark.asyncio
async def test_a_steer_from_another_surface_moves_the_waiting_card() -> None:
    a, b = _Conn("ide"), _Conn("tui")
    session = _session(a)
    _prompted(session, a)
    queue, _ = _approvals(session)
    task = asyncio.ensure_future(queue.request(session, "write_file", {"path": "x"}))
    await _until(lambda: a.calls)
    agent_loop.note_asker(_core(queue), session, snapshot(b, origin="human"))
    await _until(lambda: b.calls)
    assert ("approval.resolved", {"requestId": a.calls[0][1]["requestId"], "by": "rerouted",
                                  "outcome": "withdrawn", "decision": None}) in a.notes
    with pytest.raises(RpcError):  # the old surface's answer no longer counts
        await queue.respond(a.calls[0][1]["requestId"], "deny", conn=a)
    b.release.set()
    assert (await asyncio.wait_for(task, 5)).allowed


@pytest.mark.asyncio
async def test_unattended_and_jarvis_turns_are_unchanged() -> None:
    a, b = _Conn("ide"), _Conn("tui")
    session = _session(a)
    _prompted(session, b)
    agent_loop.note_asker(_core(), session, None, starts_turn=True)  # a Jarvis turn
    queue, _ = _approvals(session, timeout=1)
    task = asyncio.ensure_future(queue.request(session, "write_file", {"path": "x"}))
    await _until(lambda: a.calls)
    assert not b.calls
    a.release.set()
    await asyncio.wait_for(task, 5)
    decision = await asyncio.wait_for(
        queue.request(session, "write_file", {"path": "y"}, unattended=True), 8)
    assert decision.by == "timeout" and len(a.calls) == 1 and not b.calls


# -- a client's timeout is not a person's no ----------------------------------------


def _entry(conn: Any, clock: float) -> _Pending:
    request = ApprovalRequest(requestId="r", sessionId="s", tool="write_file", args={})
    return _Pending(request=request, future=asyncio.get_running_loop().create_future(),
                    unattended=False, origin_conn=conn, clock_sec=clock)


@pytest.mark.asyncio
async def test_the_desktop_cards_own_timeout_is_origin_timeout() -> None:
    late = _Conn("ide", reply={"decision": "deny", "scope": "once"}, delay=1.05)
    entry = _entry(late, clock=2.0)  # 1 s of slack: a 2 s clock times out at >= 1 s
    await ApprovalQueue()._ask_origin(entry)
    decision = entry.future.result()
    assert not decision.allowed and decision.by == "origin-timeout"
    assert decision.reason.startswith("nobody answered on the desktop app within")


@pytest.mark.asyncio
async def test_a_real_deny_stays_the_origins() -> None:
    quick = _Conn("ide", reply={"decision": "deny", "scope": "once"})
    entry = _entry(quick, clock=300.0)
    await ApprovalQueue()._ask_origin(entry)
    assert entry.future.result().by == "origin"
    # A late deny with the person's reason is theirs too.
    said = _Conn("ide", reply={"decision": "deny", "reason": "no"}, delay=1.05)
    entry = _entry(said, clock=2.0)
    await ApprovalQueue()._ask_origin(entry)
    assert entry.future.result().by == "origin"


def test_client_timeout_shapes() -> None:
    bare = {"decision": "deny", "scope": "once"}
    assert client_timed_out(bare, 300.0, 300.0, 300.0)
    assert client_timed_out(bare, 299.5, 0.0, 300.0)  # no core clock: the card's
    assert client_timed_out(bare, 300.2, 600.0, 300.0)  # a longer setting
    assert not client_timed_out(bare, 12.0, 300.0, 300.0)
    assert not client_timed_out({"decision": "allow", "scope": "once"}, 400.0, 300.0, 300.0)
    assert not client_timed_out({**bare, "actor": "person"}, 400.0, 300.0, 300.0)
    assert client_timed_out({"answers": []}, 600.0, 600.0, 600.0)
    assert not client_timed_out({"answers": [{"selected": ["A"]}]}, 600.0, 600.0, 600.0)


def test_the_model_reads_nobody_answered() -> None:
    import inspect

    source = inspect.getsource(agent_loop)
    assert "was not run; nobody answered the approval" in source


# -- questions ----------------------------------------------------------------------


def _questions(session: Any) -> QuestionQueue:
    queue = QuestionQueue(Settings())
    queue.hub = _Hub({session.id: session})  # type: ignore[assignment]
    return queue


@pytest.mark.asyncio
async def test_a_question_follows_the_same_rule() -> None:
    a = _Conn("ide")
    b = _Conn("tui", reply={"answers": [{"selected": ["yes"]}]})
    session = _session(a)
    _prompted(session, b)
    queue = _questions(session)
    answers = await asyncio.wait_for(
        queue.ask(session, [QuestionItem(question="Go?", options=[])]), 5)
    assert [m for m, _ in b.calls] == ["question.request"] and not a.calls
    assert answers[0].selected == ["yes"] and answers[0].by == "origin"


@pytest.mark.asyncio
async def test_a_questions_client_timeout_is_origin_timeout() -> None:
    late = _Conn("ide", reply={"answers": []}, delay=1.05)
    request = QuestionRequest(requestId="q", sessionId="s",
                              questions=[QuestionItem(question="Go?", options=[])])
    entry = question_mod._Pending(request=request,
                                  future=asyncio.get_running_loop().create_future(),
                                  origin_conn=late, clock_sec=2.0)
    await QuestionQueue()._ask_origin(entry)
    [answer] = entry.future.result()
    assert answer.timed_out and answer.by == "origin-timeout"


@pytest.mark.asyncio
async def test_a_waiting_question_moves_with_the_person() -> None:
    a, b = _Conn("ide"), _Conn("tui", reply={"answers": [{"text": "ok"}]})
    session = _session(a)
    _prompted(session, a)
    queue = _questions(session)
    task = asyncio.ensure_future(queue.ask(session, [QuestionItem(question="Go?", options=[])]))
    await _until(lambda: a.calls)
    agent_loop.note_asker(_core(None, queue), session, snapshot(b, origin="human"))
    answers = await asyncio.wait_for(task, 5)
    assert answers[0].text == "ok"
    assert any(m == "question.resolved" and p.get("outcome") == "withdrawn" for m, p in a.notes)
