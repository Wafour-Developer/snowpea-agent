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
        tmp_path / "home",
        settings={"agents": {"toolRounds": {"default": TINY}, "incompleteRetries": 0}},
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
    assert "delegate only what is left" in report


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
    """Child budgets are independent of the main-agent budget."""
    core = daemon.core
    assert core is not None
    core.settings.agents.toolRounds = None
    core.settings.agent.max_tool_rounds = 20
    session = await core.sessions.create(workdir, mode="auto")
    assert tool_rounds_for(core, session) == 20
    session.is_subagent = True
    assert tool_rounds_for(core, session) == SUBAGENT_TOOL_ROUNDS

    # A larger main allowance does not change a child budget.
    core.settings.agent.max_tool_rounds = 200
    assert tool_rounds_for(core, session) == SUBAGENT_TOOL_ROUNDS


async def test_tool_rounds_survives_a_definition_round_trip() -> None:
    defn = parse_agent_text("---\nname: reader\ntool_rounds: 9\n---\nYou read things.")
    assert defn.tool_rounds == 9
    assert defn.max_tool_rounds == 9
    assert parse_agent_text(render_agent_md(defn)).tool_rounds == 9
    # A definition that says nothing inherits, and writes nothing back.
    plain = parse_agent_text("---\nname: plain\n---\nYou do things.")
    assert plain.tool_rounds is None
    assert plain.max_tool_rounds is None
    assert "tool_rounds" not in render_agent_md(plain)


async def test_max_tool_rounds_definition_round_trip() -> None:
    defn = parse_agent_text("---\nname: reader\nmax_tool_rounds: 9\n---\nYou read things.")
    assert defn.max_tool_rounds == 9
    assert defn.tool_rounds == 9
    rendered = render_agent_md(defn)
    assert "max_tool_rounds: 9" in rendered
    round_tripped = parse_agent_text(rendered)
    assert round_tripped.max_tool_rounds == 9
    assert round_tripped.tool_rounds == 9


async def test_role_defaults_for_subagents(daemon: Daemon, workdir: Path) -> None:
    core = daemon.core
    assert core is not None
    core.settings.agents.toolRounds = None
    core.settings.agents.maxToolRounds = None
    core.settings.agents.maxToolRoundsBy = {}

    session = await core.sessions.create(workdir, mode="auto")
    session.is_subagent = True

    for role in (
        "explore",
        "explorer",
        "reviewer",
        "critic",
        "test-engineer",
        "verifier",
        "architect",
        "executor",
        "unknown-role",
    ):
        session.agent = role
        assert tool_rounds_for(core, session) == SUBAGENT_TOOL_ROUNDS


async def test_max_tool_rounds_settings_precedence(daemon: Daemon, workdir: Path) -> None:
    core = daemon.core
    assert core is not None
    core.settings.agents.toolRounds = None
    session = await core.sessions.create(workdir, mode="auto")
    session.is_subagent = True
    session.agent = "explore"

    # Uniform default child budget
    assert tool_rounds_for(core, session) == SUBAGENT_TOOL_ROUNDS

    # agents.maxToolRounds overrides role default
    core.settings.agents.maxToolRounds = 14
    assert tool_rounds_for(core, session) == 14

    # Definition max_tool_rounds overrides global setting
    session.max_tool_rounds = 5
    assert tool_rounds_for(core, session) == 5

    # agents.maxToolRoundsBy overrides definition
    core.settings.agents.maxToolRoundsBy = {"explore": 6}
    assert tool_rounds_for(core, session) == 6


async def test_subagent_with_max_tool_rounds_stops_at_budget(daemon: Daemon, workdir: Path) -> None:
    core = daemon.core
    assert core is not None
    parent = await core.sessions.create(workdir, mode="auto")
    everything = Recorder()
    core.hub.subscribe(everything, None)

    manager = get_manager(core)
    core.settings.agents.toolRounds = None
    core.settings.agents.maxToolRoundsBy = {"explore": 3}

    result = await manager.run(parent, "endless child that never stops reading", agent="explore")
    assert result.reason == "budget"
    assert result.rounds_used == 3
    assert result.budget == 3


async def test_a_report_is_never_empty() -> None:
    report = render_report(SubagentResult(agent_id="a-1", ok=True, summary=""))
    assert report.strip()
    assert "without a final report" in report


async def test_delegate_task_needs_no_approval_in_accept_mode() -> None:
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


def test_is_incomplete_and_continuation_brief() -> None:
    from snowpea_core.agent.subagent import continuation_brief, is_incomplete

    budgeted = SubagentResult(
        agent_id="a-1",
        ok=True,
        summary="Found src/app.py still unwritten.",
        reason="budget",
        rounds_used=3,
        budget=3,
        last_calls=['read_file {"path": "a.txt"}'],
    )
    assert is_incomplete(budgeted) is True
    brief = continuation_brief("document the package", budgeted)
    assert "reason: budget" in brief
    assert "document the package" in brief
    assert "Found src/app.py" in brief
    assert "read_file" in brief

    assert (
        is_incomplete(SubagentResult(agent_id="a-2", ok=True, summary="done", reason="complete"))
        is False
    )
    assert (
        is_incomplete(
            SubagentResult(
                agent_id="a-3", ok=False, summary="", reason="interrupted", error="interrupted"
            )
        )
        is False
    )
    assert (
        is_incomplete(
            SubagentResult(
                agent_id="a-4",
                ok=False,
                summary="",
                reason="error",
                error="boom",
                rounds_used=2,
                last_calls=['shell {"command": "true"}'],
            )
        )
        is True
    )
    assert (
        is_incomplete(
            SubagentResult(
                agent_id="a-5",
                ok=False,
                summary="",
                reason="error",
                error="delegate_task needs a non-empty task",
            )
        )
        is False
    )
    assert (
        is_incomplete(
            SubagentResult(
                agent_id="a-6",
                ok=False,
                summary="clear failure detail",
                reason="error",
                error="unknown agent",
            )
        )
        is False
    )


async def test_incomplete_budget_uses_the_configured_retry_count(
    daemon: Daemon, workdir: Path
) -> None:
    """Manager re-issues budget stops with continuation briefs up to the configured count."""
    core = daemon.core
    assert core is not None
    core.settings.agents.incompleteRetries = 3
    core.settings.agents.toolRounds = {"default": TINY}
    parent = await core.sessions.create(workdir, mode="auto")
    everything = Recorder()
    core.hub.subscribe(everything, None)

    result = await get_manager(core).run(
        parent, "endless child that never stops reading", title="survey"
    )

    # Four child sessions: first attempt + three smooth continuations.
    spawns = [e for e in everything.events if e["kind"] == "subagent.spawn"]
    assert len(spawns) == 4
    titles = [str(e["payload"].get("title") or "") for e in spawns]
    assert "survey" in titles
    assert any("continue" in title for title in titles)
    # Still unfinished after the continue (fixture never stops reading).
    assert result.reason == "budget"


async def test_budget_continuation_receives_prior_tool_result_checkpoint(
    tmp_path: Path,
) -> None:
    """A retry resumes from the prior child's actual tool findings, not just calls."""
    script = tmp_path / "checkpoint-provider.json"
    script.write_text(
        "{"
        '"steps": ['
        '{"match": "handoff sentinel", "text": "finished from checkpoint"},'
        '{"match": "checkpoint handoff", "text": "reading file", '
        '"tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]}'
        '], "default": {"text": "default"}'
        "}",
        encoding="utf-8",
    )
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{script}"
    instance = await make_daemon(
        tmp_path / "home",
        settings={"agents": {"toolRounds": {"default": 1}, "incompleteRetries": 1}},
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("handoff sentinel found in file\n", encoding="utf-8")
    try:
        core = instance.core
        assert core is not None
        parent = await core.sessions.create(project, mode="auto")
        everything = Recorder()
        core.hub.subscribe(everything, None)

        result = await get_manager(core).run(parent, "checkpoint handoff survey")

        assert result.reason == "complete"
        assert result.summary == "finished from checkpoint"
        spawns = [e for e in everything.events if e["kind"] == "subagent.spawn"]
        assert len(spawns) == 2
        prompts = [
            e["payload"]["text"]
            for e in everything.events
            if e["kind"] == "message.user" and "Prior session checkpoint" in e["payload"]["text"]
        ]
        assert prompts
        assert "Prior session checkpoint" in prompts[-1]
        assert "handoff sentinel found in file" in prompts[-1]
        assert "assistant called read_file" in prompts[-1]
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


def test_timeout_is_a_hard_stop_not_an_automatic_retry() -> None:
    from snowpea_core.agent.subagent import incomplete_retries_for, is_incomplete

    class _Core:
        settings = object()

    assert incomplete_retries_for(_Core()) == 3

    assert (
        is_incomplete(
            SubagentResult(
                agent_id="a-timeout",
                ok=False,
                summary="partial",
                reason="timeout",
                rounds_used=1,
                last_calls=['shell {"command": "sleep 99"}'],
            )
        )
        is False
    )


def test_checkpoint_redacts_sensitive_calls_and_keeps_newest_tail() -> None:
    from snowpea_core.agent.subagent import _conversation_checkpoint
    from snowpea_core.providers.base import ChatMessage, ToolCall
    from snowpea_core.session.history import History

    class _Session:
        def __init__(self) -> None:
            self.history = History()

    session = _Session()
    for idx in range(24):
        session.history.append(ChatMessage(role="tool", name="read_file", content=f"old {idx}"))
    session.history.append(
        ChatMessage(
            role="assistant",
            content="",
            sensitive=True,
            tool_calls=[
                ToolCall(id="secret", name="shell", arguments={"command": "echo SECRET_TOKEN"})
            ],
        )
    )
    session.history.append(ChatMessage(role="tool", name="shell", content="newest result"))

    checkpoint = _conversation_checkpoint(session)  # type: ignore[arg-type]

    assert "newest result" in checkpoint
    assert "[redacted]" in checkpoint
    assert "SECRET_TOKEN" not in checkpoint
    assert "old 0" not in checkpoint


async def test_every_child_attempt_is_reported_to_main_context(
    daemon: Daemon, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.agent.loop import _flush_notices
    from snowpea_core.session.manager import persist_history

    core = daemon.core
    parent = await core.sessions.create(workdir, mode="auto")
    core.settings.agents.incompleteRetries = 1
    manager = get_manager(core)
    results = iter(
        [
            SubagentResult(
                agent_id="failed-child",
                session_id="child-session-1",
                ok=False,
                summary="test failed; api_key=private-child-secret",
                reason="budget",
                status="error",
                checkpoint="tool exec result: expected 2, got 1",
            ),
            SubagentResult(
                agent_id="successful-child",
                session_id="child-session-2",
                ok=True,
                summary="fixed assertion; pytest: 1 passed",
                reason="complete",
            ),
        ]
    )

    async def run_once(*_args, **_kwargs):
        return next(results)

    monkeypatch.setattr(manager, "_run_once", run_once)
    await manager.run(parent, "fix tests")
    await _flush_notices(parent)
    await persist_history(core.store, parent)
    text = "\n".join(str(m.content) for m in parent.history.snapshot())
    assert "failed-child" in text and "successful-child" in text
    assert "expected 2, got 1" in text and "pytest: 1 passed" in text
    assert "child-session-1" in text and "child-session-2" in text
    assert "private-child-secret" not in text
    stored = await core.store.messages(parent.id)
    assert any("failed-child" in str(m["content"]) for m in stored)


async def test_live_child_tool_evidence_reaches_lead_before_child_finishes(
    daemon: Daemon, workdir: Path
) -> None:
    from snowpea_core.agent.loop import _flush_notices
    from snowpea_core.agent.subagent import SubagentRecord, _ChildWatcher
    from snowpea_core.session import events

    core = daemon.core
    parent = await core.sessions.create(workdir, mode="auto")
    record = SubagentRecord(
        agent_id="live-child",
        session_id="live-session",
        name="executor",
        task="fix",
        parent_session_id=parent.id,
    )
    watcher = _ChildWatcher(get_manager(core), record)
    for kind, payload in [
        events.tool_result("c1", "shell", False, "expected 2, got 1"),
        events.tool_result("c2", "shell", True, "pytest: 1 passed; api_key=child-secret"),
        events.tool_result("c3", "host_secret", True, "private-output", meta={"sensitive": True}),
    ]:
        await watcher.notify("session.event", {"kind": kind, "payload": payload})
    assert len(parent.pending_notices) == 3
    await _flush_notices(parent)
    text = str(parent.history.snapshot())
    assert "live-child" in text and "live-session" in text
    assert "expected 2, got 1" in text and "pytest: 1 passed" in text
    assert "child-secret" not in text and "private-output" not in text
    assert "not a completion verdict" in text


async def test_provider_error_report_is_not_a_successful_child(
    daemon: Daemon, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.providers.base import ProviderError

    class FailingProvider:
        vendor = "test"

        async def stream(self, *_args, **_kwargs):
            raise ProviderError("internal", "provider unavailable")
            yield  # pragma: no cover - async generator contract

    core = daemon.core
    core.settings.agents.incompleteRetries = 0
    monkeypatch.setattr(core.providers, "get", lambda *_args: FailingProvider())
    parent = await core.sessions.create(workdir, mode="auto")
    result = await get_manager(core).run(parent, "fix")
    assert not result.ok
    assert result.status == "error" and result.reason == "error"
    assert "provider unavailable" in result.summary
