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
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from snowpea_core.agent.subagent import RUNNING, SUBAGENT_KIND
from snowpea_core.commands import ralph
from snowpea_core.server.app_server import Daemon

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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, test-local repo
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with two tracked files for ralph to change."""
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "snowpea test")
    (project / "tracked_a.txt").write_text("original a\n", encoding="utf-8")
    (project / "tracked_b.txt").write_text("original b\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "initial")
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

    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.events]

    def texts(self) -> str:
        return "\n".join(
            str(event["payload"].get("text", "")) for event in self.of_kind("message.done")
        )


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
        listing = await agent_list_handler(_Conn(session), None, core)  # type: ignore[arg-type]
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
    diff = _git(repo, "diff", "--stat")
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

    # 6. The parent saw the whole subagent lane, reviewer included.
    spawns = recorder.of_kind("subagent.spawn")
    assert len(spawns) == 3, [event["payload"]["task"][:40] for event in spawns]
    assert len(recorder.of_kind("subagent.done")) == 3


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


class _Conn:
    """The smallest thing ``_session_for`` will accept as a connection."""

    closed = False

    def __init__(self, session: Any) -> None:
        session.origin_conn = self
