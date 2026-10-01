"""The owner's active session shares its questions and approvals with their chat.

The *active* session is the one the owner last prompted from a human surface
(``session.prompt`` / ``session.steer`` / ``session.setActive``).  Its
``ask_user`` batches and approval requests are posted, with the usual
buttons, to each binding's owner chat (``gateway.<platform>.shareActive``,
default on wherever the binding has an approver).  Other sessions' asks stay
where they were.

Same shape as ``test_gateway_chat.py``: a real in-process daemon, a real RPC
client standing in for the IDE, and ``SNOWPEA_GATEWAY_FAKE=1``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest_asyncio
from _support import PROVIDER_FIXTURES, connect, env_vars, make_daemon

from snowpea_core.gateway.base import (
    approval_callback,
    parse_question_callback,
    question_callback,
)
from snowpea_core.gateway.fake import FakeAdapter
from snowpea_core.server.app_server import Core, Daemon
from snowpea_core.server.protocol import QuestionItem, QuestionOption

TIMEOUT = 10.0
FIXTURE = PROVIDER_FIXTURES / "gateway_chat.json"
#: The approver; a Telegram private chat's id is the user's id.
OWNER = "u1"


@pytest_asyncio.fixture
async def share_env() -> AsyncIterator[None]:
    with env_vars(SNOWPEA_PROVIDER=f"fake:{FIXTURE}", SNOWPEA_GATEWAY_FAKE="1"):
        FakeAdapter.instances.clear()
        try:
            yield None
        finally:
            FakeAdapter.instances.clear()


def _core(daemon: Daemon) -> Core:
    assert daemon.core is not None
    return daemon.core


async def _bind(client: Any, workdir: Path, *, user_id: str | None = OWNER) -> FakeAdapter:
    """A catch-all telegram binding, the shape the setup wizard makes."""
    params: dict[str, Any] = {
        "platform": "telegram",
        "credentialsRef": "tg_share",
        "target": {"new_session": {"workdir": str(workdir), "mode": "auto"}},
    }
    if user_id:
        params["userId"] = user_id
    await client.ok("gateway.bind", params)
    return FakeAdapter.instances["tg_share"]


async def _ide_session(client: Any, workdir: Path) -> str:
    created = await client.ok("session.create", {"workdir": str(workdir)})
    return str(created["sessionId"])


def _items(*questions: str, other: bool = True) -> list[QuestionItem]:
    return [
        QuestionItem(
            question=text,
            header=f"q{index}",
            options=[QuestionOption(label="yes"), QuestionOption(label="no")],
            allowOther=other,
        )
        for index, text in enumerate(questions, start=1)
    ]


async def _asked(client: Any) -> dict[str, Any]:
    """The ``question.request`` the IDE (the session's origin) was sent."""
    for _ in range(int(TIMEOUT / 0.02)):
        if client.questions:
            return dict(client.questions[-1])
        await asyncio.sleep(0.02)
    raise AssertionError("the IDE was never asked")


async def _quiet(adapter: FakeAdapter, before: int) -> None:
    """Give the daemon a moment, then assert nothing new reached the chat."""
    await asyncio.sleep(0.3)
    assert len(adapter.sent) == before, adapter.texts()[before:]


# ---------------------------------------------------------------------------
# which session is active
# ---------------------------------------------------------------------------


async def test_an_ide_prompt_marks_its_session_active_and_a_chat_prompt_does_not(
    share_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        client = await connect(http, daemon)
        core = _core(daemon)
        first = await _ide_session(client, tmp_path)
        second = await _ide_session(client, tmp_path)
        assert core.sessions.active_id is None

        await client.ok("session.prompt", {"sessionId": first, "text": "/mode"})
        assert core.sessions.active_id == first
        assert core.sessions.active_at

        await client.ok("session.steer", {"sessionId": second, "text": "/mode"})
        assert core.sessions.active_id == second

        await client.ok("session.setActive", {"sessionId": first})
        assert core.sessions.active_id == first

        # A message from the messenger starts its own session but never makes
        # it the active one.
        adapter = await _bind(client, tmp_path)
        await adapter.push(f"/new {tmp_path}", channel_id=OWNER, user_id=OWNER)
        await adapter.wait_for_send(TIMEOUT)
        assert core.sessions.active_id == first
        await client.stop()
    finally:
        await daemon.stop()


async def test_a_subagent_or_job_session_never_becomes_active(
    share_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        client = await connect(http, daemon)
        core = _core(daemon)
        parent = await _ide_session(client, tmp_path)
        await client.ok("session.setActive", {"sessionId": parent})
        child = await core.sessions.create(
            workdir=tmp_path, kind="subagent", parent_session_id=parent
        )
        job = await core.sessions.create(workdir=tmp_path, kind="scheduled", job_id="j-1")

        assert core.sessions.mark_active(child) is False
        assert core.sessions.mark_active(job) is False
        refused = await client.call("session.setActive", {"sessionId": child.id})
        assert refused["error"]["data"]["code"] == "invalid_params"
        await client.ok("session.prompt", {"sessionId": job.id, "text": "/mode"})
        assert core.sessions.active_id == parent
        await client.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------


async def test_the_active_sessions_question_is_posted_and_answered_by_button(
    share_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        ide = await connect(http, daemon)  # never answers: the owner is on the phone
        core = _core(daemon)
        active = await _ide_session(ide, tmp_path)
        session = core.sessions.get(active)
        assert session is not None
        session.title = "renderer pick"
        await ide.ok("session.setActive", {"sessionId": active})
        adapter = await _bind(ide, tmp_path)

        task = asyncio.ensure_future(core.questions.ask(session, _items("first?", "second?")))
        posted = await adapter.wait_for_send(TIMEOUT)
        assert posted.channel_id == OWNER  # the approver's DM
        assert posted.text.startswith(f"[renderer pick · {active[:8]}]")
        assert "first?" in posted.text and "(1/2)" in posted.text
        request_id = parse_question_callback(posted.button_data()[0])[0]  # type: ignore[index]

        # Only the approver may answer a shared question.
        await adapter.press(question_callback(request_id, "1"), channel_id=OWNER, user_id="u2")
        await _quiet(adapter, 1)
        assert not task.done()

        await adapter.press(question_callback(request_id, "2"), channel_id=OWNER)
        second = await adapter.wait_for_send(TIMEOUT, count=2)
        assert "second?" in second.text and "(2/2)" in second.text

        # "Other" makes the next typed line the answer, not a prompt.
        await adapter.press(question_callback(request_id, "other"), channel_id=OWNER)
        await adapter.wait_for_send(TIMEOUT, count=3)
        await adapter.push("babylon", channel_id=OWNER, user_id=OWNER)
        answers = await asyncio.wait_for(task, TIMEOUT)
        assert answers[0].selected == ["no"]
        assert answers[1].text == "babylon"
        assert answers[0].by == f"gateway:telegram:{OWNER}"
        # Answered right here: no "answered by" echo, and no turn was started.
        await _quiet(adapter, 3)
        assert core.sessions.active_id == active
        await ide.stop()
    finally:
        await daemon.stop()


async def test_another_sessions_question_is_not_shared(
    share_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        ide = await connect(http, daemon)
        core = _core(daemon)
        active = await _ide_session(ide, tmp_path)
        other = await _ide_session(ide, tmp_path)
        await ide.ok("session.setActive", {"sessionId": active})
        adapter = await _bind(ide, tmp_path)
        session = core.sessions.get(other)
        assert session is not None

        task = asyncio.ensure_future(core.questions.ask(session, _items("elsewhere?")))
        asked = await _asked(ide)
        await _quiet(adapter, 0)
        await ide.ok(
            "question.respond",
            {"requestId": asked["requestId"], "answers": [{"selected": ["yes"]}]},
        )
        await asyncio.wait_for(task, TIMEOUT)
        await _quiet(adapter, 0)
        await ide.stop()
    finally:
        await daemon.stop()


async def test_a_question_answered_in_the_ide_is_closed_in_the_chat(
    share_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        ide = await connect(http, daemon)
        watcher = await connect(http, daemon)
        core = _core(daemon)
        active = await _ide_session(ide, tmp_path)
        await ide.ok("session.setActive", {"sessionId": active})
        adapter = await _bind(ide, tmp_path)
        session = core.sessions.get(active)
        assert session is not None

        task = asyncio.ensure_future(core.questions.ask(session, _items("which?")))
        posted = await adapter.wait_for_send(TIMEOUT)
        request_id = parse_question_callback(posted.button_data()[0])[0]  # type: ignore[index]
        await watcher.ok(
            "question.respond", {"requestId": request_id, "answers": [{"selected": ["yes"]}]}
        )
        await asyncio.wait_for(task, TIMEOUT)
        closed = await adapter.wait_for_send(TIMEOUT, count=2)
        assert f"question {request_id}: answered" in closed.text
        # A late press is ignored rather than answering anything.
        await adapter.press(question_callback(request_id, "1"), channel_id=OWNER)
        await _quiet(adapter, 2)
        await ide.stop()
        await watcher.stop()
    finally:
        await daemon.stop()


async def test_no_double_post_when_the_chats_own_session_is_active(
    share_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        ide = await connect(http, daemon)
        core = _core(daemon)
        adapter = await _bind(ide, tmp_path)
        await adapter.push(f"/new {tmp_path}", channel_id=OWNER, user_id=OWNER)
        opened = await adapter.wait_for_send(TIMEOUT)
        chat_session = opened.text.split()[1]
        await ide.ok("session.setActive", {"sessionId": chat_session})
        session = core.sessions.get(chat_session)
        assert session is not None

        task = asyncio.ensure_future(core.questions.ask(session, _items("once?")))
        await adapter.wait_for_send(TIMEOUT, count=2)
        await _quiet(adapter, 2)
        assert len(adapter.with_buttons()) == 1
        assert not adapter.sent[-1].text.startswith("[")  # the chat's own, unlabelled
        await adapter.push("1", channel_id=OWNER, user_id=OWNER)
        answers = await asyncio.wait_for(task, TIMEOUT)
        assert answers[0].selected == ["yes"]
        await ide.stop()
    finally:
        await daemon.stop()


async def test_share_active_false_turns_it_off(
    share_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(
        tmp_path / "home", {"gateway": {"telegram": {"shareActive": False}}}
    )
    try:
        ide = await connect(http, daemon)
        core = _core(daemon)
        active = await _ide_session(ide, tmp_path)
        await ide.ok("session.setActive", {"sessionId": active})
        adapter = await _bind(ide, tmp_path)
        session = core.sessions.get(active)
        assert session is not None

        task = asyncio.ensure_future(core.questions.ask(session, _items("quiet?")))
        await _asked(ide)
        await _quiet(adapter, 0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ide.stop()
    finally:
        await daemon.stop()


async def test_a_binding_without_an_approver_shares_nothing(
    share_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        ide = await connect(http, daemon)
        core = _core(daemon)
        active = await _ide_session(ide, tmp_path)
        await ide.ok("session.setActive", {"sessionId": active})
        adapter = await _bind(ide, tmp_path, user_id=None)
        session = core.sessions.get(active)
        assert session is not None

        task = asyncio.ensure_future(core.questions.ask(session, _items("anyone?")))
        await _quiet(adapter, 0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ide.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


async def test_the_active_sessions_approval_is_answered_from_the_chat(
    share_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """An interactive approval is asked of the IDE alone, yet still reaches the owner."""
    daemon = await make_daemon(tmp_path / "home")
    try:
        ide = await connect(http, daemon, approval_mode="ignore")
        core = _core(daemon)
        active = await _ide_session(ide, tmp_path)
        await ide.ok("session.setActive", {"sessionId": active})
        adapter = await _bind(ide, tmp_path)
        session = core.sessions.get(active)
        assert session is not None

        task = asyncio.ensure_future(
            core.approvals.request(session, "shell", {"command": "rm -rf build"})
        )
        posted = await adapter.wait_for_send(TIMEOUT)
        assert posted.channel_id == OWNER
        assert posted.text.startswith(f"[{tmp_path.name} · {active[:8]}]")
        assert "rm -rf build" in posted.text
        request_id = posted.button_data()[0].split(":")[1]
        assert ide.approval_requests[0]["requestId"] == request_id

        # Someone else in the chat cannot approve it.
        await adapter.press(approval_callback(request_id, "allow"), channel_id=OWNER, user_id="u2")
        await _quiet(adapter, 1)
        assert not task.done()

        await adapter.press(approval_callback(request_id, "allow"), channel_id=OWNER)
        decision = await asyncio.wait_for(task, TIMEOUT)
        assert decision.allowed and decision.by == f"gateway:telegram:{OWNER}"
        closed = await adapter.wait_for_send(TIMEOUT, count=2)
        assert f"approval {request_id}: allow" in closed.text
        # The IDE is told, so its dialog closes.
        resolved = await ide.wait_notification("approval.resolved", TIMEOUT)
        assert resolved["requestId"] == request_id
        await ide.stop()
    finally:
        await daemon.stop()


async def test_an_approval_answered_in_the_ide_is_closed_in_the_chat(
    share_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        ide = await connect(http, daemon, approval_mode="deny")
        core = _core(daemon)
        active = await _ide_session(ide, tmp_path)
        await ide.ok("session.setActive", {"sessionId": active})
        adapter = await _bind(ide, tmp_path)
        session = core.sessions.get(active)
        assert session is not None

        decision = await asyncio.wait_for(
            core.approvals.request(session, "shell", {"command": "make"}), TIMEOUT
        )
        assert not decision.allowed
        closed = await adapter.wait_for_send(TIMEOUT, count=2)
        assert closed.text.endswith("deny (by origin)")
        await ide.stop()
    finally:
        await daemon.stop()


async def test_another_sessions_approval_is_not_shared(
    share_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        ide = await connect(http, daemon, approval_mode="allow")
        core = _core(daemon)
        active = await _ide_session(ide, tmp_path)
        other = await _ide_session(ide, tmp_path)
        await ide.ok("session.setActive", {"sessionId": active})
        adapter = await _bind(ide, tmp_path)
        session = core.sessions.get(other)
        assert session is not None

        decision = await asyncio.wait_for(
            core.approvals.request(session, "shell", {"command": "ls"}), TIMEOUT
        )
        assert decision.allowed
        await _quiet(adapter, 0)
        await ide.stop()
    finally:
        await daemon.stop()
