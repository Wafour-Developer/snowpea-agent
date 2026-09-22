"""M15 §C: one task, one agent — the dedupe, ownership and review guards.

The policy text lives in ``prompts/tool_descriptions.DELEGATE_TASK``; everything
below asserts the parts of it that are made true in code, because a delegation
rule that only lives in prose is a rule a tired model ignores at midnight.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _support import git, init_repo, make_daemon

from snowpea_core.agent import subagent, team, team_store
from snowpea_core.agent.definition import builtin_agent_definitions
from snowpea_core.agent.subagent import DUPLICATE_CODE, SubagentResult, get_manager
from snowpea_core.commands import review_cmd, ultrawork
from snowpea_core.commands.ultrawork import Subtask, merge_overlapping, subtasks_from_payload
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.prompts import tool_descriptions
from snowpea_core.server.app_server import Core, Daemon
from snowpea_core.session.session import Session
from snowpea_core.tools import file_state, fs
from snowpea_core.tools.delegate import render_report
from snowpea_core.tools.registry import ToolContext

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "subagents.json"
TIMEOUT = 30.0


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    instance = await make_daemon(tmp_path / "home")
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
    return project


# ---------------------------------------------------------------------------
# §C1 the policy the parent reads
# ---------------------------------------------------------------------------


def test_the_delegate_description_states_the_single_owner_rule() -> None:
    text = tool_descriptions.DELEGATE_TASK
    assert "One task, one agent." in text
    assert "do not give the same task to a second agent" in text
    assert "disjoint files" in text
    assert "USE FOR:" in text and "NOT FOR:" in text
    assert "self-report" in text
    assert "Never poll" in text


# ---------------------------------------------------------------------------
# §C2 the dedupe guard
# ---------------------------------------------------------------------------


def test_the_fingerprint_ignores_wrapping_and_case() -> None:
    one = subagent.fingerprint("explore", "Find   the LOADER\n\nand report it")
    two = subagent.fingerprint("explore", "find the loader and report it")
    assert one == two
    assert one != subagent.fingerprint("reviewer", "find the loader and report it")


async def test_a_second_identical_delegation_is_refused_while_the_first_runs(
    daemon: Daemon, workdir: Path
) -> None:
    core = daemon.core
    assert core is not None
    parent = await core.sessions.create(workdir, mode="auto")
    manager = get_manager(core)

    first = asyncio.ensure_future(manager.run(parent, "summarise the tests"))
    await _wait_for_running(manager, parent.id)

    refused = await manager.run(parent, "Summarise   the tests")
    assert refused.ok is False
    assert refused.error is not None
    assert refused.error.startswith(f"{DUPLICATE_CODE}:")
    running = [r for r in manager.records() if r.status == subagent.RUNNING]
    assert running and running[0].agent_id in refused.error

    done = await asyncio.wait_for(first, TIMEOUT)
    assert done.ok is True

    # Once the first child has reported, the same brief is a fresh task again.
    again = await manager.run(parent, "summarise the tests")
    assert again.ok is True


async def test_force_allows_a_deliberate_second_run(daemon: Daemon, workdir: Path) -> None:
    core = daemon.core
    assert core is not None
    parent = await core.sessions.create(workdir, mode="auto")
    manager = get_manager(core)

    first = asyncio.ensure_future(manager.run(parent, "summarise the tests"))
    await _wait_for_running(manager, parent.id)

    second = await manager.run(parent, "summarise the tests", force=True)
    assert second.ok is True
    assert await asyncio.wait_for(first, TIMEOUT)


async def test_two_parents_are_not_each_other_s_duplicates(
    daemon: Daemon, workdir: Path
) -> None:
    core = daemon.core
    assert core is not None
    one = await core.sessions.create(workdir, mode="auto")
    two = await core.sessions.create(workdir, mode="auto")
    manager = get_manager(core)

    first = asyncio.ensure_future(manager.run(one, "summarise the tests"))
    await _wait_for_running(manager, one.id)
    other = await manager.run(two, "summarise the tests")

    assert other.ok is True
    await asyncio.wait_for(first, TIMEOUT)


async def _wait_for_running(manager: Any, parent_id: str) -> None:
    for _ in range(500):
        if any(
            record.status == subagent.RUNNING and record.parent_session_id == parent_id
            for record in manager.records()
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no child ever reached running")


# ---------------------------------------------------------------------------
# §C3 sibling file ownership
# ---------------------------------------------------------------------------


class _Backend:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def read_file(self, path: str) -> str:
        target = self.root / path
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        (self.root / path).write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    file_state.REGISTRY.clear()
    yield
    file_state.REGISTRY.clear()


def _child_ctx(root: Path, session_id: str, parent: str) -> Any:
    settings = Settings()
    core = Core(settings=settings, paths=Paths.create(root / "home"), token="t")
    session = Session(id=session_id, workdir=root, parent_session_id=parent)
    return ToolContext(session=session, core=core, backend=_Backend(root))  # type: ignore[arg-type]


async def test_a_running_sibling_owns_the_file_it_wrote(tmp_path: Path) -> None:
    (tmp_path / "shared.py").write_text("one = 1\n", encoding="utf-8")
    one = _child_ctx(tmp_path, "s-child-1", parent="s-parent")
    two = _child_ctx(tmp_path, "s-child-2", parent="s-parent")
    # Both children share one Core, which is where the subagent records live.
    two.core = one.core

    manager = get_manager(one.core)
    record = manager.new_record(one.session, "edit shared.py", "executor", title="Rename one")
    record.session_id = "s-child-1"
    record.parent_session_id = "s-parent"
    record.status = subagent.RUNNING

    assert (await fs.read_file(two, {"path": "shared.py"})).ok is True
    assert (await fs.read_file(one, {"path": "shared.py"})).ok is True
    assert (await fs.write_file(one, {"path": "shared.py", "content": "one = 2\n"})).ok is True

    blocked = await fs.patch(two, {"path": "shared.py", "old_string": "one", "new_string": "two"})
    assert blocked.ok is False
    assert blocked.error is not None
    assert blocked.error.startswith(f"{file_state.OWNED_CODE}:")
    assert "Rename one" in blocked.error
    assert "report instead of editing" in blocked.error

    # Once that sibling has finished, the collision is ordinary staleness:
    # the edit goes through with a note naming the other writer (Hermes warns
    # the same way rather than refusing).
    record.status = subagent.DONE
    noted = await fs.patch(two, {"path": "shared.py", "old_string": "one", "new_string": "two"})
    assert noted.ok is True, noted.error
    assert "note: " in (noted.output or "") and "s-child-1" in (noted.output or "")


async def test_the_ownership_guard_follows_read_before_write_setting(tmp_path: Path) -> None:
    (tmp_path / "shared.py").write_text("one = 1\n", encoding="utf-8")
    one = _child_ctx(tmp_path, "s-child-1", parent="s-parent")
    two = _child_ctx(tmp_path, "s-child-2", parent="s-parent")
    two.core = one.core
    one.core.settings.tools.readBeforeWrite = False

    assert (await fs.write_file(one, {"path": "shared.py", "content": "one = 2\n"})).ok is True
    allowed = await fs.patch(two, {"path": "shared.py", "old_string": "one", "new_string": "two"})
    assert allowed.ok is True


# ---------------------------------------------------------------------------
# §C4 the ultrawork split owns disjoint files
# ---------------------------------------------------------------------------


def test_the_splitter_prompt_asks_for_the_file_list() -> None:
    assert "disjoint set of files" in ultrawork.SPLIT_SYSTEM
    assert "`files`" in ultrawork.SPLIT_SYSTEM


def test_subtasks_carry_their_files() -> None:
    parsed = subtasks_from_payload(
        {
            "subtasks": [
                {"id": "T1", "title": "one", "task": "do one", "files": ["./a.py", "a.py"]},
                {"id": "T2", "title": "two", "task": "do two", "files": "b.py"},
            ]
        },
        "fallback",
    )
    assert [part.id for part in parsed] == ["T1", "T2"]
    assert parsed[0].files == ("a.py",)
    assert parsed[1].files == ("b.py",)


def test_subtasks_that_share_a_file_are_merged_into_one() -> None:
    merged = merge_overlapping(
        [
            Subtask("T1", "one", "do one", ("a.py", "shared.py")),
            Subtask("T2", "two", "do two", ("shared.py",)),
            Subtask("T3", "three", "do three", ("c.py",)),
        ]
    )
    assert [part.id for part in merged] == ["T1", "T3"]
    assert merged[0].merged == ("T2",)
    assert "do one" in merged[0].brief and "do two" in merged[0].brief
    assert "shared.py" in merged[0].brief
    assert set(merged[0].files) == {"a.py", "shared.py"}
    assert merged[1].merged == ()


def test_subtasks_with_no_file_lists_are_left_alone() -> None:
    parts = [Subtask("T1", "one", "do one"), Subtask("T2", "two", "do two")]
    assert merge_overlapping(parts) == parts


# ---------------------------------------------------------------------------
# §C5 the built-in explore and reviewer agents
# ---------------------------------------------------------------------------


def test_explore_and_reviewer_are_built_in_and_read_only() -> None:
    by_name = {defn.name: defn for defn in builtin_agent_definitions()}
    assert {"explore", "reviewer"} <= set(by_name)
    for name in ("explore", "reviewer"):
        tools = by_name[name].tool_list()
        assert tools is not None, f"{name} must not inherit every tool"
        assert "read_file" in tools and "grep" in tools
        assert "write_file" not in tools
        assert "patch" not in tools
        assert "shell" not in tools
    assert "very thorough" in by_name["explore"].prompt.lower()
    assert "REQUEST_CHANGES" in by_name["reviewer"].prompt
    assert "findings without evidence are opinions" in by_name["reviewer"].prompt.lower()


async def test_the_built_ins_resolve_by_name_and_narrow_the_child(
    daemon: Daemon, workdir: Path
) -> None:
    core = daemon.core
    assert core is not None
    parent = await core.sessions.create(workdir, mode="auto")
    manager = get_manager(core)

    defn = manager.definition(parent, "explore")
    assert defn is not None and defn.source == "builtin"

    child = Session(id="s-child", workdir=workdir)
    manager._apply_definition(child, defn, None)
    assert child.allowed_tools is not None
    assert "write_file" not in child.allowed_tools
    assert "read_file" in child.allowed_tools
    assert child.is_subagent is True


async def test_a_project_definition_overrides_a_built_in(
    daemon: Daemon, workdir: Path
) -> None:
    from snowpea_core.agent.definition import AgentDefinition, write_definition

    core = daemon.core
    assert core is not None
    write_definition(
        AgentDefinition(name="reviewer", description="ours", prompt="You review our way."),
        workdir,
    )
    parent = await core.sessions.create(workdir, mode="auto")
    defn = get_manager(core).definition(parent, "reviewer")
    assert defn is not None
    assert defn.source == "project"
    assert defn.prompt == "You review our way."


async def test_review_delegates_the_diff_and_relays_the_verdict(
    daemon: Daemon, tmp_path: Path
) -> None:
    core = daemon.core
    assert core is not None
    repo = init_repo(tmp_path / "repo", {"a.py": "one = 1\n"})
    (repo / "a.py").write_text("one = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("two = 2\n", encoding="utf-8")

    diff, files = await review_cmd.working_diff(str(repo))
    assert "one = 2" in diff
    assert files == ["a.py", "new.py"]

    session = await core.sessions.create(repo, mode="auto")
    briefs: list[str] = []
    said: list[str] = []

    async def fake_run(_parent: Any, brief: str, **kwargs: Any) -> SubagentResult:
        briefs.append(brief)
        assert kwargs.get("agent") == "reviewer"
        return SubagentResult(
            agent_id="a-1",
            ok=True,
            summary="VERDICT: REQUEST_CHANGES\n- a.py:1 — one is now two, nothing reads it",
        )

    manager = get_manager(core)
    manager.run = fake_run  # type: ignore[method-assign]

    ctx = _command_ctx(core, session, said)
    await review_cmd.cmd_review(ctx, "the rename")

    assert briefs and "one = 2" in briefs[0]
    assert "the rename" in briefs[0]
    assert "a.py" in briefs[0]
    assert any("REQUEST_CHANGES" in line for line in said)


async def test_review_says_so_when_there_is_nothing_to_review(
    daemon: Daemon, tmp_path: Path
) -> None:
    core = daemon.core
    assert core is not None
    repo = init_repo(tmp_path / "clean", {"a.py": "one = 1\n"})
    session = await core.sessions.create(repo, mode="auto")
    said: list[str] = []
    await review_cmd.cmd_review(_command_ctx(core, session, said), "")
    assert said and "no uncommitted change" in said[0]


def _command_ctx(core: Core, session: Session, said: list[str]) -> Any:
    from snowpea_core.commands.registry import CommandContext

    ctx = CommandContext(core=core, session=session, turn_id="t-1")

    async def say(text: str) -> None:
        said.append(text)

    ctx.say = say  # type: ignore[method-assign]
    return ctx


def test_ralph_falls_back_to_the_reviewer_when_no_project_architect_exists() -> None:
    from snowpea_core.commands import ralph

    class _Manager:
        def __init__(self, sources: dict[str, str]) -> None:
            self.sources = sources

        def definition(self, _session: Any, name: str) -> Any:
            source = self.sources.get(name)
            return None if source is None else type("D", (), {"source": source})()

    builtin = {"architect": "builtin", "reviewer": "builtin"}
    assert ralph.reviewer_agent(_Manager(builtin), None) == "reviewer"
    owned = {"architect": "project", "reviewer": "builtin"}
    assert ralph.reviewer_agent(_Manager(owned), None) == "architect"
    assert ralph.reviewer_agent(_Manager({}), None) is None


# ---------------------------------------------------------------------------
# §C5 report folding
# ---------------------------------------------------------------------------


def test_a_long_report_is_spilled_with_a_read_file_pointer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    previous = os.environ.get("SNOWPEA_HOME")
    os.environ["SNOWPEA_HOME"] = str(home)
    try:
        body = "\n".join(f"line {index}" for index in range(1, 601))
        report = render_report(
            SubagentResult(agent_id="a-1", ok=True, summary=body, rounds_used=4)
        )
    finally:
        if previous is None:
            os.environ.pop("SNOWPEA_HOME", None)
        else:
            os.environ["SNOWPEA_HOME"] = previous

    assert "line 1\n" in report
    assert "line 600" in report
    assert "lines omitted" in report
    assert 'read_file("' in report
    spilled = sorted((home / "cache" / "tool-output").glob("report-*.txt"))
    assert spilled and "line 300" in spilled[0].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("reason", "needle"),
    [
        ("complete", "check anything with an external effect"),
        ("budget", "delegate only what is left"),
        ("timeout", "ran out of time"),
        ("error", "do not re-delegate the same brief"),
        ("interrupted", "nothing here is final"),
        ("denied", "denied a permission"),
    ],
)
def test_every_reason_ends_with_a_next_step(reason: str, needle: str) -> None:
    report = render_report(
        SubagentResult(agent_id="a-1", ok=True, summary="done", reason=reason)
    )
    assert needle in report


# ---------------------------------------------------------------------------
# §C5 the optional team review stage
# ---------------------------------------------------------------------------


async def _team_fixture(daemon: Daemon, repo: Path) -> tuple[Any, Any, Any]:
    """A one-task team whose worker branch is ready to merge."""
    core = daemon.core
    assert core is not None
    manager = team.get_manager_for(core)
    session = await core.sessions.create(repo, mode="auto")
    run = team.TeamRun(
        id=team.new_team_id(), session=session, repo=repo, task="do the thing", workers=1
    )
    branch = team.branch_name(run.id, 1)
    git(repo, "checkout", "-q", "-b", branch)
    (repo / "a.py").write_text("one = 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "worker change")
    git(repo, "checkout", "-q", "-")
    await manager.store.create_team(run.id, session.id, str(repo), str(repo), run.task, 1)
    row = await manager.store.add_task(run.id, "T1", 0, "change a.py")
    row.agent_n = 1
    row.branch = branch
    return manager, run, row


def _stub_reviewer(manager: Any, verdict: str) -> list[str]:
    briefs: list[str] = []

    async def fake_run(_parent: Any, brief: str, **_kwargs: Any) -> SubagentResult:
        briefs.append(brief)
        return SubagentResult(agent_id="a-r", ok=True, summary=verdict)

    subagent.get_manager(manager.core).run = fake_run  # type: ignore[method-assign]
    return briefs


async def test_team_review_is_off_unless_it_is_asked_for(
    daemon: Daemon, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "repo", {"a.py": "one = 1\n"})
    manager, run, row = await _team_fixture(daemon, repo)
    briefs = _stub_reviewer(manager, "VERDICT: REQUEST_CHANGES\n- a.py:1 — no")

    assert manager.review_enabled() is False
    await manager._merge(run, row)

    assert briefs == []
    assert row.status == team_store.MERGED


async def test_a_request_changes_verdict_requeues_the_task_once(
    daemon: Daemon, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "repo", {"a.py": "one = 1\n"})
    manager, run, row = await _team_fixture(daemon, repo)
    manager.core.settings.team.review = True
    briefs = _stub_reviewer(manager, "VERDICT: REQUEST_CHANGES\n- a.py:1 — nothing reads it")

    await manager._merge(run, row)

    assert briefs and "one = 2" in briefs[0]
    assert row.status == team_store.QUEUED
    assert row.agent_n == 1
    assert "T1" in run.review_findings
    # The findings reach the same worker, and only once.
    entry = team.Worktree(n=1, path=repo, branch=row.branch)
    brief = manager._task_prompt(run, row, entry)
    assert "nothing reads it" in brief
    assert "T1" not in run.review_findings
    assert manager._task_prompt(run, row, entry).count("nothing reads it") == 0

    # A second merge of the same task is not reviewed again.
    briefs.clear()
    row.status = team_store.DONE
    await manager._merge(run, row)
    assert briefs == []
    assert row.status == team_store.MERGED


async def test_an_approving_verdict_merges(daemon: Daemon, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"a.py": "one = 1\n"})
    manager, run, row = await _team_fixture(daemon, repo)
    manager.core.settings.team.review = True
    briefs = _stub_reviewer(manager, "VERDICT: APPROVE\nNothing blocking.")

    await manager._merge(run, row)

    assert len(briefs) == 1
    assert row.status == team_store.MERGED
    assert run.review_findings == {}
