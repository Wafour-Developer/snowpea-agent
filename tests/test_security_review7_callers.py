"""Security review 7, part B — H-1, M-2 and ``web.remote.maxMode``: who asked.

H-1: ``/mcp add`` queued behind a running turn ran after the remote login had
disconnected, and a closed connection was read as an in-process caller, so the
RPC's limits fell away.  Who asks is now fixed when work is accepted
(``person_gate.CallerSnapshot``), carried on queued commands and prompts and on
every turn, and every later judgement uses it; a closed connection is its own
kind, nobody.

M-2: ``/schedule`` and the ``schedule_create`` tool reached the scheduler past
``job.schedule``'s rules; all three now go through ``scheduler/access.py``.

``web.remote.maxMode`` caps what a remote web login may raise to or start work in.

Every rule is a table: one row per connection kind.  The command strings
("prog", "--serve") are DATA: the probe and the server start are replaced by
recorders, nothing here starts a process.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent import loop as agent_loop
from snowpea_core.commands.registry import CommandContext, gate_command
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.scheduler import access
from snowpea_core.server import errors, job_handlers, mcp_handlers, person_gate, settings_gate
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import PERSON_ONLY_KINDS, RpcError
from snowpea_core.server.person_gate import CallerSnapshot, connection_kind, snapshot
from snowpea_core.server.protocol import (
    Empty,
    JobIdParams,
    JobScheduleParams,
    JobUpdateParams,
    SessionCreateParams,
    SessionPromptParams,
    SessionSetModeParams,
    SessionSteerParams,
)
from snowpea_core.server.session_handlers import wire_core
from snowpea_core.session import mode_guard
from snowpea_core.tools import mcp_client, mcp_config
from snowpea_core.tools.registry import ToolContext

pytestmark = pytest.mark.real_mode_guard


def _conn(kind: str | None = "ide", *, remote: bool = False, version: str = "0.3.0",
          client_id: str | None = None, closed: bool = False, authenticated: bool = True
          ) -> Any:
    return SimpleNamespace(authenticated=authenticated, closed=closed, client_kind=kind,
                           remote_login=remote, client_version=version,
                           client_id=client_id or f"{kind}-1", surface_id=f"{kind}-s")


def _callers() -> dict[str, tuple[Any, str | None]]:
    """Every connection kind the gates tell apart: name -> (connection, actor)."""
    return {
        "person": (_conn("ide"), None),
        "remote_login": (_conn("web", remote=True), None),
        "connector": (_conn("connector"), None),
        "legacy": (_conn("browser", version="0.2.0", client_id="b-legacy"), None),
        "browser": (_conn("browser", client_id="b-1"), None),
        "browser+actor": (_conn("browser", client_id="b-1"), "person"),
        "closed": (_conn("ide", closed=True), None),
        "none": (None, None),
    }


KINDS = sorted(_callers())
#: The connection kind each caller above is.
KIND_OF = {"person": "person", "remote_login": "remote_login", "connector": "connector",
           "legacy": "browser", "browser": "browser", "browser+actor": "browser",
           "closed": "closed", "none": "none"}


@pytest.fixture
def core(tmp_path: Path) -> Core:
    made = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    return wire_core(made)


@pytest.fixture
def no_processes(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the probe and the server start: nothing runs; what would is recorded."""
    started: list[Any] = []

    async def probe(config: Any) -> tuple[bool, list[Any], str | None, int]:
        started.append(config)
        return True, [], None, 1

    async def register(_core: Any, config: Any) -> None:
        started.append(("start", config))

    monkeypatch.setattr(mcp_handlers, "probe", probe)
    monkeypatch.setattr(mcp_client, "register_config", register)
    return started


class Events:
    """A subscriber that keeps what a session emits."""

    closed = False

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "session.event":
            event = params.get("event", params)
            self.seen.append((str(event.get("kind")), dict(event.get("payload") or {})))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [payload for seen, payload in self.seen if seen == kind]


# -- H-1: connection kinds, and the snapshot --------------------------------------------------


@pytest.mark.parametrize(
    ("conn", "kind"),
    [
        (_conn("ide"), "person"),
        (_conn("tui"), "person"),
        (_conn(None), "person"),  # a client that sends no clientKind
        (_conn("web", remote=True), "remote_login"),
        (_conn("ide", remote=True), "remote_login"),  # whatever it calls itself
        (_conn("browser", remote=True), "remote_login"),
        (_conn("browser"), "browser"),
        (_conn("connector"), "connector"),
        (_conn("something-new"), "connector"),
        # A connection that has closed, or never signed in, is nobody — and
        # never an in-process caller (it used to be "none").
        (_conn("ide", closed=True), "closed"),
        (_conn("web", remote=True, closed=True), "closed"),
        (_conn("browser", closed=True), "closed"),
        (_conn("ide", authenticated=False), "closed"),
        (None, "none"),
    ],
)
def test_connection_kinds(conn: Any, kind: str) -> None:
    assert connection_kind(conn) == kind
    assert kind in PERSON_ONLY_KINDS  # the wire's details.kind values


def test_a_snapshot_keeps_who_asked() -> None:
    conn = _conn("web", remote=True)
    who = snapshot(conn, "person", origin="human")
    assert (who.kind, who.actor, who.client_kind, who.client_id, who.surface_id) == (
        "remote_login", "person", "web", "web-1", "web-s")
    conn.closed = True
    conn.remote_login = False
    conn.client_kind = "ide"
    assert connection_kind(conn) == "closed"
    # ... the snapshot does not move, and the gates read it in the connection's place.
    assert connection_kind(who) == "remote_login" and who.conn is conn
    assert snapshot(who) is who and snapshot(who, caller="internal").caller == "internal"
    assert not person_gate.is_person(who, local=True) and person_gate.is_person(who)
    assert connection_kind(snapshot(None)) == "none"
    assert mode_guard.ModeCaller.from_conn(who).kind == "remote_login"
    assert mode_guard.ModeCaller.from_conn(snapshot(None)) is mode_guard.NOBODY


#: Every gate that takes a connection: what it does with one that has CLOSED.
#: ``None`` in its place is an in-process caller and passes each of them.
def _gates(core: Core, tmp_path: Path) -> dict[str, Any]:
    session = SimpleNamespace(id="s", mode="accept", auto_after_plan=False, workdir=tmp_path,
                              host_tools_from=None, origin_conn=None)
    return {
        "guard_call": lambda c: settings_gate.guard_call(c, None, core, method="mcp.add"),
        "kind_method_refusal": lambda c: settings_gate.kind_method_refusal(
            "session.prompt", {}, c, core),
        "host_choice_refusal": lambda c: settings_gate.host_choice_refusal(c, "b-1"),
        "remote_mcp_refusal": lambda c: settings_gate.remote_mcp_refusal(
            c, core, method="mcp.add", entry={"url": "https://mcp.example.com"}),
        "gate_command": lambda c: gate_command(core, session, "help", "", conn=c)
        if c is not None else gate_command(core, session, "help", "", conn=None),
        "skill_gate": lambda c: __import__(
            "snowpea_core.server.skill_gate", fromlist=["x"]).remote_write_refusal(
                c, core, method="skill.write", kind="skill", name="x", global_=True,
                content="---\nname: x\ndescription: d\n---\nbody"),
        "guard_patch": lambda c: __import__(
            "snowpea_core.server.settings_handlers", fromlist=["x"]).guard_patch(
                c, {"agent": {"busy": "queue"}}, project=False, core=core),
    }


@pytest.mark.parametrize("gate", ["guard_call", "kind_method_refusal", "host_choice_refusal",
                                  "remote_mcp_refusal", "gate_command", "skill_gate",
                                  "guard_patch"])
def test_a_closed_connection_is_never_an_in_process_caller(
    core: Core, tmp_path: Path, gate: str
) -> None:
    judge = _gates(core, tmp_path)[gate]
    for closed in (_conn("ide", closed=True), _conn("web", remote=True, closed=True),
                   _conn("browser", closed=True), snapshot(_conn("ide", closed=True))):
        with pytest.raises(RpcError) as caught:
            judge(closed)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY, gate
        assert caught.value.data["kind"] == "closed", gate
    if gate != "guard_patch":  # (settings.set from nobody is refused too: its own rule)
        judge(None)


def test_what_a_closed_connection_may_answer_and_be() -> None:
    closed = _conn("ide", closed=True)
    assert not person_gate.is_person(closed) and not person_gate.is_person(closed, local=True)
    with pytest.raises(RpcError):
        person_gate.require_person(closed, "do it")
    assert not settings_gate.may_answer(closed, None)
    assert not settings_gate.list_visible(closed, None)
    from snowpea_core.jarvis.delegation import Caller

    assert Caller.from_connection(closed).kind == "system"  # may change nothing
    assert Caller.from_connection(_conn("ide")).kind == "connection"


# -- H-1: a queued command is judged by who queued it -----------------------------------------


async def _busy_session(core: Core, workdir: Path) -> Any:
    """A session with a turn "running", so a ``turn`` command is queued."""
    workdir.mkdir(exist_ok=True)
    session = await core.sessions.create(workdir)
    session.turn_task = asyncio.get_running_loop().create_future()  # never done
    session.current_turn = "t-running"
    return session


@pytest.mark.parametrize("after", ["still connected", "disconnected", "says it is an ide now"])
async def test_a_queued_mcp_add_from_a_remote_login_stays_a_remote_logins(
    core: Core, tmp_path: Path, no_processes: list[Any], after: str
) -> None:
    session = await _busy_session(core, tmp_path / "project")
    events = Events()
    core.hub.subscribe(events, session.id)
    remote = _conn("web", remote=True)
    line = 'add run --project -- prog --serve'
    turn_id = core.commands.start(core, session, "mcp", line, remote)
    assert len(session.queued_turns) == 1 and no_processes == []
    queued = session.queued_turns.pop(0)
    assert queued.turn_id == turn_id and queued.command.who.kind == "remote_login"
    # The remote login goes away (or its connection now says something else).
    if after == "disconnected":
        remote.closed = True
    elif after == "says it is an ide now":
        remote.remote_login, remote.client_kind = False, "ide"
    item = queued.command
    await core.commands.run(core, session, item.name, item.args, item.conn, queued.turn_id,
                            actor=item.actor, caller=item.caller, who=item.who)
    path = mcp_config.config_path("project", core.paths.home, session.workdir)
    assert "run" not in mcp_config.read_servers(path), "the command server was saved"
    assert no_processes == [], "the command was probed or started"
    said = " ".join(str(p.get("text")) for p in events.of("message.done"))
    assert "local connection" in said  # the RPC's refusal, as the command's answer
    session.turn_task.cancel()


async def test_a_local_persons_queued_command_still_runs_after_it_disconnects(
    core: Core, tmp_path: Path, no_processes: list[Any]
) -> None:
    session = await _busy_session(core, tmp_path / "project")
    person = _conn("ide")
    core.commands.start(core, session, "mcp", "add run --project -- prog --serve", person)
    queued = session.queued_turns.pop(0)
    person.closed = True  # the person closed the app: what they asked for still happens
    item = queued.command
    await core.commands.run(core, session, item.name, item.args, item.conn, queued.turn_id,
                            actor=item.actor, caller=item.caller, who=item.who)
    path = mcp_config.config_path("project", core.paths.home, session.workdir)
    assert mcp_config.read_servers(path)["run"]["command"] == "prog"
    session.turn_task.cancel()
    await mcp_client.MANAGER.close_all()


async def test_a_queued_command_is_gated_again_when_it_runs(core: Core, tmp_path: Path) -> None:
    """What was allowed when it was queued may not be when its turn comes."""
    session = await _busy_session(core, tmp_path / "project")
    events = Events()
    core.hub.subscribe(events, session.id)
    remote = _conn("web", remote=True)
    gate_command(core, session, "auto", "", conn=remote)  # allowed when it is sent
    who = snapshot(remote, origin="human")  # ... what the queue keeps
    core.settings.web.remote.maxMode = "accept"  # the owner lowers the limit meanwhile
    await core.commands.run(core, session, "auto", "", remote, "t-queued", who=who)
    assert session.mode == "accept"
    (error,) = events.of("error")
    assert error["code"] == errors.JARVIS_PERSON_ONLY
    assert error["details"]["reason"] == "mode_raise" and error["details"]["max"] == "accept"
    done = events.of("turn.done")[-1]
    assert (done["turnId"], done["reason"]) == ("t-queued", "error")
    session.turn_task.cancel()


async def test_a_refused_instant_command_says_why(core: Core, tmp_path: Path) -> None:
    """Beside a running turn too: the refusal's own words, not "internal error"."""
    session = await _busy_session(core, tmp_path / "project")
    events = Events()
    core.hub.subscribe(events, session.id)
    remote = _conn("web", remote=True)
    who = snapshot(remote, origin="human")
    core.settings.web.remote.maxMode = "accept"
    await core.commands.run_beside(core, session, "auto", "", remote, "c-1", who=who)
    (result,) = events.of("command.result")
    assert result["ok"] is False and "web.remote.maxMode" in result["text"]
    assert session.mode == "accept"
    session.turn_task.cancel()


async def test_a_command_started_by_a_closed_connection_is_refused(
    core: Core, tmp_path: Path
) -> None:
    session = await core.sessions.create(tmp_path)
    for closed in (_conn("ide", closed=True), _conn("web", remote=True, closed=True)):
        with pytest.raises(RpcError) as caught:
            core.commands.start(core, session, "schedule", '"in 10m" "x"', closed)
        assert caught.value.data["kind"] == "closed"
    assert await core.scheduler.list() == []


def test_a_command_context_judges_by_its_snapshot() -> None:
    remote = _conn("web", remote=True)
    ctx = CommandContext(core=None, session=None, turn_id="t", conn=remote)  # type: ignore[arg-type]
    assert ctx.who.kind == "remote_login"
    remote.closed = True
    from snowpea_core.commands import mcp_cmd

    assert mcp_cmd._remote(ctx).kind == "remote_login"  # it was "in-process" (None) here
    assert mcp_cmd._remote(CommandContext(core=None, session=None, turn_id="t",  # type: ignore[arg-type]
                                          conn=_conn("ide"))) is None
    # A connection that had closed before the command was built is passed on
    # (the handlers refuse it), never read as in-process.
    closed = CommandContext(core=None, session=None, turn_id="t",  # type: ignore[arg-type]
                            conn=_conn("web", remote=True, closed=True))
    assert mcp_cmd._remote(closed).kind == "closed"


# -- H-1: prompts, steers and what a turn does later ------------------------------------------


async def test_a_turn_carries_who_started_it(core: Core, tmp_path: Path) -> None:
    session = await _busy_session(core, tmp_path / "project")
    remote = _conn("web", remote=True)
    turn_id = agent_loop.start_turn(core, session, "hello", who=snapshot(remote, origin="human"))
    assert session.queued_turns[0].who.kind == "remote_login"
    assert session.turn_callers[turn_id].kind == "remote_login"
    remote.closed = True  # nothing later asks the connection
    session.current_turn = turn_id
    assert agent_loop.turn_caller(core, session).kind == "remote_login"
    # A delegated child answers with its parent's caller.
    child = SimpleNamespace(current_turn="t-child", turn_callers={},
                            parent_session_id=session.id)
    assert agent_loop.turn_caller(core, child).kind == "remote_login"
    # Nobody recorded: an in-process start.
    orphan = SimpleNamespace(current_turn="t-x", turn_callers={}, parent_session_id=None)
    assert agent_loop.turn_caller(core, orphan) is person_gate.NOBODY
    session.turn_task.cancel()


@pytest.mark.parametrize(
    ("first", "then", "result"),
    [
        ("person", "remote_login", "remote_login"),  # a remote login's words join the turn
        ("remote_login", "person", "remote_login"),  # ... and stay in it
        ("none", "remote_login", "remote_login"),
        ("person", "browser", "browser"),
        ("person", "person", "person"),
        ("remote_login", "none", "remote_login"),
    ],
)
def test_words_added_to_a_turn_make_it_that_callers_too(first: str, then: str,
                                                        result: str) -> None:
    session = SimpleNamespace(turn_callers={})
    agent_loop.note_caller(session, "t-1", CallerSnapshot(kind=first))  # type: ignore[arg-type]
    agent_loop.note_caller(session, "t-1", CallerSnapshot(kind=then))  # type: ignore[arg-type]
    assert session.turn_callers["t-1"].kind == result


async def test_session_prompt_and_steer_record_the_caller(core: Core, tmp_path: Path) -> None:
    from snowpea_core.server.host_handlers import session_steer_handler
    from snowpea_core.server.session_handlers import session_prompt_handler

    session = await _busy_session(core, tmp_path / "project")
    session.turn_callers["t-running"] = snapshot(_conn("ide"))
    remote = _conn("web", remote=True)
    queued = await session_prompt_handler(
        remote, SessionPromptParams(sessionId=session.id, text="and then", whenBusy="queue"),
        core)
    assert session.turn_callers[queued.turnId].kind == "remote_login"
    # A steer goes into the RUNNING turn: that turn is the remote login's too now.
    await session_steer_handler(
        remote, SessionSteerParams(sessionId=session.id, text="also this"), core)
    assert session.turn_callers["t-running"].kind == "remote_login"
    session.turn_task.cancel()


async def test_the_mcp_add_tool_is_judged_for_who_started_the_turn(
    core: Core, tmp_path: Path, no_processes: list[Any]
) -> None:
    from snowpea_core.tools.mcp_tools import mcp_add

    session = await core.sessions.create(tmp_path)
    session.current_turn = "t-1"
    ctx = ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]
    args = {"name": "run", "command": "prog", "args": ["--serve"], "scope": "project"}
    path = mcp_config.config_path("project", core.paths.home, session.workdir)

    session.turn_callers["t-1"] = snapshot(_conn("web", remote=True))
    refused = await mcp_add(ctx, dict(args))
    assert not refused.ok and "local connection" in str(refused.error)
    assert "run" not in mcp_config.read_servers(path) and no_processes == []

    session.turn_callers["t-1"] = snapshot(_conn("ide"))  # a local person's turn: as before
    assert (await mcp_add(ctx, dict(args))).ok
    assert mcp_config.read_servers(path)["run"]["command"] == "prog"
    await mcp_client.MANAGER.close_all()


# -- M-2: one scheduler entry point ------------------------------------------------------------

SPEC = "every day at 9:00"


async def _schedule(core: Core, via: str, name: str, workdir: Path, *, channel: str | None = None,
                    mode: str = "accept") -> tuple[bool, str]:
    """Register a job as caller ``name`` through ``via``; ``(made, refusal text)``."""
    conn, actor = _callers()[name]
    before = {job.id for job in await core.scheduler.list()}
    text = ""
    if via == "rpc":
        try:
            await job_handlers.job_schedule_handler(conn, JobScheduleParams(
                spec=SPEC, task="summarise", workdir=str(workdir), channel=channel, mode=mode,
                actor=actor), core)
        except RpcError as exc:
            text = f"{exc.code}:{(exc.data or {}).get('reason')}"
    else:
        session = await core.sessions.create(workdir)
        who = snapshot(conn, actor, origin="human")
        if via == "command":
            from snowpea_core.commands.schedule_cmd import cmd_schedule

            ctx = CommandContext(core=core, session=session, turn_id="t-1", conn=conn,
                                 actor=actor, who=who)
            said: list[str] = []

            async def say(line: str) -> None:
                said.append(line)

            ctx.say = say  # type: ignore[method-assign]
            line = f'"{SPEC}" "summarise" --mode {mode}'
            await cmd_schedule(ctx, line + (f" --channel {channel}" if channel else ""))
            text = "" if said and said[0].startswith("scheduled ") else " ".join(said)
        else:
            from snowpea_core.scheduler.tools import schedule_create

            session.current_turn = "t-1"
            session.turn_callers["t-1"] = who
            result = await schedule_create(
                ToolContext(session=session, core=core, backend=None),  # type: ignore[arg-type]
                {"spec": SPEC, "task": "summarise", "mode": mode,
                 **({"channel": channel} if channel else {})})
            text = "" if result.ok else str(result.error)
    made = {job.id for job in await core.scheduler.list()} - before
    assert bool(made) is (text == ""), (via, name, text)
    return bool(made), text


@pytest.mark.parametrize("via", ["rpc", "command", "tool"])
@pytest.mark.parametrize("name", ["person", "remote_login", "browser+actor", "legacy", "none"])
async def test_created_by_is_the_caller_whichever_way_the_job_is_made(
    core: Core, tmp_path: Path, via: str, name: str
) -> None:
    made, text = await _schedule(core, via, name, tmp_path)
    assert made, text
    (job,) = await core.scheduler.list()
    want = None if name == "none" else KIND_OF[name]
    assert job.created_by == want, (via, name)
    # job.list marks a remote login's job, and the status counts it — also for
    # the slash command and the tool, which made unmarked jobs before.
    row = (await job_handlers.job_list_handler(_conn("ide"), Empty(), core)).jobs[0]
    assert row.createdBy == ("remote_login" if name == "remote_login" else None)


@pytest.mark.parametrize("via", ["rpc", "command", "tool"])
@pytest.mark.parametrize(
    ("name", "channel", "allowed"),
    [
        ("remote_login", "telegram:12345", False),  # leaves the machine
        ("remote_login", "log", True),
        ("remote_login", None, True),
        ("person", "telegram:12345", True),
        ("browser+actor", "telegram:12345", True),
    ],
)
async def test_a_remote_logins_job_never_delivers_to_a_messenger(
    core: Core, tmp_path: Path, via: str, name: str, channel: str | None, allowed: bool
) -> None:
    made, text = await _schedule(core, via, name, tmp_path, channel=channel)
    assert made is allowed, (via, name, channel, text)


@pytest.mark.parametrize("via", ["rpc", "command", "tool"])
@pytest.mark.parametrize(
    ("name", "mode", "allowed"),
    [
        ("person", "auto", True),
        ("remote_login", "auto", True),  # no limit set: as before
        ("browser", "auto", False),  # a raise without a person's click
        ("browser+actor", "auto", True),
        ("browser", "accept", True),
    ],
)
async def test_a_jobs_mode_is_judged_whichever_way_it_is_made(
    core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via: str, name: str, mode: str,
    allowed: bool
) -> None:
    if via == "tool":
        # The tool puts a raise to a person (its own approval): answered here as
        # the caller's kind would be — a person's allow, or nobody's.
        from snowpea_core.permissions.approval_queue import Decision

        async def request(*_args: Any, **_kwargs: Any) -> Decision:
            return Decision("allow" if allowed else "deny", "once", "test",
                            person=object() if allowed else None)

        monkeypatch.setattr(core.approvals, "request", request)
    made, text = await _schedule(core, via, name, tmp_path, mode=mode)
    assert made is allowed, (via, name, mode, text)


async def _cancel(core: Core, via: str, name: str, job_id: str, workdir: Path) -> bool:
    conn, actor = _callers()[name]
    if via == "rpc":
        try:
            await job_handlers.job_cancel_handler(conn, JobIdParams(jobId=job_id), core)
        except RpcError:
            pass
    else:
        session = await core.sessions.create(workdir)
        who = snapshot(conn, actor, origin="human")
        if via == "command":
            from snowpea_core.commands.schedule_cmd import cmd_schedule

            ctx = CommandContext(core=core, session=session, turn_id="t-1", conn=conn,
                                 actor=actor, who=who)

            async def say(_line: str) -> None:
                return None

            ctx.say = say  # type: ignore[method-assign]
            await cmd_schedule(ctx, f"cancel {job_id}")
        else:
            from snowpea_core.scheduler.tools import schedule_cancel

            session.current_turn = "t-1"
            session.turn_callers["t-1"] = who
            await schedule_cancel(
                ToolContext(session=session, core=core, backend=None),  # type: ignore[arg-type]
                {"id": job_id})
    job = await core.scheduler.get(job_id)
    return job is not None and job.state == "cancelled"


@pytest.mark.parametrize("via", ["rpc", "command", "tool"])
@pytest.mark.parametrize(
    ("owner", "name", "cancelled"),
    [
        ("person", "remote_login", False),  # the owner's job is not a remote login's to stop
        ("remote_login", "remote_login", True),
        ("person", "person", True),
        ("remote_login", "person", True),
        ("person", "browser+actor", True),
    ],
)
async def test_who_may_cancel_a_job_is_the_same_whichever_way(
    core: Core, tmp_path: Path, via: str, owner: str, name: str, cancelled: bool
) -> None:
    await _schedule(core, "rpc", owner, tmp_path)
    (job,) = await core.scheduler.list()
    assert await _cancel(core, via, name, job.id, tmp_path) is cancelled, (via, owner, name)


def test_the_schedule_command_has_its_rpc_twins() -> None:
    from snowpea_core.commands.persists import COMMAND_TWINS, twins

    assert "schedule" in COMMAND_TWINS
    assert twins("schedule", '"in 10m" "x"') == ("job.schedule",)
    assert twins("schedule", "cancel j-1") == ("job.cancel",)
    assert twins("schedule", "list") == twins("schedule", "") == ("job.list",)
    # ... so the command gate refuses it wherever the dispatcher refuses the method.
    session = SimpleNamespace(mode="accept", auto_after_plan=False, workdir=".")
    with pytest.raises(RpcError):
        gate_command(None, session, "schedule", '"in 10m" "x"', conn=_conn("connector"))
    gate_command(None, session, "schedule", '"in 10m" "x"', conn=_conn("web", remote=True))


async def test_a_remote_logins_job_keeps_its_runs_a_remote_logins(
    core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a scheduled run of a remote login's job schedules is a remote login's too."""
    await _schedule(core, "rpc", "remote_login", tmp_path)
    (job,) = await core.scheduler.list()
    seen: dict[str, Any] = {}

    async def run_turn(core_: Any, session: Any, _text: str, *, turn_id: str, **_kw: Any) -> str:
        session.current_turn = turn_id
        seen["who"] = agent_loop.turn_caller(core_, session)
        return turn_id

    monkeypatch.setattr(agent_loop, "run_turn", run_turn)
    await core.scheduler._execute(job)
    assert seen["who"].kind == "remote_login" and seen["who"].origin == "schedule"
    assert access.created_by(seen["who"]) == "remote_login"


# -- web.remote.maxMode ------------------------------------------------------------------------


def _max(caught: pytest.ExceptionInfo[RpcError], limit: str) -> None:
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert caught.value.data["reason"] == "mode_raise"
    assert caught.value.data["kind"] == "remote_login"
    assert caught.value.data["max"] == limit


def test_max_mode_defaults_to_no_limit() -> None:
    assert Settings().web.remote.maxMode == "auto"
    assert mode_guard.remote_max(Settings()) == "auto"
    assert mode_guard.remote_max(None) == "auto"
    for mode in ("plan", "accept", "auto"):
        assert mode_guard.above_remote_max("remote_login", mode, Settings()) is None
    with pytest.raises(ValueError):
        Settings.model_validate({"web": {"remote": {"maxMode": "everything"}}})


@pytest.mark.parametrize(
    ("limit", "mode", "refused"),
    [
        ("auto", "auto", False), ("auto", "accept", False),
        ("accept", "auto", True), ("accept", "accept", False), ("accept", "plan", False),
        ("plan", "auto", True), ("plan", "accept", True), ("plan", "plan", False),
    ],
)
@pytest.mark.parametrize("name", KINDS)
def test_only_a_remote_login_meets_the_limit(name: str, limit: str, mode: str,
                                             refused: bool) -> None:
    settings = Settings.model_validate({"web": {"remote": {"maxMode": limit}}})
    got = mode_guard.above_remote_max(KIND_OF[name], mode, settings)
    assert (got is not None) is (refused and name == "remote_login"), (name, limit, mode)


async def test_what_a_remote_login_may_not_do_above_the_limit(
    core: Core, tmp_path: Path
) -> None:
    from snowpea_core.server.host_handlers import session_steer_handler
    from snowpea_core.server.session_handlers import (
        session_create_handler,
        session_prompt_handler,
        session_set_mode_handler,
    )

    person, remote = _conn("ide"), _conn("web", remote=True)
    work = tmp_path / "project"
    work.mkdir()
    core.settings.web.remote.maxMode = "accept"

    # A session: not created in auto, not raised to auto, not to "plan, then auto".
    with pytest.raises(RpcError) as caught:
        await session_create_handler(
            remote, SessionCreateParams(workdir=str(work), mode="auto"), core)
    _max(caught, "accept")
    made = await session_create_handler(
        remote, SessionCreateParams(workdir=str(work), mode="accept"), core)
    session = core.sessions.get(made.sessionId)
    for params in (SessionSetModeParams(sessionId=session.id, mode="auto"),
                   SessionSetModeParams(sessionId=session.id, mode="plan", autoAfterPlan=True)):
        with pytest.raises(RpcError) as caught:
            await session_set_mode_handler(remote, params, core)
        _max(caught, "accept")
    assert session.mode == "accept"
    await session_set_mode_handler(
        remote, SessionSetModeParams(sessionId=session.id, mode="plan"), core)  # lowering
    await session_set_mode_handler(
        remote, SessionSetModeParams(sessionId=session.id, mode="accept"), core)  # to the limit

    # A session the owner raised: no prompt, no steer, no turn command from the
    # remote login — it may read it, lower it and answer its approvals.
    await session_set_mode_handler(
        person, SessionSetModeParams(sessionId=session.id, mode="auto"), core)
    with pytest.raises(RpcError) as caught:
        await session_prompt_handler(
            remote, SessionPromptParams(sessionId=session.id, text="go on"), core)
    _max(caught, "accept")
    with pytest.raises(RpcError) as caught:
        core.commands.start(core, session, "compact", "", remote)
    _max(caught, "accept")
    session.turn_task = asyncio.get_running_loop().create_future()
    session.current_turn = "t-running"
    with pytest.raises(RpcError) as caught:
        await session_steer_handler(
            remote, SessionSteerParams(sessionId=session.id, text="also"), core)
    _max(caught, "accept")
    assert session.rpc_steers == [] and session.queued_turns == []
    core.commands.start(core, session, "help", "", remote)  # a read is no work
    session.turn_task.cancel()
    session.turn_task, session.current_turn = None, None
    core.commands.start(core, session, "accept", "", remote)  # lowering stays open
    await asyncio.sleep(0)
    for _ in range(50):
        if session.mode == "accept":
            break
        await asyncio.sleep(0.02)
    assert session.mode == "accept"

    # A job: not scheduled above it; one already above it is not run or changed.
    with pytest.raises(RpcError) as caught:
        await job_handlers.job_schedule_handler(remote, JobScheduleParams(
            spec=SPEC, task="x", workdir=str(work), mode="auto"), core)
    _max(caught, "accept")
    owners = await job_handlers.job_schedule_handler(person, JobScheduleParams(
        spec=SPEC, task="x", workdir=str(work), mode="auto"), core)
    with pytest.raises(RpcError) as caught:
        await job_handlers.job_run_now_handler(remote, JobIdParams(jobId=owners.jobId), core)
    _max(caught, "accept")
    core.settings.web.remote.maxMode = "auto"
    theirs = await job_handlers.job_schedule_handler(remote, JobScheduleParams(
        spec=SPEC, task="x", workdir=str(work), mode="auto"), core)
    core.settings.web.remote.maxMode = "accept"  # lowered after the job was made
    with pytest.raises(RpcError) as caught:
        await job_handlers.job_update_handler(
            remote, JobUpdateParams(jobId=theirs.jobId, task="y"), core)
    _max(caught, "accept")
    # ... and its scheduled run does not happen in that mode either.
    status, text, session_id = await core.scheduler._execute(
        await core.scheduler.get(theirs.jobId))
    assert status == "error" and "mode_above_remote_max" in text and session_id is None

    # A local person meets none of it.
    await session_prompt_handler(
        person, SessionPromptParams(sessionId=session.id, text="/help"), core)


async def test_a_remote_login_starts_no_session_above_the_limit_by_default(
    core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.server.session_handlers import session_create_handler

    work = tmp_path / "project"
    work.mkdir()
    monkeypatch.setattr(core.sessions, "default_mode", lambda _workdir: "auto")
    core.settings.web.remote.maxMode = "accept"
    with pytest.raises(RpcError) as caught:
        await session_create_handler(
            _conn("web", remote=True), SessionCreateParams(workdir=str(work)), core)
    _max(caught, "accept")
    made = await session_create_handler(
        _conn("web", remote=True), SessionCreateParams(workdir=str(work), mode="accept"), core)
    assert core.sessions.get(made.sessionId).mode == "accept"
    await session_create_handler(_conn("ide"), SessionCreateParams(workdir=str(work)), core)


def test_a_remote_logins_answer_to_a_mode_question_keeps_the_limit() -> None:
    from snowpea_core.session.questions import Answer
    from snowpea_core.tools.set_mode import _caller_of

    settings = Settings.model_validate({"web": {"remote": {"maxMode": "accept"}}})
    remote = _caller_of(Answer(selected=["auto"], person=True, person_kind="remote_login"))
    assert remote.kind == "remote_login"
    with pytest.raises(RpcError) as caught:
        mode_guard.check(remote, SimpleNamespace(), "plan", "auto", settings=settings)
    _max(caught, "accept")
    mode_guard.check(remote, SimpleNamespace(), "plan", "accept", settings=settings)
    local = _caller_of(Answer(selected=["auto"], person=True, person_kind="person"))
    mode_guard.check(local, SimpleNamespace(), "plan", "auto", settings=settings)
    model = _caller_of(Answer(selected=["auto"], person=False))
    with pytest.raises(RpcError):
        mode_guard.check(model, SimpleNamespace(), "plan", "auto", settings=settings)


async def test_the_status_reports_the_limit_and_only_a_local_person_sets_it(
    core: Core,
) -> None:
    from snowpea_core.server import settings_handlers, web_remote_handlers
    from snowpea_core.server.protocol import SettingsSetParams

    assert (await web_remote_handlers.status_handler(_conn("ide"), Empty(), core)
            ).maxMode == "auto"
    patch = {"web": {"remote": {"maxMode": "plan"}}}
    for name in sorted(set(KINDS) - {"person"}):
        conn, actor = _callers()[name]
        with pytest.raises(RpcError):
            await settings_handlers.settings_set_handler(
                conn, SettingsSetParams(scope="global", patch=patch, actor=actor), core)
    await settings_handlers.settings_set_handler(
        _conn("ide"), SettingsSetParams(scope="global", patch=patch), core)
    status = await web_remote_handlers.status_handler(_conn("ide"), Empty(), core)
    assert status.maxMode == "plan"
    # It applies at once, to the session manager's own copy of the settings too.
    assert mode_guard.remote_max(core.sessions.settings) == "plan"
    assert person_gate.PERSON_METHODS["web.remote.status"] is True  # the owner's screen
