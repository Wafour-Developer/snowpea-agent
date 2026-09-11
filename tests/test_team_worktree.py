"""M7 US-020 / AC-16: ``/team N <task>`` in real git worktrees.

Everything runs in-process against the deterministic scripted fake provider, so
there is no API key and no Docker.  The assertions are the acceptance criteria
verbatim:

* three worktrees exist *while* the run is going, three tasks end ``merged``,
  the base branch gains three ``--no-ff`` merge commits, and the worktrees and
  branches are gone afterwards;
* a task whose worker always rewrites the same line another task already merged
  is re-queued exactly ``team.max_conflict_retries`` times and then fixed at
  ``failed`` with its conflict hunks attached, while the lead merges the rest;
* ``team.status`` and ``snowpea team status --json`` report those states;
* ``team.task.update`` events arrive on the lead session with rising ``seq``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from snowpea_core.agent import team, team_store
from snowpea_core.cli.commands import team_status as cli_team_status
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import TeamStatusParams, TeamStatusResult
from snowpea_core.server.team_handlers import team_status_handler

FIXTURES = Path(__file__).parent / "fixtures" / "providers" / "fake"
TEAM_SCRIPT = FIXTURES / "team.json"
CONFLICT_SCRIPT = FIXTURES / "team_conflict.json"

TIMEOUT = 90.0

TEAM_TASK = "3개 모듈에 docstring 추가"
CONFLICT_TASK = "rewrite the shared banner"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, test-local repo
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _make_repo(root: Path, files: dict[str, str]) -> Path:
    """A git repository with one commit, so worktrees have something to branch."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "snowpea test")
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def worktree_paths(repo: Path) -> list[str]:
    """Every worktree git knows about, the main one included."""
    return [
        line.split(" ", 1)[0]
        for line in _git(repo, "worktree", "list").splitlines()
        if line.strip()
    ]


def team_branches(repo: Path) -> list[str]:
    return [
        line.strip().lstrip("* ").strip()
        for line in _git(repo, "branch", "--list", "snowpea/team-*").splitlines()
        if line.strip()
    ]


def merge_commits(repo: Path) -> list[str]:
    out = _git(repo, "log", "--merges", "--format=%s")
    return [line for line in out.splitlines() if line.strip()]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Three empty modules for the team to document."""
    return _make_repo(
        tmp_path / "repo",
        {
            "module_a.py": "def a() -> str:\n    return \"a\"\n",
            "module_b.py": "def b() -> str:\n    return \"b\"\n",
            "module_c.py": "def c() -> str:\n    return \"c\"\n",
        },
    )


@pytest.fixture
def conflict_repo(tmp_path: Path) -> Path:
    """One file with one line, which both tasks will rewrite."""
    return _make_repo(tmp_path / "conflict-repo", {"shared.txt": "the original banner\n"})


@pytest.fixture
def script(request: pytest.FixtureRequest) -> Iterator[Path]:
    """Point the daemon at one fake-provider script for the whole test."""
    path = getattr(request, "param", TEAM_SCRIPT)
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{path}"
    try:
        yield path
    finally:
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


@pytest_asyncio.fixture
async def daemon(tmp_path: Path, script: Path) -> AsyncIterator[Daemon]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    instance = Daemon(port=0, home=home)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


class Recorder:
    """A pseudo-connection that keeps every event the hub sends it."""

    closed = False

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "session.event":
            self.events.append(params)

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["kind"] == kind]

    def texts(self) -> str:
        return "\n".join(
            str(event["payload"].get("text", "")) for event in self.of_kind("message.done")
        )


async def _status(daemon: Daemon, team_id: str | None = None) -> TeamStatusResult:
    core = daemon.core
    assert core is not None
    return await team_status_handler(
        None,  # type: ignore[arg-type]
        TeamStatusParams(teamId=team_id or ""),
        core,
    )


def _by_id(status: TeamStatusResult) -> dict[str, Any]:
    return {row.taskId: row for row in status.tasks}


# ---------------------------------------------------------------------------
# the pure parts
# ---------------------------------------------------------------------------


def test_team_args_parse_the_count_and_the_quoted_task() -> None:
    assert team.parse_team_args('3 "3개 모듈에 docstring 추가"') == (3, "3개 모듈에 docstring 추가")
    assert team.parse_team_args("2 add docstrings") == (2, "add docstrings")
    for bad in ("", "3", "many things", '"just a task"'):
        with pytest.raises(team.TeamError):
            team.parse_team_args(bad)


def test_the_planner_json_becomes_tasks_with_ids_and_pruned_dependencies() -> None:
    planned = team.tasks_from_payload(
        {
            "tasks": [
                {"id": "T1", "title": "one"},
                {"title": "two", "depends_on": ["T1", "nope"]},
                {"not": "a task"},
                "three",
            ]
        }
    )
    assert [entry.id for entry in planned] == ["T1", "T2", "T4"]
    assert planned[1].depends_on == ["T1"]
    assert planned[2].title == "three"


def test_branch_names_follow_the_contract() -> None:
    assert team.branch_name("tmabc", 2) == "snowpea/team-tmabc-2"


# ---------------------------------------------------------------------------
# (a) three workers, three worktrees, three --no-ff merges, clean afterwards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [TEAM_SCRIPT], indirect=True)
async def test_three_workers_merge_three_tasks_and_clean_up(daemon: Daemon, repo: Path) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto", max_concurrent=3)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    before = worktree_paths(repo)
    assert len(before) == 1

    turn_id = core.commands.start(core, session, "team", f'3 "{TEAM_TASK}"')
    turn = session.turn_task
    assert turn is not None

    # 1. The three worktrees really exist while the team is working.
    peak = 0
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while not turn.done() and asyncio.get_running_loop().time() < deadline:
        peak = max(peak, len(worktree_paths(repo)))
        await asyncio.sleep(0.01)
    await asyncio.wait_for(turn, timeout=TIMEOUT)
    assert peak == len(before) + 3, f"expected three extra worktrees, saw {peak} in total"

    # 2. The turn ended cleanly.
    done = [
        event["payload"]
        for event in recorder.of_kind("turn.done")
        if event["payload"]["turnId"] == turn_id
    ]
    assert done and done[-1]["reason"] == "complete", recorder.texts()

    # 3. Every task merged.
    status = await _status(daemon)
    assert len(status.tasks) == 3, status.tasks
    assert {row.status for row in status.tasks} == {team_store.MERGED}, [
        (row.taskId, row.status, row.note) for row in status.tasks
    ]
    assert all(row.agentN in (1, 2, 3) for row in status.tasks)
    assert status.state == "done"

    # 4. The base branch gained one --no-ff merge commit per task.
    merges = merge_commits(repo)
    assert len(merges) == 3, merges
    assert all(subject.startswith("Merge snowpea/team-") for subject in merges), merges

    # 5. The work is actually on the base branch.
    for name in ("module_a.py", "module_b.py", "module_c.py"):
        assert (repo / name).read_text(encoding="utf-8").startswith('"""Module')

    # 6. The worktrees and the branches are gone.
    assert worktree_paths(repo) == before
    assert team_branches(repo) == []
    assert not list((repo / ".snowpea" / "worktrees").glob("*/.git"))


# ---------------------------------------------------------------------------
# (b) an always-conflicting task: two retries, then failed, team carries on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [CONFLICT_SCRIPT], indirect=True)
async def test_a_conflicting_task_retries_twice_then_fails(
    daemon: Daemon, conflict_repo: Path
) -> None:
    core = daemon.core
    assert core is not None
    assert core.settings.team.max_conflict_retries == 2
    session = await core.sessions.create(conflict_repo, mode="auto", max_concurrent=2)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    turn_id = await asyncio.wait_for(
        core.commands.run(core, session, "team", f'2 "{CONFLICT_TASK}"'), timeout=TIMEOUT
    )

    status = await _status(daemon)
    rows = _by_id(status)
    assert set(rows) == {"C1", "C2"}, status.tasks

    merged = [row for row in status.tasks if row.status == team_store.MERGED]
    failed = [row for row in status.tasks if row.status == team_store.FAILED]
    assert len(merged) == 1, [(row.taskId, row.status, row.note) for row in status.tasks]
    assert len(failed) == 1, [(row.taskId, row.status, row.note) for row in status.tasks]

    loser = failed[0]
    # Exactly two re-queues, then the task is fixed at failed.
    assert loser.retries == 2, loser
    # The hunks that could not be merged are still attached to it.
    assert loser.conflictHunks, loser
    assert "shared.txt" in loser.conflictHunks
    assert loser.conflictSummary

    # The lead did not stop: the other task merged and the turn completed.
    done = [
        event["payload"]
        for event in recorder.of_kind("turn.done")
        if event["payload"]["turnId"] == turn_id
    ]
    assert done and done[-1]["reason"] == "complete", recorder.texts()
    assert len(merge_commits(conflict_repo)) == 1

    # Only the winning commit is on the base branch.
    banner = (conflict_repo / "shared.txt").read_text(encoding="utf-8")
    assert banner.strip() == "banner written by the first worker"

    # Cleanup still happened even though a task failed.
    assert len(worktree_paths(conflict_repo)) == 1
    assert team_branches(conflict_repo) == []

    # The re-queues were visible as `conflict` while they lasted.
    seen = [
        event["payload"]["status"]
        for event in recorder.of_kind("team.task.update")
        if event["payload"]["taskId"] == loser.taskId
    ]
    assert seen.count(team_store.CONFLICT) == 2, seen
    assert seen[-1] == team_store.FAILED, seen


# ---------------------------------------------------------------------------
# (c) team.status over RPC and through the CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [TEAM_SCRIPT], indirect=True)
async def test_team_status_and_the_cli_report_the_board(
    daemon: Daemon, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto", max_concurrent=3)
    await asyncio.wait_for(
        core.commands.run(core, session, "team", f'3 "{TEAM_TASK}"'), timeout=TIMEOUT
    )

    # Addressing the team by id and letting the daemon pick the latest agree.
    latest = await _status(daemon)
    named = await _status(daemon, latest.teamId)
    assert named.model_dump() == latest.model_dump()
    assert latest.task == TEAM_TASK
    assert latest.workers == 3

    # `snowpea team status --json` against the same daemon.
    capsys.readouterr()
    code = await cli_team_status(latest.teamId, daemon.paths.home, as_json=True)
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["teamId"] == latest.teamId
    assert printed["state"] == "done"
    assert {row["status"] for row in printed["tasks"]} == {team_store.MERGED}
    assert all(row["retries"] == 0 for row in printed["tasks"])

    # And the human-readable form names every task.
    code = await cli_team_status(None, daemon.paths.home, as_json=False)
    assert code == 0
    text = capsys.readouterr().out
    assert latest.teamId in text
    for row in latest.tasks:
        assert row.taskId in text

    # An unknown team is a clean not-found, not a traceback.
    with pytest.raises(Exception, match="no such team"):
        await _status(daemon, "tm-does-not-exist")


# ---------------------------------------------------------------------------
# (d) team.task.update events
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [TEAM_SCRIPT], indirect=True)
async def test_task_update_events_carry_the_board_transitions(
    daemon: Daemon, repo: Path
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto", max_concurrent=3)
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    await asyncio.wait_for(
        core.commands.run(core, session, "team", f'3 "{TEAM_TASK}"'), timeout=TIMEOUT
    )

    updates = recorder.of_kind("team.task.update")
    assert updates, "no team.task.update events reached the lead session"

    seqs = [event["seq"] for event in updates]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs

    team_ids = {event["payload"]["teamId"] for event in updates}
    assert len(team_ids) == 1

    for task_id in ("T1", "T2", "T3"):
        states = [
            event["payload"]["status"]
            for event in updates
            if event["payload"]["taskId"] == task_id
        ]
        assert states[0] == team_store.QUEUED, states
        assert states[-1] == team_store.MERGED, states
        assert team_store.CLAIMED in states and team_store.DONE in states, states

    # Every update names the worker that owned the task, once one did.
    owned = [event["payload"] for event in updates if event["payload"]["status"] != "queued"]
    assert all(payload["agentN"] in (1, 2, 3) for payload in owned), owned
    assert all(payload["retries"] == 0 for payload in owned), owned


# ---------------------------------------------------------------------------
# argument handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [TEAM_SCRIPT], indirect=True)
async def test_team_without_arguments_explains_itself(daemon: Daemon, repo: Path) -> None:
    from snowpea_core.commands import team_cmd

    core = daemon.core
    assert core is not None
    session = await core.sessions.create(repo, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    await asyncio.wait_for(core.commands.run(core, session, "team", ""), timeout=TIMEOUT)
    assert team_cmd.USAGE in recorder.texts()


@pytest.mark.parametrize("script", [TEAM_SCRIPT], indirect=True)
async def test_team_outside_a_git_repository_fails_the_turn(
    daemon: Daemon, tmp_path: Path
) -> None:
    core = daemon.core
    assert core is not None
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    session = await core.sessions.create(plain, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)

    turn_id = await asyncio.wait_for(
        core.commands.run(core, session, "team", '2 "anything"'), timeout=TIMEOUT
    )
    reasons = [
        event["payload"]["reason"]
        for event in recorder.of_kind("turn.done")
        if event["payload"]["turnId"] == turn_id
    ]
    assert reasons == ["error"], recorder.texts()
    assert "git repository" in recorder.texts()
