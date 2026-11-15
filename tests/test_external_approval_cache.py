"""Security review H1: an external session's "allow for this session" never skips the engine.

The fake Claude Code CLI asks; the person allows ``git status`` for the
session.  The same call is then answered from the session's memory, but a
push (or any other call) goes to the queue again — to the person — and a
guarded request is never remembered at all.  The command strings are data:
nothing here runs them (the fake CLI's ``ApprovalAsk`` is built in the test).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _support import env_vars
from test_external_manager import Conn, Env, until

from snowpea_core.external.base import ApprovalAsk

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    setup = Env(tmp_path)
    with env_vars(FAKE_CLI_LOG=str(setup.log), FAKE_CLI_SCENARIO=None,
                  FAKE_CLI_SCENARIO_FILE=None):
        await setup.manager.start()
        try:
            yield setup
        finally:
            await setup.manager.shutdown()


async def _ask(setup: Env, record: Any, ask: ApprovalAsk) -> tuple[Any, dict[str, Any]]:
    before = len(setup.hub.pending())
    task = asyncio.ensure_future(setup.manager._ask_approval(record, ask))
    fresh = await until(lambda: setup.hub.pending()[before:])
    return task, fresh[-1]


async def test_session_allow_is_per_exact_call_and_never_skips_the_engine(env: Env) -> None:
    person = Conn("ide")
    ref = (await env.manager.create("claude", str(env.project), conn=person))["session"]["ref"]
    record = env.manager._records[("claude", ref["id"])]

    status = ApprovalAsk(tool="Bash", args={"command": "git status"}, risk="low")
    task, request = await _ask(env, record, status)
    await env.approvals.respond(request["requestId"], "allow", "session", conn=person)
    first = await task
    assert first.allow and first.scope == "session"
    # The very same call again: the session's memory answers.
    again = await env.manager._ask_approval(record, status)
    assert again.allow and again.by == "cache"

    # A push is not "git status": it goes to the queue (to the person).
    push = ApprovalAsk(tool="Bash", args={"command": "git push --force origin main"},
                       risk="high")
    task, request = await _ask(env, record, push)
    assert request["args"]["command"] == "git push --force origin main"
    await env.approvals.respond(request["requestId"], "deny", conn=person)
    answer = await task
    assert not answer.allow and answer.by != "cache"

    # Another git subcommand that only reads is a different call too.
    log = ApprovalAsk(tool="Bash", args={"command": "git log -1"}, risk="low")
    task, request = await _ask(env, record, log)
    await env.approvals.respond(request["requestId"], "deny", conn=person)
    assert (await task).by != "cache"


async def test_a_guarded_call_is_never_remembered(env: Env) -> None:
    person = Conn("ide")
    ref = (await env.manager.create("claude", str(env.project), conn=person))["session"]["ref"]
    record = env.manager._records[("claude", ref["id"])]
    push = ApprovalAsk(tool="Bash", args={"command": "git push origin main"}, risk="high")
    task, request = await _ask(env, record, push)
    await env.approvals.respond(request["requestId"], "allow", "session", conn=person)
    assert (await task).allow
    assert not record.approval_cache  # a push is asked every time
    task, request = await _ask(env, record, push)
    await env.approvals.respond(request["requestId"], "deny", conn=person)
    assert not (await task).allow


async def test_one_write_allowed_for_the_session_is_not_every_write(env: Env) -> None:
    person = Conn("ide")
    ref = (await env.manager.create("claude", str(env.project), conn=person))["session"]["ref"]
    record = env.manager._records[("claude", ref["id"])]
    inside = ApprovalAsk(tool="Write", args={"file_path": str(env.project / "a.py"),
                                             "content": "x = 1\n"}, risk="medium")
    task, request = await _ask(env, record, inside)
    await env.approvals.respond(request["requestId"], "allow", "session", conn=person)
    assert (await task).allow
    assert (await env.manager._ask_approval(record, inside)).by == "cache"

    other = ApprovalAsk(tool="Write", args={"file_path": str(env.project / "b.py"),
                                            "content": "y = 2\n"}, risk="medium")
    task, request = await _ask(env, record, other)
    await env.approvals.respond(request["requestId"], "deny", conn=person)
    assert (await task).by != "cache"

    outside = ApprovalAsk(tool="Write", args={"file_path": str(Path(env.outside) / "x.txt"),
                                              "content": ""}, risk="high")
    task, request = await _ask(env, record, outside)
    await env.approvals.respond(request["requestId"], "deny", conn=person)
    assert (await task).by != "cache"
