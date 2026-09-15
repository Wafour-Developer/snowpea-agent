"""Team pipeline mode: the project's roster, staged (M6/M7 §9).

``/team 3 "…"`` runs three *identical* workers in three git worktrees
(:mod:`snowpea_core.agent.team`).  ``/team "…"`` — no number — runs the
**members of the active project team, by role**, as ordinary subagents in the
session's own checkout::

    explore? -> plan -> implement (xN) -> test? -> verify? -> review? -> fix? -> review?

Each stage is one :meth:`~snowpea_core.agent.subagent.SubagentManager.run`, so
the ``subagent.*`` events a TUI or IDE already renders show the whole pipeline
as a tree under the lead.  There are no worktrees: the implement stage is
file-scoped instead, tasks that claim the same file are merged into one before
anything starts, and :mod:`snowpea_core.tools.file_state`'s sibling-ownership
guard is the safety net under that.

Stage owners come from the roster, never from the model: the active team
(``agents.activeTeam``), the roster ``/team <name> "…"`` names for one run, or
the global ``agents.default_team`` roster.  An implementer is required; every
other stage is skipped when the roster has nobody for it, and the lead plans
for itself when there is no planner.

The shape (staged pipeline, pre-assigned owners, file-scoped subtasks, short
handoff documents between stages, a bounded fix loop) is ported from
oh-my-claudecode's ``team`` skill; see
``docs/design/deviations/CORE-team-pipeline.md``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.agent import reply_language
from snowpea_core.agent.definition import complete_text, parse_generated_json
from snowpea_core.agent.subagent import SubagentManager, SubagentResult, get_manager
from snowpea_core.agent.team import git
from snowpea_core.agent.team_config import active_team, default_roster, teams_with_source
from snowpea_core.prompts.compose import workflow_brief
from snowpea_core.prompts.loader import load
from snowpea_core.providers.base import ChatMessage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.agent.team_pipeline")

#: The stages, in the order they run.
EXPLORE = "explore"
PLAN = "plan"
IMPLEMENT = "implement"
TEST = "test"
VERIFY = "verify"
REVIEW = "review"
FIX = "fix"

STAGE_ORDER: tuple[str, ...] = (EXPLORE, PLAN, IMPLEMENT, TEST, VERIFY, REVIEW)

#: Stage -> the roster names that may own it, best first.  A roster member
#: that matches none of these owns no stage and is reported as unused.
STAGE_AGENTS: dict[str, tuple[str, ...]] = {
    PLAN: ("architect", "planner"),
    EXPLORE: ("explore", "explorer"),
    IMPLEMENT: ("executor",),
    TEST: ("test-engineer",),
    VERIFY: ("verifier",),
    REVIEW: ("critic", "reviewer"),
}

#: The one stage that cannot be skipped: with nobody to write the code there
#: is no pipeline, only a plan.
REQUIRED_STAGES: frozenset[str] = frozenset({IMPLEMENT})

#: Ceiling on the task list, whatever ``team.pipeline.maxTasks`` says.
MAX_TASKS = 8

#: How much of the change the reviewer is handed inline.
MAX_REVIEW_DIFF_CHARS = 24000

#: Lines kept from one stage's report when it becomes the next stage's handoff.
HANDOFF_LINES = 20

#: Verdict tokens the test and review stages answer with.
APPROVE = "APPROVE"
REQUEST_CHANGES = "REQUEST_CHANGES"
NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"
TESTS_FAIL = "TESTS: FAIL"
TESTS_PASS = "TESTS: PASS"
VERIFY_FAIL = "VERIFY: FAIL"
VERIFY_PASS = "VERIFY: PASS"

#: Review rounds at most: one review, one fix, one re-review.  A third round is
#: not a disagreement the team can settle, so it is reported as unfinished.
MAX_REVIEW_ROUNDS = 2

#: What the second review is told, so it judges the fix rather than re-reading
#: the change cold and repeating findings the implementer has already answered.
SECOND_LOOK = (
    "This is a second look: the implementer has since fixed what you raised. "
    "Judge whether your findings were answered, not whether you would have "
    "written it this way."
)

PLAN_SYSTEM_NAME = "workflows/team-pipeline-plan"


class PipelineError(RuntimeError):
    """The pipeline could not start: no implementer, no task, no plan."""


# ---------------------------------------------------------------------------
# roster -> stages
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StagePlan:
    """Who owns each stage, and who on the roster owns nothing."""

    #: Stage -> agent name; a stage with no owner is absent from the mapping.
    owners: dict[str, str]
    #: Roster members that fill no stage, in roster order.
    unused: tuple[str, ...]
    #: Where the roster came from, for the opening line.
    source: str = "default"

    def owner(self, stage: str) -> str | None:
        return self.owners.get(stage)

    def rows(self) -> list[tuple[str, str]]:
        """``[(stage, agent)]`` in pipeline order, for ``/team list``."""
        return [(stage, self.owners[stage]) for stage in STAGE_ORDER if stage in self.owners]


def stage_assignments(
    roster: Sequence[str], known: Collection[str] | None = None, *, source: str = "default"
) -> StagePlan:
    """Map a roster onto the pipeline stages, best candidate per stage.

    ``known`` is the set of agent names that actually resolve here; a roster
    entry naming an agent that no longer exists is treated as absent rather
    than assigned a stage the delegation would then refuse.  Passing ``None``
    skips that check, which is what the unit tests want.
    """
    members = [name for name in dict.fromkeys(str(n).strip() for n in roster) if name]
    if known is not None:
        members = [name for name in members if name in known]
        available = set(members)
    else:
        available = set(members)
    owners: dict[str, str] = {}
    taken: set[str] = set()
    for stage in STAGE_ORDER:
        for candidate in STAGE_AGENTS[stage]:
            if candidate in available and candidate not in taken:
                owners[stage] = candidate
                taken.add(candidate)
                break
    missing = sorted(REQUIRED_STAGES - set(owners))
    if missing:
        wanted = ", ".join(
            f"'{STAGE_AGENTS[stage][0]}'" for stage in missing if STAGE_AGENTS.get(stage)
        )
        raise PipelineError(
            f"the team roster has no {', '.join(missing)} agent: add {wanted} to it "
            f"(roster: {', '.join(members) or 'empty'}). "
            "Use /team <N> \"<task>\" for the worktree workers instead."
        )
    unused = tuple(name for name in members if name not in taken)
    return StagePlan(owners=owners, unused=unused, source=source)


def roster_for(core: Core, session: Session) -> tuple[list[str], str]:
    """The roster this session's pipeline runs, and what to call it.

    The project's active team, else the global ``agents.default_team`` roster.
    An empty result means the user has no team at all, which the command turns
    into a hint rather than a run.
    """
    team = active_team(core.settings, session.workdir)
    if team is not None:
        return list(team.agents), team.name
    return list(default_roster(core.settings, session.workdir)), (
        core.settings.agents.default_team or "default"
    )


def named_roster(core: Core, workdir: Path | str, name: str) -> list[str] | None:
    """The members of team ``name`` — project first, then global — or ``None``.

    This is what ``/team <name> "<task>"`` runs on for one invocation, so a
    global ("external") team is as usable as the project's own without the
    project having to adopt it.
    """
    entry = teams_with_source(core.settings, workdir).get(name)
    return None if entry is None else list(entry[0])


def plan_for(
    core: Core,
    session: Session,
    roster: Sequence[str] | None = None,
    *,
    source: str | None = None,
) -> StagePlan:
    """:func:`stage_assignments` over a roster and the definitions visible here.

    ``roster`` overrides what the session would have used, which is how
    ``/team <name> "<task>"`` runs a team the project has not adopted.
    """
    from snowpea_core.commands.agent_cmd import definitions_for

    if roster is None:
        members, default_source = roster_for(core, session)
    else:
        members, default_source = list(roster), source or "named"
    known = {defn.name for defn in definitions_for(core, session.workdir)}
    return stage_assignments(members, known, source=source or default_source)


def stages_map(core: Core, workdir: Path | str, roster: Sequence[str]) -> dict[str, str]:
    """``{stage: member}`` for one roster, or ``{}`` when it has no implementer.

    The reporting counterpart of :func:`stage_assignments`: ``agent.list`` has
    to describe every team the user might pick, including the ones that cannot
    run a pipeline, so this answers rather than raising.
    """
    from snowpea_core.commands.agent_cmd import definitions_for

    known = {defn.name for defn in definitions_for(core, workdir)}
    try:
        return dict(stage_assignments(roster, known).rows())
    except PipelineError:
        return {}


# ---------------------------------------------------------------------------
# the task list
# ---------------------------------------------------------------------------


@dataclass
class PipelineTask:
    """One file-scoped unit of the implement stage."""

    id: str
    title: str
    brief: str
    files: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    #: Ids folded into this one because they claimed the same files.
    merged: tuple[str, ...] = ()
    #: Filled in as the pipeline runs.
    report: str = ""
    ok: bool = False

    @property
    def exclusive(self) -> bool:
        """True when nothing may run beside it: it claims no file to be disjoint from."""
        return not self.files


def _files(entry: dict[str, Any]) -> tuple[str, ...]:
    """The ``files`` list of one planner entry, normalised for comparison."""
    raw = entry.get("files")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: dict[str, None] = {}
    for item in raw:
        text = str(item).strip().lstrip("./")
        if text:
            seen.setdefault(text, None)
    return tuple(seen)


def _deps(entry: dict[str, Any]) -> list[str]:
    raw = entry.get("dependsOn") or entry.get("depends_on") or entry.get("deps") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def tasks_from_payload(
    data: dict[str, Any], fallback: str, *, max_tasks: int = MAX_TASKS
) -> list[PipelineTask]:
    """The planner's JSON as an ordered, acyclic task list.

    A dependency is kept only when it names a task listed *before* this one:
    the planner was asked for an ordered list, and honouring a forward or
    circular reference would leave the run with nothing ready to start.
    """
    raw: list[Any] = []
    for key in ("tasks", "subtasks"):
        value = data.get(key)
        if isinstance(value, list):
            raw = value
            break
    out: list[PipelineTask] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw[: max(1, max_tasks)], start=1):
        if isinstance(entry, str):
            text = entry.strip()
            if text:
                out.append(PipelineTask(f"T{index}", text, text))
            continue
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or entry.get("task") or entry.get("name") or "").strip()
        brief = str(entry.get("brief") or entry.get("task") or title).strip()
        if not (title or brief):
            continue
        task_id = str(entry.get("id") or f"T{index}").strip() or f"T{index}"
        while task_id in seen:
            task_id = f"{task_id}x"
        seen.add(task_id)
        earlier = {task.id for task in out}
        out.append(
            PipelineTask(
                id=task_id,
                title=title or brief,
                brief=brief or title,
                files=_files(entry),
                depends_on=tuple(dep for dep in _deps(entry) if dep in earlier),
            )
        )
    if not out:
        out = [PipelineTask("T1", fallback, fallback)]
    return out


def merge_overlapping(tasks: list[PipelineTask]) -> list[PipelineTask]:
    """Fold tasks that claim the same file into one brief (M15 §C4).

    Ported from ``/ultrawork``'s splitter validation: without a worktree each,
    two agents editing one file is how a run ends with half of one change and
    half of the other.  The plan is a proposal, so it is checked here rather
    than trusted.  Tasks that share no file are left exactly as planned, and a
    merged task inherits both sets of dependencies.
    """
    owner: dict[str, int] = {}
    groups: list[PipelineTask] = []
    for task in tasks:
        target = next((owner[name] for name in task.files if name in owner), None)
        if target is None:
            owner.update({name: len(groups) for name in task.files})
            groups.append(task)
            continue
        head = groups[target]
        shared = sorted(name for name in task.files if owner.get(name) == target)
        merged_ids = (*head.merged, task.id)
        groups[target] = PipelineTask(
            id=head.id,
            title=head.title,
            brief=(
                f"{head.brief}\n\n"
                "These two pieces of work were merged because they change the same "
                f"file(s) ({', '.join(shared)}); do both, in one pass, yourself:\n\n"
                f"{task.brief}"
            ),
            files=tuple(dict.fromkeys((*head.files, *task.files))),
            depends_on=tuple(
                dep
                for dep in dict.fromkeys((*head.depends_on, *task.depends_on))
                if dep != head.id and dep not in merged_ids
            ),
            merged=merged_ids,
        )
        owner.update({name: target for name in task.files})
        log.info(
            "team pipeline merged %s into %s: both claim %s",
            task.id,
            head.id,
            ", ".join(shared) or "the same files",
        )
    gone = {merged for task in groups for merged in task.merged}
    for task in groups:
        task.depends_on = tuple(dep for dep in task.depends_on if dep not in gone)
    return groups


def waves(tasks: Sequence[PipelineTask], limit: int) -> list[list[PipelineTask]]:
    """Group the task list into batches that may run at the same time.

    A batch holds tasks whose dependencies have all landed in an earlier batch
    and whose file sets are disjoint, up to ``limit``.  A task that claims no
    file gets a batch to itself: nothing can be proved disjoint from it.
    """
    remaining = list(tasks)
    done: set[str] = set()
    batches: list[list[PipelineTask]] = []
    width = max(1, int(limit))
    while remaining:
        ready = [task for task in remaining if set(task.depends_on) <= done]
        if not ready:  # pragma: no cover - parsing drops forward references
            ready = [remaining[0]]
        batch: list[PipelineTask] = []
        claimed: set[str] = set()
        for task in ready:
            if len(batch) >= width:
                break
            if task.exclusive:
                if batch:
                    continue
                batch = [task]
                break
            if claimed & set(task.files):
                continue
            batch.append(task)
            claimed |= set(task.files)
        batches.append(batch)
        for task in batch:
            remaining.remove(task)
            done.add(task.id)
    return batches


# ---------------------------------------------------------------------------
# handoffs
# ---------------------------------------------------------------------------


def handoff(stage: str, text: str, *, limit: int = HANDOFF_LINES) -> str:
    """One stage's report, trimmed to what the next stage has to know.

    Deliberately small (M15 §C: a report, not a transcript).  The lead carries
    it forward verbatim rather than re-summarising it, so nothing is invented
    between stages.
    """
    body = "\n".join(line for line in (text or "").strip().splitlines() if line.strip())
    if not body:
        return ""
    lines = body.splitlines()
    if len(lines) > limit:
        lines = [*lines[:limit], f"… ({len(body.splitlines()) - limit} more lines)"]
    return f"What {stage} handed over:\n" + "\n".join(lines)


def _bullets(items: Iterable[str]) -> str:
    rows = [f"- {item}" for item in items]
    return "\n".join(rows) if rows else "- (none named)"


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    """What one stage did, for the final report."""

    stage: str
    agent: str
    ok: bool
    text: str = ""


@dataclass
class PipelineRun:
    """Everything one ``/team "<task>"`` invocation accumulates."""

    task: str
    plan: StagePlan
    tasks: list[PipelineTask] = field(default_factory=list)
    stages: list[StageResult] = field(default_factory=list)
    review_rounds: int = 0
    verdict: str = ""
    tests: str = ""
    verification: str = ""
    unfinished: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def changed_files(self) -> list[str]:
        seen: dict[str, None] = {}
        for task in self.tasks:
            for name in task.files:
                seen.setdefault(name, None)
        return list(seen)

    def passed(self) -> bool:
        return not _unfinished(self)


class TeamPipeline:
    """Runs one staged pipeline over a roster.

    ``roster`` defaults to what the session would use (its active team, else
    the global default roster).  ``/team <name> "<task>"`` passes one
    explicitly, and the run then delegates through a stand-in parent carrying
    that roster so the §3.1 membership guard admits its members for this run
    only — the project's own ``activeTeam`` is never written.
    """

    def __init__(
        self,
        core: Core,
        session: Session,
        roster: Sequence[str] | None = None,
        *,
        source: str | None = None,
    ) -> None:
        self.core = core
        self.session = session
        self.roster = None if roster is None else list(roster)
        self.source = source
        self.manager: SubagentManager = get_manager(core)
        self.language = reply_language(core)
        #: Replaced in :meth:`run` once the roster is known.
        self.anchor: Session = session

    # -- settings ------------------------------------------------------
    @property
    def _settings(self) -> Any:
        return getattr(self.core.settings.team, "pipeline", None)

    def max_tasks(self) -> int:
        value: Any = getattr(self._settings, "maxTasks", None)
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            number = MAX_TASKS
        return max(1, min(MAX_TASKS, number))

    def _switch(self, name: str, stage: str, plan: StagePlan) -> bool:
        """``team.pipeline.<name>``: unset means "on when the roster has one"."""
        value = getattr(self._settings, name, None)
        if value is None:
            return plan.owner(stage) is not None
        return bool(value) and plan.owner(stage) is not None

    # -- entry point ---------------------------------------------------
    async def run(self, task: str, *, say: Any) -> str:
        """Run the whole pipeline; returns the report the lead answers with."""
        brief = (task or "").strip()
        if not brief:
            raise PipelineError("team mode needs a task")
        plan = plan_for(self.core, self.session, self.roster, source=self.source)
        self.anchor = self._anchor(plan)
        run = PipelineRun(task=brief, plan=plan)

        rows = ", ".join(f"{stage}={agent}" for stage, agent in plan.rows())
        opening = [f"team pipeline on roster '{plan.source}': {rows or 'no stages'}."]
        if plan.unused:
            opening.append(f"  not used by any stage: {', '.join(plan.unused)}")
        await say("\n".join(opening))

        findings = await self._explore(run)
        await self._plan(run, findings)
        if not run.tasks:
            raise PipelineError("the plan stage did not produce any work items")
        await say(
            "\n".join(
                [f"{len(run.tasks)} tasks:", *(f"  {t.id} {t.title}" for t in run.tasks)]
            )
        )

        await self._implement(run, findings)
        await self._test(run)
        await self._verify(run)
        await self._review(run)
        return self.report(run)

    # -- stages --------------------------------------------------------
    async def _explore(self, run: PipelineRun) -> str:
        agent = run.plan.owner(EXPLORE)
        if agent is None:
            return ""
        result = await self._delegate(
            agent,
            workflow_brief(
                "team-pipeline-explore",
                reply_language=self.language,
                TASK=run.task,
                WORKDIR=self.session.workdir,
            ),
            title=EXPLORE,
        )
        text = _text(result)
        run.stages.append(StageResult(EXPLORE, agent, result.ok, text))
        return handoff(EXPLORE, text) if result.ok else ""

    async def _plan(self, run: PipelineRun, findings: str) -> None:
        system = load(PLAN_SYSTEM_NAME).replace("${MAX_TASKS}", str(self.max_tasks()))
        instruction = (
            f"Plan this task for the project at {self.session.workdir}. "
            f"Reply with the JSON object only.\n\nTask: {run.task}"
        )
        if findings:
            instruction = f"{instruction}\n\n{findings}"
        agent = run.plan.owner(PLAN)
        if agent is None:
            # No planner on the roster: the lead plans for itself, one plain
            # provider call, exactly as ``/team N`` does.
            provider = self.core.providers.get(self.session.provider, self.session.model)
            text = await complete_text(
                provider,
                [
                    ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=instruction),
                ],
            )
            ok = True
        else:
            result = await self._delegate(
                agent, f"{system}\n\n{instruction}", title=PLAN
            )
            text, ok = _text(result), result.ok
        run.stages.append(StageResult(PLAN, agent or "lead", ok, text))
        if not ok:
            raise PipelineError(f"the plan stage failed: {text[:200] or 'no reason given'}")
        try:
            payload = parse_generated_json(text)
        except Exception as exc:  # noqa: BLE001 - a bad plan is a failed run
            raise PipelineError(f"the plan stage did not return a task list: {exc}") from exc
        run.tasks = merge_overlapping(
            tasks_from_payload(payload, run.task, max_tasks=self.max_tasks())
        )

    async def _implement(self, run: PipelineRun, findings: str) -> None:
        agent = run.plan.owners[IMPLEMENT]
        previous = findings
        for batch in waves(run.tasks, self.manager.limit_for(self.session)):
            results = await asyncio.gather(
                *(self._implement_one(run, agent, task, previous) for task in batch)
            )
            previous = handoff(
                IMPLEMENT,
                "\n".join(f"{task.id}: {text}" for task, text in zip(batch, results, strict=True)),
            )

    async def _implement_one(
        self, run: PipelineRun, agent: str, task: PipelineTask, previous: str
    ) -> str:
        result = await self._delegate(
            agent,
            workflow_brief(
                "team-pipeline-task",
                reply_language=self.language,
                TASK=run.task,
                TASK_ID=task.id,
                TASK_TITLE=task.title,
                TASK_BRIEF=task.brief,
                FILES=_bullets(task.files),
                HANDOFF=previous,
                WORKDIR=self.session.workdir,
            ),
            title=f"{IMPLEMENT}: {task.title}",
        )
        task.report = _text(result)
        task.ok = result.ok
        if not result.ok:
            run.unfinished.append(f"{task.id} {task.title}: {result.error or 'the agent failed'}")
        run.stages.append(
            StageResult(f"{IMPLEMENT} {task.id}", agent, result.ok, task.report)
        )
        return task.report

    async def _test(self, run: PipelineRun) -> None:
        agent = run.plan.owner(TEST)
        if not self._switch("test", TEST, run.plan) or agent is None:
            return
        result = await self._delegate(
            agent,
            workflow_brief(
                "team-pipeline-test",
                reply_language=self.language,
                TASK=run.task,
                FILES=_bullets(run.changed_files()),
                HANDOFF=handoff(IMPLEMENT, "\n".join(t.report for t in run.tasks)),
            ),
            title=TEST,
        )
        text = _text(result)
        run.tests = _test_verdict(result, text)
        run.stages.append(StageResult(TEST, agent, result.ok, text))
        if run.tests == TESTS_FAIL:
            run.unfinished.append("the test stage reported FAIL")
        elif run.tests == NEEDS_MORE_EVIDENCE:
            run.unfinished.append(
                f"the test stage needs more evidence: {_evidence_problem(result, text)}"
            )

    async def _verify(self, run: PipelineRun) -> None:
        agent = run.plan.owner(VERIFY)
        if not self._switch("verify", VERIFY, run.plan) or agent is None:
            run.notes.append("verify stage skipped: no verifier on the roster")
            return
        result = await self._delegate(
            agent,
            workflow_brief(
                "team-pipeline-verify",
                reply_language=self.language,
                TASK=run.task,
                TESTS=run.tests or "not run",
                FILES=_bullets(run.changed_files()),
                HANDOFF=handoff(TEST, _stage_text(run, TEST)),
                WORKDIR=self.session.workdir,
            ),
            title=VERIFY,
        )
        text = _text(result)
        run.verification = _verify_verdict(result, text)
        run.stages.append(StageResult(VERIFY, agent, result.ok, text))
        if run.verification == VERIFY_FAIL:
            run.unfinished.append("the verify stage reported FAIL")
        elif run.verification == NEEDS_MORE_EVIDENCE:
            run.unfinished.append(
                f"the verify stage needs more evidence: {_evidence_problem(result, text)}"
            )

    async def _review(self, run: PipelineRun) -> None:
        agent = run.plan.owner(REVIEW)
        if not self._switch("review", REVIEW, run.plan) or agent is None:
            return
        while run.review_rounds < MAX_REVIEW_ROUNDS:
            run.review_rounds += 1
            diff = await self._diff(run)
            result = await self._delegate(
                agent,
                workflow_brief(
                    "team-pipeline-review",
                    reply_language=self.language,
                    TASK=run.task,
                    ROUND_NOTE=("" if run.review_rounds == 1 else SECOND_LOOK),
                    DIFF=diff or "(no diff was available; judge the files themselves)",
                    WORKDIR=self.session.workdir,
                ),
                title=REVIEW if run.review_rounds == 1 else f"{REVIEW} ({run.review_rounds})",
            )
            text = _text(result)
            run.stages.append(StageResult(REVIEW, agent, result.ok, text))
            run.verdict = _review_verdict(result, text)
            if run.verdict != REQUEST_CHANGES:
                if run.verdict != APPROVE:
                    run.unfinished.append(
                        f"the review stage did not approve: {run.verdict} "
                        f"({_evidence_problem(result, text)})"
                    )
                return
            if run.review_rounds >= MAX_REVIEW_ROUNDS:
                break
            await self._fix(run, text)
        run.unfinished.append(
            f"the reviewer still asks for changes after {run.review_rounds} rounds"
        )

    async def _fix(self, run: PipelineRun, findings: str) -> None:
        """One fix pass by the implementer of the task the findings name."""
        agent = run.plan.owners[IMPLEMENT]
        task = _task_for(run, findings)
        result = await self._delegate(
            agent,
            workflow_brief(
                "team-pipeline-fix",
                reply_language=self.language,
                TASK_ID=task.id,
                TASK_TITLE=task.title,
                FILES=_bullets(task.files or run.changed_files()),
                FINDINGS=findings,
                WORKDIR=self.session.workdir,
            ),
            title=FIX,
        )
        run.stages.append(StageResult(FIX, agent, result.ok, _text(result)))
        if not result.ok:
            run.unfinished.append(f"the fix pass on {task.id} failed")

    # -- helpers -------------------------------------------------------
    def _anchor(self, plan: StagePlan) -> Session:
        """A stand-in parent carrying this run's roster.

        It is the lead's own session in every respect that matters — same id,
        so ``subagent.*`` events land where a client is watching and the
        concurrency semaphore and sibling-file registry are shared — except
        that ``team``/``team_agents`` name the roster *this run* uses.  That is
        what lets ``/team <name> "<task>"`` delegate to a team the project has
        not adopted without writing ``activeTeam`` (M6/M7 §9).
        """
        from snowpea_core.session.session import Session as SessionType

        lead = self.session
        members = tuple(dict.fromkeys(self.roster or roster_for(self.core, lead)[0]))
        if not members:
            return lead
        anchor = SessionType(
            id=lead.id,
            workdir=lead.workdir,
            mode=lead.mode,
            provider=lead.provider,
            model=lead.model,
            agent=lead.agent,
            team=plan.source,
            team_agents=members,
            origin_surface=lead.origin_surface,
            created_at=lead.created_at,
            max_concurrent=getattr(lead, "max_concurrent", None) or 0,
            origin_conn=lead.origin_conn,
        )
        anchor.unattended = lead.unattended
        anchor.memory_namespace = lead.memory_namespace
        anchor.backend = lead.backend
        return anchor

    async def _delegate(self, agent: str, brief: str, *, title: str) -> SubagentResult:
        """One stage = one subagent turn, reported on the lead's session."""
        return await self.manager.run(self.anchor, brief, agent=agent, title=title)

    async def _diff(self, run: PipelineRun) -> str:
        """The working-tree diff of the changed files, or ``""`` outside git."""
        files = run.changed_files()
        args = ["diff", "--", *files] if files else ["diff"]
        result = await git(self.session.workdir, *args)
        if not result.ok:
            return ""
        text = result.stdout.strip()
        if not text and files:
            result = await git(self.session.workdir, "diff", "HEAD", "--", *files)
            text = result.stdout.strip() if result.ok else ""
        return text[:MAX_REVIEW_DIFF_CHARS]

    # -- the answer ----------------------------------------------------
    def report(self, run: PipelineRun) -> str:
        """The lead's own account of the run — never a child's raw output.

        Same shape as ``delegate_task``'s ``render_report``: a header of plain
        lines, then what happened, then what is left.
        """
        done = [task for task in run.tasks if task.ok]
        head = [
            f"stages: {', '.join(f'{stage}={agent}' for stage, agent in run.plan.rows())}",
            f"tasks: {len(done)}/{len(run.tasks)} finished",
            f"tests: {run.tests or 'not run'}",
            f"verify: {run.verification or 'not run'}",
            f"review: {run.verdict or 'not run'}",
        ]
        parts = ["\n".join(head)]
        lines = []
        for task in run.tasks:
            mark = "ok" if task.ok else "failed"
            merged = f" (merged {', '.join(task.merged)})" if task.merged else ""
            lines.append(f"- {task.id} [{mark}] {task.title}{merged}")
            first = next(
                (row for row in (task.report or "").splitlines() if row.strip()), ""
            )
            if first:
                lines.append(f"  {first.strip()[:200]}")
        parts.append("What the team changed:\n" + "\n".join(lines))
        files = run.changed_files()
        if files:
            parts.append("Files the plan claimed:\n" + _bullets(files))
        if run.notes:
            parts.append("Notes:\n" + _bullets(run.notes))
        unfinished = _unfinished(run)
        if unfinished:
            parts.append("Left unfinished:\n" + _bullets(unfinished))
        else:
            parts.append("Nothing was left unfinished.")
        return "\n\n".join(parts)


def _text(result: SubagentResult) -> str:
    return (result.summary or result.error or "").strip()


_APPROVAL_DENIED = re.compile(
    r"\b(approval\.resolved[^.\n]*(?:denied|deny)|"
    r"approval[^.\n]*denied|denied[^.\n]*approval)\b",
    re.I,
)
_FAILED_TOOL = re.compile(r"\b(tool call failed|tool failed|failed tool call)\b", re.I)
_COMMAND_EVIDENCE = re.compile(r"(?m)^\s*(?:\$|>)\s*\S+")


def _explicit_line(text: str, allowed: Collection[str]) -> str:
    allowed_upper = {item.upper(): item for item in allowed}
    for line in (text or "").splitlines():
        token = " ".join(line.strip().split()).upper()
        if token in allowed_upper:
            return allowed_upper[token]
    return ""


def _has_tool_evidence(result: SubagentResult, text: str = "") -> bool:
    """Whether the child actually looked at something.

    ``rounds_used`` is deliberately not evidence: a child that answered in one
    round without calling anything still used a round, so counting it would
    make every claimed verdict self-certifying.
    """
    return bool(result.last_calls) or _COMMAND_EVIDENCE.search(text) is not None


def _hard_evidence_problem(result: SubagentResult, text: str) -> str:
    """Evidence defects that override any claimed verdict."""
    if not result.ok:
        return result.error or f"the child ended with status {result.status}"
    reason = (result.reason or "").lower()
    if reason and reason != "complete":
        return f"the child ended with reason {result.reason}"
    if _APPROVAL_DENIED.search(text):
        return "the child reported a denied approval"
    if _FAILED_TOOL.search(text):
        return "the child reported a failed tool call"
    if not text.strip():
        return "the child returned an empty report"
    return ""


def _evidence_problem(result: SubagentResult, text: str) -> str:
    hard = _hard_evidence_problem(result, text)
    if hard:
        return hard
    if not _has_tool_evidence(result, text):
        return "the child report contained no tool evidence"
    return "the child did not include the required verdict line"


def _test_verdict(result: SubagentResult, text: str) -> str:
    if _hard_evidence_problem(result, text):
        return NEEDS_MORE_EVIDENCE
    token = _explicit_line(text, (TESTS_PASS, TESTS_FAIL))
    if token == TESTS_FAIL:
        return TESTS_FAIL
    if token == TESTS_PASS and _has_tool_evidence(result, text):
        return TESTS_PASS
    return NEEDS_MORE_EVIDENCE


def _verify_verdict(result: SubagentResult, text: str) -> str:
    if _hard_evidence_problem(result, text):
        return NEEDS_MORE_EVIDENCE
    token = _explicit_line(text, (VERIFY_PASS, VERIFY_FAIL))
    if token == VERIFY_FAIL:
        return VERIFY_FAIL
    if token == VERIFY_PASS and _has_tool_evidence(result, text):
        return VERIFY_PASS
    return NEEDS_MORE_EVIDENCE


def _review_verdict(result: SubagentResult, text: str) -> str:
    if _hard_evidence_problem(result, text):
        return NEEDS_MORE_EVIDENCE
    token = _explicit_line(
        text,
        (
            "VERDICT: APPROVE",
            "VERDICT: REQUEST_CHANGES",
            "VERDICT: REJECT",
            "VERDICT: NEEDS_MORE_EVIDENCE",
        ),
    )
    if token in ("VERDICT: REQUEST_CHANGES", "VERDICT: REJECT"):
        return REQUEST_CHANGES
    if token == "VERDICT: NEEDS_MORE_EVIDENCE":
        return NEEDS_MORE_EVIDENCE
    if token == "VERDICT: APPROVE":
        # An approval nobody looked at is not an approval: without a tool call
        # behind it the child only asserted that the change is fine.
        return APPROVE if _has_tool_evidence(result, text) else NEEDS_MORE_EVIDENCE
    return "NO_VERDICT"


def _verdict(text: str) -> str:
    """The reviewer's explicit verdict token, read from its own answer."""
    return _review_verdict(
        SubagentResult("parser", True, text, rounds_used=1, last_calls=["read_file"]),
        text,
    )


def _stage_text(run: PipelineRun, stage: str) -> str:
    return "\n".join(item.text for item in run.stages if item.stage == stage)


def _unfinished(run: PipelineRun) -> list[str]:
    unfinished = list(run.unfinished)
    if run.tests != TESTS_PASS:
        unfinished.append(f"tests were not PASS ({run.tests or 'not run'})")
    if run.verification and run.verification != VERIFY_PASS:
        unfinished.append(f"verify was not PASS ({run.verification})")
    if run.verdict != APPROVE:
        unfinished.append(f"review was not APPROVE ({run.verdict or 'not run'})")
    for stage in run.stages:
        if not stage.ok:
            unfinished.append(f"{stage.stage} failed: {stage.text[:160] or 'no report'}")
    return list(dict.fromkeys(unfinished))


def _task_for(run: PipelineRun, findings: str) -> PipelineTask:
    """The task a reviewer's findings belong to: the one that owns the files.

    Findings name paths, so the task that claimed the most of them is the one
    whose implementer fixes them.  With nothing to go on the first task owns
    it, because somebody has to.
    """
    best, score = run.tasks[0], 0
    for task in run.tasks:
        hits = sum(1 for name in task.files if name and name in findings)
        if hits > score:
            best, score = task, hits
    return best


async def run_pipeline(
    core: Core,
    session: Session,
    task: str,
    say: Any,
    roster: Sequence[str] | None = None,
    *,
    source: str | None = None,
) -> str:
    """Entry point ``/team "<task>"`` and ``/team <name> "<task>"`` call."""
    return await TeamPipeline(core, session, roster, source=source).run(task, say=say)


def parse_pipeline_args(args: str) -> str:
    """``'run "add docstrings"'`` / ``'"add docstrings"'`` -> the task."""
    text = (args or "").strip()
    head, _, rest = text.partition(" ")
    if head.lower() == "run":
        text = rest.strip()
    if len(text) > 1 and text[:1] in {'"', "'"} and text[-1:] == text[:1]:
        text = text[1:-1]
    return text.strip()


def stage_line(core: Core, workdir: Path | str, roster: Sequence[str]) -> str:
    """One line naming the stage each member of ``roster`` fills."""
    stages = stages_map(core, workdir, roster)
    if not stages:
        return "cannot run /team \"<task>\": no implementer (add 'executor')"
    filled = set(stages.values())
    spare = [name for name in dict.fromkeys(roster) if name not in filled]
    line = " | ".join(f"{stage}: {agent}" for stage, agent in stages.items())
    return line + (f"  (no stage: {', '.join(spare)})" if spare else "")


def stage_summary(core: Core, session: Session) -> list[str]:
    """``/team list``: which stage each member of the active roster fills."""
    try:
        plan = plan_for(core, session)
    except PipelineError as exc:
        return [f"  pipeline: {exc}"]
    rows = [f"  {stage}: {agent}" for stage, agent in plan.rows()]
    if plan.unused:
        rows.append(f"  (no stage): {', '.join(plan.unused)}")
    return rows


__all__ = [
    "APPROVE",
    "EXPLORE",
    "FIX",
    "HANDOFF_LINES",
    "IMPLEMENT",
    "MAX_REVIEW_DIFF_CHARS",
    "MAX_REVIEW_ROUNDS",
    "MAX_TASKS",
    "PLAN",
    "REQUEST_CHANGES",
    "REQUIRED_STAGES",
    "REVIEW",
    "STAGE_AGENTS",
    "STAGE_ORDER",
    "TEST",
    "VERIFY",
    "NEEDS_MORE_EVIDENCE",
    "TESTS_FAIL",
    "TESTS_PASS",
    "VERIFY_FAIL",
    "VERIFY_PASS",
    "PipelineError",
    "PipelineRun",
    "PipelineTask",
    "StagePlan",
    "StageResult",
    "TeamPipeline",
    "handoff",
    "merge_overlapping",
    "parse_pipeline_args",
    "plan_for",
    "named_roster",
    "roster_for",
    "run_pipeline",
    "stage_line",
    "stages_map",
    "stage_assignments",
    "stage_summary",
    "tasks_from_payload",
    "waves",
]
