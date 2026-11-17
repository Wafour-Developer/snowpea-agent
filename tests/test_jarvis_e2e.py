"""M19 Jarvis mode (beta), the whole feature end to end, as a client drives it.

One story on a throwaway home: a scripted provider plays Jarvis and the worker
sessions, the fake Claude Code CLI plays the external session, the fake chat
platform plays the person's messenger, and the fake mail MCP server plays the
mailbox.  No model, CLI, messenger or mail account is ever contacted.

enable → overview (snowpea worker, external Claude session, browser-host session
with exposeBrowser on and off) → staged: a risky approval reaches the person
and the messenger → the person confirms Jarvis's ``delegation_set`` proposal
with one press → Jarvis decides the next approval (ledger + ``jarvis.decision``)
→ a guarded request still asks → the person types in the worker: paused;
handback → a stall of the delegated session is an escalation that survives a
reconnect → the briefing fires and reaches the messenger → disable: the chats
go back, routes are cleared, the external session keeps working → restart:
grants, pauses and the briefing schedule are intact.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _external_adapter import FAKE_CLAUDE

# Written when Jarvis supervised every session: every session is shared (1.56.0).
from _support import RpcClient, connect, env_vars, pinned_cli
from _support import make_shared_daemon as make_daemon

from snowpea_core.agent import loop as agent_loop
from snowpea_core.external.claude import ClaudeAdapter
from snowpea_core.gateway.fake import FakeAdapter
from snowpea_core.jarvis.clock import Clock
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage
from snowpea_core.session import events
from snowpea_core.session.history import message_text
from snowpea_core.tools import mcp_catalog, mcp_client

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")

TIMEOUT = 20.0
PERSON = "u1"
MAIL_SERVER = Path(__file__).parent / "fixtures" / "mcp" / "mail_server.py"
BRIEFING = "Good morning. One mail needs a reply; the build cleanup is done."
MAIL_TOOL = "mcp__fakemail-mail__search_threads"


# -- fixtures -------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[None]:
    mail = {
        "suffix": "mail",
        "title": "Fake mail",
        "kind": "mail",
        "transport": "stdio",
        "auth": "none",
        "entry": {"command": sys.executable, "args": [str(MAIL_SERVER), "{MAIL_LOG}"]},
        "readTools": ["search_threads", "get_thread", "list_labels"],
        "sendTools": ["create_draft", "send_mail"],
    }
    preset = {
        "id": "fakemail",
        "title": "fakemail",
        "description": "fake",
        "category": "mail",
        "homepage": "",
        "inputs": [
            {"name": "MAIL_LOG", "label": "log", "secret": False, "required": True,
             "placeholder": ""},
        ],
        "servers": [mail],
        "recipes": [],
    }
    monkeypatch.setattr(mcp_catalog, "PRESETS", (preset,))
    mcp_client.MANAGER.listeners = []
    FakeAdapter.instances.clear()
    with env_vars(
        SNOWPEA_GATEWAY_FAKE="1",
        FAKE_CLI_SCENARIO="normal",
        FAKE_CLI_SCENARIO_FILE=None,
        FAKE_CLI_LOG=str(tmp_path / "fake-cli.log"),
    ):
        try:
            yield None
        finally:
            await mcp_client.MANAGER.close_all()
            mcp_client.MANAGER.listeners = []
            FakeAdapter.instances.clear()


class Skew(Clock):
    """The service's clock, movable forward (stall detection)."""

    def __init__(self) -> None:
        self.offset = 0.0

    def monotonic(self) -> float:
        return super().monotonic() + self.offset


class Brain:
    """Jarvis, the worker sessions and the mail worker on one scripted provider."""

    vendor = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.worker_id = ""
        self.jarvis_results: list[str] = []
        self.jarvis_wakes: list[str] = []
        self.decided: list[str] = []

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        system = "\n".join(message_text(m) for m in messages if m.role == "system")
        said = [m for m in messages if m.role != "system"
                and not message_text(m).startswith("<system-reminder>")]
        last = said[-1]
        text = message_text(last)
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=5, output_tokens=1))
        if "You are Jarvis" in system:
            async for event in self._jarvis(last, text):
                yield event
            return
        if "isolated reading worker" in system:
            if last.role == "user":
                yield self._call("m1", MAIL_TOOL, {"query": "newer_than:1d"})
                yield StreamEvent(kind="done", stop_reason="tool_use")
                return
            yield StreamEvent(kind="text_delta", text="Digest: 1 mail from boss@example.com.")
            yield StreamEvent(kind="done", stop_reason="end_turn")
            return
        # A worker session: "RUN: <command>" runs it in the shell.
        if last.role == "user" and text.startswith("RUN: "):
            command = text[len("RUN: "):].splitlines()[0]
            yield self._call(f"w{len(messages)}", "shell", {"command": command})
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        reply = "done" if last.role == "tool" else f"echo {text.splitlines()[0][:60]}"
        yield StreamEvent(kind="text_delta", text=reply)
        yield StreamEvent(kind="done", stop_reason="end_turn")

    @staticmethod
    def _call(call_id: str, name: str, args: dict[str, Any]) -> StreamEvent:
        return StreamEvent(kind="tool_call", tool_call=ToolCall(id=call_id, name=name,
                                                                arguments=args))

    async def _jarvis(self, last: ChatMessage, text: str) -> Any:
        if last.role == "tool":
            self.jarvis_results.append(text)
            yield StreamEvent(kind="text_delta", text=f"ok: {text[:200]}")
        elif "take over the worker" in text:
            yield self._call("j-grant", "delegation_set",
                             {"scope": "session", "target": self.worker_id, "level": "full",
                              "note": "the person asked"})
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        elif "Waiting for your decision" in text:
            self.jarvis_wakes.append(text)
            found = re.search(r"approval (ap-[0-9a-f]+)", text)
            if found:
                self.decided.append(found.group(1))
                yield self._call(f"j-{found.group(1)}", "approval_decide",
                                 {"requestId": found.group(1), "decision": "allow",
                                  "reason": "routine cleanup inside the project"})
                yield StreamEvent(kind="done", stop_reason="tool_use")
                return
            yield StreamEvent(kind="text_delta", text="noted")
        elif "[briefing]" in text:
            self.jarvis_wakes.append(text)
            yield StreamEvent(kind="text_delta", text=BRIEFING)
        else:
            self.jarvis_wakes.append(text)
            yield StreamEvent(kind="text_delta", text="noted")
        yield StreamEvent(kind="done", stop_reason="end_turn")


def _only_the_fake(core: Any) -> None:
    from snowpea_core.server.external_handlers import ensure_manager

    registry = ensure_manager(core).registry
    for cli in registry.ids():
        registry.unregister(cli)
    registry.register(ClaudeAdapter(interrupt_grace=1.0, init_timeout=10.0))


async def _start(home: Path, brain: Brain, settings: dict[str, Any] | None = None) -> Any:
    daemon = await make_daemon(home, settings)
    daemon.core.providers.get = lambda _p, _m: brain
    _only_the_fake(daemon.core)
    return daemon


async def _wait(predicate: Any, timeout: float = TIMEOUT, what: str = "condition") -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError(f"{what} not reached")


async def _idle(session: Any) -> None:
    await _wait(lambda: session.turn_task is None or session.turn_task.done(), what="idle")


def _pending(client: RpcClient, *, route: str | None = None, tool_args: str = "") -> list[Any]:
    return [
        p["request"] for p in client.of_method("approval.pending")
        if (route is None or p["request"].get("route") == route)
        and tool_args in str(p["request"].get("args"))
    ]


def _chat_posts(adapter: FakeAdapter, needle: str) -> list[Any]:
    return [m for m in adapter.sent if needle in m.text or needle in "".join(m.button_data())]


async def _overview(client: RpcClient) -> list[dict[str, Any]]:
    return list((await client.ok("jarvis.overview", {}))["rows"])


# -- the story ------------------------------------------------------------------------


async def test_jarvis_mode_end_to_end(http: aiohttp.ClientSession, tmp_path: Path) -> None:
    home = tmp_path / "home"
    brain = Brain()
    settings = {
        "jarvis": {
            "folder": str(tmp_path / "JarvisFolder"),
            "stallMinutes": 1,
            "external": {"clis": {"claude": {**pinned_cli(FAKE_CLAUDE)}}},
        }
    }
    daemon = await _start(home, brain, settings)
    core = daemon.core
    proj = tmp_path / "proj"
    proj.mkdir()
    shop = tmp_path / "shop"
    shop.mkdir()
    person = await connect(http, daemon, approval_mode="ignore")
    try:
        await core.gateway.bind(
            "telegram", "tg_e2e", {"new_session": {"workdir": str(tmp_path), "mode": "auto"}},
            user_id=PERSON,
        )
        chat = FakeAdapter.instances["tg_e2e"]
        applied = await person.ok(
            "mcp.preset.apply", {"id": "fakemail", "inputs": {"MAIL_LOG": str(tmp_path / "m")}}
        )
        assert applied["servers"], applied

        # A worker whose client went away (its asks are unattended), and a browser-host one.
        worker = await core.sessions.create(proj, mode="accept")
        worker.title = "Build cleanup"
        brain.worker_id = worker.id
        browser = await core.sessions.create(shop, mode="auto")
        browser.title = "Shop checkout"
        browser.host_tools_from = "browser-1"

        # -- enable ---------------------------------------------------------------
        status = await person.ok("jarvis.enable", {})
        assert status.get("enabled", True)
        jarvis = core.jarvis.session()
        assert jarvis is not None
        core.jarvis.clock = skew = Skew()
        await person.wait_notification("jarvis.state")
        root = str(proj.resolve())
        created = await person.ok("external.create",
                                  {"cli": "claude", "project": root, "mode": "default"})
        ext = created["session"]["ref"]

        async def claude_models_loaded() -> bool:
            clis = (await person.ok("external.clis", {}))["clis"]
            return next(c for c in clis if c["id"] == "claude")["modelsLoaded"]

        for _ in range(int(TIMEOUT / 0.1)):
            if await claude_models_loaded():
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("the claude model listing never answered")
        assert all(c["modelsLoaded"] is False
                   for c in (await person.ok("external.clis", {}))["clis"] if c["id"] != "claude")
        await core.hub.emit_event(browser.id, events.turn_started("tb", "pay"))
        await core.hub.emit_event(browser.id, events.message_done("Paid the order."))
        await core.hub.emit_event(browser.id, events.turn_done("tb", "complete"))

        rows = await _overview(person)
        by_id = {r.get("sessionId") or f"ext:{r['external']['cli']}:{r['external']['id']}": r
                 for r in rows}
        assert worker.id in by_id and by_id[worker.id]["level"] == "staged"
        assert f"ext:claude:{ext['id']}" in by_id
        external_row = by_id[f"ext:claude:{ext['id']}"]
        assert external_row["host"] == "snowpea" and external_row["controlled"] is True
        assert by_id[browser.id]["exposed"] is True and "Paid" in by_id[browser.id]["digest"]
        assert jarvis.id not in by_id
        await person.ok("settings.set", {"scope": "global",
                                         "patch": {"jarvis": {"exposeBrowser": False}}})
        await _wait(lambda: core.settings.jarvis.exposeBrowser is False, what="settings")
        # 1.56.0: a hidden browser session is locked off: not in the overview at all.
        assert browser.id not in {r.get("sessionId") for r in await _overview(person)}
        await person.ok("settings.set", {"scope": "global",
                                         "patch": {"jarvis": {"exposeBrowser": True}}})

        # -- staged: a risky approval is the person's, in the app and the chat -----
        agent_loop.start_turn(core, worker, "RUN: touch notes.txt", unattended=True,
                              origin="schedule")

        risky = (await _wait(lambda: _pending(person, tool_args="touch notes.txt"),
                             what="risky approval"))[0]
        assert risky["route"] == "person"
        await _wait(lambda: _chat_posts(chat, risky["requestId"]), what="risky in the chat")
        await person.ok("approval.respond", {"requestId": risky["requestId"], "decision": "allow"})
        await _idle(worker)

        # -- the person asks Jarvis (in the chat) to take the worker over ----------
        await chat.push("please take over the worker", channel_id="c1", user_id=PERSON)
        proposal = (await _wait(lambda: [m for m in chat.with_buttons()
                                         if "asks for a delegation" in m.text],
                                what="grant proposal"))[0]
        assert "level: full" in proposal.text
        await chat.press(proposal.button_data()[0], channel_id=proposal.channel_id,
                         user_id=PERSON)
        await _wait(lambda: any("the person confirmed" in r for r in brain.jarvis_results),
                    what="grant confirmed")
        await _idle(jarvis)
        grants = (await person.ok("jarvis.delegation.list", {}))["grants"]
        mine = [g for g in grants if g["target"] == worker.id]
        assert mine and mine[0]["level"] == "full" and mine[0]["proposedBy"] == "jarvis"
        row = {r.get("sessionId"): r for r in await _overview(person)}[worker.id]
        assert row["level"] == "full" and row["levelScope"] == "session"
        assert row["grantId"] == mine[0]["id"]

        # -- Jarvis decides the next risky approval --------------------------------
        agent_loop.start_turn(core, worker, "RUN: mkdir out", unattended=True,
                              origin="schedule")
        routed = (await _wait(lambda: _pending(person, tool_args="mkdir out"),
                              what="routed approval"))[0]
        assert routed["route"] == "jarvis"
        await _wait(lambda: routed["requestId"] in brain.decided, what="Jarvis decided")
        await _idle(worker)
        decision = await _wait(
            lambda: [d for d in person.of_method("jarvis.decision")
                     if d.get("entry", d).get("requestId") == routed["requestId"]],
            what="jarvis.decision",
        )
        entry = decision[0].get("entry", decision[0])
        assert entry["decidedBy"] == "jarvis" and entry["decision"] == "allow"
        ledger = (await person.ok("jarvis.ledger", {"sessionId": worker.id}))["entries"]
        assert any(e.get("requestId") == routed["requestId"] and e["decidedBy"] == "jarvis"
                   for e in ledger)
        # Nothing routed to Jarvis reached the chat.
        assert not _chat_posts(chat, routed["requestId"])

        # -- a guarded request still asks the person --------------------------------
        agent_loop.start_turn(core, worker, "RUN: git push --force origin main",
                              unattended=True, origin="schedule")
        guarded = (await _wait(lambda: _pending(person, tool_args="git push"),
                               what="guarded approval"))[0]
        assert guarded["route"] == "person"
        await _wait(lambda: _chat_posts(chat, guarded["requestId"]), what="guarded in the chat")
        await person.ok("approval.respond",
                        {"requestId": guarded["requestId"], "decision": "deny"})
        await _idle(worker)

        # -- the person types in the worker: paused; then hands it back ------------
        await person.ok("session.prompt", {"sessionId": worker.id, "text": "let me look"})
        await _idle(worker)
        await core.jarvis.settle()
        state = (await person.ok("jarvis.status", {}))["paused"]
        assert [p for p in state if p.get("sessionId") == worker.id]
        agent_loop.start_turn(core, worker, "RUN: touch cache.txt", unattended=True,
                              origin="schedule")
        held = (await _wait(lambda: _pending(person, tool_args="touch cache.txt"),
                            what="approval while paused"))[0]
        assert held["route"] == "person"  # paused: Jarvis decides nothing there
        await person.ok("approval.respond", {"requestId": held["requestId"], "decision": "deny"})
        await _idle(worker)
        await person.ok("jarvis.handback", {"sessionId": worker.id})
        assert not (await person.ok("jarvis.status", {}))["paused"]

        # -- a stall of the delegated session is an escalation that survives --------
        await core.hub.emit_event(worker.id, events.turn_started("t-stall", "long job"))
        skew.offset += 120.0
        await core.jarvis.intake.check_stalls()
        await _wait(lambda: [e for e in person.of_method("jarvis.escalation")
                             if e["reason"] == "stalled"], what="stall escalation")
        await person.stop()
        person = await connect(http, daemon, approval_mode="ignore")
        open_ = (await person.ok("jarvis.escalations.list", {}))["escalations"]
        assert [e for e in open_ if e["reason"] == "stalled" and e["sessionId"] == worker.id]
        await core.hub.emit_event(worker.id, events.turn_done("t-stall", "complete"))
        await _idle(jarvis)

        # -- the briefing fires and is delivered ------------------------------------
        on = await person.ok("jarvis.briefing.set", {
            "enabled": True, "schedule": "0 9 * * *",
            "sections": ["sessions", "waiting", "todos", "mail"],
        })
        before = len(chat.sent)
        await person.ok("job.runNow", {"jobId": on["jobId"]}, timeout=60)
        posted = await _wait(lambda: [m for m in chat.sent[before:] if BRIEFING in m.text],
                             what="briefing in the chat")
        assert "reports/briefing-" in posted[0].text
        wake = next(w for w in brain.jarvis_wakes if "[briefing]" in w)
        assert "boss@example.com" in wake  # the mail digest, from the isolated worker
        assert "Build cleanup" in wake
        await _idle(jarvis)

        # -- the person takes the external session over (kept across the restart) ---
        await person.ok("jarvis.takeover", {"external": ext})

        # -- disable: the chats go back, routes cleared, external sessions keep working
        agent_loop.start_turn(core, worker, "RUN: touch logs.txt", unattended=True,
                              origin="schedule")
        left = (await _wait(lambda: _pending(person, tool_args="touch logs.txt"),
                            what="approval left pending"))[0]
        await person.ok("jarvis.disable", {})
        assert [r["route"] for r in (await person.ok("approval.list", {}))["requests"]
                if r["requestId"] == left["requestId"]] == [None]
        conn = core.gateway.connection(core.gateway.list()[0].id, "c1")
        assert conn is None or not conn.routed
        before = len(chat.sent)
        await chat.push("hello again", channel_id="c1", user_id=PERSON)
        reply = await chat.wait_for_send(TIMEOUT, count=before + 1)
        assert "echo hello again" in "\n".join(m.text for m in chat.sent[before:]), reply
        await person.ok("approval.respond", {"requestId": left["requestId"], "decision": "deny"})
        await _idle(worker)
        sent = await person.ok("external.send", {"ref": ext, "text": "carry on"})
        record = core.external._records[(ext["cli"], ext["id"])]
        await _wait(lambda: any(e["kind"] == "turn.done" and e.get("turnId") == sent["turnId"]
                                for e in record.events), what="external turn")

        # Jarvis back on before the restart: the restart must keep what it decided.
        await person.ok("jarvis.enable", {})
        grants_before = (await person.ok("jarvis.delegation.list", {}))["grants"]
        paused_before = (await person.ok("jarvis.status", {}))["paused"]
        assert [p for p in paused_before if p.get("external") == ext]
        await person.stop()
    finally:
        await daemon.stop()

    # -- restart ----------------------------------------------------------------------
    daemon = await _start(home, brain)
    try:
        person = await connect(http, daemon, approval_mode="ignore")
        status = await person.ok("jarvis.status", {})
        assert status["enabled"] and status["running"]
        assert [p for p in status["paused"] if p.get("external") == ext]
        grants = (await person.ok("jarvis.delegation.list", {}))["grants"]
        assert {g["id"] for g in grants} == {g["id"] for g in grants_before}
        again = await person.ok("jarvis.briefing.get", {})
        assert again["enabled"] and again["jobId"] == on["jobId"]
        assert again["nextRunAt"]
        escalations = (await person.ok("jarvis.escalations.list", {}))["escalations"]
        assert [e for e in escalations if e["reason"] == "stalled"]
        await person.stop()
    finally:
        await daemon.stop()
