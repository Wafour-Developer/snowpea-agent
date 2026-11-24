"""1.68.0 (jobApprovals): a scheduled run's approval reaches a person.

The owner's report (2026-11-19): a 09:00 routine's first shell command was
asked, nobody saw a card anywhere, the run ended "declined" after five minutes
and the job's row did not say why.  These tests drive a real daemon with the
scripted fake provider ("list the files" runs ``shell: echo listed`` in accept
mode, which the mode matrix asks for, with the security checks off — the 1.66
default):

* the request is broadcast to a client that never opened the run's session,
  names the job (``ApprovalRequest.job``) and is announced as ``job.event
  approval_needed``; ``job.list`` shows it as ``waitingApproval``;
* answered ``task`` it is kept with the job and the next run does not ask;
* unanswered, the run is recorded with the reason ("approval needed: …");
* grants given with job.schedule / job.update skip the first ask, are listed
  and removable, and only a person may give them; a mode raise is a person's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.scheduler.grants import covers, from_input
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION

# Security off, as a person on 1.66.0+ has it unless they turned the checks on.
pytestmark = [pytest.mark.asyncio, pytest.mark.real_security_default]

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "scheduler.json"
TIMEOUT = 30.0


@pytest_asyncio.fixture
async def provider_env() -> AsyncIterator[None]:
    with fake_provider(FIXTURE):
        yield


async def _job(client: RpcClient, workdir: Path, **extra: Any) -> str:
    result = await client.ok("job.schedule", {
        "spec": "0 9 * * *", "task": "list the files", "mode": "accept",
        "workdir": str(workdir), "title": "morning sync", **extra,
    })
    return str(result["jobId"])


async def _run_now(client: RpcClient, job_id: str) -> None:
    # An occurrence is keyed by its second: a second run must not share it.
    await asyncio.sleep(1.1)
    await client.ok("job.runNow", {"jobId": job_id}, timeout=40)


async def _row(client: RpcClient, job_id: str) -> dict[str, Any]:
    listing = await client.ok("job.list", {})
    return next(item for item in listing["jobs"] if item["jobId"] == job_id)


async def test_capability_and_version() -> None:
    assert "jobApprovals" in CAPABILITIES and PROTOCOL_VERSION == "1.75.0"


async def test_a_routine_approval_reaches_another_client_and_task_scope_persists(
    tmp_path: Path, provider_env: None
) -> None:
    daemon = await make_daemon(tmp_path / "home", {"scheduler": {"approvalTimeoutSec": 30}})
    assert daemon.core is not None
    # The 1.66 default: the security checks are off; the mode rule still asks.
    assert daemon.core.settings.security.enabled is False
    async with aiohttp.ClientSession() as http:
        owner = await connect(http, daemon, timeout=TIMEOUT, approval_mode="ignore")
        other = await connect(http, daemon, timeout=TIMEOUT)
        try:
            job_id = await _job(owner, tmp_path)
            run = asyncio.ensure_future(owner.ok("job.runNow", {"jobId": job_id}, timeout=40))
            pending = await other.wait_notification("approval.pending")
            request = pending["request"]
            assert request["job"] == {"id": job_id, "title": "morning sync"}
            assert request["timeoutSec"] == 30
            assert request["args"]["command"] == "echo listed"
            needed = [e for e in other.of_method("job.event") if e["kind"] == "approval_needed"]
            assert needed and needed[0]["jobId"] == job_id
            assert needed[0]["payload"]["requestId"] == request["requestId"]
            assert needed[0]["payload"]["title"] == "morning sync"
            waiting = (await _row(other, job_id))["waitingApproval"]
            assert waiting["requestId"] == request["requestId"]
            assert waiting["summary"] == "echo listed"

            answer = await other.ok("approval.respond", {
                "requestId": request["requestId"], "decision": "allow", "scope": "task"})
            assert answer["scope"] == "task"
            await run
            row = await _row(other, job_id)
            assert row["lastStatus"] == "ok" and row["lastError"] is None
            assert [(g["tool"], g["kind"], g["by"]) for g in row["grants"]] == [
                ("shell", "rule", "approval")]
            assert row["waitingApproval"] is None

            # The next run opens a new session and is not asked again.
            asked = len(other.of_method("approval.pending"))
            await _run_now(owner, job_id)
            assert len(other.of_method("approval.pending")) == asked
            assert (await _row(other, job_id))["lastStatus"] == "ok"
        finally:
            await owner.stop()
            await other.stop()
    await daemon.stop()


async def test_an_unanswered_routine_approval_is_recorded_with_its_reason(
    tmp_path: Path, provider_env: None
) -> None:
    daemon = await make_daemon(
        tmp_path / "home",
        # approvals.timeoutSec no longer decides a routine's wait.
        {"approvals": {"timeoutSec": 600}, "scheduler": {"approvalTimeoutSec": 1}},
    )
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon, timeout=TIMEOUT, approval_mode="ignore")
        try:
            job_id = await _job(client, tmp_path)
            await _run_now(client, job_id)
            row = await _row(client, job_id)
            assert row["lastStatus"] == "denied_by_timeout"
            assert row["lastError"].startswith("approval needed: shell `echo listed`")
            assert "always in this routine" in row["lastError"]
            assert row["lastSessionId"]
            runs = (await client.ok("job.runs", {"jobId": job_id}))["runs"]
            assert runs[0]["error"] == row["lastError"]
            assert runs[0]["sessionId"] == row["lastSessionId"]
            kinds = [e["kind"] for e in client.of_method("job.event")]
            assert kinds == ["started", "approval_needed", "denied"]
        finally:
            await client.stop()
    await daemon.stop()


async def test_grants_given_with_the_job_skip_the_first_ask_and_can_be_removed(
    tmp_path: Path, provider_env: None
) -> None:
    daemon = await make_daemon(tmp_path / "home", {"scheduler": {"approvalTimeoutSec": 1}})
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon, timeout=TIMEOUT, approval_mode="ignore")
        try:
            job_id = await _job(client, tmp_path, grants=[{"pattern": "^echo( .*)?$"}])
            await _run_now(client, job_id)
            row = await _row(client, job_id)
            assert row["lastStatus"] == "ok"
            assert not client.of_method("approval.pending")
            [grant] = row["grants"]
            assert grant["by"] == "person" and grant["display"] == "echo( .*)?"

            updated = await client.ok("job.update", {"jobId": job_id,
                                                     "removeGrants": [grant["id"]]})
            assert updated["grants"] == []
            await _run_now(client, job_id)
            assert (await _row(client, job_id))["lastStatus"] == "denied_by_timeout"

            # The exact line, added later, covers it again.
            updated = await client.ok("job.update", {"jobId": job_id,
                                                     "addGrants": [{"line": "echo  listed"}]})
            assert [g["line"] for g in updated["grants"]] == ["echo listed"]
            await _run_now(client, job_id)
            assert (await _row(client, job_id))["lastStatus"] == "ok"
        finally:
            await client.stop()
    await daemon.stop()


async def test_a_mode_set_on_the_job_runs_it_unasked(
    tmp_path: Path, provider_env: None
) -> None:
    daemon = await make_daemon(tmp_path / "home", {"scheduler": {"approvalTimeoutSec": 1}})
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon, timeout=TIMEOUT, approval_mode="ignore")
        try:
            job_id = await _job(client, tmp_path)
            updated = await client.ok("job.update", {"jobId": job_id, "mode": "auto",
                                                     "approvalTimeoutSec": 120})
            assert updated["mode"] == "auto" and updated["approvalTimeoutSec"] == 120
            await _run_now(client, job_id)
            assert (await _row(client, job_id))["lastStatus"] == "ok"
            assert not client.of_method("approval.pending")
        finally:
            await client.stop()
    await daemon.stop()


async def test_only_a_person_gives_grants_and_never_a_catch_all(
    tmp_path: Path, provider_env: None
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon, timeout=TIMEOUT)
        ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
        browser = RpcClient(ws, timeout=TIMEOUT)
        browser.start()
        try:
            await browser.ok("system.hello", {
                "token": daemon.token, "clientVersion": "0.4.0",
                "protocolVersion": PROTOCOL_VERSION, "clientKind": "browser"})
            bad = await client.call("job.schedule", {
                "spec": "0 9 * * *", "task": "list the files", "workdir": str(tmp_path),
                "grants": [{"pattern": ".*"}]})
            assert bad["error"]["data"]["code"] == "invalid_params"
            job_id = await _job(client, tmp_path)
            refused = await browser.call("job.update", {
                "jobId": job_id, "addGrants": [{"pattern": "^echo( .*)?$"}]})
            assert refused["error"]["data"]["code"] == "jarvis_person_only"
            assert refused["error"]["data"]["details"]["reason"] == "browser_unconfirmed"
            confirmed = await browser.ok("job.update", {
                "jobId": job_id, "addGrants": [{"pattern": "^echo( .*)?$"}],
                "actor": "person", "personConfirm": "window"})
            assert len(confirmed["grants"]) == 1
            # Removing one is no widening: anyone who may update the job.
            gone = await browser.ok("job.update", {
                "jobId": job_id, "removeGrants": [confirmed["grants"][0]["id"]]})
            assert gone["grants"] == []
        finally:
            await browser.stop()
            await client.stop()
    await daemon.stop()


async def test_grant_matching_is_the_allowlist_s() -> None:
    rule = from_input("shell", "^git fetch( .*)?$", None, now=None)
    assert covers([rule], "shell", {"command": "git fetch --prune"})
    # A chain is covered only when each plain part is.
    assert not covers([rule], "shell", {"command": "git fetch && git merge --ff-only"})
    merge = from_input("shell", "^git merge( .*)?$", None, now=None)
    assert covers([rule, merge], "shell", {"command": "git fetch && git merge --ff-only"})
    # Never a substitution, a redirect or another tool.
    assert not covers([rule], "shell", {"command": "git fetch $(id)"})
    assert not covers([rule], "shell", {"command": "git fetch > out"})
    assert not covers([rule], "bash_other", {"command": "git fetch"})
    line = from_input("shell", None, "git -C /r rev-parse HEAD; git status --porcelain",
                      now=None)
    assert covers([line], "shell", {"command": "git -C /r rev-parse HEAD;  git status --porcelain"})
    assert not covers([line], "shell", {"command": "git -C /r rev-parse HEAD; id"})
    with pytest.raises(ValueError):
        from_input("shell", ".*", None, now=None)
    with pytest.raises(ValueError):
        from_input("shell", None, None, now=None)
    with pytest.raises(ValueError):
        from_input("repl.send", None, None, now=None)


# ---------------------------------------------------------------------------
# the routine's default mode (owner decision 2026-11-19)
# ---------------------------------------------------------------------------


async def _browser(http: aiohttp.ClientSession, daemon: Any) -> RpcClient:
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    browser = RpcClient(ws, timeout=TIMEOUT)
    browser.start()
    await browser.ok("system.hello", {
        "token": daemon.token, "clientVersion": "0.4.0",
        "protocolVersion": PROTOCOL_VERSION, "clientKind": "browser"})
    return browser


async def _mode_of(client: RpcClient, params: dict[str, Any]) -> str:
    made = await client.ok("job.schedule", {"spec": "0 9 * * *", "task": "list the files",
                                            **params})
    return str((await _row(client, made["jobId"]))["mode"])


async def test_a_persons_routine_defaults_to_auto_while_the_checks_are_off(
    tmp_path: Path, provider_env: None
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    async with aiohttp.ClientSession() as http:
        person = await connect(http, daemon, timeout=TIMEOUT)
        browser = await _browser(http, daemon)
        try:
            where = {"workdir": str(tmp_path)}
            assert await _mode_of(person, where) == "auto"
            # An explicit mode always wins.
            assert await _mode_of(person, {**where, "mode": "accept"}) == "accept"
            assert await _mode_of(person, {**where, "mode": "plan"}) == "plan"
            # A browser's click is its person's; without the marker it is not.
            assert await _mode_of(browser, {**where, "actor": "person"}) == "auto"
            assert await _mode_of(browser, where) == "accept"

            # /schedule typed in the TUI is the person's form too.
            session = await person.ok("session.create", {"workdir": str(tmp_path),
                                                         "mode": "accept"})
            turn = await person.ok("session.prompt", {"sessionId": session["sessionId"],
                                                      "text": '/schedule "in 60s" "echo hi"'})
            await person.wait_turn(turn["turnId"])
            listing = await person.ok("job.list", {})
            [typed] = [job for job in listing["jobs"] if job["task"] == "echo hi"]
            assert typed["mode"] == "auto"
        finally:
            await browser.stop()
            await person.stop()
    await daemon.stop()


async def test_with_the_checks_on_a_routine_keeps_accept(
    tmp_path: Path, provider_env: None
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    assert daemon.core is not None
    # Switched on by the person (a stored true nobody chose is migrated off, 1.66.0).
    daemon.core.settings.security.enabled = True
    async with aiohttp.ClientSession() as http:
        person = await connect(http, daemon, timeout=TIMEOUT)
        try:
            assert await _mode_of(person, {"workdir": str(tmp_path)}) == "accept"
        finally:
            await person.stop()
    await daemon.stop()


async def test_the_models_routine_stays_accept() -> None:
    """``schedule_create`` (the model) never takes the person's default."""
    from types import SimpleNamespace

    from snowpea_core.scheduler.access import default_job_mode

    off = SimpleNamespace(settings=SimpleNamespace(security=SimpleNamespace(enabled=False)))
    for who in (SimpleNamespace(kind="none"), SimpleNamespace(kind="connector"),
                SimpleNamespace(kind="remote_login"), SimpleNamespace(kind="agent"),
                SimpleNamespace(kind="browser", actor=None)):
        assert default_job_mode(off, who) == "accept"
    assert default_job_mode(off, SimpleNamespace(kind="person")) == "auto"
