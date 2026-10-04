"""M7 US-019 / AC-04: ``/ralph`` drives a real repository to a reviewed finish.

The provider is the deterministic scripted fake, so this runs in CI with no API
key.  The assertions are the acceptance criteria verbatim: a non-empty
``git diff`` at the end, two subagents observed running at the same time through
``agent.list``, ``turn.done{reason:"complete"}``, and every story in
``prd.json`` marked as passing.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from _support import OriginConn, Recorder, git

from snowpea_core.agent.subagent import RUNNING, SUBAGENT_KIND
from snowpea_core.commands import ralph
from snowpea_core.server.app_server import Daemon
from snowpea_core.session.session import Session

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "ralph.json"
TIMEOUT = 60.0

TASK = "add a failing test then make it pass"

#: The nine commands AC-03 requires ``/help`` to list.
REQUIRED_COMMANDS = (
    "ralph",
    "ralplan",
    "ultrawork",
    "deepinit",
    "deep-research",
    "deep-interview",
    "plan",
    "accept",
    "auto",
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with two tracked files for ralph to change."""
    project = tmp_path / "repo"
    project.mkdir()
    git(project, "init", "-q")
    git(project, "config", "user.email", "test@example.com")
    git(project, "config", "user.name", "snowpea test")
    (project / "tracked_a.txt").write_text("original a\n", encoding="utf-8")
    (project / "tracked_b.txt").write_text("original b\n", encoding="utf-8")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "initial")
    return project


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    instance = Daemon(port=0, home=home)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


# ---------------------------------------------------------------------------
# the pure parts of the loop
# ---------------------------------------------------------------------------


def test_stories_are_read_out_of_the_generator_json() -> None:
    payload = {
        "stories": [
            {"id": "S1", "title": "one", "verify": "make test", "independent": True},
            {"title": "two", "depends_on": ["S1"], "independent": False},
            {"not": "a story"},
        ]
    }
    stories = ralph.stories_from_payload(payload)
    assert [story.id for story in stories] == ["S1", "S2"]
    assert stories[0].verify == ["make test"]
    assert stories[1].depends_on == ["S1"]
    assert stories[1].independent is False


def test_ready_stories_batches_independent_work_and_respects_dependencies() -> None:
    a = ralph.Story(id="S1", title="a")
    b = ralph.Story(id="S2", title="b")
    c = ralph.Story(id="S3", title="c", depends_on=["S1"])
    assert [s.id for s in ralph.ready_stories([a, b, c], 3)] == ["S1", "S2"]
    assert [s.id for s in ralph.ready_stories([a, b, c], 1)] == ["S1"]

    a.passed = True
    assert [s.id for s in ralph.ready_stories([a, b, c], 3)] == ["S2", "S3"]

    a.passed = b.passed = c.passed = True
    assert ralph.ready_stories([a, b, c], 3) == []


def test_a_dependent_story_runs_alone() -> None:
    serial = ralph.Story(id="S1", title="serial", independent=False)
    other = ralph.Story(id="S2", title="other")
    assert [s.id for s in ralph.ready_stories([serial, other], 3)] == ["S1"]


# ---------------------------------------------------------------------------
# /help (AC-03)
# ---------------------------------------------------------------------------


async def test_help_lists_the_nine_commands(daemon: Daemon, repo: Path) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    await asyncio.wait_for(core.commands.run(core, session, "help", ""), timeout=TIMEOUT)

    listed = recorder.texts()
    missing = [name for name in REQUIRED_COMMANDS if f"/{name} " not in listed]
    assert not missing, f"/help is missing {missing}\n{listed}"


# ---------------------------------------------------------------------------
# the end-to-end run (AC-04)
# ---------------------------------------------------------------------------


async def test_ralph_runs_the_repo_to_an_approved_finish(daemon: Daemon, repo: Path) -> None:
    from snowpea_core.server.agent_handlers import agent_list_handler

    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto", max_concurrent=3)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    turn_id = core.commands.start(core, session, "ralph", f'"{TASK}"')
    task = session.turn_task
    assert task is not None

    # Poll agent.list the way `snowpea agents --json` does, and remember the
    # widest simultaneous running count we ever saw.
    peak = 0
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while not task.done() and asyncio.get_running_loop().time() < deadline:
        listing = await agent_list_handler(OriginConn(session), None, core)  # type: ignore[arg-type]
        running = [
            row for row in listing.agents if row.kind == SUBAGENT_KIND and row.status == RUNNING
        ]
        peak = max(peak, len(running))
        await asyncio.sleep(0.01)
    await asyncio.wait_for(task, timeout=TIMEOUT)

    # 1. The turn finished cleanly, and only because the reviewer approved.
    done = [
        event["payload"]
        for event in recorder.of_kind("turn.done")
        if event["payload"]["turnId"] == turn_id
    ]
    assert done, f"no turn.done for {turn_id}; saw {recorder.kinds()}"
    assert done[-1]["reason"] == "complete", recorder.texts()

    # 2. The working tree really changed.
    diff = git(repo, "diff", "--stat")
    assert diff.strip(), "ralph finished without touching the repository"
    assert (repo / "tracked_a.txt").read_text(encoding="utf-8").startswith("patched")
    assert (repo / "tracked_b.txt").read_text(encoding="utf-8").startswith("patched")

    # 3. Two subagents were running at the same time.
    assert peak >= 2, f"expected two concurrent subagents, saw at most {peak}"

    # 4. The PRD records every story as passing.
    prd = json.loads((repo / ".snowpea" / "ralph" / "prd.json").read_text(encoding="utf-8"))
    assert prd["task"] == TASK
    assert prd["allPassed"] is True
    assert len(prd["stories"]) == 2
    assert all(story["passed"] for story in prd["stories"]), prd["stories"]

    # 5. A human-readable trail was left behind.
    progress = (repo / ".snowpea" / "ralph" / "progress.md").read_text(encoding="utf-8")
    assert "S1" in progress and "S2" in progress
    assert "APPROVED" in progress

    # 7. The next ordinary turn sees a concise command report in main history.
    assistant_messages = [m.content for m in session.history.messages if m.role == "assistant"]
    assert any(
        "Ralph workflow finished with outcome: complete" in text
        for text in assistant_messages
    )
    assert any("Patch tracked_a.txt" in text for text in assistant_messages)


async def test_ralph_command_report_survives_store_resume(daemon: Daemon, repo: Path) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto", max_concurrent=3)

    await asyncio.wait_for(
        core.commands.run(core, session, "ralph", f'"{TASK}"'), timeout=TIMEOUT
    )
    stored = await core.store.messages(session.id)
    assert any(
        item["role"] == "assistant"
        and "Ralph workflow finished with outcome: complete" in item["content"].get("content", "")
        for item in stored
    )

    session_id = session.id
    await core.sessions.close(session_id)
    restored = await core.sessions.restore(session_id)
    assert restored is not None
    restored_text = "\n".join(str(m.content) for m in restored.history.messages)
    assert "Ralph workflow finished with outcome: complete" in restored_text
    assert "State files: .snowpea/ralph/prd.json" in restored_text


async def test_rejected_review_is_repaired_before_completion(
    daemon: Daemon, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.agent.subagent import get_manager

    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto", max_concurrent=3)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    reviews = iter([
        (False, "REJECT — mention the verification evidence in the implementation report."),
        (True, "APPROVE — reviewer feedback was addressed."),
    ])
    seen_tasks: list[str] = []
    manager = get_manager(core)
    original_run = manager.run

    async def reject_once(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        return next(reviews)

    async def spy_run(parent: Any, task: str, **kwargs: Any) -> Any:
        seen_tasks.append(task)
        return await original_run(parent, task, **kwargs)

    monkeypatch.setattr(ralph, "review", reject_once)
    monkeypatch.setattr(manager, "run", spy_run)
    turn_id = await asyncio.wait_for(
        core.commands.run(core, session, "ralph", f'"{TASK}"'), timeout=TIMEOUT
    )

    reasons = [
        event["payload"]["reason"]
        for event in recorder.of_kind("turn.done")
        if event["payload"]["turnId"] == turn_id
    ]
    assert reasons == ["complete"], recorder.texts()
    assert "reviewer requested changes" in recorder.texts()
    assert any("Reviewer feedback to address" in task for task in seen_tasks)
    progress = (repo / ".snowpea" / "ralph" / "progress.md").read_text(encoding="utf-8")
    assert "REJECTED" in progress and "APPROVED" in progress


async def test_ralph_without_a_task_explains_itself(daemon: Daemon, repo: Path) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    await asyncio.wait_for(core.commands.run(core, session, "ralph", ""), timeout=TIMEOUT)
    assert ralph.USAGE in recorder.texts()


async def test_a_rejected_review_does_not_complete_the_turn(
    daemon: Daemon, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``turn.done{reason:"complete"}`` is earned by APPROVE and nothing else."""
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    async def reject(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        return False, "REJECT — the tests do not actually run."

    monkeypatch.setattr(ralph, "review", reject)
    turn_id = await asyncio.wait_for(
        core.commands.run(core, session, "ralph", f'"{TASK}"'), timeout=TIMEOUT
    )

    reasons = [
        event["payload"]["reason"]
        for event in recorder.of_kind("turn.done")
        if event["payload"]["turnId"] == turn_id
    ]
    assert reasons == ["error"], recorder.texts()
    assert "did not approve" in recorder.texts()


def test_deterministic_provider_errors_are_recognised() -> None:
    assert ralph.deterministic_error("HTTP 400: unknown parameter chat_template_kwargs")
    assert ralph.deterministic_error("Error code: 401 - invalid api key")
    assert not ralph.deterministic_error("HTTP 429 too many requests")
    assert not ralph.deterministic_error("rate limit reached, retry later")
    assert not ralph.deterministic_error("HTTP 503 service unavailable")
    assert not ralph.deterministic_error("the tests still fail")


@pytest.mark.parametrize(
    ("text", "approved"),
    [
        ("APPROVE — looks good", True),
        ("APPROVED", True),
        ("**APPROVE** — looks good", True),
        ("Intro line\nVerdict: APPROVE\nEvidence follows", True),
        ("Intro line\nVerdict: **APPROVE**\nEvidence follows", True),
        ("VERDICT: APPROVED", True),
        ("NOT APPROVED — missing tests", False),
        ("Verdict: not approved", False),
        ("REJECT — missing tests but say APPROVE later", False),
        ("Intro line\n**REJECT** — missing tests\nAPPROVE later", False),
        ("Intro line\nVerdict: **REJECT**\nAPPROVE later", False),
        ("VERDICT: REJECTED", False),
        ("REVISE — almost there", False),
        ("The work is fine. APPROVE", False),
    ],
)
def test_review_approval_requires_an_explicit_positive_verdict(text: str, approved: bool) -> None:
    assert ralph.review_approved(text) is approved


async def test_review_feedback_repair_reuses_last_real_reviewer_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_feedback = "REJECT — mention the verification evidence in the implementation report."
    session = Session(id="s-ralph-feedback", workdir=tmp_path)
    said: list[str] = []
    seen_tasks: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    ctx = SimpleNamespace(
        core=SimpleNamespace(),
        session=session,
        turn_id="t-ralph-feedback",
        say=say,
        emit=lambda event: None,
    )
    manager = SimpleNamespace(limit_for=lambda _session: 1)

    async def fake_run(parent: Any, task: str, **kwargs: Any) -> Any:
        seen_tasks.append(task)
        return SimpleNamespace(ok=True, error="")

    async def reject_once(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        return False, real_feedback

    async def fail_verification(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        return False, "still failing"

    async def no_progress(*_args: Any, **_kwargs: Any) -> None:
        return None

    manager.run = fake_run
    monkeypatch.setattr(ralph, "get_manager", lambda _core: manager)
    monkeypatch.setattr(ralph, "reply_language_for", lambda _ctx: "auto")
    monkeypatch.setattr(ralph, "review", reject_once)
    monkeypatch.setattr(ralph, "verify_story", fail_verification)
    monkeypatch.setattr(ralph, "_progress", no_progress)

    story = ralph.Story(id="S1", title="repair me")
    approved, verdict, iteration = await ralph._review_until_approved(
        ctx,  # type: ignore[arg-type]
        "fix the thing",
        [story],
        start_iteration=0,
        max_iterations=2,
    )

    assert approved is False
    assert verdict == real_feedback
    assert iteration == 2
    assert len(seen_tasks) == 2
    assert all(real_feedback in task for task in seen_tasks)
    assert all("review feedback repair did not pass" not in task for task in seen_tasks)


def test_review_feedback_batches_preserve_previous_passes() -> None:
    stories = [ralph.Story(id=f"S{i}", title=f"story {i}") for i in range(1, 5)]
    for story in stories:
        story.note = "review rejected"
    assert [story.id for story in ralph.ready_stories(stories, 2)] == ["S1", "S2"]
    stories[0].passed = stories[1].passed = True
    assert [story.id for story in ralph.ready_stories(stories, 2)] == ["S3", "S4"]


@pytest.mark.parametrize(
    ("error", "runs"),
    [
        ("HTTP 400: unknown parameter chat_template_kwargs", 1),  # stop at once
        ("the sandbox is gone", 2),  # stop when the same failure repeats
    ],
)
async def test_ralph_stops_early_when_every_subagent_fails_the_same_way(
    daemon: Daemon, repo: Path, monkeypatch: pytest.MonkeyPatch, error: str, runs: int
) -> None:
    from snowpea_core.agent.subagent import SubagentResult, get_manager

    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    calls: list[str] = []

    async def failing_run(parent: Any, task: str, **_kwargs: Any) -> SubagentResult:
        calls.append(task)
        return SubagentResult(agent_id=f"a-{len(calls)}", ok=False, error=error)

    monkeypatch.setattr(get_manager(core), "run", failing_run)
    turn_id = await asyncio.wait_for(
        core.commands.run(core, session, "ralph", f'"{TASK}"'), timeout=TIMEOUT
    )
    batch = len(calls) // runs
    assert batch >= 1 and len(calls) == batch * runs, len(calls)
    reasons = [
        event["payload"]["reason"]
        for event in recorder.of_kind("turn.done")
        if event["payload"]["turnId"] == turn_id
    ]
    assert reasons == ["error"]
    assert "retrying will not fix" in recorder.texts()


# ---------------------------------------------------------------------------
# command.progress (snowpea-browser): the plan and each iteration as data
# ---------------------------------------------------------------------------


async def test_ralph_emits_command_progress_for_plan_iterations_and_outcome(
    daemon: Daemon, repo: Path
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto", max_concurrent=3)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    await asyncio.wait_for(
        core.commands.run(core, session, "ralph", f'"{TASK}"'), timeout=TIMEOUT
    )
    progress = [event["payload"] for event in recorder.of_kind("command.progress")]
    assert len(progress) >= 3, progress
    plan, *middle, last = progress
    assert plan["command"] == "ralph" and plan["iteration"] == 0
    assert plan["outcome"] is None
    assert [row["status"] for row in plan["stories"]] == ["pending", "pending"]
    assert {row["id"] for row in plan["stories"]} == {"S1", "S2"}
    assert all(row["title"] for row in plan["stories"])
    assert middle and all(row["iteration"] >= 1 and row["outcome"] is None for row in middle)
    assert last["outcome"] == "complete"
    assert all(row["status"] == "pass" for row in last["stories"])
    # The text lines are still there, unchanged.
    assert "ralph: 2 stories planned." in recorder.texts()
    assert "ralph iteration 1:" in recorder.texts()


async def test_ralph_progress_carries_stopped_when_it_gives_up_early(
    daemon: Daemon, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.agent.subagent import SubagentResult, get_manager

    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    async def failing_run(parent: Any, task: str, **_kwargs: Any) -> SubagentResult:
        return SubagentResult(
            agent_id="a", ok=False, summary="", error="HTTP 400: unknown parameter x"
        )

    monkeypatch.setattr(get_manager(core), "run", failing_run)
    await asyncio.wait_for(
        core.commands.run(core, session, "ralph", f'"{TASK}"'), timeout=TIMEOUT
    )
    last = recorder.of_kind("command.progress")[-1]["payload"]
    assert last["outcome"] == "stopped"
    assert last["iteration"] == 1
    assert "fail" in {row["status"] for row in last["stories"]}
    failed = next(row for row in last["stories"] if row["status"] == "fail")
    assert "HTTP 400" in failed["note"]


# ---------------------------------------------------------------------------
# PRD robustness: a stricter retry, JSON mode where the vendor takes it, and a
# single-story fallback instead of failing the command
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    """Answers each ``stream`` call with the next scripted reply."""

    def __init__(self, replies: list[str], json_mode: bool = False) -> None:
        from types import SimpleNamespace

        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.preset = SimpleNamespace(supports_json_mode=json_mode)

    async def stream(self, messages: list[Any], tools: list[Any], **kwargs: Any) -> Any:
        from snowpea_core.providers.base import StreamEvent

        self.calls.append({"messages": list(messages), **kwargs})
        text = self.replies.pop(0) if self.replies else ""
        if text:
            yield StreamEvent(kind="text_delta", text=text)


def _prd_ctx(provider: Any, workdir: Path) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        core=SimpleNamespace(providers=SimpleNamespace(get=lambda *_a: provider)),
        session=SimpleNamespace(provider=None, model=None, workdir=workdir),
    )


GOOD_PRD = '{"stories": [{"id": "S1", "title": "do it", "verify": ["true"]}]}'


async def test_prd_retries_once_more_strictly_and_uses_json_mode_where_supported(
    tmp_path: Path,
) -> None:
    # The first attempt answers in prose; the stricter retry answers in JSON.
    provider = _ScriptedProvider(["Sure! Here is the plan.", GOOD_PRD], json_mode=True)
    stories, fallback = await ralph.build_prd(_prd_ctx(provider, tmp_path), "do it")
    assert fallback is None
    assert [story.title for story in stories] == ["do it"]
    assert len(provider.calls) == 2
    assert "response_format" not in provider.calls[0]
    assert provider.calls[1]["response_format"] == {"type": "json_object"}
    assert provider.calls[1]["messages"][-1].content == ralph.PRD_RETRY_INSTRUCTION


async def test_prd_retry_sends_no_json_mode_to_a_vendor_without_it(tmp_path: Path) -> None:
    provider = _ScriptedProvider(["no json here", GOOD_PRD], json_mode=False)
    stories, fallback = await ralph.build_prd(_prd_ctx(provider, tmp_path), "do it")
    assert fallback is None and len(stories) == 1
    assert all("response_format" not in call for call in provider.calls)


async def test_prd_falls_back_to_one_story_when_the_retry_fails_too(tmp_path: Path) -> None:
    task = "Make the importer skip blank rows " + "x" * 200 + "\nand log them."
    provider = _ScriptedProvider([], json_mode=True)  # empty replies throughout
    stories, fallback = await ralph.build_prd(_prd_ctx(provider, tmp_path), task)
    assert fallback is not None and "empty reply" in fallback
    assert len(stories) == 1
    only = stories[0]
    assert only.id == "S1"
    assert only.acceptance == task
    assert only.title.startswith("Make the importer skip blank rows")
    assert len(only.title) <= ralph.FALLBACK_TITLE_CHARS
    assert "\n" not in only.title


async def test_ralph_runs_the_fallback_story_and_says_so(
    daemon: Daemon, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.agent.subagent import SubagentResult, get_manager

    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    provider = _ScriptedProvider([])
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)

    async def ok_run(parent: Any, task: str, **_kwargs: Any) -> SubagentResult:
        return SubagentResult(agent_id="a", ok=True, summary="done")

    async def approve(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        return True, "APPROVE"

    monkeypatch.setattr(get_manager(core), "run", ok_run)
    monkeypatch.setattr(ralph, "review", approve)
    turn_id = await asyncio.wait_for(
        core.commands.run(core, session, "ralph", f'"{TASK}"'), timeout=TIMEOUT
    )
    texts = recorder.texts()
    assert "running the task as a single story" in texts
    assert "ralph: 1 stories planned." in texts
    reasons = [
        event["payload"]["reason"]
        for event in recorder.of_kind("turn.done")
        if event["payload"]["turnId"] == turn_id
    ]
    assert reasons == ["complete"]
    last = recorder.of_kind("command.progress")[-1]["payload"]
    assert last["outcome"] == "complete"
    assert [row["id"] for row in last["stories"]] == ["S1"]


# ---------------------------------------------------------------------------
# check failures: a repeat stops the loop, a broken check is rewritten rather
# than the work redone, and invalid checks are repaired before iteration 1
# ---------------------------------------------------------------------------

BROKEN_PYTHON_CHECK = (
    "python3 -c \"import os; try: os.stat('x'); except OSError: raise SystemExit(1)\""
)


def test_check_problem_compiles_python_c_and_parses_shell() -> None:
    assert ralph.check_problem(BROKEN_PYTHON_CHECK) is not None
    assert ralph.check_problem("python3 -u -c \"import os; assert os.sep\"") is None
    assert ralph.check_problem("pytest -q tests/test_x.py") is None
    if ralph.shutil.which("bash"):
        assert ralph.check_problem("test -f a && (grep foo a") is not None


def test_a_broken_check_is_told_apart_from_a_failing_one() -> None:
    syntax = 'Traceback:\n  File "<string>", line 1\n    try: x\nSyntaxError: invalid syntax'
    assert ralph.check_is_broken(1, syntax)
    assert ralph.check_is_broken(127, "bash: nosuchtool: command not found")
    assert ralph.check_is_broken(2, "bash: -c: syntax error near unexpected token `)'")
    assert not ralph.check_is_broken(1, "AssertionError")
    assert not ralph.check_is_broken(1, "ModuleNotFoundError: No module named 'foo'")
    assert not ralph.check_is_broken(1, "FAILED tests/test_x.py::test_y - assert 1 == 2")


def test_failure_signatures_ignore_timings_and_addresses() -> None:
    one = ralph.failure_signature("pytest", 1, "1 failed in 0.12s at 0x7f00aa")
    two = ralph.failure_signature("pytest", 1, "1 failed   in 0.31s at 0x7f11bb")
    assert one == two
    assert one != ralph.failure_signature("pytest", 1, "AssertionError: other")


async def _run_ralph_with(
    daemon: Daemon,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    verify: list[str],
    repairs: list[list[str] | None],
) -> tuple[Recorder, str, list[str], list[str]]:
    """``/ralph`` on one story with ``verify``; subagents succeed, review approves."""
    from snowpea_core.agent.subagent import SubagentResult, get_manager

    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    runs: list[str] = []
    repair_problems: list[str] = []

    async def prd(_ctx: Any, _task: str) -> tuple[list[ralph.Story], None]:
        return [ralph.Story(id="S1", title="the story", verify=list(verify))], None

    async def ok_run(parent: Any, task: str, **_kwargs: Any) -> SubagentResult:
        runs.append(task)
        return SubagentResult(agent_id="a", ok=True, summary="done")

    async def repair(_ctx: Any, _task: str, _story: Any, problem: str) -> list[str] | None:
        repair_problems.append(problem)
        return repairs.pop(0) if repairs else None

    async def approve(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        return True, "APPROVE"

    monkeypatch.setattr(ralph, "build_prd", prd)
    monkeypatch.setattr(ralph, "repair_checks", repair)
    monkeypatch.setattr(ralph, "review", approve)
    monkeypatch.setattr(get_manager(core), "run", ok_run)
    turn_id = await asyncio.wait_for(
        core.commands.run(core, session, "ralph", f'"{TASK}"'), timeout=TIMEOUT
    )
    return recorder, turn_id, runs, repair_problems


def _reasons(recorder: Recorder, turn_id: str) -> list[str]:
    return [
        event["payload"]["reason"]
        for event in recorder.of_kind("turn.done")
        if event["payload"]["turnId"] == turn_id
    ]


async def test_the_same_check_failure_twice_stops_ralph(
    daemon: Daemon, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, turn_id, runs, repairs = await _run_ralph_with(
        daemon, repo, monkeypatch, ["test -f never-created.txt"], []
    )
    assert len(runs) == 2, "a third identical attempt should never start"
    assert repairs == []  # an ordinary failing check is the work's problem
    assert _reasons(recorder, turn_id) == ["error"]
    assert "failed the same way twice" in recorder.texts()
    assert recorder.of_kind("command.progress")[-1]["payload"]["outcome"] == "stopped"


async def test_a_check_that_errors_is_rewritten_instead_of_redoing_the_work(
    daemon: Daemon, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, turn_id, runs, repairs = await _run_ralph_with(
        daemon, repo, monkeypatch, ["snowpea-no-such-tool --check"], [["true"]]
    )
    assert len(runs) == 1, "the work was not redone for a broken check"
    assert len(repairs) == 1 and "exited 127" in repairs[0]
    assert _reasons(recorder, turn_id) == ["complete"]
    assert "check rewritten" in recorder.texts()


async def test_a_broken_check_that_cannot_be_repaired_stops_after_the_repeat(
    daemon: Daemon, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, turn_id, runs, repairs = await _run_ralph_with(
        daemon, repo, monkeypatch, ["snowpea-no-such-tool --check"], [None]
    )
    assert len(runs) == 1 and len(repairs) == 1
    assert _reasons(recorder, turn_id) == ["error"]
    assert "failed the same way twice" in recorder.texts()


async def test_invalid_checks_are_repaired_before_the_first_iteration(
    daemon: Daemon, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder, turn_id, runs, repairs = await _run_ralph_with(
        daemon, repo, monkeypatch, [BROKEN_PYTHON_CHECK], [["true"]]
    )
    assert len(repairs) == 1 and "python -c" in repairs[0]
    assert len(runs) == 1
    assert "invalid check rewritten" in recorder.texts()
    assert _reasons(recorder, turn_id) == ["complete"]
    prd = json.loads((repo / ".snowpea" / "ralph" / "prd.json").read_text(encoding="utf-8"))
    assert prd["stories"][0]["verify"] == ["true"]


async def test_repair_checks_asks_the_model_and_reads_its_verify_list(tmp_path: Path) -> None:
    provider = _ScriptedProvider(['{"verify": ["python3 - <<\'PY\'\\nassert 1\\nPY"]}'], True)
    story = ralph.Story(id="S1", title="t", verify=[BROKEN_PYTHON_CHECK])
    revised = await ralph.repair_checks(_prd_ctx(provider, tmp_path), "task", story, "bad")
    assert revised == ["python3 - <<'PY'\nassert 1\nPY"]
    assert provider.calls[0]["response_format"] == {"type": "json_object"}
    assert "bad" in provider.calls[0]["messages"][-1].content

    nothing = _ScriptedProvider([])
    assert await ralph.repair_checks(_prd_ctx(nothing, tmp_path), "task", story, "bad") is None
