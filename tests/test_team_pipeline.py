"""M6/M7 §9: ``/team "<task>"`` runs the project roster by role, staged.

The unit half pins the parts that must be true before a model is asked
anything — which roster member owns which stage, that a plan is validated and
its overlapping tasks merged, that dependencies order the run and disjoint
tasks share a wave.  The integration half drives the whole pipeline through
the scripted fake provider, including the bounded review→fix→review loop, and
checks that ``/team <N> …`` still takes the worktree path.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _support import init_repo, make_daemon

from snowpea_core.agent import team_pipeline
from snowpea_core.agent.team_pipeline import (
    EXPLORE,
    IMPLEMENT,
    MAX_REVIEW_ROUNDS,
    PLAN,
    REVIEW,
    TEST,
    VERIFY,
    PipelineError,
    PipelineTask,
    handoff,
    merge_overlapping,
    parse_pipeline_args,
    stage_assignments,
    tasks_from_payload,
    waves,
)
from snowpea_core.commands import team_cmd, workers_cmd
from snowpea_core.commands.registry import CommandContext
from snowpea_core.config.settings import DEFAULT_AGENT_TEAM
from snowpea_core.server.app_server import Daemon

FIXTURES = Path(__file__).parent / "fixtures" / "providers" / "fake"
PIPELINE_SCRIPT = FIXTURES / "team_pipeline.json"
STUBBORN_SCRIPT = FIXTURES / "team_pipeline_stubborn.json"

TIMEOUT = 120.0

ROSTER = ["architect", "executor", "test-engineer", "reviewer"]


# ---------------------------------------------------------------------------
# roster -> stages
# ---------------------------------------------------------------------------


def test_the_default_roster_fills_every_stage() -> None:
    plan = stage_assignments(DEFAULT_AGENT_TEAM)
    assert dict(plan.rows()) == {
        EXPLORE: "explorer",
        PLAN: "architect",
        IMPLEMENT: "executor",
        TEST: "test-engineer",
        VERIFY: "verifier",
        REVIEW: "critic",
    }
    assert plan.unused == ()


def test_each_stage_falls_back_to_its_second_choice() -> None:
    plan = stage_assignments(["planner", "explore", "executor", "verifier"])
    assert dict(plan.rows()) == {
        EXPLORE: "explore",
        PLAN: "planner",
        IMPLEMENT: "executor",
        VERIFY: "verifier",
    }
    assert plan.owner(TEST) is None


def test_optional_stages_are_skipped_when_nobody_fills_them() -> None:
    plan = stage_assignments(["executor"])
    assert plan.rows() == [(IMPLEMENT, "executor")]
    assert plan.owner(PLAN) is None and plan.owner(REVIEW) is None


def test_a_roster_without_an_implementer_is_refused() -> None:
    with pytest.raises(PipelineError) as raised:
        stage_assignments(["architect", "reviewer"])
    assert "no implement agent" in str(raised.value)
    assert "executor" in str(raised.value)
    assert "/team <N>" in str(raised.value)


def test_a_roster_member_that_does_not_resolve_is_ignored() -> None:
    plan = stage_assignments(
        ["architect", "executor", "ghost"], known={"architect", "executor"}
    )
    assert dict(plan.rows()) == {PLAN: "architect", IMPLEMENT: "executor"}
    assert "ghost" not in plan.unused


def test_one_agent_never_owns_two_stages() -> None:
    # `explore` is both the explore candidate and nothing else; `executor`
    # cannot be borrowed for review just because the roster is short.
    plan = stage_assignments(["executor", "critic"])
    assert dict(plan.rows()) == {IMPLEMENT: "executor", REVIEW: "critic"}


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def test_the_plan_is_read_into_file_scoped_tasks() -> None:
    tasks = tasks_from_payload(
        {
            "tasks": [
                {"id": "T1", "title": "one", "brief": "do one", "files": ["./a.py"]},
                {"id": "T2", "title": "two", "brief": "do two", "files": ["b.py"],
                 "dependsOn": ["T1"]},
            ]
        },
        "fallback",
    )
    assert [task.id for task in tasks] == ["T1", "T2"]
    assert tasks[0].files == ("a.py",)
    assert tasks[1].depends_on == ("T1",)


def test_a_forward_or_circular_dependency_is_dropped() -> None:
    tasks = tasks_from_payload(
        {
            "tasks": [
                {"id": "T1", "title": "one", "files": ["a.py"], "dependsOn": ["T2"]},
                {"id": "T2", "title": "two", "files": ["b.py"], "dependsOn": ["T2"]},
            ]
        },
        "fallback",
    )
    assert tasks[0].depends_on == () and tasks[1].depends_on == ()


def test_the_task_list_is_capped_and_never_empty() -> None:
    payload = {"tasks": [{"id": f"T{n}", "title": f"t{n}"} for n in range(1, 21)]}
    assert len(tasks_from_payload(payload, "fallback", max_tasks=3)) == 3
    fallback = tasks_from_payload({"tasks": []}, "do the thing")
    assert [task.title for task in fallback] == ["do the thing"]


def test_tasks_that_claim_the_same_file_are_merged() -> None:
    merged = merge_overlapping(
        [
            PipelineTask("T1", "one", "do one", ("shared.py",)),
            PipelineTask("T2", "two", "do two", ("shared.py", "other.py")),
            PipelineTask("T3", "three", "do three", ("apart.py",), ("T2",)),
        ]
    )
    assert [task.id for task in merged] == ["T1", "T3"]
    assert merged[0].merged == ("T2",)
    assert merged[0].files == ("shared.py", "other.py")
    assert "do two" in merged[0].brief
    # T3 depended on a task that no longer exists on its own.
    assert merged[1].depends_on == ()


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


def test_disjoint_tasks_share_a_wave_and_dependencies_split_them() -> None:
    tasks = [
        PipelineTask("T1", "one", "b", ("a.py",)),
        PipelineTask("T2", "two", "b", ("b.py",)),
        PipelineTask("T3", "three", "b", ("c.py",), ("T1",)),
    ]
    assert [[task.id for task in wave] for wave in waves(tasks, 3)] == [["T1", "T2"], ["T3"]]


def test_the_wave_never_exceeds_the_concurrency_limit() -> None:
    tasks = [PipelineTask(f"T{n}", "t", "b", (f"{n}.py",)) for n in range(1, 5)]
    assert [len(wave) for wave in waves(tasks, 2)] == [2, 2]


def test_a_task_that_claims_no_file_runs_alone() -> None:
    tasks = [
        PipelineTask("T1", "one", "b", ("a.py",)),
        PipelineTask("T2", "two", "b", ()),
        PipelineTask("T3", "three", "b", ("c.py",)),
    ]
    batches = [[task.id for task in wave] for wave in waves(tasks, 4)]
    assert ["T2"] in batches
    assert all(len(wave) == 1 for wave in batches if "T2" in wave)


# ---------------------------------------------------------------------------
# handoffs and small helpers
# ---------------------------------------------------------------------------


def test_a_handoff_is_trimmed_to_twenty_lines() -> None:
    text = handoff("implement", "\n".join(f"line {n}" for n in range(40)))
    assert len(text.splitlines()) <= team_pipeline.HANDOFF_LINES + 2
    assert text.startswith("What implement handed over:")
    assert handoff("implement", "   ") == ""


def test_extract_handoff_fenced_and_fallback() -> None:
    fenced = (
        "Some preamble text\n"
        "```handoff\n"
        "Decided: keep existing architecture\n"
        "Files touched: core/loop.py\n"
        "Findings: all good\n"
        "Remaining: nothing\n"
        "Risks: none\n"
        "```\n"
        "Some trailing text\n"
    )
    extracted = team_pipeline.extract_handoff(fenced)
    assert "Decided: keep existing architecture" in extracted
    assert "Files touched: core/loop.py" in extracted
    assert "Some preamble text" not in extracted
    assert "Some trailing text" not in extracted

    # Fallback to first 20 non-empty lines
    plain = "\n".join(f"line {n}" for n in range(50))
    fallback = team_pipeline.extract_handoff(plain, limit=20)
    lines = fallback.splitlines()
    assert len(lines) == 20
    assert lines[0] == "line 0"
    assert lines[-1] == "line 19"


def test_spill_diff_caps_and_spills(tmp_path: Path) -> None:
    short_diff = "diff --git a/foo.py b/foo.py\n+hello"
    assert team_pipeline.spill_diff(short_diff, home=tmp_path, cap=100) == short_diff

    # Long diff over cap
    long_diff = "diff --git a/foo.py b/foo.py\n" + "\n".join(f"+line {n}" for n in range(500))
    spilled = team_pipeline.spill_diff(long_diff, home=tmp_path, cap=200)
    assert len(spilled) < len(long_diff)
    assert "[… " in spilled
    assert "lines omitted — read_file(" in spilled


def test_stage_table_and_handoff_paths_in_report() -> None:
    plan = team_pipeline.stage_assignments(["executor", "test-engineer"])
    run = team_pipeline.PipelineRun(task="test task", plan=plan, id="tr-12345678")
    run.handoff_paths["explore"] = "/path/to/.snowpea/handoffs/tr-12345678/explore.md"
    run.stages.append(
        team_pipeline.StageResult(
            stage="explore",
            agent="explorer",
            ok=True,
            text="all good",
            rounds=3,
            budget=8,
            input_tokens=100,
            output_tokens=50,
        )
    )
    table = team_pipeline._format_stage_table(run.stages)
    assert "| Stage | Agent | Rounds/Budget | Input Tokens | Output Tokens |" in table
    assert "| explore | explorer | 3/8 | 100 | 50 |" in table

    tp = team_pipeline.TeamPipeline.__new__(team_pipeline.TeamPipeline)
    rep = tp.report(run)
    assert "Stage hand-offs:" in rep
    assert "- explore: /path/to/.snowpea/handoffs/tr-12345678/explore.md" in rep
    assert "| explore | explorer | 3/8 | 100 | 50 |" in rep


def test_the_verdict_is_read_from_the_reviewer_s_own_answer() -> None:
    assert team_pipeline._verdict("VERDICT: APPROVE\nnothing to raise") == "APPROVE"
    assert team_pipeline._verdict("VERDICT: REQUEST_CHANGES\n…") == "REQUEST_CHANGES"
    assert team_pipeline._verdict("VERDICT: REJECT\n…") == "REQUEST_CHANGES"
    assert team_pipeline._verdict("I have opinions") == "NO_VERDICT"


def test_test_verdict_requires_an_explicit_line_and_tool_evidence() -> None:
    with_tools = team_pipeline.SubagentResult(
        "a-1", True, "pytest passed\nTESTS: PASS", rounds_used=1, last_calls=["shell"]
    )
    assert team_pipeline._test_verdict(with_tools, with_tools.summary) == team_pipeline.TESTS_PASS
    assert (
        team_pipeline._test_verdict(with_tools, "pytest passed")
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )
    no_tools = team_pipeline.SubagentResult("a-2", True, "TESTS: PASS")
    assert (
        team_pipeline._test_verdict(no_tools, no_tools.summary)
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )
    denied = team_pipeline.SubagentResult(
        "a-3", True, "approval.resolved denied\nTESTS: PASS", rounds_used=1
    )
    assert (
        team_pipeline._test_verdict(denied, denied.summary)
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )
    failed = team_pipeline.SubagentResult("a-4", False, "TESTS: PASS", error="tool failed")
    assert (
        team_pipeline._test_verdict(failed, failed.summary)
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )
    assert team_pipeline._test_verdict(with_tools, "TESTS: FAIL") == team_pipeline.TESTS_FAIL


def test_review_approval_requires_an_explicit_line_and_tool_evidence() -> None:
    approved = team_pipeline.SubagentResult(
        "a-1", True, "VERDICT: APPROVE\nchecked a.py", rounds_used=1, last_calls=["read_file"]
    )
    assert team_pipeline._review_verdict(approved, approved.summary) == team_pipeline.APPROVE
    no_tools = team_pipeline.SubagentResult("a-2", True, "VERDICT: APPROVE\nseems fine")
    assert (
        team_pipeline._review_verdict(no_tools, no_tools.summary)
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )
    empty = team_pipeline.SubagentResult("a-3", True, "", rounds_used=1)
    assert (
        team_pipeline._review_verdict(empty, empty.summary)
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )
    assert team_pipeline._review_verdict(approved, "VERDICT: NEEDS_MORE_EVIDENCE") == (
        team_pipeline.NEEDS_MORE_EVIDENCE
    )


def test_budget_exhaustion_keeps_an_explicit_pass_when_there_is_evidence() -> None:
    budgeted = team_pipeline.SubagentResult(
        "a-1",
        True,
        "ran pytest\nTESTS: PASS",
        reason="budget",
        rounds_used=10,
        budget=10,
        last_calls=["shell"],
    )
    assert team_pipeline._test_verdict(budgeted, budgeted.summary) == (
        "TESTS: PASS (budget exhausted: 10/10 rounds)"
    )

    verify = team_pipeline.SubagentResult(
        "a-2",
        True,
        "rechecked\nVERIFY: PASS",
        reason="budget",
        rounds_used=14,
        budget=14,
        last_calls=["shell"],
    )
    assert team_pipeline._verify_verdict(verify, verify.summary) == (
        "VERIFY: PASS (budget exhausted: 14/14 rounds)"
    )

    review = team_pipeline.SubagentResult(
        "a-3",
        True,
        "VERDICT: APPROVE\nlooked at the diff",
        reason="budget",
        rounds_used=16,
        budget=16,
        last_calls=["read_file"],
    )
    assert team_pipeline._review_verdict(review, review.summary) == (
        "APPROVE (budget exhausted: 16/16 rounds)"
    )


def test_budget_exhaustion_without_a_verdict_marker_needs_more_evidence() -> None:
    budgeted = team_pipeline.SubagentResult(
        "a-1",
        True,
        "tests look good",
        reason="budget",
        rounds_used=10,
        budget=10,
        last_calls=["shell"],
    )
    assert (
        team_pipeline._test_verdict(budgeted, budgeted.summary)
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )
    assert (
        team_pipeline._verify_verdict(budgeted, budgeted.summary)
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )
    assert (
        team_pipeline._review_verdict(budgeted, budgeted.summary)
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )


def test_budget_exhaustion_without_tool_evidence_needs_more_evidence() -> None:
    budgeted = team_pipeline.SubagentResult(
        "a-1",
        True,
        "VERDICT: APPROVE\nTESTS: PASS\nVERIFY: PASS",
        reason="budget",
        rounds_used=10,
        budget=10,
    )
    assert (
        team_pipeline._test_verdict(budgeted, budgeted.summary)
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )
    assert (
        team_pipeline._verify_verdict(budgeted, budgeted.summary)
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )
    assert (
        team_pipeline._review_verdict(budgeted, budgeted.summary)
        == team_pipeline.NEEDS_MORE_EVIDENCE
    )


def test_findings_are_routed_to_the_task_that_owns_the_files() -> None:
    run = team_pipeline.PipelineRun(
        task="t",
        plan=stage_assignments(["executor"]),
        tasks=[
            PipelineTask("T1", "one", "b", ("a.py",)),
            PipelineTask("T2", "two", "b", ("b.py",)),
        ],
    )
    assert team_pipeline._task_for(run, "b.py:3 — wrong").id == "T2"
    assert team_pipeline._task_for(run, "no path here").id == "T1"


def test_the_task_is_parsed_out_of_both_spellings() -> None:
    assert parse_pipeline_args('"add docstrings"') == "add docstrings"
    assert parse_pipeline_args('run "add docstrings"') == "add docstrings"
    assert parse_pipeline_args("add docstrings") == "add docstrings"
    assert parse_pipeline_args("   ") == ""


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A git project whose ``.snowpea`` names a four-role team."""
    root = init_repo(
        tmp_path / "project",
        {
            "module_a.py": 'def a() -> str:\n    return "a"\n',
            "module_b.py": 'def b() -> str:\n    return "b"\n',
        },
    )
    settings = root / ".snowpea"
    settings.mkdir(exist_ok=True)
    (settings / "settings.json").write_text(
        json.dumps({"agents": {"teams": {"delivery": ROSTER}, "activeTeam": "delivery"}}),
        encoding="utf-8",
    )
    return root


class Said:
    """Collects what the command answered, in order."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    async def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


async def _daemon(home: Path, script: Path) -> Daemon:
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{script}"
    return await make_daemon(home)


@pytest_asyncio.fixture
async def daemon(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Daemon]:
    script = getattr(request, "param", PIPELINE_SCRIPT)
    previous = os.environ.get("SNOWPEA_PROVIDER")
    instance = await _daemon(tmp_path / "home", script)
    try:
        yield instance
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


async def _run(daemon: Daemon, project: Path, task: str) -> tuple[str, Said]:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    said = Said()
    report = await asyncio.wait_for(
        team_pipeline.run_pipeline(core, session, task, said), TIMEOUT
    )
    return report, said


async def test_the_pipeline_plans_implements_tests_and_reviews(
    daemon: Daemon, project: Path
) -> None:
    report, said = await _run(daemon, project, "document both modules")

    # The roster is announced with its stage owners before anything runs.
    assert "plan=architect" in said.text
    assert "implement=executor" in said.text
    assert "review=reviewer" in said.text

    # Both file-scoped tasks really ran: the files on disk changed.
    assert (project / "module_a.py").read_text(encoding="utf-8").startswith('"""Module A.')
    assert (project / "module_b.py").read_text(encoding="utf-8").startswith('"""Module B.')

    # The lead's report, not a child's transcript.
    assert "tasks: 2/2 finished" in report
    assert "tests: TESTS: PASS" in report
    assert "verify: not run" in report
    assert "review: APPROVE" in report
    assert "verify stage skipped: no verifier on the roster" in report
    assert "Nothing was left unfinished." in report
    assert "T1" in report and "T2" in report
    assert "Stage hand-offs:" in report
    assert "Stage telemetry:" in report
    handoff_files = list((project / ".snowpea" / "handoffs").glob("*/*.md"))
    assert handoff_files


async def test_every_stage_is_a_subagent_the_surface_can_render(
    daemon: Daemon, project: Path
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    await asyncio.wait_for(
        team_pipeline.run_pipeline(core, session, "document both modules", Said()), TIMEOUT
    )
    from snowpea_core.agent.subagent import get_manager

    records = [record for record in get_manager(core).records()
               if record.parent_session_id == session.id]
    titles = [record.title for record in records]
    names = {record.name for record in records}
    assert PLAN in titles
    assert any(title.startswith("implement: ") for title in titles)
    assert TEST in titles and REVIEW in titles and team_pipeline.FIX in titles
    assert {"architect", "executor", "test-engineer", "reviewer"} <= names


async def test_request_changes_buys_one_fix_and_one_more_review(
    daemon: Daemon, project: Path
) -> None:
    report, _ = await _run(daemon, project, "document both modules")
    from snowpea_core.agent.subagent import get_manager

    titles = [record.title for record in get_manager(daemon.core).records()]
    assert titles.count(team_pipeline.FIX) == 1
    assert titles.count(REVIEW) == 1 and f"{REVIEW} (2)" in titles
    assert "review: APPROVE" in report


@pytest.mark.parametrize("daemon", [STUBBORN_SCRIPT], indirect=True)
async def test_a_reviewer_that_never_approves_ends_the_run_as_unfinished(
    daemon: Daemon, project: Path
) -> None:
    report, _ = await _run(daemon, project, "document module a")
    from snowpea_core.agent.subagent import get_manager

    titles = [record.title for record in get_manager(daemon.core).records()]
    assert len([t for t in titles if t.startswith(REVIEW)]) == MAX_REVIEW_ROUNDS
    assert titles.count(team_pipeline.FIX) == 1
    assert "review: REQUEST_CHANGES" in report
    assert "Left unfinished:" in report
    assert "after 2 rounds" in report


async def test_the_review_stage_is_off_when_the_setting_says_so(
    daemon: Daemon, project: Path
) -> None:
    core = daemon.core
    assert core is not None
    core.settings.team.pipeline.review = False
    core.settings.team.pipeline.test = False
    report, _ = await _run(daemon, project, "document both modules")
    assert "review: not run" in report
    assert "tests: not run" in report


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


async def test_a_leading_number_forwards_to_worker_mode_with_a_note(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/team <N>`` is the old spelling, and it still runs.

    Refusing it left someone who typed it with nothing done; forwarding it
    silently left them never learning the new name.  So it runs the worker
    path and says, once, what the current spelling is.
    """
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    seen: list[tuple[str, Any]] = []
    said = Said()

    async def fake_pipeline(*args: Any, **kwargs: Any) -> str:
        seen.append(("pipeline", args[2]))
        return "pipeline report"

    monkeypatch.setattr(team_cmd, "run_pipeline", fake_pipeline)
    ctx = CommandContext(core=core, session=session, turn_id="turn-1")
    ctx.say = said  # type: ignore[method-assign]

    await team_cmd.cmd_team(ctx, '3 "add docstrings"')
    assert seen == [], "the pipeline is not what /team N means"
    assert said.text.startswith(f"{team_cmd.WORKERS_COMPAT_NOTE}\n")
    assert "`/workers N` is the current spelling" in said.text
    assert "3 worktrees" in said.text, "the worker run itself happened"

    # The pipeline spellings still work, and still reach the pipeline.
    await team_cmd.cmd_team(ctx, '"add docstrings"')
    await team_cmd.cmd_team(ctx, 'run "add docstrings"')
    assert seen == [("pipeline", "add docstrings"), ("pipeline", "add docstrings")]


async def test_workers_runs_the_worktree_path(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    seen: list[tuple[int, str]] = []

    class FakeManager:
        async def start(self, _session: Any, workers: int, task: str) -> str:
            seen.append((workers, task))
            return "tm-test"

        async def status(self, _team_id: str | None = None) -> Any:
            class Status:
                tasks: list[Any] = []

            return Status()

        async def wait(self, _team_id: str) -> None:
            return None

    monkeypatch.setattr(workers_cmd, "get_manager_for", lambda _core: FakeManager())
    ctx = CommandContext(core=core, session=session, turn_id="turn-1")
    ctx.say = Said()  # type: ignore[method-assign]

    await workers_cmd.cmd_workers(ctx, '3 "add docstrings"')
    assert seen == [(3, "add docstrings")]

    # `/worker` is the same command under its other name.
    await workers_cmd.cmd_workers(ctx, '2 "again"')
    assert seen[-1] == (2, "again")


async def test_workers_with_no_task_is_a_usage_error(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    started: list[Any] = []
    monkeypatch.setattr(
        workers_cmd,
        "get_manager_for",
        lambda _core: started.append(True),  # type: ignore[arg-type,return-value]
    )
    said = Said()
    ctx = CommandContext(core=core, session=session, turn_id="turn-1")
    ctx.say = said  # type: ignore[method-assign]

    await workers_cmd.cmd_workers(ctx, "3")
    assert started == []
    assert workers_cmd.USAGE in said.text


def test_both_names_are_registered_and_team_no_longer_takes_a_count() -> None:
    names = {command.name for command in workers_cmd.COMMANDS}
    assert names == {"workers", "worker"}
    assert "<N>" not in team_cmd.USAGE
    assert "/workers" in workers_cmd.USAGE


async def test_team_list_names_the_stage_each_member_fills(
    daemon: Daemon, project: Path
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    said = Said()
    ctx = CommandContext(core=core, session=session, turn_id="turn-1")
    ctx.say = said  # type: ignore[method-assign]
    await team_cmd.cmd_team(ctx, "list")
    assert "Pipeline stages for the active roster:" in said.text
    assert "plan: architect" in said.text
    assert "implement: executor" in said.text
    assert "review: reviewer" in said.text


# ---------------------------------------------------------------------------
# named teams, `/team use none`, and what agent.list offers a picker
# ---------------------------------------------------------------------------


GLOBAL_TEAM = {
    "agents": {
        "teams": {"external": ["explorer", "executor", "verifier"]},
        "default_team": "external",
    }
}


@pytest_asyncio.fixture
async def daemon_with_global_team(tmp_path: Path) -> AsyncIterator[Daemon]:
    """A daemon whose global settings define a team the project has not adopted."""
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{PIPELINE_SCRIPT}"
    instance = await make_daemon(tmp_path / "home", GLOBAL_TEAM)
    try:
        yield instance
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


async def test_a_named_global_team_runs_once_without_being_adopted(
    daemon_with_global_team: Daemon, project: Path
) -> None:
    from snowpea_core.agent.subagent import get_manager
    from snowpea_core.config.project import ProjectSettings

    core = daemon_with_global_team.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    said = Said()
    ctx = CommandContext(core=core, session=session, turn_id="turn-1")
    ctx.say = said  # type: ignore[method-assign]

    await asyncio.wait_for(team_cmd.cmd_team(ctx, 'external "document both modules"'), TIMEOUT)

    # It really ran on the global team's roster, not on the project's own.
    assert "roster 'external'" in said.text
    assert "explore=explorer" in said.text
    assert "implement=executor" in said.text
    assert "verify=verifier" in said.text
    names = {record.name for record in get_manager(core).records()}
    assert "explorer" in names and "architect" not in names

    # And the project's active team is exactly what it was.
    assert ProjectSettings.load(project).agents.activeTeam == "delivery"
    assert session.team == "delivery"


async def test_an_unknown_team_name_lists_the_known_ones(
    daemon_with_global_team: Daemon, project: Path
) -> None:
    core = daemon_with_global_team.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    said = Said()
    ctx = CommandContext(core=core, session=session, turn_id="turn-1")
    ctx.say = said  # type: ignore[method-assign]

    await team_cmd.cmd_team(ctx, 'ghost "document both modules"')
    assert "unknown team 'ghost'" in said.text
    assert "delivery" in said.text and "external" in said.text


async def test_an_unquoted_task_is_never_read_as_a_team_name(
    daemon_with_global_team: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon_with_global_team.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    seen: list[Any] = []

    async def fake_pipeline(*args: Any, **kwargs: Any) -> str:
        seen.append((args[2], args[4] if len(args) > 4 else None))
        return "report"

    monkeypatch.setattr(team_cmd, "run_pipeline", fake_pipeline)
    ctx = CommandContext(core=core, session=session, turn_id="turn-1")
    await team_cmd.cmd_team(ctx, "add docstrings to the parser")
    assert seen == [("add docstrings to the parser", None)]


async def test_use_none_clears_the_active_team(
    daemon_with_global_team: Daemon, project: Path
) -> None:
    from snowpea_core.config.project import ProjectSettings

    core = daemon_with_global_team.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    said = Said()
    ctx = CommandContext(core=core, session=session, turn_id="turn-1")
    ctx.say = said  # type: ignore[method-assign]

    await team_cmd.cmd_team(ctx, "use none")
    assert ProjectSettings.load(project).agents.activeTeam is None
    assert session.team is None and session.team_agents == ()
    assert "cleared" in said.text.lower()

    # The team itself is still there to be picked again.
    await team_cmd.cmd_team(ctx, "use delivery")
    assert ProjectSettings.load(project).agents.activeTeam == "delivery"


async def test_team_list_shows_every_team_with_its_source_and_stages(
    daemon_with_global_team: Daemon, project: Path
) -> None:
    core = daemon_with_global_team.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    said = Said()
    ctx = CommandContext(core=core, session=session, turn_id="turn-1")
    ctx.say = said  # type: ignore[method-assign]

    await team_cmd.cmd_team(ctx, "list")
    assert "delivery [project]" in said.text
    assert "external [global]" in said.text
    assert "* delivery" in said.text
    assert "plan: architect" in said.text
    assert "explore: explorer" in said.text


async def test_agent_list_offers_every_team_with_its_stages(
    daemon_with_global_team: Daemon, project: Path
) -> None:
    from _support import OriginConn

    from snowpea_core.server.agent_handlers import agent_list_handler

    core = daemon_with_global_team.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    listing = await agent_list_handler(OriginConn(session), None, core)  # type: ignore[arg-type]
    teams = {row.name: row for row in listing.agents if row.kind == "team"}

    assert set(teams) == {"delivery", "external"}
    assert teams["delivery"].active is True
    assert teams["delivery"].source == "project"
    assert teams["delivery"].agents == ROSTER
    assert teams["delivery"].stages == {
        PLAN: "architect",
        IMPLEMENT: "executor",
        TEST: "test-engineer",
        REVIEW: "reviewer",
    }
    assert teams["external"].active is False
    assert teams["external"].source == "global"
    assert teams["external"].stages == {
        EXPLORE: "explorer",
        IMPLEMENT: "executor",
        VERIFY: "verifier",
    }
    # The active team is the first row, as it was before this became a list.
    assert listing.agents[0].name == "delivery"


async def test_a_team_that_cannot_run_the_pipeline_is_listed_with_no_stages(
    daemon: Daemon, project: Path
) -> None:
    from _support import OriginConn

    from snowpea_core.server.agent_handlers import agent_list_handler

    core = daemon.core
    assert core is not None
    core.settings.agents.teams["readers"] = ["explore", "reviewer"]
    session = await core.sessions.create(project, mode="auto")
    listing = await agent_list_handler(OriginConn(session), None, core)  # type: ignore[arg-type]
    readers = next(row for row in listing.agents if row.name == "readers")
    assert readers.kind == "team"
    assert readers.agents == ["explore", "reviewer"]
    assert readers.stages == {}
