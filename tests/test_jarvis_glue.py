"""M19 core integration: the glue between the work packages.

* unrestricted mode for an external session, asked from a messenger by the
  bound person and allowed there with one press (and every way it is not);
* the briefing's own text delivered to the messenger;
* requests already pending when Jarvis mode turns on are routed then;
* a handback from a messenger does not depend on ``jarvis.messenger.allowGrants``;
* ``jarvis-task`` workers stay out of Jarvis's reach;
* an assistant preset's read tools are safe for the delegation engine.

A real in-process daemon on a throwaway home, the fake chat platform, the fake
Claude Code CLI and a scripted provider; nothing real is contacted.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _external_adapter import FAKE_CLAUDE
from _support import env_vars, make_daemon

from snowpea_core.external.claude import ClaudeAdapter
from snowpea_core.gateway.fake import FakeAdapter
from snowpea_core.jarvis.ports import TASK_WORKER_SURFACE
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage
from snowpea_core.server.protocol import ApprovalRequest
from snowpea_core.session.history import message_text
from snowpea_core.tools.registry import ToolContext

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")

TIMEOUT = 15.0
PERSON = "u1"
MEMBER = "u2"
ASK = "UNRESTRICTED please"


@pytest_asyncio.fixture(autouse=True)
async def fake_chat() -> AsyncIterator[None]:
    with env_vars(
        SNOWPEA_GATEWAY_FAKE="1",
        FAKE_CLI_SCENARIO="normal",
        FAKE_CLI_SCENARIO_FILE=None,
    ):
        FakeAdapter.instances.clear()
        try:
            yield None
        finally:
            FakeAdapter.instances.clear()


class _Jarvis:
    """Jarvis asks for an unrestricted Claude session when the message says so."""

    vendor = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.project = ""
        self.results: list[str] = []
        self.wakes: list[str] = []
        self.reply = "Good morning. Nothing needs you."
        self.asks = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        # The runtime-context reminder after a tool result is not what is answered.
        said = [m for m in messages if not message_text(m).startswith("<system-reminder>")]
        last = said[-1]
        text = message_text(last)
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=5, output_tokens=1))
        if last.role == "tool":
            self.results.append(text)
            yield StreamEvent(kind="text_delta", text=f"tool said: {text[:300]}")
        elif ASK in text and self.project:
            self.asks += 1
            call = ToolCall(
                id=f"c{len(self.results)}",
                name="session_create",
                arguments={
                    "project": self.project,
                    "cli": "claude",
                    "mode": "unrestricted",
                    "prompt": f"clean up the build ({self.asks})",
                },
            )
            yield StreamEvent(kind="tool_call", tool_call=call)
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        elif "[briefing]" in text:
            self.wakes.append(text)
            yield StreamEvent(kind="text_delta", text=self.reply)
        else:
            yield StreamEvent(kind="text_delta", text=f"echo {text.splitlines()[0][:80]}")
        yield StreamEvent(kind="done", stop_reason="end_turn")


class Rig:
    def __init__(self, daemon: Any, tmp: Path, provider: _Jarvis) -> None:
        self.daemon = daemon
        self.core = daemon.core
        self.tmp = tmp
        self.provider = provider
        self.adapter: FakeAdapter = FakeAdapter.instances["tg_g"]

    @property
    def bridge(self) -> Any:
        return self.core.gateway.jarvis

    async def project(self, name: str = "proj") -> str:
        path = self.tmp / name
        path.mkdir(exist_ok=True)
        await self.core.sessions.create(path, mode="accept")  # registers the project
        return str(path.resolve())

    def ledger(self) -> list[dict[str, Any]]:
        path = Path(self.daemon.paths.home) / "jarvis" / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def unrestricted_sessions(self) -> list[dict[str, Any]]:
        manager = getattr(self.core, "external", None)
        if manager is None:
            return []
        return [row for row in manager.opened() if row.get("mode") == "unrestricted"]


def _only_the_fake(core: Any) -> None:
    from snowpea_core.server.external_handlers import ensure_manager

    registry = ensure_manager(core).registry
    for cli in registry.ids():
        registry.unregister(cli)
    registry.register(ClaudeAdapter(interrupt_grace=1.0, init_timeout=10.0))


async def _rig(tmp_path: Path, **jarvis: Any) -> Rig:
    settings = {
        "jarvis": {
            "folder": str(tmp_path / "JarvisFolder"),
            "external": {"clis": {"claude": {"path": str(FAKE_CLAUDE)}}},
            **jarvis,
        }
    }
    provider = _Jarvis()
    with env_vars(FAKE_CLI_LOG=str(tmp_path / "fake.log")):
        daemon = await make_daemon(tmp_path / "home", settings)
    daemon.core.providers.get = lambda _p, _m: provider
    _only_the_fake(daemon.core)
    await daemon.core.gateway.bind(
        "telegram",
        "tg_g",
        {"new_session": {"workdir": str(tmp_path), "mode": "auto"}},
        user_id=PERSON,
    )
    return Rig(daemon, tmp_path, provider)


@pytest_asyncio.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    made = await _rig(tmp_path)
    try:
        yield made
    finally:
        await made.daemon.stop()


async def _wait(predicate: Any, timeout: float = TIMEOUT) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def _idle(session: Any) -> None:
    await _wait(lambda: session.turn_task is None or session.turn_task.done())


def _warnings(adapter: FakeAdapter) -> list[Any]:
    return [m for m in adapter.sent if m.text.startswith("⚠️ unrestricted")]


# -- 1. unrestricted mode from a messenger ----------------------------------------------


async def test_the_bound_person_allows_an_unrestricted_session_with_one_press(rig: Rig) -> None:
    await rig.core.jarvis.enable()
    jarvis = rig.core.jarvis.session()
    rig.provider.project = await rig.project()
    await rig.adapter.push(ASK, channel_id="c1", user_id=PERSON)
    warning = (await _wait(lambda: _warnings(rig.adapter)))[0]
    assert "claude" in warning.text and "clean up the build" in warning.text
    assert "guarded-class rules do not apply" in warning.text
    # Someone else's press does nothing; the bound person's opens it.
    allow = warning.button_data()[0]
    await rig.adapter.press(allow, channel_id=warning.channel_id, user_id=MEMBER)
    await asyncio.sleep(0.2)
    assert rig.unrestricted_sessions() == []
    await rig.adapter.press(allow, channel_id=warning.channel_id, user_id=PERSON)
    await _wait(lambda: rig.provider.results)
    await _idle(jarvis)
    assert "created ext:claude:" in rig.provider.results[-1], rig.provider.results
    rows = rig.unrestricted_sessions()
    assert len(rows) == 1 and rows[0]["controlled"] is False
    entry = next(e for e in rig.ledger() if e.get("kind") == "unrestricted")
    assert entry["reason"] == "chosen on messenger:telegram" and entry["decidedBy"] == "person"
    assert entry["project"] == rig.provider.project and "claude" in entry["summary"]
    # The person's own words went in as the first prompt.
    record = rig.core.external._records[("claude", rows[0]["ref"]["id"])]
    await _wait(lambda: any(e["kind"] == "message.user" for e in record.events))
    # A second ask in the same turn is not possible: one press per message.
    assert rig.bridge._turns == {}


async def test_no_unrestricted_session_without_the_bound_persons_press(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    try:
        core = rig.core
        await core.jarvis.enable()
        jarvis = core.jarvis.session()
        root = rig.provider.project = await rig.project()
        rig.bridge.confirm_timeout_sec = 0.3

        # Jarvis on its own (a wake turn), even inside fully delegated work: refused,
        # nothing is posted.
        from snowpea_core.jarvis.delegation import Caller

        core.jarvis.engine.set_grant(
            "project", root, "full", caller=Caller(kind="connection", surface="ide")
        )
        jarvis.current_turn = "t-wake"
        jarvis.turn_origins["t-wake"] = "system"
        ctx = ToolContext(session=jarvis, core=core, backend=None)  # type: ignore[arg-type]
        args = {"project": root, "cli": "claude", "mode": "unrestricted"}
        refused = await jarvis.session_tools["session_create"].run(ctx, args)
        assert not refused.ok and "person's own choice" in refused.error
        # A human turn that did not come from a chat (the app): no surface to ask.
        jarvis.turn_origins["t-wake"] = "human"
        refused = await jarvis.session_tools["session_create"].run(ctx, args)
        assert not refused.ok and "person's own choice" in refused.error
        assert _warnings(rig.adapter) == []

        # Another member of the chat asking: their turn is not the person's.
        await rig.adapter.push(ASK, channel_id="c1", user_id=MEMBER)
        await _wait(lambda: rig.provider.results)
        await _idle(jarvis)
        assert "person's own choice" in rig.provider.results[-1]
        assert _warnings(rig.adapter) == []

        # The bound person, but nobody presses in time.
        await rig.adapter.push(ASK, channel_id="c1", user_id=PERSON)
        await _wait(lambda: len(rig.provider.results) == 2)
        await _idle(jarvis)
        assert "no answer in time" in rig.provider.results[-1]
        assert len(_warnings(rig.adapter)) == 1

        # The bound person declines.
        await rig.adapter.push(ASK, channel_id="c1", user_id=PERSON)
        warning = (await _wait(lambda: _warnings(rig.adapter)[1:]))[0]
        await rig.adapter.press(warning.button_data()[1], channel_id=warning.channel_id)
        await _wait(lambda: len(rig.provider.results) == 3)
        await _idle(jarvis)
        assert "declined" in rig.provider.results[-1]

        # With jarvis.messenger.allowGrants off nothing is even asked.
        core.settings.jarvis.messenger.allowGrants = False
        await rig.adapter.push(ASK, channel_id="c1", user_id=PERSON)
        await _wait(lambda: len(rig.provider.results) == 4)
        await _idle(jarvis)
        assert "allowGrants" in rig.provider.results[-1]
        assert len(_warnings(rig.adapter)) == 2
        assert rig.unrestricted_sessions() == []
        assert not [e for e in rig.ledger() if e.get("kind") == "unrestricted"]
        # external_send cannot reach an unrestricted session either way (it never takes a mode).
        schema = jarvis.session_tools["external_send"].spec().input_schema
        assert set(schema["properties"]) == {"sessionId", "text"}
    finally:
        await rig.daemon.stop()


# -- 2. the briefing in the messenger ---------------------------------------------------


async def test_the_briefing_text_reaches_the_messenger(rig: Rig) -> None:
    core = rig.core
    await core.jarvis.enable()
    jarvis = core.jarvis.session()
    await _idle(jarvis)
    rig.provider.reply = "Morning briefing. " + "x" * 6000
    from snowpea_core.jarvis import briefing

    await briefing.set_briefing(core, enabled=True, schedule="0 9 * * *", sections=["todos"])
    job = await core.scheduler.get((await briefing.get(core)).jobId)
    before = len(rig.adapter.sent)
    await core.scheduler.run_now(job.id)
    sent = await _wait(
        lambda: [m for m in rig.adapter.sent[before:] if "briefing" in m.text.lower()]
    )
    text = sent[0].text
    assert "Morning briefing." in text and len(text) <= 3500
    assert "reports/briefing-" in text  # the pointer to the saved report
    # One message; the "ready" notice does not follow it into the chat.
    await asyncio.sleep(0.3)
    assert not [m for m in rig.adapter.sent[before:] if "briefing is ready" in m.text]
    report = next((core.jarvis.folder() / "reports").glob("briefing-*.md"))
    assert "Morning briefing." in report.read_text()
    # Every wake Jarvis receives is one stable message.user origin: system, no surface.
    wakes = [t for t, o in jarvis.turn_origins.items() if o == "system"]
    assert wakes
    assert all(jarvis.turn_message_origins[t] == {"kind": "system", "surface": None}
               for t in wakes)

    # Off: the briefing stays in the app.
    core.settings.jarvis.notify.messenger = False
    before = len(rig.adapter.sent)
    await _idle(jarvis)
    wakes = len(rig.provider.wakes)
    # (Directly: a second runNow in the same second is the same run to the scheduler.)
    status, _line, _session = await briefing.run(core, core.jarvis, job, None)
    assert status == "ok"
    await _wait(lambda: len(rig.provider.wakes) > wakes)
    await _idle(jarvis)
    await asyncio.sleep(0.3)
    assert len(rig.adapter.sent) == before


# -- 3. requests pending when Jarvis turns on -------------------------------------------


async def test_requests_pending_at_enable_are_routed_and_reach_the_chat_once(rig: Rig) -> None:
    core = rig.core
    folder = rig.tmp / "work"
    folder.mkdir()
    worker = await core.sessions.create(folder, mode="auto")
    worker.title = "Worker"
    risky = asyncio.ensure_future(
        core.approvals.request(worker, "shell", {"command": "rm -rf build"}, unattended=True)
    )
    safe = asyncio.ensure_future(
        core.approvals.request(
            worker, "shell", {"command": "git status"}, unattended=True, tags=("exec",)
        )
    )
    await _wait(lambda: len(core.approvals.list()) == 2)
    assert all(r.route is None for r in core.approvals.list())

    before = len(rig.adapter.sent)
    await core.jarvis.enable()
    # The safe one is answered by the engine; the risky one is the person's, in the chat once.
    assert (await asyncio.wait_for(safe, TIMEOUT)).allowed
    pending = core.approvals.list()
    assert [r.route for r in pending] == ["person"]
    request_id = pending[0].requestId
    await _wait(
        lambda: [m for m in rig.adapter.sent[before:] if request_id in "".join(m.button_data())]
    )
    await core.jarvis.route_pending()  # again: nothing new is posted
    await asyncio.sleep(0.3)
    posts = [m for m in rig.adapter.sent if request_id in "".join(m.button_data())]
    assert len(posts) == 1

    await core.jarvis.disable()
    assert [r.route for r in core.approvals.list()] == [None]
    await core.approvals.respond(request_id, "deny", "once", by="test")
    assert not (await asyncio.wait_for(risky, TIMEOUT)).allowed


# -- 4. handback from a messenger --------------------------------------------------------


async def test_handback_from_a_messenger_does_not_need_allow_grants(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, messenger={"allowGrants": False})
    try:
        core = rig.core
        await core.jarvis.enable()
        folder = rig.tmp / "proj"
        folder.mkdir()
        worker = await core.sessions.create(folder, mode="auto")
        worker.title = "Login bug"
        from snowpea_core.session import events

        await core.hub.emit_event(worker.id, events.turn_started("t1", "go"))

        async def says(text: str, user: str = PERSON) -> str:
            before = len(rig.adapter.sent)
            await rig.adapter.push(text, channel_id="c1", user_id=user)
            return (await rig.adapter.wait_for_send(TIMEOUT, count=before + 1)).text

        assert "handback" in await says("/jarvis takeover login")
        assert core.jarvis.engine.pause_state(worker.id).paused
        assert "bound to this chat" in await says("/jarvis handback login", user=MEMBER)
        assert "handed back" in await says("/jarvis handback login")
        assert not core.jarvis.engine.pause_state(worker.id).paused
        # Grants still need the setting.
        from snowpea_core.jarvis.delegation import Caller, PersonOnly

        with pytest.raises(PersonOnly):
            core.jarvis.engine.set_grant(
                "session", worker.id, "full",
                caller=Caller.messenger("telegram", bound_person=True),
            )
    finally:
        await rig.daemon.stop()


# -- 5. jarvis-task workers --------------------------------------------------------------


async def test_jarvis_task_workers_are_out_of_jarvis_reach(rig: Rig) -> None:
    core = rig.core
    await core.jarvis.enable()
    jarvis = core.jarvis.session()
    folder = rig.tmp / "proj"
    folder.mkdir()
    worker = await core.sessions.create(
        folder, mode="accept", origin_surface=TASK_WORKER_SURFACE, kind="scheduled", job_id="j1"
    )
    worker.allowed_tools = {"read_file"}
    worker.exclusive_tools = True
    service = core.jarvis
    # Not supervised: no digest, no overview row, not readable or instructable.
    assert worker.id not in service.intake.digests
    assert worker.id not in {row.sessionId for row in service.overview_rows()}
    jarvis.current_turn = "t-h"
    jarvis.turn_origins["t-h"] = "human"
    ctx = ToolContext(session=jarvis, core=core, backend=None)  # type: ignore[arg-type]
    for name, args in (
        ("session_read", {"sessionId": worker.id}),
        ("session_prompt", {"sessionId": worker.id, "text": "forward the mail"}),
        ("session_steer", {"sessionId": worker.id, "text": "x"}),
        ("session_interrupt", {"sessionId": worker.id}),
    ):
        result = await jarvis.session_tools[name].run(ctx, args)
        assert not result.ok and "scheduled-task worker" in result.error, (name, result.error)
    listed = await jarvis.session_tools["session_list"].run(ctx, {})
    assert worker.id not in (listed.output or "")
    # Its requests are the person's, never Jarvis's or the engine's.
    request = ApprovalRequest(
        requestId="ap-w", sessionId=worker.id, tool="read_file", args={"path": "x"},
        risk="low", timeoutSec=0,
    )
    assert service.route_approval(request, worker, ("read",)) == "person"
    # Its tool list is exactly what it was given: none of Jarvis's, no hook-registered tool.
    active = {tool.name for tool in core.tools.active(worker)}
    assert active == {"read_file"}
    hooked = {t.name for factory in service.hooks.tools for t in factory(service)}
    assert {"external_list", "external_send", "briefing_set"} <= hooked
    assert not active & (hooked | set(jarvis.session_tools))
    # A stored row of a worker is refused the same way.
    await core.sessions.close(worker.id)
    result = await jarvis.session_tools["session_read"].run(ctx, {"sessionId": worker.id})
    assert not result.ok and "scheduled-task worker" in result.error


# -- 6. preset read tools and the engine -------------------------------------------------


@pytest.mark.parametrize("preset_id", ["google", "microsoft365"])
async def test_preset_read_tools_route_auto_and_send_tools_are_secret_send(
    tmp_path: Path, preset_id: str
) -> None:
    from snowpea_core.jarvis import presets
    from snowpea_core.tools import mcp_catalog, mcp_client

    daemon = await make_daemon(
        tmp_path / "home", {"jarvis": {"folder": str(tmp_path / "J"), "enabled": True}}
    )
    try:
        core = daemon.core
        preset = mcp_catalog.preset(preset_id)
        assert preset is not None
        named = [(f"me-{s['suffix']}", s) for s in preset["servers"]]
        core.settings.mcp.permissions = presets.desired_permissions(named)
        folder = tmp_path / "proj"
        folder.mkdir()
        worker = await core.sessions.create(folder, mode="auto")
        service = core.jarvis
        checked = 0
        for server, spec in named:
            for tool, expect in [(t, "read") for t in spec["readTools"]] + [
                (t, "send") for t in spec["sendTools"]
            ]:
                tag = mcp_client.permission_for(core, server, tool)
                assert tag == expect, (server, tool, tag)
                name = mcp_client.tool_name(server, tool)
                request = ApprovalRequest(
                    requestId=f"ap-{checked}", sessionId=worker.id, tool=name, args={},
                    risk="medium", timeoutSec=0,
                )
                route = service.route_approval(request, worker, (tag,))
                verdict = service.engine.routed(request.requestId).verdict
                if expect == "read":
                    assert route == "auto", (name, verdict)
                    assert verdict.classification == "safe" and verdict.rules == ("tag:read",)
                else:
                    assert route == "person", (name, verdict)
                    assert "secret_send" in verdict.guarded, (name, verdict)
                checked += 1
        assert checked >= 10
        # The same tool untagged (no preset) is not read: it talks to a server.
        core.settings.mcp.permissions = {}
        server, spec = named[0]
        name = mcp_client.tool_name(server, spec["readTools"][0])
        request = ApprovalRequest(
            requestId="ap-untagged", sessionId=worker.id, tool=name, args={}, risk="medium",
            timeoutSec=0,
        )
        tag = mcp_client.permission_for(core, server, spec["readTools"][0])
        assert service.route_approval(request, worker, (tag,)) != "auto"
    finally:
        await daemon.stop()



# -- route changes are announced -------------------------------------------------------


class _Origin:
    """An interactive surface: it is asked over approval.request and never answers."""

    client_kind = "ide"
    client_id = "ide-x"
    authenticated = True
    closed = False

    def __init__(self) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self.on_close: list[Any] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.frames.append((method, params))

    async def call(self, method: str, params: Any = None, **_: Any) -> Any:
        await asyncio.Event().wait()

    def of(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.frames if name == method]


async def test_route_changes_are_announced_to_the_requests_audience(
    http: Any, tmp_path: Path
) -> None:
    from _support import connect

    from snowpea_core.jarvis.delegation import Caller
    from snowpea_core.server.protocol import QuestionItem

    rig = await _rig(tmp_path)
    try:
        core = rig.core
        owner = await connect(http, rig.daemon, approval_mode="ignore")
        other = await connect(http, rig.daemon, approval_mode="ignore")
        folder = rig.tmp / "work"
        folder.mkdir()
        worker = await core.sessions.create(folder, mode="accept")
        root = str(folder.resolve())

        def changes(client: Any, method: str = "approval.route.changed") -> list[Any]:
            return client.of_method(method)

        # Waiting before Jarvis mode: routed at enable (jarvis_enabled), to everyone.
        shared = asyncio.ensure_future(core.approvals.request(
            worker, "shell", {"command": "touch a.txt"}, unattended=True, tags=("exec",)))
        question = asyncio.ensure_future(
            core.questions.ask(worker, [QuestionItem(question="which?", header="q")]))
        # An interactive one: only its asking connection hears about it.
        origin = _Origin()
        attended = await core.sessions.create(folder, mode="accept", origin_conn=origin)
        interactive = asyncio.ensure_future(core.approvals.request(
            attended, "shell", {"command": "touch b.txt"}, tags=("exec",)))
        await _wait(lambda: len(core.approvals.list()) == 1 and core.questions.list()
                    and core.approvals.count(attended.id))
        request_id = core.approvals.list()[0].requestId
        await core.jarvis.enable()
        first = await _wait(lambda: changes(owner))
        assert first[0] == {"requestId": request_id, "sessionId": worker.id, "route": "person",
                            "reason": "jarvis_enabled", "hostToolsFrom": None}
        assert changes(other) == changes(owner)
        mine = await _wait(lambda: origin.of("approval.route.changed"))
        assert mine[0]["sessionId"] == attended.id and mine[0]["reason"] == "jarvis_enabled"
        assert attended.id not in {c["sessionId"] for c in changes(owner) + changes(other)}
        interactive.cancel()
        asked = await _wait(lambda: changes(owner, "question.route.changed"))
        assert asked[0]["reason"] == "jarvis_enabled" and asked[0]["route"] == "jarvis"
        assert core.approvals.get(request_id).route == "person"

        # Full delegation: a risky request is Jarvis's; the person types there: paused.
        core.jarvis.engine.set_grant(
            "session", worker.id, "full", caller=Caller(kind="connection", surface="ide"))
        risky = asyncio.ensure_future(core.approvals.request(
            worker, "shell", {"command": "mkdir out"}, unattended=True, tags=("exec",)))
        await _wait(lambda: len(core.approvals.list()) == 2)
        held = next(r for r in core.approvals.list() if r.requestId != request_id)
        assert held.route == "jarvis"
        await core.jarvis.person_typed(worker.id, root, "ide")
        paused = await _wait(lambda: [c for c in changes(owner) if c["reason"] == "paused"])
        assert paused[0]["requestId"] == held.requestId and paused[0]["route"] == "person"
        assert core.approvals.get(held.requestId).route == "person"
        # Jarvis can no longer decide it.
        jarvis = core.jarvis.session()
        jarvis.current_turn = "t-w"
        jarvis.turn_origins["t-w"] = "system"
        ctx = ToolContext(session=jarvis, core=core, backend=None)  # type: ignore[arg-type]
        refused = await jarvis.session_tools["approval_decide"].run(
            ctx, {"requestId": held.requestId, "decision": "allow"})
        assert not refused.ok
        # Handed back: routed again, to Jarvis (resumed).
        core.jarvis.engine.handback(worker.id, caller=Caller(kind="connection", surface="ide"))
        await core.jarvis.settle()
        resumed = await _wait(lambda: [c for c in changes(owner) if c["reason"] == "resumed"])
        assert resumed[0]["route"] == "jarvis"
        # The grant is revoked before Jarvis decides: its decision is refused, the request
        # is the person's (refused).
        for grant in core.jarvis.engine.list_grants():
            core.jarvis.engine.revoke(grant.id, caller=Caller(kind="connection", surface="ide"))
        refused = await jarvis.session_tools["approval_decide"].run(
            ctx, {"requestId": held.requestId, "decision": "allow"})
        assert not refused.ok
        refusal = await _wait(lambda: [c for c in changes(owner) if c["reason"] == "refused"])
        assert refusal[0]["route"] == "person"

        # Off: every route is cleared (jarvis_disabled), the question's too.
        await core.jarvis.disable()
        cleared = [c for c in changes(owner) if c["reason"] == "jarvis_disabled"]
        assert {c["requestId"] for c in cleared} == {request_id, held.requestId}
        assert all(c["route"] is None for c in cleared)
        assert [c for c in changes(owner, "question.route.changed")
                if c["reason"] == "jarvis_disabled"]
        for request in core.approvals.list():
            await core.approvals.respond(request.requestId, "deny", by="test")
        await asyncio.wait_for(asyncio.gather(shared, risky), TIMEOUT)
        question.cancel()
        await owner.stop()
        await other.stop()
    finally:
        await rig.daemon.stop()


async def test_what_jarvis_leaves_undecided_becomes_the_persons(
    http: Any, tmp_path: Path
) -> None:
    from _support import connect

    from snowpea_core.jarvis.delegation import Caller

    rig = await _rig(tmp_path)
    try:
        core = rig.core
        client = await connect(http, rig.daemon, approval_mode="ignore")
        await core.jarvis.enable()
        folder = rig.tmp / "work"
        folder.mkdir()
        worker = await core.sessions.create(folder, mode="accept")
        core.jarvis.engine.set_grant(
            "session", worker.id, "full", caller=Caller(kind="connection", surface="ide"))
        task = asyncio.ensure_future(core.approvals.request(
            worker, "shell", {"command": "mkdir out"}, unattended=True, tags=("exec",)))
        await _wait(lambda: core.approvals.list())
        request = core.approvals.list()[0]
        assert request.route == "jarvis"
        # Jarvis's wake turn only says "echo ...": the request is the person's after it.
        declined = await _wait(lambda: [c for c in client.of_method("approval.route.changed")
                                        if c["reason"] == "declined"])
        assert declined[0]["requestId"] == request.requestId and declined[0]["route"] == "person"
        assert core.approvals.get(request.requestId).route == "person"
        escalation = await _wait(lambda: [e for e in client.of_method("jarvis.escalation")
                                          if e.get("requestId") == request.requestId])
        assert escalation[0]["importance"] == "high"
        # And it reached the person's chat.
        await _wait(lambda: [m for m in rig.adapter.sent
                             if request.requestId in "".join(m.button_data())])
        await core.approvals.respond(request.requestId, "deny", by="test")
        await asyncio.wait_for(task, TIMEOUT)
        await client.stop()
    finally:
        await rig.daemon.stop()
