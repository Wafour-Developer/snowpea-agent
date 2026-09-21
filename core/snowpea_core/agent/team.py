"""Team mode: N workers, one git worktree each, a lead that merges (M7 §5).

``/team 3 "add docstrings to three modules"`` runs one lead and three workers:

1. The lead asks the provider to split the task into a JSON list of tasks with
   ids, titles and dependencies, and writes them to the shared board
   (``team_tasks`` in the daemon's state database).
2. It creates one git worktree per worker at
   ``<workdir>/.snowpea/worktrees/<teamId>-<n>`` on branch
   ``snowpea/team-<teamId>-<n>`` with ``git worktree add -b``.
3. Each worker loops: claim the next queued task whose dependencies are already
   merged (one conditional ``UPDATE``, so two workers never share a task), run
   it as a subagent whose session workdir *is* the worktree, commit the result
   on its own branch, mark the task ``done`` and post to the team board.
4. The lead merges ``done`` tasks into the main worktree in completion order
   with ``git merge --no-ff``.  A conflict is not fatal: the merge is aborted,
   the conflicted hunks are captured, the task goes to ``conflict`` and is
   re-queued to the same worker with those hunks in its next prompt, and the
   lead moves on to the next task.  After ``team.max_conflict_retries`` re-queues (default 2)
   the task is fixed at ``failed`` with its hunks attached and the rest of the
   team carries on.
5. When every task is ``merged`` or ``failed`` the worktrees are removed and
   the branches deleted, so only successful commits remain on the base branch.

Every transition emits ``team.task.update{teamId, taskId, status, agentN,
retries}`` on the **lead's** session, which is what a client renders.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.agent import team_store
from snowpea_core.agent.agent import reply_language
from snowpea_core.agent.definition import complete_text, parse_generated_json
from snowpea_core.agent.subagent import SharedBackend, get_manager
from snowpea_core.agent.team_guide import guide_for_session, render_team_guide
from snowpea_core.agent.team_store import TaskRow, TeamStore, get_store
from snowpea_core.exec.local import LocalBackend
from snowpea_core.prompts.compose import workflow_brief
from snowpea_core.prompts.loader import load
from snowpea_core.providers.base import ChatMessage
from snowpea_core.server.protocol import TeamStatusResult, TeamTask, TeamTaskUpdate
from snowpea_core.session.session import Session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.agent.team")

#: Attribute the manager is cached under on :class:`Core`.
CORE_ATTR = "_teams"

#: Where worktrees go, relative to the session's workdir (contract §5).
WORKTREE_DIR = Path(".snowpea") / "worktrees"

#: Branch name template.
BRANCH_PREFIX = "snowpea/team-"

#: Bounds on the worker count a caller may ask for.
MIN_WORKERS = 1
MAX_WORKERS = 16

#: Bounds on how many tasks the planner may produce.
MAX_TASKS = 24

#: How long the worker and merge loops sleep when there is nothing to do.
POLL_SEC = 0.02

#: How much of a conflict diff is kept on the task row.
MAX_HUNK_CHARS = 4000

#: Definition the optional post-merge review runs as (M15 §C5); a project file
#: of the same name overrides the built-in read-only one.
REVIEW_AGENT = "reviewer"

#: How much of a merged diff the reviewer is handed inline.
MAX_REVIEW_DIFF_CHARS = 24000

#: The verdict that sends a task back to its author, once.
REQUEST_CHANGES = "REQUEST_CHANGES"

PLAN_SYSTEM = load("workflows/team-plan")


def _lead_guide_text(core: Core, session: Session) -> str:
    """The team guide for the planner; workers get theirs from the system prompt."""
    guide = guide_for_session(core, session)
    return render_team_guide(guide, audience="lead") if guide else ""


class TeamError(RuntimeError):
    """Team mode could not start: no git repository, no tasks, bad arguments."""


def new_team_id() -> str:
    return f"tm{uuid.uuid4().hex[:8]}"


def branch_name(team_id: str, n: int) -> str:
    return f"{BRANCH_PREFIX}{team_id}-{n}"


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


@dataclass
class GitResult:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip()


async def git(cwd: Path | str, *args: str, timeout: float = 120.0) -> GitResult:
    """Run one git command and capture its output."""
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return GitResult(124, "", f"git {' '.join(args)} timed out after {timeout:g}s")
    return GitResult(
        process.returncode or 0,
        raw_out.decode("utf-8", "replace"),
        raw_err.decode("utf-8", "replace"),
    )


async def repo_root(workdir: Path | str) -> Path:
    """The top of the git repository containing ``workdir``."""
    result = await git(workdir, "rev-parse", "--show-toplevel")
    if not result.ok or not result.stdout.strip():
        raise TeamError(
            f"team mode needs a git repository; {workdir} is not inside one "
            f"({result.text or 'git rev-parse failed'})"
        )
    return Path(result.stdout.strip())


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


@dataclass
class PlannedTask:
    id: str
    title: str
    depends_on: list[str] = field(default_factory=list)


def tasks_from_payload(data: dict[str, Any]) -> list[PlannedTask]:
    """Read the planner's JSON into task rows, ids filled in where missing."""
    raw: list[Any] = []
    for key in ("tasks", "stories"):
        value = data.get(key)
        if isinstance(value, list):
            raw = value
            break
    planned: list[PlannedTask] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw[:MAX_TASKS], start=1):
        if isinstance(entry, str):
            entry = {"title": entry}
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or entry.get("task") or entry.get("name") or "").strip()
        if not title:
            continue
        task_id = str(entry.get("id") or f"T{index}").strip() or f"T{index}"
        while task_id in seen:
            task_id = f"{task_id}x"
        seen.add(task_id)
        deps_raw = entry.get("depends_on") or entry.get("deps") or []
        if isinstance(deps_raw, str):
            deps_raw = [deps_raw]
        deps = [str(item).strip() for item in deps_raw if str(item).strip()]
        planned.append(PlannedTask(id=task_id, title=title, depends_on=deps))
    known = {task.id for task in planned}
    for task in planned:
        task.depends_on = [dep for dep in task.depends_on if dep in known and dep != task.id]
    return planned


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


@dataclass
class Worktree:
    """One worker's checkout."""

    n: int
    path: Path
    branch: str


@dataclass
class TeamRun:
    """Everything one ``/team`` invocation needs while it is running."""

    id: str
    session: Session
    repo: Path
    task: str
    workers: int
    worktrees: list[Worktree] = field(default_factory=list)
    #: Increments each time a task is committed, so the lead merges in
    #: completion order rather than board order.
    done_counter: int = 0
    runner: asyncio.Task[None] | None = None
    state: str = "running"
    #: Task ids already reviewed, so ``team.review`` costs one pass per task
    #: however often a task is re-queued.
    reviewed: set[str] = field(default_factory=set)
    #: Findings waiting to be handed back to the worker that wrote the task.
    review_findings: dict[str, str] = field(default_factory=dict)

    def worktree(self, n: int) -> Worktree | None:
        for entry in self.worktrees:
            if entry.n == n:
                return entry
        return None


class TeamManager:
    """Starts team runs and answers ``team.status``."""

    def __init__(self, core: Core) -> None:
        self.core = core
        self._runs: dict[str, TeamRun] = {}

    @property
    def store(self) -> TeamStore:
        return get_store(self.core)

    def get(self, team_id: str) -> TeamRun | None:
        return self._runs.get(team_id)

    def max_conflict_retries(self) -> int:
        return max(0, int(self.core.settings.team.max_conflict_retries))

    def review_enabled(self) -> bool:
        """``team.review`` — off unless the user turned it on."""
        return bool(getattr(self.core.settings.team, "review", False))

    # -- entry point ---------------------------------------------------
    async def start(self, session: Session, n: int, task: str) -> str:
        """Plan, create the worktrees and launch the run; returns the team id.

        The run itself continues in the background — ``team.start`` answers with
        an id a client can poll, and ``/team`` awaits :meth:`wait`.
        """
        brief = (task or "").strip()
        if not brief:
            raise TeamError("team mode needs a task")
        workers = max(MIN_WORKERS, min(MAX_WORKERS, int(n)))
        repo = await repo_root(session.workdir)

        planned = await self.plan(session, brief, workers)
        if not planned:
            raise TeamError("the model did not split this task into any work items")

        team_id = new_team_id()
        store = self.store
        await store.create_team(
            team_id, session.id, str(session.workdir), str(repo), brief, workers
        )
        for index, entry in enumerate(planned, start=1):
            await store.add_task(team_id, entry.id, index, entry.title, entry.depends_on)
            await self._emit(team_id, entry.id, team_store.QUEUED, None, 0)

        run = TeamRun(id=team_id, session=session, repo=repo, task=brief, workers=workers)
        run.worktrees = await self._create_worktrees(run)
        self._runs[team_id] = run
        await store.post(team_id, "lead", f"{len(planned)} tasks, {workers} workers")
        run.runner = asyncio.ensure_future(self._run(run))
        return team_id

    async def wait(self, team_id: str) -> None:
        """Block until the run finishes; re-raises whatever it raised."""
        run = self._runs.get(team_id)
        if run is None or run.runner is None:
            return
        await run.runner

    async def interrupt_for_session(self, session_id: str) -> None:
        """Cancel live worktree-team runs owned by ``session_id``."""
        for run in list(self._runs.values()):
            if run.session.id != session_id:
                continue
            run.session.interrupt.set()
            if run.runner is not None and not run.runner.done():
                run.runner.cancel()
                try:
                    await run.runner
                except asyncio.CancelledError:
                    pass

    async def plan(self, session: Session, task: str, workers: int) -> list[PlannedTask]:
        """Ask the session's provider for the task list."""
        provider = self.core.providers.get(session.provider, session.model)
        plan_system = PLAN_SYSTEM.replace("${TEAM_GUIDE}", _lead_guide_text(self.core, session))
        messages = [
            ChatMessage(role="system", content=plan_system),
            ChatMessage(
                role="user",
                content=(
                    f"Split this task for {workers} parallel agents working in the project at "
                    f"{session.workdir}. Reply with the JSON object only.\n\nTask: {task}"
                ),
            ),
        ]
        text = await complete_text(provider, messages)
        return tasks_from_payload(parse_generated_json(text))

    # -- worktrees -----------------------------------------------------
    async def _create_worktrees(self, run: TeamRun) -> list[Worktree]:
        """``git worktree add -b snowpea/team-<id>-<n> <path>`` once per worker."""
        base = Path(run.session.workdir) / WORKTREE_DIR
        base.mkdir(parents=True, exist_ok=True)
        created: list[Worktree] = []
        for n in range(1, run.workers + 1):
            path = base / f"{run.id}-{n}"
            branch = branch_name(run.id, n)
            result = await git(run.repo, "worktree", "add", "-b", branch, str(path), "HEAD")
            if not result.ok:
                for entry in created:
                    await self._remove_worktree(run, entry)
                raise TeamError(f"could not create a worktree for agent {n}: {result.text}")
            created.append(Worktree(n=n, path=path, branch=branch))
        return created

    async def _remove_worktree(self, run: TeamRun, entry: Worktree) -> None:
        await git(run.repo, "worktree", "remove", "--force", str(entry.path))
        await git(run.repo, "worktree", "prune")
        await git(run.repo, "branch", "-D", entry.branch)

    async def _cleanup(self, run: TeamRun) -> None:
        for entry in run.worktrees:
            try:
                await self._remove_worktree(run, entry)
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                log.debug("could not clean up worktree %s", entry.path, exc_info=True)

    # -- events --------------------------------------------------------
    async def _emit(
        self, team_id: str, task_id: str, status: str, agent_n: int | None, retries: int
    ) -> None:
        run = self._runs.get(team_id)
        session_id = run.session.id if run is not None else None
        if session_id is None:
            row = await self.store.team(team_id)
            session_id = row.session_id if row is not None else None
        if session_id is None:
            return
        payload = TeamTaskUpdate(
            teamId=team_id,
            taskId=task_id,
            status=status,  # type: ignore[arg-type]
            agentN=agent_n,
            retries=retries,
            assignee=None if agent_n is None else f"agent-{agent_n}",
        ).model_dump(mode="json")
        payload.pop("kind", None)
        try:
            await self.core.hub.emit_event(session_id, ("team.task.update", payload))
        except Exception:  # noqa: BLE001 - a dead lead must not stop the team
            log.debug("could not emit team.task.update on %s", session_id, exc_info=True)

    async def _transition(
        self,
        run: TeamRun,
        row: TaskRow,
        status: str,
        *,
        agent_n: int | None = None,
        retries: int | None = None,
        conflict_hunks: str | None = None,
        note: str | None = None,
        done_seq: int | None = None,
    ) -> None:
        """Persist a state change and tell the lead's session about it."""
        await self.store.set_status(
            run.id,
            row.id,
            status,
            agent_n=agent_n,
            retries=retries,
            conflict_hunks=conflict_hunks,
            note=note,
            done_seq=done_seq,
        )
        row.status = status
        if agent_n is not None:
            row.agent_n = agent_n
        if retries is not None:
            row.retries = retries
        if conflict_hunks is not None:
            row.conflict_hunks = conflict_hunks
        if note is not None:
            row.note = note
        await self._emit(run.id, row.id, status, row.agent_n, row.retries)

    # -- the loops -----------------------------------------------------
    async def _run(self, run: TeamRun) -> None:
        """Workers and the merge loop, then cleanup — always cleanup."""
        cancelled = False
        try:
            workers = [
                asyncio.ensure_future(self._worker(run, entry)) for entry in run.worktrees
            ]
            merger = asyncio.ensure_future(self._merge_loop(run))
            try:
                await asyncio.gather(*workers)
            except asyncio.CancelledError:
                cancelled = True
                for worker in workers:
                    worker.cancel()
                merger.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                raise
            finally:
                if not merger.cancelled():
                    try:
                        await merger
                    except asyncio.CancelledError:
                        cancelled = True
        finally:
            await self._cleanup(run)
            rows = await self.store.tasks(run.id)
            failed = [row for row in rows if row.status == team_store.FAILED]
            run.state = "interrupted" if cancelled else (
                "failed" if failed and len(failed) == len(rows) else "done"
            )
            await self.store.set_team_state(run.id, run.state)
            await self.store.post(
                run.id,
                "lead",
                f"run finished: {len(rows) - len(failed)} merged, {len(failed)} failed",
            )

    async def _finished(self, run: TeamRun) -> bool:
        rows = await self.store.tasks(run.id)
        return bool(rows) and all(row.terminal for row in rows)

    async def _claim_next(self, run: TeamRun, entry: Worktree) -> TaskRow | None:
        """Take the next task this worker may run, or ``None`` for now.

        Ready means queued, dependencies merged, and either unassigned or
        re-queued to this worker after a conflict.  A task whose dependency
        failed can never run, so it fails with it instead of pinning the loop.
        """
        rows = await self.store.tasks(run.id)
        by_id = {row.id: row for row in rows}
        for row in rows:
            if row.status not in (team_store.QUEUED, team_store.CONFLICT):
                continue
            if row.agent_n is not None and row.agent_n != entry.n:
                continue
            deps = [by_id.get(dep) for dep in row.depends_on]
            if any(dep is not None and dep.status == team_store.FAILED for dep in deps):
                await self._transition(
                    run,
                    row,
                    team_store.FAILED,
                    agent_n=entry.n,
                    note="blocked: a task it depends on failed",
                )
                continue
            if not all(dep is not None and dep.status == team_store.MERGED for dep in deps):
                continue
            if await self.store.claim(run.id, row.id, entry.n, entry.branch):
                row.status = team_store.CLAIMED
                row.agent_n = entry.n
                row.branch = entry.branch
                await self._emit(run.id, row.id, team_store.CLAIMED, entry.n, row.retries)
                return row
        return None

    async def _worker(self, run: TeamRun, entry: Worktree) -> None:
        """One worker: claim, work, commit, mark done — until nothing is left."""
        while True:
            if await self._finished(run):
                return
            row = await self._claim_next(run, entry)
            if row is None:
                await asyncio.sleep(POLL_SEC)
                continue
            try:
                await self._work(run, entry, row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad task, not one bad team
                log.exception("team %s: agent %s failed on %s", run.id, entry.n, row.id)
                await self._transition(
                    run,
                    row,
                    team_store.FAILED,
                    note=f"{type(exc).__name__}: {exc}",
                )

    async def _work(self, run: TeamRun, entry: Worktree, row: TaskRow) -> None:
        """Run one task as a subagent in the worktree and commit the result."""
        manager = get_manager(self.core)
        anchor = self._anchor(run, entry)
        # Name the worker so its model profile is consulted: with no active
        # team the anchor carries no roster, and an unnamed subagent falls
        # outside ``agents.models`` entirely (CORE-model-assignment B-P2-2).
        worker_agent = None if anchor.team_agents else "executor"
        result = await manager.run(
            anchor, self._task_prompt(run, row, entry), agent=worker_agent
        )
        if not result.ok:
            await self._transition(
                run,
                row,
                team_store.FAILED,
                note=f"the worker failed: {result.error or 'no reason given'}",
            )
            await self.store.post(
                run.id, "worker", f"{row.id} failed", agent_n=entry.n, task_id=row.id
            )
            return

        await git(entry.path, "add", "-A")
        status = await git(entry.path, "status", "--porcelain")
        if status.stdout.strip():
            message = f"team {run.id} agent {entry.n}: {row.title}"
            if row.retries:
                message += f" (retry {row.retries})"
            commit = await git(entry.path, "commit", "-q", "-m", message)
            if not commit.ok:
                await self._transition(
                    run, row, team_store.FAILED, note=f"could not commit: {commit.text}"
                )
                return

        # Whether or not this attempt added a commit, the question the lead
        # cares about is whether the branch still has anything the base branch
        # has not got.  A re-queued task whose worker reproduced the same tree
        # has nothing new to commit but is still unmerged, and must go back to
        # the merge loop so its conflict is counted rather than silently lost.
        if not await self._has_unmerged_commits(run, entry):
            await self._transition(
                run,
                row,
                team_store.MERGED,
                note="the worker added nothing the base branch does not already have",
            )
            await self.store.post(
                run.id, "worker", f"{row.id}: nothing to merge", agent_n=entry.n, task_id=row.id
            )
            return

        run.done_counter += 1
        await self._transition(
            run,
            row,
            team_store.DONE,
            done_seq=run.done_counter,
            note=(result.summary or "").strip()[:500],
        )
        await self.store.post(
            run.id,
            "worker",
            f"{row.id} committed on {entry.branch}",
            agent_n=entry.n,
            task_id=row.id,
        )

    async def _has_unmerged_commits(self, run: TeamRun, entry: Worktree) -> bool:
        """True when the worker's branch tip is not already in the base branch."""
        head = await git(entry.path, "rev-parse", "HEAD")
        tip = head.stdout.strip()
        if not tip:
            return False
        ancestor = await git(run.repo, "merge-base", "--is-ancestor", tip, "HEAD")
        return not ancestor.ok

    async def _merge_loop(self, run: TeamRun) -> None:
        """Merge ``done`` tasks into the main worktree, in completion order."""
        while True:
            rows = await self.store.tasks(run.id)
            if rows and all(row.terminal for row in rows):
                return
            ready = sorted(
                (row for row in rows if row.status == team_store.DONE),
                key=lambda row: row.done_seq,
            )
            if not ready:
                if not rows:
                    return
                await asyncio.sleep(POLL_SEC)
                continue
            await self._merge(run, ready[0])

    async def _merge(self, run: TeamRun, row: TaskRow) -> None:
        """One ``git merge --no-ff``; a conflict re-queues instead of stopping."""
        branch = row.branch or branch_name(run.id, row.agent_n or 1)
        message = f"Merge {branch} for team task {row.id}: {row.title}"
        result = await git(run.repo, "merge", "--no-ff", "-m", message, branch)
        if result.ok:
            findings = await self._review_merge(run, row)
            if findings:
                run.review_findings[row.id] = findings
                await self._transition(
                    run,
                    row,
                    team_store.QUEUED,
                    agent_n=row.agent_n,
                    note="the reviewer requested changes; re-queued to the same agent",
                )
                await self.store.post(
                    run.id,
                    "lead",
                    f"{row.id}: review requested changes; back to agent {row.agent_n}",
                    task_id=row.id,
                )
                return
            await self._transition(run, row, team_store.MERGED, note="merged with --no-ff")
            await self.store.post(
                run.id, "lead", f"merged {row.id} from {branch}", task_id=row.id
            )
            return

        hunks = await self._conflict_hunks(run)
        await git(run.repo, "merge", "--abort")
        if not hunks:
            hunks = result.text[:MAX_HUNK_CHARS]
        limit = self.max_conflict_retries()
        if row.retries >= limit:
            await self._transition(
                run,
                row,
                team_store.FAILED,
                conflict_hunks=hunks,
                note=f"still conflicting after {limit} retries",
            )
            await self.store.post(
                run.id,
                "lead",
                f"{row.id} failed after {limit} conflict retries",
                task_id=row.id,
            )
            return
        # AC-16: the re-queued task shows as ``conflict`` with its retry count
        # until the same agent redoes it; the worker claims that state too.
        await self._transition(
            run,
            row,
            team_store.CONFLICT,
            retries=row.retries + 1,
            conflict_hunks=hunks,
            note="merge conflicted; re-queued to the same agent",
        )
        await self.store.post(
            run.id,
            "lead",
            f"{row.id} conflicted; re-queued to agent {row.agent_n} (retry {row.retries})",
            task_id=row.id,
        )

    async def _review_merge(self, run: TeamRun, row: TaskRow) -> str:
        """The reviewer's findings when it wants changes, else ``""``.

        Runs once per task and only when ``team.review`` is on.  The merge has
        already landed: a re-queue sends the *same* worker back into its own
        worktree to fix what was found, which is cheaper and less surprising
        than reverting a commit the rest of the board may already build on.
        """
        if not self.review_enabled() or row.id in run.reviewed:
            return ""
        run.reviewed.add(row.id)
        diff = await git(run.repo, "diff", "HEAD~1", "HEAD")
        text = diff.stdout.strip()
        if not text:
            return ""
        manager = get_manager(self.core)
        anchor = self._review_anchor(run)
        brief = workflow_brief(
            "team-review",
            reply_language=reply_language(self.core),
            REPO=run.repo,
            TASK_ID=row.id,
            TASK_TITLE=row.title,
            DIFF=text[:MAX_REVIEW_DIFF_CHARS],
        )
        agent = REVIEW_AGENT if manager.definition(anchor, REVIEW_AGENT) else None
        result = await manager.run(
            anchor, brief, agent=agent, title=f"Review {row.id}: {row.title}"
        )
        verdict = (result.summary or "").strip()
        if not result.ok or REQUEST_CHANGES not in verdict.upper():
            return ""
        return verdict

    def _review_anchor(self, run: TeamRun) -> Session:
        """A stand-in parent at the repository root, outside the team roster.

        The reviewer reads the merged tree, not a worktree, and it is not one
        of the workers — carrying the lead's ``team_agents`` here would have
        the membership guard refuse it by name.
        """
        lead = run.session
        anchor = Session(
            id=lead.id,
            workdir=run.repo,
            mode=lead.mode,
            provider=lead.provider,
            model=lead.model,
            origin_surface=lead.origin_surface,
            created_at=lead.created_at,
            max_concurrent=max(1, run.workers),
            origin_conn=lead.origin_conn,
        )
        anchor.unattended = lead.unattended
        anchor.memory_namespace = lead.memory_namespace
        backend = getattr(lead, "backend", None)
        if backend is None or getattr(backend, "kind", "local") == "local":
            anchor.backend = LocalBackend(run.repo)
        else:  # pragma: no cover - docker / ssh share the parent's backend
            anchor.backend = SharedBackend(backend)  # type: ignore[assignment]
        return anchor

    async def _conflict_hunks(self, run: TeamRun) -> str:
        """The conflicted diff, captured before ``git merge --abort`` erases it."""
        names = await git(run.repo, "diff", "--name-only", "--diff-filter=U")
        diff = await git(run.repo, "diff", "--diff-filter=U")
        parts = []
        if names.stdout.strip():
            parts.append("conflicted files:\n" + names.stdout.strip())
        if diff.stdout.strip():
            parts.append(diff.stdout.strip())
        return "\n\n".join(parts)[:MAX_HUNK_CHARS]

    # -- worker sessions ------------------------------------------------
    def _anchor(self, run: TeamRun, entry: Worktree) -> Session:
        """A stand-in parent whose workdir is the worktree.

        ``SubagentManager`` builds a child session from its parent's workdir,
        mode, provider and backend, and reports on ``parent.id``.  Handing it
        this unregistered stand-in — the lead's id, the worktree's path — is
        what puts the worker inside its own checkout while its ``subagent.*``
        events still land on the lead's session.

        ``team``/``team_agents`` are copied too.  Without them
        ``SubagentManager.run`` never fills in the ``"executor"`` default, so
        ``record.name`` stayed empty, ``agents.models`` was never consulted and
        every worktree worker ignored per-agent profiles — and the team
        membership guard never fired either (CORE-model-assignment B-P2-2).
        """
        lead = run.session
        anchor = Session(
            id=lead.id,
            workdir=entry.path,
            mode=lead.mode,
            provider=lead.provider,
            model=lead.model,
            agent=lead.agent,
            team=lead.team,
            team_agents=lead.team_agents,
            origin_surface=lead.origin_surface,
            created_at=lead.created_at,
            max_concurrent=max(1, run.workers),
            origin_conn=lead.origin_conn,
        )
        anchor.unattended = lead.unattended
        anchor.memory_namespace = lead.memory_namespace
        backend = getattr(lead, "backend", None)
        if backend is None or getattr(backend, "kind", "local") == "local":
            anchor.backend = LocalBackend(entry.path)
        else:  # docker / ssh: the worktree is a path inside the same backend
            anchor.backend = SharedBackend(backend)  # type: ignore[assignment]
        return anchor

    def _task_prompt(self, run: TeamRun, row: TaskRow, entry: Worktree) -> str:
        """The brief one worker subagent receives, conflict hunks included."""
        language = reply_language(self.core)
        parts = [
            workflow_brief(
                "team-task",
                reply_language=language,
                WORKER_N=entry.n,
                WORKERS=run.workers,
                TASK=run.task,
                TASK_ID=row.id,
                TASK_TITLE=row.title,
                WORKTREE_PATH=entry.path,
                BRANCH=entry.branch,
            )
        ]
        if row.conflict_hunks:
            parts.append(workflow_brief("team-conflict", HUNKS=row.conflict_hunks))
        findings = run.review_findings.pop(row.id, "")
        if findings:
            parts.append(workflow_brief("team-review-fix", FINDINGS=findings))
        return "\n\n".join(parts)

    # -- status ---------------------------------------------------------
    async def status(self, team_id: str | None = None) -> TeamStatusResult:
        """``team.status`` — the board, the retry counts and the worktrees."""
        store = self.store
        header = await (store.team(team_id) if team_id else store.latest_team())
        if header is None:
            raise TeamError(f"no such team: {team_id}" if team_id else "no team has run yet")
        rows = await store.tasks(header.id)
        run = self._runs.get(header.id)
        state = header.state
        if run is not None and run.runner is not None and not run.runner.done():
            state = "running"
        elif rows and all(row.terminal for row in rows):
            failed = [row for row in rows if row.status == team_store.FAILED]
            state = "failed" if failed and len(failed) == len(rows) else "done"
        worktrees = [str(entry.path) for entry in run.worktrees] if run is not None else []
        return TeamStatusResult(
            teamId=header.id,
            state=state,  # type: ignore[arg-type]
            workers=header.workers,
            task=header.task,
            worktrees=worktrees,
            tasks=[self._task_info(row) for row in rows],
        )

    @staticmethod
    def _task_info(row: TaskRow) -> TeamTask:
        hunks = row.conflict_hunks
        return TeamTask(
            taskId=row.id,
            title=row.title,
            status=row.status,  # type: ignore[arg-type]
            assignee=None if row.agent_n is None else f"agent-{row.agent_n}",
            agentN=row.agent_n,
            retries=row.retries,
            branch=row.branch,
            note=row.note,
            dependsOn=list(row.depends_on),
            conflictHunks=hunks,
            conflictSummary=_hunk_summary(hunks),
        )


def _hunk_summary(hunks: str) -> str:
    """One line naming the conflicted files and how much diff was kept."""
    if not hunks:
        return ""
    lines = len(hunks.splitlines())
    header, _, _rest = hunks.partition("\n\n")
    files = [line.strip() for line in header.splitlines()[1:] if line.strip()]
    if header.startswith("conflicted files:") and files:
        return f"{len(files)} conflicted file(s): {', '.join(files[:5])} ({lines} diff lines)"
    return f"{lines} conflict lines"


def get_manager_for(core: Core) -> TeamManager:
    """The daemon's one :class:`TeamManager`, created on first use."""
    manager = getattr(core, CORE_ATTR, None)
    if manager is None:
        manager = TeamManager(core)
        setattr(core, CORE_ATTR, manager)
    return manager


def parse_team_args(args: str) -> tuple[int, str]:
    """``'3 "add docstrings"'`` -> ``(3, "add docstrings")``."""
    text = (args or "").strip()
    if not text:
        raise TeamError("usage")
    head, _, rest = text.partition(" ")
    if not head.isdigit():
        raise TeamError("usage")
    task = rest.strip()
    if task[:1] in {'"', "'"} and task[-1:] == task[:1] and len(task) > 1:
        task = task[1:-1]
    else:
        try:
            parts = shlex.split(task)
        except ValueError:
            parts = []
        if len(parts) == 1:
            task = parts[0]
    if not task.strip():
        raise TeamError("usage")
    return int(head), task.strip()


__all__ = [
    "BRANCH_PREFIX",
    "CORE_ATTR",
    "MAX_TASKS",
    "MAX_WORKERS",
    "MIN_WORKERS",
    "MAX_REVIEW_DIFF_CHARS",
    "PLAN_SYSTEM",
    "REQUEST_CHANGES",
    "REVIEW_AGENT",
    "WORKTREE_DIR",
    "GitResult",
    "PlannedTask",
    "TeamError",
    "TeamManager",
    "TeamRun",
    "Worktree",
    "branch_name",
    "get_manager_for",
    "git",
    "new_team_id",
    "parse_team_args",
    "repo_root",
    "tasks_from_payload",
]
