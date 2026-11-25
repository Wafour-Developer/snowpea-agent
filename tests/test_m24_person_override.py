"""A block asks the person, once (0.2.94, owner decision 2026-11-18), and the browser
may answer person-only approvals (protocol 1.61.0, ``browserPersonApprovals``).

"보안검사에 걸리더라도 유저가 허용하면 계속 이어서 할수 있어야 한다": the CHECK
verdict stays ``blocked`` (the security tables are unchanged); the APPROVAL path
turns it into a person-only ask.  The one exception: an agent READING the daemon's
token or address, or saved logins.

Every command line below is DATA: a Python string handed to the pure checker or to
an approval queue whose calls never run anything.  ``/home/u`` is fictional.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.security import control_plane as cp
from snowpea_core.security import written
from snowpea_core.security.check import Context, check_call, check_command
from snowpea_core.security.lists import Lists
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION

USER = "/home/u"
HOME = f"{USER}/.snowpea"
WORK = f"{USER}/proj"
ESSENTIAL = Lists(command_presets=("essential",), path_presets=("essential",))
NOTHING = Lists()


@pytest.fixture(autouse=True)
def _fresh() -> None:
    written.reset()


def _ctx() -> Context:
    return Context(workdir=Path(WORK), user_home=Path(USER), home=Path(HOME),
                   projects=(WORK,), mounts=(), env_venv=False)


def _line(line: str, lists: Lists = ESSENTIAL) -> Any:
    return check_command(line, lists, _ctx(), cwd=Path(WORK))


# -- the one exception, in one place ------------------------------------------------------


def test_the_unallowable_reads_are_exactly_the_secrets() -> None:
    assert cp.unallowable_reads() == (
        "daemon.json", "token", "credentials.json", "mcp-oauth.json",
        ".daemon.json", ".token", ".credentials.json", ".mcp-oauth.json",
        ".codex/auth.json", ".claude/.credentials.json", "terminals", "machines.json", "machines",
        "~/.codex/auth.json", "~/.claude/.credentials.json", "~/.claude.json",
        "~/.gemini/oauth_creds.json", "~/.config/cursor/auth.json",
        "~/.cursor/cli-config.json", "~/.config/anthropic",
    )
    assert cp.unallowable_read(cp.entry_for(("token",)))
    assert not cp.unallowable_read(cp.entry_for(("settings.json",)))
    assert not cp.unallowable_read(cp.entry_for(("trust",)))
    assert not cp.unallowable_read(None)


# (line, person may allow, reasonCode of the ask)
LINES = [
    ("sudo rm -rf /", True, "blocked_command"),
    ("echo '{}' > ~/.snowpea/trust/projects.json", True, "blocked_control"),
    ("rm ~/.snowpea/token", True, "blocked_control"),
    ("cp evil.json ~/.snowpea/credentials.json", True, "blocked_control"),
    ("printf x > ~/.snowpea/security/switch.json", True, "blocked_control"),
    ("cat ~/.snowpea/token", False, None),
    ("cat /home/u/.snowpea/daemon.json", False, None),
    ("grep -r key ~/.snowpea", False, None),
    ("cp ~/.snowpea/credentials.json /tmp/x", False, None),
    ("cat ~/.codex/auth.json", False, None),
    ("cat ~/.claude.json", False, None),
    # A block the person could allow never carries a secret read along with it.
    ("sudo rm -rf / ; cat ~/.snowpea/token", False, None),
    ("rm ~/.snowpea/token && cat ~/.snowpea/credentials.json", False, None),
    ("python3 -c \"print(open('/home/u/.snowpea/credentials.json').read())\"", False, None),
]


@pytest.mark.parametrize(("line", "may", "code"), LINES)
def test_a_block_is_the_persons_to_allow_but_a_secret_read(line: str, may: bool,
                                                          code: str | None) -> None:
    verdict = _line(line)
    assert verdict.blocked, line  # the CHECK verdict is unchanged: still blocked
    assert verdict.person_may_allow is may, line
    assert verdict.secret_read is (not may), line
    if code is not None:
        assert verdict.block_code == code, line


def test_a_file_tool_read_of_a_secret_stays_refused() -> None:
    verdict = check_call("read_file", {"path": f"{HOME}/token"}, NOTHING, _ctx(),
                         tags=("read",))
    assert verdict.blocked and verdict.secret_read and not verdict.person_may_allow
    verdict = check_call("write_file", {"path": f"{HOME}/token", "content": "x"}, NOTHING,
                         _ctx(), tags=("write",))
    assert verdict.blocked and verdict.person_may_allow


def test_a_protected_path_asks_as_a_protected_folder(tmp_path: Path) -> None:
    keep = tmp_path / "keep"
    lists = Lists(paths=((str(keep), "write"),))
    verdict = check_call("write_file", {"path": str(keep / "a"), "content": "x"}, lists,
                         _ctx(), tags=("write",))
    assert verdict.blocked and verdict.person_may_allow
    assert verdict.block_code == "blocked_path"
    assert verdict.block_reason == "touches a protected folder"


# -- the approval path ----------------------------------------------------------------------


class Hub:
    def __init__(self, sessions: dict[str, Any] | None = None) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []
        known = sessions or {}
        self.sessions = SimpleNamespace(get=known.get, all=lambda: list(known.values()))

    async def notify(self, method: str, params: dict[str, Any], **_kw: Any) -> None:
        self.notes.append((method, params))

    def of(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.notes if m == method]


class Conn:
    """A connection: answers ``approval.request`` with ``reply``, records notifications."""

    def __init__(self, kind: str, client_id: str, *, version: str = "0.4.0",
                 reply: dict[str, Any] | None = None, remote: bool = False,
                 peer: str = "127.0.0.1") -> None:
        self.authenticated, self.closed, self.remote_login = True, False, remote
        self.peer = peer
        self.client_kind, self.client_id, self.client_version = kind, client_id, version
        self.surface_id = f"{kind}-s"
        self.reply = reply or {}
        self.notes: list[tuple[str, dict[str, Any]]] = []

    async def call(self, _method: str, _params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return self.reply

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notes.append((method, params))


def _session(tmp: Path, **extra: Any) -> Any:
    values = {"id": "s-1", "workdir": str(tmp), "origin_conn": None, "host_tools_from": None,
              "owned_by": None, "mode": "auto"}
    values.update(extra)
    return SimpleNamespace(**values)


def _queue(tmp: Path, sessions: dict[str, Any] | None = None) -> tuple[ApprovalQueue, Hub]:
    hub = Hub(sessions)
    queue = ApprovalQueue(Settings(), Paths.create(tmp / "home"), hub)  # type: ignore[arg-type]
    return queue, hub


async def _pending(queue: ApprovalQueue) -> Any:
    for _ in range(200):
        if queue.list():
            return queue.list()[0]
        await asyncio.sleep(0.01)
    raise AssertionError("nothing was asked")


async def test_a_blocked_command_asks_the_person_once(tmp_path: Path) -> None:
    queue, hub = _queue(tmp_path)
    queue.router = lambda *_a: "jarvis"
    session = _session(tmp_path)
    args = {"command": "rm -rf ~"}
    task = asyncio.ensure_future(queue.request(session, "shell", args, unattended=True,
                                               timeout_sec=30, tags=("exec",)))
    pending = await _pending(queue)
    assert pending.personOnly is True and pending.neverDelegable is True
    assert pending.route == "person"  # never Jarvis, never auto
    assert pending.remember is None and pending.folder is None
    assert pending.reason == "on your block list"
    assert pending.reasonCode == "blocked_command"
    assert pending.blocked is not None and pending.blocked.by == "commands:essential"
    assert pending.note.startswith("on your block list: ")
    assert hub.of("security.blocked") == []  # asked, not refused
    # Jarvis, an in-process caller, a connector: no.
    for by, conn in (("jarvis", None), ("system", None),
                     ("connector", Conn("connector", "c-1"))):
        with pytest.raises(RpcError) as caught:
            await queue.respond(pending.requestId, "allow", "always", by=by, conn=conn)
        assert caught.value.code in (errors.JARVIS_PERSON_ONLY, errors.NOT_FOUND)
    applied = await queue.respond(pending.requestId, "allow", "always", by="ide",
                                  conn=Conn("ide", "ide-1"))
    assert applied is not None and applied.allowed and applied.scope == "once"
    decision = await task
    assert decision.allowed and decision.scope == "once" and decision.person is not None
    # Never remembered: no cache, no rule, the same call asks again.
    assert queue._cache == set() and queue._lines == set()  # noqa: SLF001
    again = asyncio.ensure_future(queue.request(session, "shell", args, unattended=True,
                                                timeout_sec=30, tags=("exec",)))
    second = await _pending(queue)
    await queue.respond(second.requestId, "deny", by="ide", conn=Conn("ide", "ide-1"))
    assert not (await again).allowed
    # The audit: what blocked it, in both logs.
    logs = tmp_path / "home" / "logs"
    approvals = [json.loads(x) for x in (logs / "approvals.jsonl").read_text().splitlines()]
    assert approvals[0]["blocked"]["by"] == "commands:essential"
    assert approvals[0]["scope"] == "once" and approvals[0]["decision"] == "allow"
    audit = [json.loads(x) for x in (logs / "security.jsonl").read_text().splitlines()]
    assert [a["event"] for a in audit] == ["person_allowed", "person_denied"]
    assert audit[0]["by"] == "commands:essential" and audit[0]["answeredBy"] == "ide"


async def test_an_allowed_edit_of_the_security_lists_is_audited(tmp_path: Path) -> None:
    queue, _hub = _queue(tmp_path)
    session = _session(tmp_path)
    task = asyncio.ensure_future(queue.request(
        session, "settings_set", {"patch": {"security": {"enabled": False}}},
        unattended=True, timeout_sec=30, tags=("config",)))
    pending = await _pending(queue)
    assert pending.reasonCode == "blocked_control"
    assert pending.reason == "touches snowpea's own controls"
    assert pending.blocked is not None and pending.blocked.kind == "settings"
    await queue.respond(pending.requestId, "allow", by="ide", conn=Conn("ide", "ide-1"))
    assert (await task).allowed
    audit = (tmp_path / "home" / "logs" / "security.jsonl").read_text().splitlines()
    record = json.loads(audit[-1])
    assert record["event"] == "person_allowed" and record["kind"] == "settings"
    assert record["by"] == "security" and record["tool"] == "settings_set"


async def test_nobody_present_is_still_refused(tmp_path: Path) -> None:
    parent = _session(tmp_path, id="s-job", unattended=True)
    child = _session(tmp_path, id="s-child", kind="subagent", parent_session_id="s-job")
    queue, hub = _queue(tmp_path, {"s-job": parent, "s-child": child})
    for session in (parent, child):
        decision = await queue.request(session, "shell", {"command": "rm -rf ~"},
                                       unattended=True, timeout_sec=5, tags=("exec",))
        assert not decision.allowed and decision.by == "security"
        assert decision.code == errors.SECURITY_BLOCKED
    assert queue.list() == [] and len(hub.of("security.blocked")) == 2


async def test_a_secret_read_is_refused_with_a_person_present(tmp_path: Path) -> None:
    queue, hub = _queue(tmp_path)
    origin = Conn("ide", "ide-1", reply={"decision": "allow"})
    session = _session(tmp_path, origin_conn=origin)
    for args in ({"command": f"cat {tmp_path}/home/token"},
                 {"command": "cat ~/.codex/auth.json"}):
        decision = await queue.request(session, "shell", args, timeout_sec=5,
                                       tags=("exec",))
        assert not decision.allowed and decision.code == errors.SECURITY_BLOCKED, args
    assert len(hub.of("security.blocked")) == 2


async def test_an_external_cli_block_asks_its_person(tmp_path: Path) -> None:
    from snowpea_core.external.base import ApprovalAsk
    from snowpea_core.external.manager import ExternalManager

    queue, _hub = _queue(tmp_path)
    manager = ExternalManager.__new__(ExternalManager)
    manager.approvals = queue  # type: ignore[attr-defined]
    manager.home = tmp_path / "home"  # type: ignore[attr-defined]
    record = SimpleNamespace(cli="claude", id="abc", real_cwd=str(tmp_path),
                             info=SimpleNamespace(cwd=str(tmp_path), title=None),
                             project=str(tmp_path), approval_cache=set(), turn_cancel=None,
                             last_activity=0.0)
    ask = ApprovalAsk(tool="Bash", args={"command": "bash -c 'rm -rf /'"}, risk="high")
    task = asyncio.ensure_future(manager._ask_approval(record, ask))  # type: ignore[arg-type]
    pending = await _pending(queue)
    assert pending.personOnly and pending.blocked is not None and pending.external
    # A browser never answers an external CLI's request.
    browser = Conn("browser", "b-1")
    with pytest.raises(RpcError) as caught:
        await queue.respond(pending.requestId, "allow", by="b", conn=browser, actor="person",
                            person_confirm="window")
    assert caught.value.data["reason"] == "browser_not_allowed"
    await queue.respond(pending.requestId, "allow", "session", by="ide",
                        conn=Conn("ide", "ide-1"))
    answer = await task
    assert answer.allow and answer.scope == "once" and record.approval_cache == set()
    # A read of a login stays refused for an external CLI too.
    ask = ApprovalAsk(tool="Bash", args={"command": "cat ~/.claude.json"}, risk="high")
    answer = await manager._ask_approval(record, ask)  # type: ignore[arg-type]
    assert not answer.allow and answer.by == "security"


# -- the browser answers person-only approvals ----------------------------------------------

#: The browser's own native confirmation (1.61.0).
CONFIRMED = {"actor": "person", "personConfirm": "window"}
BLOCKED_LINE = {"command": "rm -rf ~"}


async def test_a_confirmed_browser_origin_allows_a_blocked_call_once(tmp_path: Path) -> None:
    queue, _hub = _queue(tmp_path)
    browser = Conn("browser", "b-1", reply={"decision": "allow", "scope": "always",
                                            **CONFIRMED})
    session = _session(tmp_path, origin_conn=browser, host_tools_from="b-1")
    decision = await queue.request(session, "shell", BLOCKED_LINE, timeout_sec=30,
                                   tags=("exec",))
    assert decision.allowed and decision.scope == "once" and decision.person is not None
    assert browser.notes == [] and queue._cache == set()  # noqa: SLF001


@pytest.mark.parametrize(("version", "extra", "conn_kw", "reason"), [
    # A shipped 0.3.0 browser stamps actor on a page click: never enough.
    ("0.3.0", {"actor": "person"}, {}, "browser_legacy"),
    ("0.3.0", CONFIRMED, {}, "browser_legacy"),
    ("0.4.0", {"actor": "person"}, {}, "browser_unconfirmed"),
    ("0.4.0", {"personConfirm": "window"}, {}, "browser_unconfirmed"),
    ("0.4.0", CONFIRMED, {"peer": "192.168.50.17"}, "browser_remote"),
])
async def test_a_browser_origin_reply_that_does_not_count_comes_back(
    tmp_path: Path, version: str, extra: dict[str, Any], conn_kw: dict[str, Any], reason: str,
) -> None:
    queue, _hub = _queue(tmp_path)
    browser = Conn("browser", "b-1", version=version,
                   reply={"decision": "allow", "scope": "once", **extra}, **conn_kw)
    session = _session(tmp_path, origin_conn=browser, host_tools_from="b-1")
    task = asyncio.ensure_future(queue.request(session, "shell", BLOCKED_LINE,
                                               timeout_sec=30, tags=("exec",)))
    for _ in range(200):
        if browser.notes:
            break
        await asyncio.sleep(0.01)
    (method, params), = browser.notes
    assert method == "approval.pending"
    assert params["refused"]["code"] == "jarvis_person_only"
    assert params["refused"]["reason"] == reason
    assert not task.done()
    await queue.respond(params["request"]["requestId"], "allow", by="ide",
                        conn=Conn("ide", "ide-1"))
    assert (await task).allowed


async def test_a_browser_through_the_remote_listener_is_refused(tmp_path: Path) -> None:
    queue, _hub = _queue(tmp_path)
    session = _session(tmp_path, host_tools_from="b-1")
    task = asyncio.ensure_future(queue.request(session, "shell", BLOCKED_LINE, unattended=True,
                                               timeout_sec=30, tags=("exec",)))
    pending = await _pending(queue)
    remote = Conn("browser", "b-1", remote=True)
    with pytest.raises(RpcError) as caught:
        await queue.respond(pending.requestId, "allow", by="b", conn=remote,
                            actor="person", person_confirm="window")
    assert caught.value.data["reason"] == "browser_remote"
    await queue.respond(pending.requestId, "deny", by="ide", conn=Conn("ide", "ide-1"))
    assert not (await task).allowed


async def test_a_browser_that_attached_to_an_ide_session_may_allow(
        tmp_path: Path) -> None:
    """m31 (owner, 2026-11-19): a browser that attached to an IDE session continues
    it, so it answers that session's blocked call — still only confirmed by the
    person in its own window (the 1.61.0 rules stay)."""
    browser = Conn("browser", "b-1")
    session = _session(tmp_path, origin_surface="ide", origin_conn=browser,
                       origin_client_id="b-1", host_tools_from="b-1")
    queue, _hub = _queue(tmp_path, {"s-1": session})
    task = asyncio.ensure_future(queue.request(session, "shell", BLOCKED_LINE, unattended=True,
                                               timeout_sec=30, tags=("exec",)))
    pending = await _pending(queue)
    with pytest.raises(RpcError) as caught:
        await queue.respond(pending.requestId, "allow", by="b", conn=browser, actor="person")
    assert caught.value.data["reason"] == "browser_unconfirmed"
    applied = await queue.respond(pending.requestId, "allow", by="b", conn=browser,
                                  actor="person", person_confirm="window")
    assert applied is not None and applied.scope == "once"
    assert (await task).allowed


async def test_a_browser_answers_only_its_own_sessions(tmp_path: Path) -> None:
    parent = _session(tmp_path, id="s-top", host_tools_from="b-1")
    child = _session(tmp_path, id="s-sub", kind="subagent", parent_session_id="s-top")
    other = _session(tmp_path, id="s-other", host_tools_from="b-2")
    queue, _hub = _queue(tmp_path, {"s-top": parent, "s-sub": child, "s-other": other})
    mine = Conn("browser", "b-1")

    async def ask(session: Any, tool: str = "shell", **kw: Any) -> tuple[Any, Any]:
        task = asyncio.ensure_future(queue.request(session, tool, dict(BLOCKED_LINE),
                                                   unattended=True, timeout_sec=30,
                                                   tags=("exec",), **kw))
        for _ in range(200):
            found = [r for r in queue.list() if r.sessionId == session.id]
            if found:
                return task, found[0]
            await asyncio.sleep(0.01)
        raise AssertionError("nothing was asked")

    # Its own subagent's blocked call (through the parent chain): confirmed only.
    task, pending = await ask(child)
    with pytest.raises(RpcError) as caught:
        await queue.respond(pending.requestId, "allow", by="b", conn=mine, actor="person")
    assert caught.value.data["reason"] == "browser_unconfirmed"
    applied = await queue.respond(pending.requestId, "allow", "always", by="b", conn=mine,
                                  actor="person", person_confirm="window")
    assert applied is not None and applied.scope == "once"
    assert (await task).allowed
    # Another browser's session: never (not revealed either).
    task, pending = await ask(other)
    with pytest.raises(RpcError) as caught:
        await queue.respond(pending.requestId, "allow", by="b", conn=mine, actor="person",
                            person_confirm="window")
    assert caught.value.code in (errors.JARVIS_PERSON_ONLY, errors.NOT_FOUND)
    await queue.respond(pending.requestId, "deny", by="ide", conn=Conn("ide", "ide-1"))
    await task
    # Not a security ask (Jarvis's authority, a mode raise): never a browser's.
    for tool in ("delegation_set", "set_mode"):
        task = asyncio.ensure_future(queue.request(parent, tool, {"mode": "auto"},
                                                   unattended=True, timeout_sec=30,
                                                   person_only=True))
        pending = None
        for _ in range(200):
            found = [r for r in queue.list() if r.tool == tool]
            if found:
                pending = found[0]
                break
            await asyncio.sleep(0.01)
        assert pending is not None
        with pytest.raises(RpcError) as caught:
            await queue.respond(pending.requestId, "allow", by="b", conn=mine,
                                actor="person", person_confirm="window")
        assert caught.value.data["reason"] == "browser_not_allowed", tool
        await queue.respond(pending.requestId, "deny", by="ide", conn=Conn("ide", "ide-1"))
        await task


def test_the_browser_rule_reasons_and_visibility() -> None:
    from snowpea_core.server import settings_gate as gate

    mine = Conn("browser", "b-1")
    shipped = Conn("browser", "b-1", version="0.3.0")
    remote = Conn("browser", "b-1", peer="10.0.0.9")
    session = SimpleNamespace(id="s", host_tools_from="b-1", owned_by=None)
    stranger = SimpleNamespace(id="t", host_tools_from=None, owned_by=None,
                               origin_conn=None, asker_conn=None)
    request = {"tool": "shell", "personOnly": True, "blocked": {"by": "custom", "why": "x"}}
    unchecked = {"tool": "shell", "personOnly": True,
                 "reason": "cannot be checked against your block list"}
    refusal = gate.browser_person_refusal
    assert gate.BROWSER_PERSON_CONFIRM_MIN == (0, 4, 0)
    assert refusal(mine, session, request, "person", "window") is None
    assert refusal(mine, session, unchecked, "person", "window") is None
    assert refusal(mine, session, request, "person", None) == "browser_unconfirmed"
    assert refusal(mine, session, request, None, "window") == "browser_unconfirmed"
    assert refusal(shipped, session, request, "person", None) == "browser_legacy"
    assert refusal(shipped, session, request, "person", "window") == "browser_legacy"
    assert refusal(remote, session, request, "person", "window") == "browser_remote"
    assert refusal(mine, stranger, request, "person", "window") == "browser_not_owner"
    for never in ({"tool": "tmux.adopt", "reason": "x"}, {"tool": "delegation_set"},
                  {"tool": "set_mode"}, {"tool": "schedule_create"},
                  {"tool": "Bash", "blocked": {}, "reason": "x",
                   "external": {"cli": "claude", "id": "x"}},
                  {"tool": "x", "reason": "x", "externalTerminal": {"pane": "%1"}},
                  {"tool": "x", "reason": "x", "route": "jarvis"}):
        assert refusal(mine, session, never, "person", "window") == "browser_not_allowed"
    # Lists: its own session's security ask, not a stranger's, a shipped browser's
    # or Jarvis's authority.
    assert gate.list_visible(mine, session, person_only=True, request=request)
    assert not gate.list_visible(mine, stranger, person_only=True, request=request)
    assert not gate.list_visible(remote, session, person_only=True, request=request)
    assert not gate.list_visible(mine, session, person_only=True,
                                 request={"tool": "delegation_set"})
    assert not gate.list_visible(Conn("connector", "c"), session, person_only=True,
                                 request=request)
    # A connector still never answers one.
    assert not gate.may_answer(Conn("connector", "c"), session, person_only=True,
                               actor="person", request=request, person_confirm="window")


def test_protocol_and_capability() -> None:
    assert PROTOCOL_VERSION == "1.80.0"
    assert "browserPersonApprovals" in CAPABILITIES
