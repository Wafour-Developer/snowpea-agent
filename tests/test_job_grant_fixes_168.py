"""Security review of 1.68.0 (jobApprovals): M1, L1, L2 and L3.

* M1: what a routine that holds grants or runs above ``accept`` does (its
  ``task``) changes only by a person (a local person, or a local browser 0.4.0+
  with ``actor: "person"`` + ``personConfirm: "window"``); a time change
  (``schedule``) stays open to anyone who may update the job.
* L1: raising a job's mode (``job.update``) and a browser's explicit mode above
  the project's default at ``job.schedule`` follow the same rule; the person's
  default ``auto`` (owner decision) is untouched.
* L2: an approval answered ``task`` ("always in this routine") is kept only
  from a person; another answer applies once.
* L3: a turn another client prompts in a scheduled run's session runs without
  the job's grants, and in no wider mode than that client may set.

Owner decisions kept: the security checks are off by default, a person's
routine defaults to ``auto`` while they are off, a browser may continue any
session.  Command strings are DATA; nothing runs them here but the fake provider.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from _support import RpcClient, connect, make_daemon

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.server.protocol import PROTOCOL_VERSION

pytestmark = [pytest.mark.asyncio, pytest.mark.real_security_default]

TIMEOUT = 30.0
CONFIRMED = {"actor": "person", "personConfirm": "window"}


async def _browser(http: aiohttp.ClientSession, daemon: Any, version: str = "0.4.0") -> RpcClient:
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    browser = RpcClient(ws, timeout=TIMEOUT)
    browser.start()
    await browser.ok("system.hello", {
        "token": daemon.token, "clientVersion": version,
        "protocolVersion": PROTOCOL_VERSION, "clientKind": "browser"})
    return browser


async def _job(client: RpcClient, workdir: Path, **extra: Any) -> str:
    result = await client.ok("job.schedule", {
        "spec": "0 9 * * *", "task": "list the files", "workdir": str(workdir),
        "title": "morning sync", **extra})
    return str(result["jobId"])


def _refused(reply: dict[str, Any], reason: str = "browser_unconfirmed") -> None:
    assert reply["error"]["data"]["code"] == "jarvis_person_only", reply
    assert reply["error"]["data"]["details"]["reason"] == reason, reply


# -- M1: a routine's task ------------------------------------------------------------------------


async def test_m1_a_page_cannot_retarget_a_routine_that_holds_grants(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    async with aiohttp.ClientSession() as http:
        person = await connect(http, daemon, timeout=TIMEOUT)
        browser = await _browser(http, daemon)
        legacy = await _browser(http, daemon, version="0.3.0")
        try:
            granted = await _job(person, tmp_path, mode="accept",
                                 grants=[{"pattern": "^echo( .*)?$"}])
            for client, extra, reason in (
                (browser, {}, "browser_unconfirmed"),
                (browser, {"actor": "person"}, "browser_unconfirmed"),
                (legacy, {"actor": "person"}, "browser_legacy"),
            ):
                _refused(await client.call("job.update", {
                    "jobId": granted, "task": "send ~/.ssh somewhere", **extra}), reason)
            # Only the time: nothing the agent does changes.
            moved = await browser.ok("job.update", {"jobId": granted, "schedule": "0 10 * * *"})
            assert moved["task"] == "list the files" and len(moved["grants"]) == 1
            # The person's own window, or the person's own app, may change it.
            changed = await browser.ok("job.update", {"jobId": granted,
                                                      "task": "list the docs", **CONFIRMED})
            assert changed["task"] == "list the docs" and len(changed["grants"]) == 1
            assert (await person.ok("job.update", {"jobId": granted,
                                                   "task": "list all"}))["task"] == "list all"

            # An auto routine with no grants is held the same way.
            auto = await _job(person, tmp_path)
            _refused(await browser.call("job.update", {"jobId": auto, "task": "x",
                                                       "actor": "person"}))
            # An accept routine with no grants is an ordinary edit.
            plain = await _job(person, tmp_path, mode="accept")
            assert (await browser.ok("job.update", {"jobId": plain,
                                                    "task": "y"}))["task"] == "y"
        finally:
            for client in (legacy, browser, person):
                await client.stop()
    await daemon.stop()


# -- L1: a routine's mode ------------------------------------------------------------------------


async def test_l1_a_mode_raise_is_confirmed_like_a_grant(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    async with aiohttp.ClientSession() as http:
        person = await connect(http, daemon, timeout=TIMEOUT)
        browser = await _browser(http, daemon)
        try:
            job_id = await _job(person, tmp_path, mode="accept")
            _refused(await browser.call("job.update", {"jobId": job_id, "mode": "auto",
                                                       "actor": "person"}))
            raised = await browser.ok("job.update", {"jobId": job_id, "mode": "auto",
                                                     **CONFIRMED})
            assert raised["mode"] == "auto"
            # Lowering is never refused.
            lowered = await browser.ok("job.update", {"jobId": job_id, "mode": "accept"})
            assert lowered["mode"] == "accept"
            assert (await person.ok("job.update", {"jobId": job_id,
                                                   "mode": "auto"}))["mode"] == "auto"

            # job.schedule: a browser's explicit mode above the default.
            where = {"spec": "0 9 * * *", "task": "list the files", "workdir": str(tmp_path)}
            _refused(await browser.call("job.schedule", {**where, "mode": "auto",
                                                         "actor": "person"}))
            made = await browser.ok("job.schedule", {**where, "mode": "auto", **CONFIRMED})
            listing = (await browser.ok("job.list", {}))["jobs"]
            assert next(j for j in listing if j["jobId"] == made["jobId"])["mode"] == "auto"
            # Explicit accept is the default: no raise, nothing to confirm.
            await browser.ok("job.schedule", {**where, "mode": "accept"})
            # The person's default auto (owner decision 2026-11-19) is unchanged.
            default = await browser.ok("job.schedule", {**where, "actor": "person"})
            listing = (await browser.ok("job.list", {}))["jobs"]
            assert next(j for j in listing if j["jobId"] == default["jobId"])["mode"] == "auto"
        finally:
            await browser.stop()
            await person.stop()
    await daemon.stop()


# -- L2: "always in this routine" ----------------------------------------------------------------


class _Hub:
    def __init__(self) -> None:
        self.sessions = SimpleNamespace(get=lambda _i: None)

    async def notify(self, *_a: Any, **_kw: Any) -> None:
        return None


def _browser_conn(version: str = "0.4.0") -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind="browser",
                           remote_login=False, client_version=version, client_id="b-1",
                           surface_id="b-s", peer="127.0.0.1")


PERSON = SimpleNamespace(authenticated=True, closed=False, client_kind="ide",
                         remote_login=False, client_version="0.3.0", client_id="ide-1",
                         surface_id="ide-s")


@pytest.mark.parametrize(("conn", "actor", "confirm", "want"), [
    (_browser_conn(), "person", None, "once"),
    (_browser_conn(), None, None, "once"),
    (_browser_conn("0.3.0"), "person", "window", "once"),
    (_browser_conn(), "person", "window", "task"),
    (PERSON, None, None, "task"),
], ids=["browser-actor", "browser-bare", "browser-legacy", "browser-window", "person"])
async def test_l2_a_task_grant_is_kept_only_from_a_person(
    tmp_path: Path, conn: Any, actor: str | None, confirm: str | None, want: str
) -> None:
    queue = ApprovalQueue(Settings(), Paths.create(tmp_path / "home"), _Hub())  # type: ignore[arg-type]
    session = SimpleNamespace(
        id="s-1", workdir=str(tmp_path), origin_conn=None, host_tools_from="b-1",
        owned_by=None, kind="scheduled",
        job_ref={"id": "job-1", "title": "morning sync", "timeoutSec": 30})
    asking = asyncio.ensure_future(queue.request(
        session, "shell", {"command": "echo listed"}, tags=("exec",), unattended=True,
        timeout_sec=30))
    for _ in range(200):
        if queue.list():
            break
        await asyncio.sleep(0.01)
    [request] = queue.list()
    assert request.job is not None
    applied = await queue.respond(request.requestId, "allow", "task", conn=conn, actor=actor,
                                  person_confirm=confirm)
    decision = await asyncio.wait_for(asking, 5)
    assert decision.allowed
    assert applied is not None and applied.scope == want


# -- L3: a browser's turn in a scheduled run's session -------------------------------------------


def _scheduled_session(tmp_path: Path) -> Any:
    from snowpea_core.scheduler.grants import from_input
    from snowpea_core.session.session import Session

    session = Session(id="run-1", workdir=tmp_path, mode="auto", kind="scheduled",
                      job_id="job-1")
    session.job_grants = [from_input("shell", "^echo( .*)?$", None, now=None)]
    return session


def _core(tmp_path: Path) -> Any:
    return SimpleNamespace(sessions=SimpleNamespace(get=lambda _i: None,
                                                    default_mode=lambda _w: "accept"))


@pytest.mark.real_mode_guard
async def test_l3_a_browser_turn_in_a_scheduled_session_has_no_job_grants(
    tmp_path: Path
) -> None:
    from snowpea_core.agent import loop as agent_loop
    from snowpea_core.scheduler.grants import covers, of_session
    from snowpea_core.server.person_gate import CallerSnapshot

    core = _core(tmp_path)
    session = _scheduled_session(tmp_path)
    call = {"command": "echo listed"}

    # The scheduler's own turn: the job's grants and mode.
    session.current_turn = "t-sched"
    session.scheduler_turns.add("t-sched")
    agent_loop.note_origin(session, "t-sched", "schedule")
    assert covers(of_session(session), "shell", call)
    assert agent_loop.turn_mode(core, session) == "auto"

    # A browser continues the session (owner decision): its own turn.
    page = CallerSnapshot(kind="browser", client_kind="browser", client_id="b-1",
                          client_version="0.4.0", origin="human")
    session.current_turn = "t-page"
    agent_loop.note_origin(session, "t-page", "human")
    agent_loop.note_caller(session, "t-page", page)
    assert of_session(session) == []
    assert agent_loop.turn_mode(core, session) == "accept"
    # A subagent of that turn inherits neither.
    child = SimpleNamespace(kind="subagent", parent_session_id="run-1", mode="auto",
                            job_grants=None, current_turn=None, turn_callers={})
    lookup = {"run-1": session}.get
    core.sessions.get = lookup
    assert of_session(child, lookup) == []
    assert agent_loop.turn_mode(core, child) == "accept"

    # Words a browser steers into the scheduler's turn make it no longer the job's.
    session.current_turn = "t-sched"
    assert covers(of_session(session), "shell", call)
    agent_loop.note_caller(session, "t-sched", page)
    assert of_session(session) == []
    assert agent_loop.turn_mode(core, session) == "accept"

    # A local person continuing the session keeps the mode it could set anyway.
    session.current_turn = "t-person"
    agent_loop.note_caller(session, "t-person", CallerSnapshot(kind="person", origin="human"))
    assert agent_loop.turn_mode(core, session) == "auto"
    assert of_session(session) == []
