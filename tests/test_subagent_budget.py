"""A child that runs out of tool rounds still reports (CORE-subagent-budget).

The defect these tests describe was seen in the desktop app: a delegated child
made fifty tool calls, the loop ended its turn with an ``error`` event and no
``message.done`` at all, and the parent's ``delegate_task`` came back with an
empty summary — so the parent delegated the very same task again.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from _support import Recorder, make_daemon

from snowpea_core.agent.definition import parse_agent_text, render_agent_md
from snowpea_core.agent.loop import SUBAGENT_TOOL_ROUNDS, tool_rounds_for
from snowpea_core.agent.subagent import (
    BUDGET_LINE,
    SubagentRecord,
    SubagentResult,
    _ChildWatcher,
    _last_assistant_text,
    get_manager,
)
from snowpea_core.server.app_server import Daemon
from snowpea_core.tools.delegate import render_report

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "subagent_budget.json"

#: A budget small enough that the child cannot possibly finish inside it.
TINY = 3


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    instance = await make_daemon(
        tmp_path / "home", settings={"agents": {"toolRounds": {"default": TINY}}}
    )
    try:
        yield instance
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "a.txt").write_text("something to read\n", encoding="utf-8")
    return project


async def test_a_child_out_of_rounds_reports_and_the_parent_is_told(
    daemon: Daemon, workdir: Path
) -> None:
    core = daemon.core
    assert core is not None
    parent = await core.sessions.create(workdir, mode="auto")
    everything = Recorder()
    core.hub.subscribe(everything, None)

    result = await get_manager(core).run(parent, "endless child that never stops reading")

    # The child said something before it ended, and that something is the report.
    assert result.reason == "budget"
    assert "still unwritten" in result.summary
    assert result.rounds_used == TINY
    assert [call.split(" ", 1)[0] for call in result.last_calls] == ["read_file"] * 3

    # On the child's own session: a real message.done, then turn.done budget.
    child_events = [e for e in everything.events if e["sessionId"] == result.session_id]
    said = [e["payload"]["text"] for e in child_events if e["kind"] == "message.done"]
    assert said and "still unwritten" in said[-1]
    done = [e["payload"]["reason"] for e in child_events if e["kind"] == "turn.done"]
    assert done == ["budget"]
    assert [e for e in child_events if e["kind"] == "error"] == []

    # And what the parent model actually reads: never empty, and explicit
    # about why the task is unfinished.
    report = render_report(result)
    assert "reason: budget" in report
    assert f"roundsUsed: {TINY}" in report
    assert "still unwritten" in report
    assert "read_file" in report
    assert "do not simply" in report


async def test_the_child_is_told_its_budget(daemon: Daemon, workdir: Path) -> None:
    """The brief carries the number, so the child can spend it deliberately."""
    core = daemon.core
    assert core is not None
    parent = await core.sessions.create(workdir, mode="auto")
    everything = Recorder()
    core.hub.subscribe(everything, None)

    result = await get_manager(core).run(parent, "endless child that never stops reading")

    prompts = [
        e["payload"]["text"]
        for e in everything.events
        if e["sessionId"] == result.session_id and e["kind"] == "message.user"
    ]
    assert prompts and BUDGET_LINE.format(n=TINY) in prompts[0]


async def test_the_budget_comes_from_settings_then_the_definition(
    daemon: Daemon, workdir: Path
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(workdir, mode="auto")

    # The mapping's "default" key applies to every agent...
    assert tool_rounds_for(core, session) == TINY
    # ...a per-agent entry outranks it...
    core.settings.agents.toolRounds = {"default": TINY, "executor": 11}
    session.agent = "executor"
    assert tool_rounds_for(core, session) == 11
    # ...and an agent definition's own ``tool_rounds:`` outranks both.
    session.tool_rounds = 17
    assert tool_rounds_for(core, session) == 17


async def test_a_child_gets_more_rounds_than_the_session_default(
    daemon: Daemon, workdir: Path
) -> None:
    """Nothing configured: a worker's floor is higher, because it reads more."""
    core = daemon.core
    assert core is not None
    core.settings.agents.toolRounds = None
    core.settings.agent.max_tool_rounds = 20
    session = await core.sessions.create(workdir, mode="auto")
    assert tool_rounds_for(core, session) == 20
    session.is_subagent = True
    assert tool_rounds_for(core, session) == SUBAGENT_TOOL_ROUNDS


async def test_tool_rounds_survives_a_definition_round_trip() -> None:
    defn = parse_agent_text("---\nname: reader\ntool_rounds: 9\n---\nYou read things.")
    assert defn.tool_rounds == 9
    assert parse_agent_text(render_agent_md(defn)).tool_rounds == 9
    # A definition that says nothing inherits, and writes nothing back.
    plain = parse_agent_text("---\nname: plain\n---\nYou do things.")
    assert plain.tool_rounds is None
    assert "tool_rounds" not in render_agent_md(plain)


async def test_a_report_is_never_empty() -> None:
    report = render_report(SubagentResult(agent_id="a-1", ok=True, summary=""))
    assert report.strip()
    assert "without a final report" in report


def test_delegate_task_needs_no_approval_in_accept_mode() -> None:
    """Delegating is not an exec: the child inherits the mode and its own calls ask."""
    from snowpea_core.permissions.policy import PermissionPolicy

    policy = PermissionPolicy()
    assert policy.decide("accept", "delegate") == "allow"
    assert policy.decide("plan", "delegate") == "allow"
    assert policy.decide("auto", "delegate") == "allow"


# ---------------------------------------------------------------------------
# the summary is the final answer, never the prose before a tool call
# ---------------------------------------------------------------------------


async def test_prose_written_before_a_tool_call_is_not_the_summary() -> None:
    """What made the parent read "That grep swept node_modules…" as a report."""
    from snowpea_core.providers.base import ChatMessage, ToolCall
    from snowpea_core.session.history import History

    class _Session:
        def __init__(self, messages: list[ChatMessage]) -> None:
            self.history = History()
            for message in messages:
                self.history.append(message)

    reaching = ChatMessage(
        role="assistant",
        content="Now let me verify the toolchain claims and run the suite.",
        tool_calls=[ToolCall(id="c1", name="shell", arguments={"command": "pytest"})],
    )
    # Cut off mid-work: the last thing it said was on its way to a tool.
    assert _last_assistant_text(_Session([reaching])) == ""  # type: ignore[arg-type]
    # A real answer after the tool round is the summary.
    answered = ChatMessage(role="assistant", content="Done: 3 tests fixed in loop.py.")
    assert (
        _last_assistant_text(_Session([reaching, answered]))  # type: ignore[arg-type]
        == "Done: 3 tests fixed in loop.py."
    )


async def test_a_tool_call_drops_an_earlier_message_done() -> None:
    """A surface that publishes intermediate prose cannot poison the summary."""
    class _Manager:
        async def emit_update(self, record: SubagentRecord, *, last_text: str = "") -> None:
            return None

    record = SubagentRecord(agent_id="a-1", name="", task="t", parent_session_id="s-1")
    watcher = _ChildWatcher(_Manager(), record)  # type: ignore[arg-type]

    async def event(kind: str, payload: dict[str, object]) -> None:
        await watcher.notify("session.event", {"kind": kind, "payload": payload})

    await event("message.done", {"text": "That grep swept node_modules…"})
    assert record.summary == "That grep swept node_modules…"
    await event("tool.call", {"name": "grep", "args": {"pattern": "x"}})
    assert record.summary == ""


async def test_a_report_without_a_final_answer_says_so() -> None:
    result = SubagentResult(
        agent_id="a-1",
        ok=False,
        summary="",
        status="error",
        reason="error",
        rounds_used=7,
        last_calls=['shell {"command": "pytest"}'],
    )
    report = render_report(result)
    assert "without a final report" in report
    assert "roundsUsed: 7" in report
    assert "pytest" in report
