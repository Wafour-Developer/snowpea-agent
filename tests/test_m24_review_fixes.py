"""Security review of "a block asks the person" (0.2.94, protocol 1.61.0).

H1: moving, hard-linking or archiving a secret away is a read.  H2: a read is
excused by a write of the same word only from the same command.  H3/M1: a
messenger or a remote web login never allows a blocked call.  L1: a browser
answers only a shell, code, file or host page tool's security ask.  L3: the
audit trail names who answered.

Every command line below is DATA: a Python string handed to the pure checker or
to an approval queue whose calls never run anything.  ``/home/u`` is fictional.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from test_m24_person_override import Conn, _pending, _queue, _session

from snowpea_core.security import written
from snowpea_core.security.check import Context, check_command
from snowpea_core.security.lists import Lists
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

USER = "/home/u"
HOME = f"{USER}/.snowpea"
WORK = f"{USER}/proj"
ESSENTIAL = Lists(command_presets=("essential",), path_presets=("essential",))
EVERY_LIST = (ESSENTIAL, Lists())


@pytest.fixture(autouse=True)
def _fresh() -> None:
    written.reset()


def _ctx() -> Context:
    return Context(workdir=Path(WORK), user_home=Path(USER), home=Path(HOME),
                   projects=(WORK,), mounts=(), env_venv=False)


def _line(line: str, lists: Lists) -> Any:
    return check_command(line, lists, _ctx(), cwd=Path(WORK))


SECRET_READS = [
    # H1: the content lives on under another name.
    "mv ~/.snowpea/token /tmp/t; cat /tmp/t",
    "ln ~/.snowpea/token ./t && cat ./t",
    "mv ~/.snowpea /tmp/h; cat /tmp/h/daemon.json",
    "mv ~/.snowpea/token /tmp/t",
    "ln ~/.snowpea/credentials.json ./c",
    "mv ~/.snowpea/* /tmp/x",
    "mv /home/u /tmp/x",
    "rename s/a/b/ ~/.snowpea/token",
    "tar cf /tmp/a.tar --remove-files ~/.snowpea/token",
    "mv ~/.claude/.credentials.json /tmp/t; cat /tmp/t",
    "ln ~/.claude/.credentials.json ./t && cat ./t",
    "mv ~/.claude /tmp/c; cat /tmp/c/.credentials.json",
    "mv ~/.codex/auth.json /tmp/t; cat /tmp/t",
    "ln ~/.codex/auth.json ./t && cat ./t",
    "mv ~/.codex /tmp/c",
    # H2: a write by ANOTHER command does not hide the read.
    "cat ~/.snowpea/token; touch ~/.snowpea/token",
    "cat ~/.snowpea/token; tee -a ~/.snowpea/token < /dev/null",
    "cat ~/.snowpea/token; : > ~/.snowpea/token",
    "cat ~/.snowpea/daemon.json && touch ~/.snowpea/daemon.json",
    "cat ~/.codex/auth.json; touch ~/.codex/auth.json",
]


@pytest.mark.parametrize("line", SECRET_READS)
def test_relocating_or_rewriting_a_secret_is_still_a_read(line: str) -> None:
    for lists in EVERY_LIST:
        verdict = _line(line, lists)
        assert verdict.blocked, line
        assert verdict.secret_read, line
        assert not verdict.person_may_allow, line


@pytest.mark.parametrize("line", [
    "rm ~/.snowpea/token",
    "unlink ~/.snowpea/token",
    "shred ~/.snowpea/token",
    "rm ~/.codex/auth.json",
    "touch ~/.snowpea/token",
    ": > ~/.snowpea/token",
    "mv /tmp/x ~/.snowpea/token",
    "mv ~/.snowpea/trust/projects.json /tmp/p",
])
def test_a_pure_delete_or_write_of_a_secret_stays_a_person_ask(line: str) -> None:
    for lists in EVERY_LIST:
        verdict = _line(line, lists)
        assert verdict.blocked, line
        assert not verdict.secret_read, line
        assert verdict.person_may_allow, line


async def test_a_relocated_secret_is_refused_with_a_person_present(tmp_path: Path) -> None:
    queue, hub = _queue(tmp_path)
    origin = Conn("ide", "ide-1", reply={"decision": "allow"})
    for mode in ("plan", "ask", "accept", "auto"):
        session = _session(tmp_path, origin_conn=origin, mode=mode)
        for line in (f"mv {tmp_path}/home/token /tmp/t; cat /tmp/t",
                     f"cat {tmp_path}/home/token; touch {tmp_path}/home/token"):
            decision = await queue.request(session, "shell", {"command": line},
                                           timeout_sec=5, tags=("exec",))
            assert not decision.allowed and decision.code == errors.SECURITY_BLOCKED, line
    assert queue.list() == []
    assert len(hub.of("security.blocked")) == 8


# -- H3 / M1: only a person at this machine allows a blocked call ---------------------------


BLOCKED_LINE = {"command": "rm -rf ~"}


@pytest.mark.parametrize(("by", "conn", "reason"), [
    ("gateway:telegram:42", None, "messenger_blocked"),
    ("web", Conn("web", "w-1", remote=True), "remote_blocked"),
])
async def test_a_messenger_or_remote_login_never_allows_a_blocked_call(
    tmp_path: Path, by: str, conn: Any, reason: str,
) -> None:
    queue, _hub = _queue(tmp_path)
    session = _session(tmp_path)
    task = asyncio.ensure_future(queue.request(session, "shell", dict(BLOCKED_LINE),
                                               unattended=True, timeout_sec=30,
                                               tags=("exec",)))
    pending = await _pending(queue)
    assert pending.blocked is not None
    with pytest.raises(RpcError) as caught:
        await queue.respond(pending.requestId, "allow", by=by, conn=conn)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert caught.value.data["reason"] == reason
    assert "at this machine" in caught.value.message
    assert not task.done()
    # They may still decline it.
    await queue.respond(pending.requestId, "deny", by=by, conn=conn)
    assert not (await task).allowed


@pytest.mark.parametrize(("by", "conn"), [
    ("gateway:telegram:42", None),
    ("web", Conn("web", "w-1", remote=True)),
])
async def test_the_same_callers_answer_an_ordinary_person_only_ask(
    tmp_path: Path, by: str, conn: Any,
) -> None:
    queue, _hub = _queue(tmp_path)
    session = _session(tmp_path)
    task = asyncio.ensure_future(queue.request(session, "set_mode", {"mode": "auto"},
                                               unattended=True, timeout_sec=30,
                                               person_only=True))
    pending = await _pending(queue)
    assert pending.personOnly and pending.blocked is None
    applied = await queue.respond(pending.requestId, "allow", by=by, conn=conn)
    assert applied is not None and applied.allowed
    assert (await task).allowed


def test_the_chat_shows_no_allow_button_for_a_blocked_call() -> None:
    from snowpea_core.jarvis.messenger import MessengerBridge

    bridge = MessengerBridge(None)  # type: ignore[arg-type]
    blocked = {"requestId": "ap-1", "tool": "shell", "args": dict(BLOCKED_LINE),
               "sessionId": "s-1", "personOnly": True,
               "blocked": {"by": "commands:essential", "why": "x"}}
    text, buttons = bridge.approval_message(blocked)
    assert "allow it on the machine" in text
    assert [b.data.endswith(":deny") or "deny" in b.data for b in buttons] == [True]
    assert not any("allow" in b.data for b in buttons)
    ordinary = {k: v for k, v in blocked.items() if k not in ("blocked", "personOnly")}
    _text, buttons = bridge.approval_message(ordinary)
    assert any("allow" in b.data for b in buttons)


# -- L1: what a browser may answer is an allow-list ------------------------------------------


def test_a_browser_answers_only_listed_tools() -> None:
    from snowpea_core.server import settings_gate as gate

    takes = gate._browser_takes  # noqa: SLF001
    card = {"blocked": {"by": "custom", "why": "x"}}
    for tool in ("shell", "execute_code", "read_file", "write_file", "patch", "edit_file",
                 "apply_patch", "repl.send", "repl.navigate"):
        assert takes({"tool": tool, **card}), tool
        assert takes({"tool": tool, "reason": "cannot be checked"}), tool
    for tool in ("schedule_create", "set_mode", "delegation_set", "delegate_task",
                 "tmux.adopt", "jarvis_task", "settings_set", "mcp_add", "skill_install",
                 "git_commit", "process_kill", "web_search", ""):
        assert not takes({"tool": tool, **card}), tool
    # Listed, but not a security ask, or an external CLI's or Jarvis's: no.
    assert not takes({"tool": "shell"})
    assert not takes({"tool": "shell", **card, "external": {"cli": "claude", "id": "x"}})
    assert not takes({"tool": "shell", **card, "externalTerminal": {"pane": "%1"}})
    assert not takes({"tool": "shell", **card, "route": "jarvis"})
    assert not takes(None)


# -- L3: the audit trail --------------------------------------------------------------------


async def test_the_audit_names_who_answered(tmp_path: Path) -> None:
    session = _session(tmp_path, host_tools_from="b-1")
    queue, _hub = _queue(tmp_path, {"s-1": session})
    task = asyncio.ensure_future(queue.request(session, "shell", dict(BLOCKED_LINE),
                                               unattended=True, timeout_sec=30,
                                               tags=("exec",)))
    pending = await _pending(queue)
    await queue.respond(pending.requestId, "allow", by="b", conn=Conn("browser", "b-1"),
                        actor="person", person_confirm="window")
    assert (await task).allowed
    task = asyncio.ensure_future(queue.request(session, "shell", dict(BLOCKED_LINE),
                                               unattended=True, timeout_sec=30,
                                               tags=("exec",)))
    pending = await _pending(queue)
    await queue.respond(pending.requestId, "deny", by="gateway:telegram:42")
    assert not (await task).allowed
    logs = tmp_path / "home" / "logs"
    audit = [json.loads(x) for x in (logs / "security.jsonl").read_text().splitlines()]
    assert audit[0]["event"] == "person_allowed"
    assert audit[0]["connection"] == "browser" and audit[0]["clientId"] == "b-1"
    assert audit[0]["personConfirmWindow"] is True and audit[0]["remote"] is False
    assert audit[1]["event"] == "person_denied"
    assert audit[1]["connection"] == "messenger" and audit[1]["clientId"] is None
    assert audit[1]["personConfirmWindow"] is False and audit[1]["remote"] is False


async def test_a_refused_block_is_in_the_approval_log(tmp_path: Path) -> None:
    queue, _hub = _queue(tmp_path)
    session = _session(tmp_path, unattended=True)
    decision = await queue.request(session, "shell", dict(BLOCKED_LINE), unattended=True,
                                   timeout_sec=5, tags=("exec",))
    assert not decision.allowed and decision.code == errors.SECURITY_BLOCKED
    logs = tmp_path / "home" / "logs"
    approvals = [json.loads(x) for x in (logs / "approvals.jsonl").read_text().splitlines()]
    assert approvals[-1]["decision"] == "deny"
    assert approvals[-1]["blocked"]["by"] == "commands:essential"
    assert approvals[-1]["blocked"]["kind"] == "command"
