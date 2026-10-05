"""CORE-plan-continuity: one current plan, carried from planning to execution.

The incident: ``/ralplan`` agreed a plan, the user typed ``/ralph 실행해줘``, and
ralph planned the words "실행해줘" into "add main.py and a Makefile".  These
tests pin the store, the two tools, the plan-first gate, and every consumer —
``/ralph``, ``/team``, ``/workers``, ``/ultrawork`` and the main agent's prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from _support import OriginConn, Recorder

from snowpea_core.agent import agent as agent_mod
from snowpea_core.agent import plan_store
from snowpea_core.commands import plan_gate, ralph, team_cmd, ultrawork
from snowpea_core.commands.registry import CommandContext
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.prompts import compose
from snowpea_core.server.app_server import Daemon
from snowpea_core.session.questions import Answer
from snowpea_core.session.session import Session
from snowpea_core.tools import plan_tools
from snowpea_core.tools.registry import ToolContext, ToolRegistry, register_builtin_tools

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "ralph.json"
TIMEOUT = 60.0

PLAN_MD = """# Plan: Voxel sandbox

Status: pending approval

## Principles
1. Keep the renderer in one module.

## The plan
1. Add the chunk mesher — files: src/mesh.ts — verified by: npm test
2. Add the camera controls — files: src/camera.ts — verified by: npm test
3. Wire both into main.ts — files: src/main.ts — verified by: npm run build

## Acceptance criteria
1. npm test passes
"""


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


def test_steps_come_from_the_plan_heading_not_the_principles(tmp_path: Path) -> None:
    plan = plan_store.save_plan(tmp_path, "Voxel sandbox", PLAN_MD, source="ralplan")
    assert [step.id for step in plan.steps] == ["S1", "S2", "S3"]
    assert plan.steps[0].title.startswith("Add the chunk mesher")
    assert plan.source == "ralplan" and plan.status == "active"
    assert (tmp_path / ".snowpea" / "plans" / "current.md").read_text(encoding="utf-8") == PLAN_MD
    state = json.loads((tmp_path / ".snowpea" / "plans" / "current.json").read_text("utf-8"))
    assert state["title"] == "Voxel sandbox" and len(state["steps"]) == 3


def test_a_spec_falls_back_to_its_acceptance_criteria() -> None:
    spec = "# Login\n\n## Outcome\nUsers log in.\n\n## Acceptance criteria\n1. a\n2. b\n"
    assert [step.title for step in plan_store.derive_steps(spec)] == ["a", "b"]
    korean = "# 계획\n\n## 원칙\n- 하나\n\n## 단계\n1. 첫째\n2. 둘째\n"
    assert [step.title for step in plan_store.derive_steps(korean)] == ["첫째", "둘째"]
    bold = "Goal — x\n\n**Steps**\n1. one\n2. two\n"
    assert [step.title for step in plan_store.derive_steps(bold)] == ["one", "two"]
    assert [step.title for step in plan_store.derive_steps("no list", "Title")] == ["Title"]


def test_given_steps_win_and_are_capped(tmp_path: Path) -> None:
    steps = [{"title": f"step {n}"} for n in range(40)]
    plan = plan_store.save_plan(tmp_path, "Big", "# Big\n", steps=steps)
    assert len(plan.steps) == plan_store.MAX_STEPS
    assert plan.steps[-1].id == f"S{plan_store.MAX_STEPS}"


def test_saving_again_archives_the_previous_plan(tmp_path: Path) -> None:
    plan_store.save_plan(tmp_path, "First", PLAN_MD)
    plan_store.save_plan(tmp_path, "Second", "# Second\n\n## Steps\n1. only\n")
    current = plan_store.load_current(tmp_path)
    assert current is not None and current.title == "Second"
    archived = list((tmp_path / ".snowpea" / "plans" / "archive").glob("*-first.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8"))["status"] == "archived"
    assert archived[0].with_suffix(".md").read_text(encoding="utf-8") == PLAN_MD


def test_mark_step_finishes_and_reopens_the_plan(tmp_path: Path) -> None:
    plan_store.save_plan(tmp_path, "Voxel sandbox", PLAN_MD)
    plan = plan_store.mark_step(tmp_path, "s1", "done", "npm test passed")
    assert plan.step("S1").status == "done" and plan.step("S1").note == "npm test passed"
    assert plan_store.summary_line(plan) == (
        "Current plan: Voxel sandbox (saved just now by manual) — 1/3 steps done; next: S2 "
        f"{plan.step('S2').title} (.snowpea/plans/current.md)"
    )
    plan_store.mark_step(tmp_path, "S2", "done")
    plan = plan_store.mark_step(tmp_path, "S3", "done")
    assert plan.status == "done"
    assert plan_store.active_plan(tmp_path) is None
    assert plan_store.prompt_line(tmp_path) == ""
    plan = plan_store.mark_step(tmp_path, "S3", "blocked", "the build broke")
    assert plan.status == "active"
    with pytest.raises(plan_store.PlanError):
        plan_store.mark_step(tmp_path, "S9", "done")
    with pytest.raises(plan_store.PlanError):
        plan_store.mark_step(tmp_path, "S1", "finished")


def test_a_missing_or_broken_plan_is_no_plan(tmp_path: Path) -> None:
    assert plan_store.load_current(tmp_path) is None
    directory = tmp_path / ".snowpea" / "plans"
    directory.mkdir(parents=True)
    (directory / "current.json").write_text("{not json", encoding="utf-8")
    assert plan_store.load_current(tmp_path) is None
    assert plan_store.prompt_line(tmp_path) == ""


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------


def _tool_ctx(tmp_path: Path, mode: str = "plan") -> tuple[ToolContext, list[Any]]:
    seen: list[Any] = []

    async def emit_event(_session_id: str, event: Any) -> None:
        seen.append(event)

    session = Session(id="s-plan", workdir=tmp_path, mode=mode)  # type: ignore[arg-type]
    core = SimpleNamespace(hub=SimpleNamespace(emit_event=emit_event))
    return ToolContext(session=session, core=core, backend=None), seen  # type: ignore[arg-type]


def test_both_tools_are_registered_and_allowed_in_plan_mode() -> None:
    registry = register_builtin_tools(ToolRegistry())
    policy = PermissionPolicy()
    for name in ("plan_save", "plan_update_step"):
        tool = registry.get(name)
        assert tool is not None
        assert policy.decide("plan", tool.permission, tool, {}) == "allow"


async def test_plan_save_then_update_step(tmp_path: Path) -> None:
    ctx, seen = _tool_ctx(tmp_path)
    result = await plan_tools.plan_save(
        ctx,
        {
            "title": "Voxel sandbox",
            "markdown": PLAN_MD,
            "steps": [{"title": "mesher"}, {"title": "camera"}],
            "source": "ralplan",
        },
    )
    assert result.ok, result.error
    assert "0/2 steps done" in result.output
    assert ctx.session.plan_saved is True
    assert seen and seen[-1][0] == "plan.updated"
    assert seen[-1][1]["total"] == 2 and seen[-1][1]["next"] == "S1 mesher"

    result = await plan_tools.plan_update_step(ctx, {"id": "S1", "status": "done", "note": "ok"})
    assert result.ok and "1/2 steps done" in result.output
    assert seen[-1][1]["done"] == 1

    refused = await plan_tools.plan_update_step(ctx, {"id": "S7", "status": "done"})
    assert not refused.ok and "S1, S2" in (refused.error or "")
    empty = await plan_tools.plan_save(ctx, {"title": "x", "markdown": ""})
    assert not empty.ok


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "vague"),
    [
        ("fix this", True),
        ("ralph fix this", True),
        ("개선해줘", True),
        ("게임 만들어줘", True),
        ('"improve performance"', True),
        ("improve performance", True),
        ("로그인 페이지에 소셜 로그인 버튼 추가해줘", False),
        ("결제 실패 시 재시도 로직 추가해줘", False),
        ("README 정리해줘", False),
        ("pytest 실패 고쳐줘", False),
        ("fix the flaky test", False),
        ("Upgrade to Python 3.12", False),
        ("add dark mode to settings", False),
        ("fix the null check in src/hooks/bridge.ts:326", False),
        ("add validation to processKeywordDetector", False),
        ("1. add X\n2. test Y", False),
        ("rename load_config()", False),
        ("fix #123", False),
        ("add a failing test then make it pass", False),
        ("README.md 오타 고쳐줘", False),
        ("", False),
    ],
)
def test_the_vague_detector(text: str, vague: bool) -> None:
    assert plan_gate.is_vague(text) is vague


@pytest.mark.parametrize(
    ("text", "execute"),
    [
        ("", True),
        ("실행해줘", True),
        ("구현해줘", True),
        ("진행해줘", True),
        ("시작해줘", True),
        ("이대로 해줘", True),
        ("그대로 진행", True),
        ("계획대로 실행해 주세요", True),
        ("go ahead", True),
        ("implement it", True),
        ("do it", True),
        ("run the plan", True),
        ("Voxel sandbox", True),
        ("그냥 구현해줘", True),
        ("전부 구현해줘", True),
        ("나머지 구현해줘", True),
        ("다음 단계 진행해줘", True),
        ("이어서 해줘", True),
        ("계속 진행해줘", True),
        ("S3 진행해줘", True),
        ("ㄱㄱ", True),
        ("--resume", True),
        ("add a test", False),
        ("게임 만들어줘", False),
        ("해줘", False),
    ],
)
def test_execute_only_phrases(text: str, execute: bool) -> None:
    plan = plan_store.Plan(id="p", title="Voxel sandbox")
    assert plan_gate.is_execute_request(text, plan) is execute


def test_the_bypass_spellings() -> None:
    assert plan_gate.strip_bypass("--force 개선해줘") == ("개선해줘", True)
    assert plan_gate.strip_bypass("개선해줘 --now") == ("개선해줘", True)
    assert plan_gate.strip_bypass("!개선해줘") == ("개선해줘", True)
    assert plan_gate.strip_bypass("개선해줘") == ("개선해줘", False)


# ---------------------------------------------------------------------------
# a daemon, for the commands
# ---------------------------------------------------------------------------


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


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


class _Scripted:
    """A provider answering every call with the next reply; records the prompts."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.preset = SimpleNamespace(supports_json_mode=False)

    async def stream(self, messages: list[Any], tools: list[Any], **_kwargs: Any) -> Any:
        from snowpea_core.providers.base import StreamEvent

        self.prompts.append("\n".join(str(message.content) for message in messages))
        text = self.replies.pop(0) if self.replies else ""
        if text:
            yield StreamEvent(kind="text_delta", text=text)


def _ok_subagents(core: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from snowpea_core.agent.subagent import SubagentResult, get_manager

    briefs: list[str] = []

    async def ok_run(_parent: Any, task: str, **_kwargs: Any) -> SubagentResult:
        briefs.append(task)
        return SubagentResult(agent_id=f"a-{len(briefs)}", ok=True, summary="done")

    monkeypatch.setattr(get_manager(core), "run", ok_run)
    return briefs


PRD_FROM_PLAN = json.dumps(
    {
        "stories": [
            {"id": "S1", "title": "mesher", "verify": ["true"], "plan_step": "S1"},
            {"id": "S2", "title": "camera", "verify": ["true"], "plan_step": ["S2"]},
            {"id": "S3", "title": "wire", "verify": ["true"], "plan_step": "S3",
             "depends_on": ["S1", "S2"]},
        ]
    }
)


async def _approve(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
    return True, "APPROVE"


# ---------------------------------------------------------------------------
# /ralph
# ---------------------------------------------------------------------------


async def test_ralph_execute_phrase_builds_the_prd_from_the_plan(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD, source="ralplan")
    session = await core.sessions.create(project, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    provider = _Scripted(PRD_FROM_PLAN)
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    _ok_subagents(core, monkeypatch)
    monkeypatch.setattr(ralph, "review", _approve)

    turn_id = await asyncio.wait_for(
        core.commands.run(core, session, "ralph", "실행해줘"), timeout=TIMEOUT
    )

    # The PRD request carried the plan, not the words "실행해줘".
    assert "## The plan" in provider.prompts[0]
    assert "S1 [pending] Add the chunk mesher" in provider.prompts[0]
    assert '"plan_step"' in provider.prompts[0]
    prd = json.loads((project / ".snowpea" / "ralph" / "prd.json").read_text("utf-8"))
    assert prd["task"].startswith("Voxel sandbox")
    assert [story["plan_step"] for story in prd["stories"]] == ["S1", "S2", "S3"]
    # Every story passed, so every step is done and the plan finished.
    plan = plan_store.load_current(project)
    assert plan is not None and plan.status == "done"
    assert all(step.status == "done" for step in plan.steps)
    updates = recorder.of_kind("plan.updated")
    assert updates and updates[-1]["payload"]["done"] == 3
    done = [e["payload"]["reason"] for e in recorder.of_kind("turn.done")
            if e["payload"]["turnId"] == turn_id]
    assert done == ["complete"]


async def test_ralph_skips_done_steps_and_marks_a_failure_in_progress(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    plan_store.mark_step(project, "S1", "done")
    session = await core.sessions.create(project, mode="auto")
    reply = json.dumps(
        {"stories": [{"id": "S1", "title": "camera", "verify": ["false"], "plan_step": "S2"}]}
    )
    provider = _Scripted(reply, json.dumps({"verify": ["false"]}))
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    _ok_subagents(core, monkeypatch)
    monkeypatch.setattr(ralph, "review", _approve)

    await asyncio.wait_for(core.commands.run(core, session, "ralph", ""), timeout=TIMEOUT)

    assert "S1 [done]" in provider.prompts[0] and "Skip steps marked done" in provider.prompts[0]
    plan = plan_store.load_current(project)
    assert plan is not None
    assert plan.step("S1").status == "done"
    assert plan.step("S2").status == "in_progress" and "false" in plan.step("S2").note
    assert plan.step("S3").status == "pending"


async def test_ralph_still_resumes_an_unfinished_prd_first(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    time.sleep(0.01)
    state = project / ".snowpea" / "ralph"
    state.mkdir(parents=True)
    (state / "prd.json").write_text(
        json.dumps(
            {
                "task": "the old task",
                "stories": [
                    {"id": "S1", "title": "old one", "verify": ["true"], "passed": True},
                    {"id": "S2", "title": "old two", "verify": ["true"], "passed": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    session = await core.sessions.create(project, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    provider = _Scripted()
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    _ok_subagents(core, monkeypatch)
    monkeypatch.setattr(ralph, "review", _approve)

    await asyncio.wait_for(core.commands.run(core, session, "ralph", "실행해줘"), timeout=TIMEOUT)

    assert "resuming .snowpea/ralph/prd.json" in recorder.texts()
    assert provider.prompts == []  # no new PRD was asked for
    plan = plan_store.load_current(project)
    assert plan is not None and all(step.status == "pending" for step in plan.steps)


async def test_a_plan_saved_after_the_prd_wins_over_resuming_it(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ralplan → ``/ralph 실행해줘`` runs the new plan, not an older unfinished PRD."""
    core = daemon.core
    assert core is not None
    state = project / ".snowpea" / "ralph"
    state.mkdir(parents=True)
    (state / "prd.json").write_text(
        json.dumps({"task": "old", "stories": [{"id": "S1", "title": "old", "passed": False}]}),
        encoding="utf-8",
    )
    old = time.time() - 60
    os.utime(state / "prd.json", (old, old))
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    session = await core.sessions.create(project, mode="auto")
    provider = _Scripted(PRD_FROM_PLAN)
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    _ok_subagents(core, monkeypatch)
    monkeypatch.setattr(ralph, "review", _approve)

    await asyncio.wait_for(core.commands.run(core, session, "ralph", "실행해줘"), timeout=TIMEOUT)

    assert provider.prompts and "## The plan" in provider.prompts[0]
    assert list(state.glob("prd-*.json")), "the replaced PRD is kept"


async def test_a_real_task_with_a_plan_gets_the_plan_as_context(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    session = await core.sessions.create(project, mode="auto")
    provider = _Scripted(json.dumps({"stories": [{"id": "S1", "title": "x", "verify": ["true"]}]}))
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    _ok_subagents(core, monkeypatch)
    monkeypatch.setattr(ralph, "review", _approve)

    await asyncio.wait_for(
        core.commands.run(core, session, "ralph", '"add a README badge"'), timeout=TIMEOUT
    )

    assert "Task: add a README badge" in provider.prompts[0]
    assert "for context" in provider.prompts[0] and "Voxel sandbox" in provider.prompts[0]
    prd = json.loads((project / ".snowpea" / "ralph" / "prd.json").read_text("utf-8"))
    assert prd["task"] == "add a README badge"
    assert "plan_step" not in prd["stories"][0]


# ---------------------------------------------------------------------------
# the gate on /ralph
# ---------------------------------------------------------------------------


def _answer(core: Any, monkeypatch: pytest.MonkeyPatch, label: str | None) -> list[Any]:
    asked: list[Any] = []

    async def ask(_session: Any, questions: list[Any], **_kwargs: Any) -> list[Answer]:
        asked.append(questions)
        if label is None:
            return [Answer(declined=True, by="origin")]
        return [Answer(selected=[label], by="origin")]

    monkeypatch.setattr(core.questions, "ask", ask)
    return asked


async def test_an_attended_vague_ralph_asks_and_queues_ralplan(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.agent import loop as agent_loop

    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    OriginConn(session)
    asked = _answer(core, monkeypatch, plan_gate.TEXT["ko"]["plan"])
    queued: list[str] = []
    monkeypatch.setattr(agent_loop, "start_turn", lambda _c, _s, text, **_k: queued.append(text))
    provider = _Scripted()
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)

    await asyncio.wait_for(core.commands.run(core, session, "ralph", "개선해줘"), timeout=TIMEOUT)

    assert asked and asked[0][0].options[0].label == "/ralplan 으로 계획부터 (추천)"
    assert queued == ["/ralplan 개선해줘"]
    assert provider.prompts == []  # nothing was planned or run


async def test_cancel_runs_nothing_and_run_as_typed_runs(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    OriginConn(session)
    _answer(core, monkeypatch, plan_gate.TEXT["en"]["cancel"])
    provider = _Scripted(json.dumps({"stories": [{"id": "S1", "title": "x", "verify": ["true"]}]}))
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    _ok_subagents(core, monkeypatch)
    monkeypatch.setattr(ralph, "review", _approve)

    await asyncio.wait_for(
        core.commands.run(core, session, "ralph", "improve performance"), timeout=TIMEOUT
    )
    assert provider.prompts == []

    _answer(core, monkeypatch, plan_gate.TEXT["en"]["run"])
    await asyncio.wait_for(
        core.commands.run(core, session, "ralph", "improve performance"), timeout=TIMEOUT
    )
    assert provider.prompts and "Task: improve performance" in provider.prompts[0]


@pytest.mark.parametrize("args", ["--force 개선해줘", "!개선해줘"])
async def test_the_bypass_skips_the_gate(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch, args: str
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    OriginConn(session)
    asked = _answer(core, monkeypatch, plan_gate.TEXT["ko"]["cancel"])
    provider = _Scripted(json.dumps({"stories": [{"id": "S1", "title": "x", "verify": ["true"]}]}))
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    _ok_subagents(core, monkeypatch)
    monkeypatch.setattr(ralph, "review", _approve)

    await asyncio.wait_for(core.commands.run(core, session, "ralph", args), timeout=TIMEOUT)
    assert asked == []
    assert "Task: 개선해줘" in provider.prompts[0]


async def test_unattended_and_planned_and_off_are_never_gated(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    asked = _answer(core, monkeypatch, plan_gate.TEXT["ko"]["cancel"])
    session = await core.sessions.create(project, mode="auto")  # no origin: unattended
    ctx = CommandContext(core=core, session=session, turn_id="t-1")
    assert await plan_gate.ask_before_running(ctx, "ralph", "개선해줘") is True

    OriginConn(session)
    core.settings.planning.gate = "off"
    assert await plan_gate.ask_before_running(ctx, "ralph", "개선해줘") is True
    core.settings.planning.gate = "ask"
    assert await plan_gate.ask_before_running(ctx, "ralph", "fix src/a.py") is True
    assert asked == []

    # With an active plan, a vague /ralph is not gated: it is planned with the
    # plan as context.
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    provider = _Scripted(json.dumps({"stories": [{"id": "S1", "title": "x", "verify": ["true"]}]}))
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    _ok_subagents(core, monkeypatch)
    monkeypatch.setattr(ralph, "review", _approve)
    await asyncio.wait_for(core.commands.run(core, session, "ralph", "개선해줘"), timeout=TIMEOUT)
    assert asked == []
    assert "Task: 개선해줘" in provider.prompts[0]
    assert "which this request may be about" in provider.prompts[0]


# ---------------------------------------------------------------------------
# /ultrawork, /team, /workers
# ---------------------------------------------------------------------------


class _Said:
    def __init__(self) -> None:
        self.lines: list[str] = []

    async def __call__(self, text: str) -> None:
        self.lines.append(text)


async def test_ultrawork_splits_the_plan_and_marks_its_steps(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    session = await core.sessions.create(project, mode="auto")
    reply = json.dumps(
        {
            "subtasks": [
                {"id": "T1", "title": "mesher", "task": "mesher", "plan_step": "S1"},
                {"id": "T2", "title": "camera+wire", "task": "rest", "plan_step": ["S2", "S3"]},
            ]
        }
    )
    provider = _Scripted(reply)
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    _ok_subagents(core, monkeypatch)
    ctx = CommandContext(core=core, session=session, turn_id="t-1")
    ctx.say = _Said()  # type: ignore[method-assign]

    await ultrawork.cmd_ultrawork(ctx, "go ahead")

    assert "## The plan" in provider.prompts[0] and '"plan_step"' in provider.prompts[0]
    plan = plan_store.load_current(project)
    assert plan is not None and plan.status == "done"


async def test_team_passes_the_plan_for_an_execute_only_task(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    session = await core.sessions.create(project, mode="auto")
    seen: list[tuple[str, dict[str, Any]]] = []

    async def fake_pipeline(*args: Any, **kwargs: Any) -> str:
        seen.append((args[2], kwargs))
        return "Nothing was left unfinished."

    monkeypatch.setattr(team_cmd, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(team_cmd, "_has_a_roster", lambda _ctx: True)
    ctx = CommandContext(core=core, session=session, turn_id="t-1")
    ctx.say = _Said()  # type: ignore[method-assign]

    await team_cmd.cmd_team(ctx, '"구현해줘"')
    await team_cmd.cmd_team(ctx, '"add a README badge"')

    (task, kwargs), (task2, kwargs2) = seen
    assert task.startswith("Voxel sandbox") and kwargs["from_plan"] is True
    assert kwargs["plan"].title == "Voxel sandbox"
    assert task2 == "add a README badge" and kwargs2["from_plan"] is False


async def test_workers_n_alone_runs_the_plan(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.commands import workers_cmd

    core = daemon.core
    assert core is not None
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    session = await core.sessions.create(project, mode="auto")
    seen: list[tuple[int, str, dict[str, Any]]] = []

    class FakeManager:
        async def start(self, _session: Any, workers: int, task: str, **kwargs: Any) -> str:
            seen.append((workers, task, kwargs))
            return "tm-test"

        async def status(self, _team_id: str | None = None) -> Any:
            return SimpleNamespace(tasks=[])

        async def wait(self, _team_id: str) -> None:
            return None

    monkeypatch.setattr(workers_cmd, "get_manager_for", lambda _core: FakeManager())
    ctx = CommandContext(core=core, session=session, turn_id="t-1")
    ctx.say = _Said()  # type: ignore[method-assign]

    await workers_cmd.cmd_workers(ctx, "3")
    workers, task, kwargs = seen[0]
    assert workers == 3 and task.startswith("Voxel sandbox") and kwargs["from_plan"] is True


async def test_the_team_planners_prompt_carries_the_plan(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.agent import team_pipeline
    from snowpea_core.agent.team import get_manager_for

    core = daemon.core
    assert core is not None
    plan = plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    session = await core.sessions.create(project, mode="auto")
    reply = json.dumps({"tasks": [{"id": "T1", "title": "mesher", "plan_step": "S1"},
                                  {"id": "T2", "title": "bogus", "plan_step": "S9"}]})
    provider = _Scripted(reply, reply)
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)

    pipeline = team_pipeline.TeamPipeline(core, session, plan=plan, from_plan=True)
    run = team_pipeline.PipelineRun(
        task=plan_gate.plan_label(plan),
        plan=team_pipeline.StagePlan(owners={team_pipeline.IMPLEMENT: "executor"}, unused=()),
    )
    await pipeline._plan(run, "")
    assert "## The plan" in provider.prompts[0]
    assert [task.plan_step for task in run.tasks] == [("S1",), ()]

    planned = await get_manager_for(core).plan(session, "go", 2, plan=plan, from_plan=True)
    assert "## The plan" in provider.prompts[1]
    assert [entry.plan_step for entry in planned] == [("S1",), ()]


async def test_the_pipeline_marks_steps_only_when_the_run_passed(
    daemon: Daemon, project: Path
) -> None:
    from snowpea_core.agent import team_pipeline

    core = daemon.core
    assert core is not None
    plan = plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    session = await core.sessions.create(project, mode="auto")
    pipeline = team_pipeline.TeamPipeline(core, session, plan=plan, from_plan=True)
    stages = team_pipeline.StagePlan(owners={team_pipeline.IMPLEMENT: "executor"}, unused=())
    run = team_pipeline.PipelineRun(task="t", plan=stages)
    run.tasks = [team_pipeline.PipelineTask("T1", "mesher", "b", plan_step=("S1",), ok=True)]
    await pipeline._sync_plan(run)  # tests and review never ran: not passed
    assert plan_store.load_current(project).step("S1").status == "in_progress"
    run.tests = team_pipeline.TESTS_PASS
    run.verdict = team_pipeline.APPROVE
    await pipeline._sync_plan(run)
    assert plan_store.load_current(project).step("S1").status == "done"


# ---------------------------------------------------------------------------
# plain prompts
# ---------------------------------------------------------------------------


def test_the_volatile_tier_carries_the_plan_line(tmp_path: Path) -> None:
    plan_store.save_plan(tmp_path, "Voxel sandbox", PLAN_MD)
    session = Session(id="s-1", workdir=tmp_path)
    line = agent_mod.plan_prompt_line(session)
    assert line.startswith(
        "Current plan: Voxel sandbox (saved just now by manual) — 0/3 steps done; next: S1"
    )
    tiers = compose.build_tiers(plan_line=line)
    assert line in tiers.volatile and line not in tiers.stable
    assert "plan_update_step" in tiers.stable  # the one stable instruction
    assert "plan_save the revised version" in tiers.stable
    # A role or a subagent is handed a brief, not the plan.
    assert "plan_update_step" not in compose.build_tiers(role="executor").stable
    assert "plan_update_step" not in compose.build_tiers(subagent=True).stable
    prompt = agent_mod.build_system_prompt(session, [])
    assert prompt.rstrip().endswith(line)

    child = Session(id="s-2", workdir=tmp_path, is_subagent=True)
    assert line not in agent_mod.build_system_prompt(child, [])


# ---------------------------------------------------------------------------
# plan mode
# ---------------------------------------------------------------------------


async def test_leaving_plan_mode_registers_the_plan_file_it_wrote(
    daemon: Daemon, project: Path
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="plan")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    docs = project / "docs"
    docs.mkdir()
    (docs / "voxel.md").write_text(PLAN_MD, encoding="utf-8")
    plan_store.note_written(session, "write_file", {"path": "docs/voxel.md"})
    plan_store.note_written(session, "write_file", {"path": "src/main.py"})  # not a plan
    assert session.plan_files == ["docs/voxel.md"]

    await core.sessions.set_mode(session, "accept")

    plan = plan_store.load_current(project)
    assert plan is not None and plan.title == "Voxel sandbox" and plan.source == "plan"
    assert len(plan.steps) == 3
    assert recorder.of_kind("plan.updated")


async def test_a_saved_plan_is_not_replaced_by_a_written_file(
    daemon: Daemon, project: Path
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="plan")
    (project / "notes.md").write_text("# Notes\n", encoding="utf-8")
    plans = project / ".snowpea" / "plans"
    plans.mkdir(parents=True)
    (plans / "draft.md").write_text(PLAN_MD, encoding="utf-8")
    plan_store.note_written(session, "write_file", {"path": ".snowpea/plans/draft.md"})
    plan_store.save_plan(project, "Saved one", "# Saved one\n\n## Steps\n1. a\n")
    session.plan_saved = True

    await core.sessions.set_mode(session, "auto")
    plan = plan_store.load_current(project)
    assert plan is not None and plan.title == "Saved one"


# ---------------------------------------------------------------------------
# REVISE round: one matcher, plan-keyed marks, attended chats, staleness,
# hand edits, plan-mode adoption, child sessions
# ---------------------------------------------------------------------------


def test_ralph_resume_and_run_the_plan_share_one_matcher() -> None:
    for phrase in ("그냥 구현해줘", "계속 진행해줘", "ㄱㄱ", "실행해줘", "continue", ""):
        assert ralph.is_resume_request(phrase) == plan_gate.is_carry_on(phrase) is True
    assert ralph.is_resume_request("add a test") is False


def test_a_phrase_naming_one_step_targets_it() -> None:
    plan = plan_store.Plan(
        id="p", title="T", steps=[plan_store.Step("S1", "a"), plan_store.Step("S3", "c")]
    )
    assert plan_gate.target_step("S3 진행해줘", plan) == "S3"
    assert plan_gate.target_step("s1 go ahead", plan) == "S1"
    assert plan_gate.target_step("S9 진행해줘", plan) is None
    assert plan_gate.target_step("구현해줘", plan) is None
    request = plan_gate.plan_request(plan, only="S3")
    assert "step S3 only" in request and "Skip steps marked done" not in request


def test_plan_context_calls_only_a_concrete_task_a_new_one() -> None:
    plan = plan_store.Plan(id="p", title="T", steps=[plan_store.Step("S1", "a")])
    assert "not this plan" in plan_gate.plan_context("add a README badge", plan)
    assert "not this plan" not in plan_gate.plan_context("개선해줘", plan)


async def test_marks_are_keyed_to_the_plan_they_started_from(tmp_path: Path) -> None:
    old = plan_store.save_plan(tmp_path, "Old", PLAN_MD)
    new = plan_store.save_plan(tmp_path, "New", PLAN_MD)
    assert old.id != new.id
    assert await plan_store.mark_steps(tmp_path, [("S1", "done", "x")], plan_id=old.id) is None
    assert plan_store.load_current(tmp_path).step("S1").status == "pending"
    for step in ("S1", "S2", "S3"):
        await plan_store.mark_steps(tmp_path, [(step, "done", "")], plan_id=new.id)
    assert plan_store.load_current(tmp_path).status == "done"
    # A finished plan is never reopened by a command's late failure report.
    assert (
        await plan_store.mark_steps(tmp_path, [("S1", "in_progress", "late")], plan_id=new.id)
        is None
    )
    assert plan_store.load_current(tmp_path).step("S1").status == "done"


async def test_a_plan_replaced_mid_run_keeps_its_steps(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.agent.subagent import SubagentResult, get_manager

    core = daemon.core
    assert core is not None
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    session = await core.sessions.create(project, mode="auto")
    provider = _Scripted(PRD_FROM_PLAN)
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    monkeypatch.setattr(ralph, "review", _approve)

    async def replacing_run(_parent: Any, _task: str, **_kwargs: Any) -> SubagentResult:
        if plan_store.load_current(project).title == "Voxel sandbox":
            plan_store.save_plan(project, "Something else", "# Else\n\n## Steps\n1. x\n2. y\n")
        return SubagentResult(agent_id="a", ok=True, summary="done")

    monkeypatch.setattr(get_manager(core), "run", replacing_run)
    await asyncio.wait_for(core.commands.run(core, session, "ralph", "구현해줘"), timeout=TIMEOUT)

    plan = plan_store.load_current(project)
    assert plan is not None and plan.title == "Something else"
    assert all(step.status == "pending" for step in plan.steps)
    prd = json.loads((project / ".snowpea" / "ralph" / "prd.json").read_text("utf-8"))
    assert all(story["plan_id"] != plan.id for story in prd["stories"])


async def test_ralph_resumes_a_prd_of_the_same_plan_with_its_progress(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    plan = plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    state = project / ".snowpea" / "ralph"
    state.mkdir(parents=True)
    (state / "prd.json").write_text(
        json.dumps(
            {
                "task": plan_gate.plan_label(plan),
                "stories": [
                    {"id": "S1", "title": "mesher", "verify": ["true"], "passed": True,
                     "plan_step": "S1", "plan_id": plan.id},
                    {"id": "S2", "title": "camera", "verify": ["true"], "passed": False,
                     "plan_step": "S2", "plan_id": plan.id},
                ],
            }
        ),
        encoding="utf-8",
    )
    # Even written "before" the plan, a PRD of this very plan is resumed.
    old = time.time() - 600
    os.utime(state / "prd.json", (old, old))
    session = await core.sessions.create(project, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    provider = _Scripted()
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    briefs = _ok_subagents(core, monkeypatch)
    monkeypatch.setattr(ralph, "review", _approve)

    await asyncio.wait_for(core.commands.run(core, session, "ralph", "구현해줘"), timeout=TIMEOUT)

    assert "1/2 stories already pass" in recorder.texts()
    assert provider.prompts == []
    assert len(briefs) == 1 and "camera" in briefs[0]  # only the unfinished story ran
    current = plan_store.load_current(project)
    assert current.step("S1").status == "done" and current.step("S2").status == "done"


async def test_ralph_on_one_named_step_and_its_fallback_title(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD, source="ralplan")
    session = await core.sessions.create(project, mode="auto")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    provider = _Scripted()  # no usable PRD at all: the fallback story
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    _ok_subagents(core, monkeypatch)
    monkeypatch.setattr(ralph, "review", _approve)

    await asyncio.wait_for(
        core.commands.run(core, session, "ralph", "S2 진행해줘"), timeout=TIMEOUT
    )

    assert "step S2 only" in provider.prompts[0]
    assert '"Voxel sandbox" (saved just now by ralplan)' in recorder.texts()
    prd = json.loads((project / ".snowpea" / "ralph" / "prd.json").read_text("utf-8"))
    assert [story["title"] for story in prd["stories"]] == ["S2 of Voxel sandbox"]
    assert prd["stories"][0]["plan_step"] == "S2"
    plan = plan_store.load_current(project)
    assert [step.status for step in plan.steps] == ["pending", "done", "pending"]


def test_the_summary_says_how_old_the_plan_is_and_who_made_it() -> None:
    from datetime import UTC, datetime

    plan = plan_store.Plan(
        id="p", title="T", source="ralplan", createdAt="2026-10-01T00:00:00.000Z",
        steps=[plan_store.Step("S1", "a")],
    )
    now = datetime(2026, 10, 4, 1, 0, tzinfo=UTC)
    line = plan_store.summary_line(plan, now)
    assert line.startswith("Current plan: T (saved 3d ago by ralplan)")
    assert plan_store.age(plan, datetime(2026, 10, 1, 5, 0, tzinfo=UTC)) == "5h ago"


async def test_a_plan_can_be_abandoned_through_the_tool_and_plan_clear(
    daemon: Daemon, project: Path
) -> None:
    ctx, seen = _tool_ctx(project, mode="accept")
    plan_store.save_plan(project, "Voxel sandbox", PLAN_MD)
    result = await plan_tools.plan_save(ctx, {"clear": True})
    assert result.ok and "cleared" in result.output
    assert plan_store.load_current(project) is None
    assert seen[-1][1]["status"] == "archived"
    assert list((project / ".snowpea" / "plans" / "archive").glob("*-voxel-sandbox.md"))

    core = daemon.core
    assert core is not None
    plan_store.save_plan(project, "Second", "# Second\n\n## Steps\n1. a\n")
    session = await core.sessions.create(project, mode="accept")
    recorder = Recorder()
    core.hub.subscribe(recorder, session.id)
    await asyncio.wait_for(core.commands.run(core, session, "plan", "clear"), timeout=TIMEOUT)
    assert plan_store.load_current(project) is None
    assert session.mode == "accept"  # /plan clear does not enter plan mode
    assert "Cleared the current plan" in recorder.texts()


def test_saving_writes_the_new_plan_and_keeps_a_copy_of_the_old(tmp_path: Path) -> None:
    plan_store.save_plan(tmp_path, "First", PLAN_MD)
    plan_store.save_plan(tmp_path, "Second", "# Second\n\n## Steps\n1. only\n")
    directory = tmp_path / ".snowpea" / "plans"
    assert not list(directory.glob("*.tmp"))
    assert plan_store.load_current(tmp_path).title == "Second"
    assert len(list((directory / "archive").glob("*-first.md"))) == 1


def test_a_hand_edit_rederives_the_steps_and_keeps_their_status(tmp_path: Path) -> None:
    plan_store.save_plan(tmp_path, "Voxel sandbox", PLAN_MD)
    first = plan_store.load_current(tmp_path).steps[0].title
    plan_store.mark_step(tmp_path, "S1", "done", "ok")
    md = tmp_path / ".snowpea" / "plans" / "current.md"
    md.write_text(
        PLAN_MD.replace("3. Wire both", "3. Add a sky box — files: src/sky.ts\n4. Wire both"),
        encoding="utf-8",
    )
    plan = plan_store.load_current(tmp_path)
    assert [step.id for step in plan.steps] == ["S1", "S2", "S3", "S4"]
    assert plan.steps[0].title == first and plan.steps[0].status == "done"
    assert plan.steps[2].title.startswith("Add a sky box") and plan.steps[2].status == "pending"
    state = json.loads((tmp_path / ".snowpea" / "plans" / "current.json").read_text("utf-8"))
    assert len(state["steps"]) == 4

    # Steps the caller gave are the caller's: an edit changes only the hash.
    plan_store.save_plan(tmp_path, "Given", "# Given\n\n1. x\n", steps=["mine"])
    md.write_text("# Given\n\n1. x\n2. y\n", encoding="utf-8")
    assert [step.title for step in plan_store.load_current(tmp_path).steps] == ["mine"]


async def test_entering_plan_mode_starts_a_fresh_pass_and_accepts_txt(
    daemon: Daemon, project: Path
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="accept")
    session.plan_saved = True
    session.plan_files.append("old.md")
    await core.sessions.set_mode(session, "plan")
    assert session.plan_saved is False and session.plan_files == []

    (project / "PLAN.txt").write_text("Steps:\n1. first\n2. second\n", encoding="utf-8")
    plan_store.note_written(session, "write_file", {"path": "PLAN.txt"})
    plan_store.note_written(session, "write_file", {"path": "src/a.py"})
    assert session.plan_files == ["PLAN.txt"]
    await core.sessions.set_mode(session, "accept")
    plan = plan_store.load_current(project)
    assert plan is not None and [step.title for step in plan.steps] == ["first", "second"]


async def test_a_ralplan_turn_without_plan_save_still_leaves_its_plan(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.agent import loop as agent_loop
    from snowpea_core.providers.base import ChatMessage

    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="plan")

    async def fake_turn(_core: Any, sess: Any, *_args: Any, **_kwargs: Any) -> None:
        sess.history.append(ChatMessage(role="user", content="/ralplan voxel"))
        sess.history.append(ChatMessage(role="assistant", content=PLAN_MD))
        sess.history.append(ChatMessage(role="assistant", content="queued: /ralph"))

    monkeypatch.setattr(agent_loop, "run_turn", fake_turn)
    await asyncio.wait_for(core.commands.run(core, session, "ralplan", "voxel"), timeout=TIMEOUT)

    plan = plan_store.load_current(project)
    assert plan is not None and plan.source == "ralplan" and plan.title == "Voxel sandbox"
    assert len(plan.steps) == 3


async def test_children_may_not_touch_the_plan(tmp_path: Path) -> None:
    ctx, _seen = _tool_ctx(tmp_path, mode="auto")
    ctx.session.is_subagent = True
    plan_store.save_plan(tmp_path, "Voxel sandbox", PLAN_MD)
    saved = await plan_tools.plan_save(ctx, {"title": "x", "markdown": "# x\n1. y\n"})
    marked = await plan_tools.plan_update_step(ctx, {"id": "S1", "status": "done"})
    assert not saved.ok and not marked.ok
    assert plan_store.load_current(tmp_path).title == "Voxel sandbox"


def test_attended_means_a_person_can_answer() -> None:
    from snowpea_core.gateway.router import Binding, GatewayConnection, GatewayRouter

    router = GatewayRouter()
    gated = SimpleNamespace(
        id="s-chat", unattended=False, origin_conn=None, origin_surface="gateway:telegram:42"
    )
    with_approver = Binding(id="b1", platform="telegram", credentials_ref="r", user_id="7")
    conn = GatewayConnection(router, with_approver, "42")
    conn.session_id = gated.id
    router._conns[("b1", "42")] = conn
    core = SimpleNamespace(gateway=router)
    assert plan_gate.attended(gated, core) is True

    no_approver = Binding(id="b2", platform="telegram", credentials_ref="r")
    other = SimpleNamespace(
        id="s-open", unattended=False, origin_conn=None, origin_surface="gateway:telegram:9"
    )
    conn2 = GatewayConnection(router, no_approver, "9")
    conn2.session_id = other.id
    router._conns[("b2", "9")] = conn2
    assert plan_gate.attended(other, core) is False

    scheduled = SimpleNamespace(id="s-job", unattended=True, origin_conn=None, origin_surface=None)
    assert plan_gate.attended(scheduled, core) is False
    tui = SimpleNamespace(id="s-tui", unattended=False, origin_conn=object())
    assert plan_gate.attended(tui, core) is True


async def test_a_gateway_chat_with_an_approver_is_gated(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    session.origin_surface = "gateway:telegram:42"
    monkeypatch.setattr(core, "gateway", SimpleNamespace(has_approver=lambda _s: True))
    asked = _answer(core, monkeypatch, plan_gate.TEXT["ko"]["cancel"])
    provider = _Scripted()
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: provider)
    await asyncio.wait_for(core.commands.run(core, session, "ralph", "개선해줘"), timeout=TIMEOUT)
    assert asked and provider.prompts == []

    session.unattended = True  # what a scheduled run is
    asked.clear()
    provider.replies = [json.dumps({"stories": [{"id": "S1", "title": "x", "verify": ["true"]}]})]
    _ok_subagents(core, monkeypatch)
    monkeypatch.setattr(ralph, "review", _approve)
    await asyncio.wait_for(core.commands.run(core, session, "ralph", "개선해줘"), timeout=TIMEOUT)
    assert asked == [] and provider.prompts


@pytest.mark.parametrize("args", ['2 "개선해줘"'])
async def test_workers_and_team_n_meet_the_gate_without_a_plan(
    daemon: Daemon, project: Path, monkeypatch: pytest.MonkeyPatch, args: str
) -> None:
    from snowpea_core.commands import workers_cmd

    core = daemon.core
    assert core is not None
    session = await core.sessions.create(project, mode="auto")
    OriginConn(session)
    asked = _answer(core, monkeypatch, plan_gate.TEXT["ko"]["cancel"])
    started: list[Any] = []
    monkeypatch.setattr(workers_cmd, "get_manager_for", lambda _core: started.append(1))
    ctx = CommandContext(core=core, session=session, turn_id="t-1")
    ctx.say = _Said()  # type: ignore[method-assign]

    await workers_cmd.cmd_workers(ctx, args)
    await team_cmd.cmd_team(ctx, args)  # /team N forwards to /workers
    assert len(asked) == 2 and started == []
