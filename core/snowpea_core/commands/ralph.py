"""``/ralph <task>`` — the PRD loop (M7 contract §4).

The original oh-my-claudecode ``ralph`` skill is a prompt that tells the model
to keep going until the work is done.  Here the loop itself is code, because
"keep going until done" is a control-flow question and a prompt answers it
differently every run:

1. Ask the provider for 3–8 user stories with acceptance criteria and the
   shell commands that verify them, and write them to
   ``<workdir>/.snowpea/ralph/prd.json``.
2. Each iteration, take the failing stories that nothing blocks and implement
   them through ``delegate_task`` subagents — two or more at a time whenever
   two or more are independent.
3. Run each story's verification commands on the session's execution backend.
   A story passes only when every one of its commands exits zero.
4. When every story passes, a reviewer subagent (the ``architect`` definition
   when the project has one) gets the diff summary and must answer ``APPROVE``.
5. Only then does the turn end with ``turn.done{reason:"complete"}``.

Iterations are capped by ``ralph.max_iterations`` (default 10).  Progress is
appended to ``<workdir>/.snowpea/ralph/progress.md`` so a human can read what
happened after the fact.

Differences from the OMC original
---------------------------------
* OMC's ralph re-invokes itself through a hook that re-injects the prompt
  ("the boulder never stops"); snowpea runs a real ``while`` loop in one turn,
  so interrupting the turn interrupts ralph.
* OMC delegates to its ``executor`` agent by name; snowpea delegates through
  ``delegate_task`` and only names an agent when the project defines one.
* Verification in OMC is whatever the reviewer agent decides to run; here the
  PRD names the commands, they run on the session backend, and their exit code
  is the fact that decides whether a story passed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.agent import reply_language
from snowpea_core.agent.definition import complete_text, parse_generated_json
from snowpea_core.agent.subagent import get_manager
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.prompts.compose import workflow_brief
from snowpea_core.prompts.loader import render
from snowpea_core.providers.base import ChatMessage
from snowpea_core.server import errors
from snowpea_core.session import events

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.commands.ralph")

USAGE = 'Usage: /ralph "<task>"'

#: Where the loop keeps its state, relative to the workdir.
STATE_DIR = Path(".snowpea") / "ralph"
PRD_NAME = "prd.json"
PROGRESS_NAME = "progress.md"

#: Agent definition asked to sign the work off, when the project defines one.
REVIEWER_AGENT = "architect"

#: The word the reviewer has to say.
APPROVAL_WORD = "APPROVE"

#: Ceiling on stories, so a chatty model cannot make the loop unbounded.
MAX_STORIES = 8
MIN_STORIES = 1

PRD_SYSTEM = render(
    "workflows/ralph-prd", MIN_STORIES=MIN_STORIES, MAX_STORIES=MAX_STORIES
)

RALPH_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "What ralph should drive to completion."}
    },
    "required": ["task"],
}


# ---------------------------------------------------------------------------
# the PRD
# ---------------------------------------------------------------------------


@dataclass
class Story:
    """One PRD row plus its pass/fail state."""

    id: str
    title: str
    acceptance: str = ""
    verify: list[str] = field(default_factory=list)
    independent: bool = True
    depends_on: list[str] = field(default_factory=list)
    passed: bool = False
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "acceptance": self.acceptance,
            "verify": list(self.verify),
            "independent": self.independent,
            "depends_on": list(self.depends_on),
            "passed": self.passed,
            "note": self.note,
        }


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def stories_from_payload(data: dict[str, Any]) -> list[Story]:
    """Read the generator's JSON into :class:`Story` rows."""
    raw = data.get("stories")
    if not isinstance(raw, list):
        raw = [data] if data.get("title") else []
    stories: list[Story] = []
    for index, entry in enumerate(raw[:MAX_STORIES], start=1):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or entry.get("story") or "").strip()
        if not title:
            continue
        stories.append(
            Story(
                id=str(entry.get("id") or f"S{index}"),
                title=title,
                acceptance=str(entry.get("acceptance") or "").strip(),
                verify=_as_list(entry.get("verify")),
                independent=bool(entry.get("independent", True)),
                depends_on=_as_list(entry.get("depends_on")),
            )
        )
    return stories


async def build_prd(ctx: CommandContext, task: str) -> list[Story]:
    """Ask the session's provider for the story list."""
    provider = ctx.core.providers.get(ctx.session.provider, ctx.session.model)
    messages = [
        ChatMessage(role="system", content=PRD_SYSTEM),
        ChatMessage(
            role="user",
            content=(
                "Write the PRD for this task in the project at "
                f"{ctx.session.workdir}. Reply with the JSON object only.\n\nTask: {task}"
            ),
        ),
    ]
    text = await complete_text(provider, messages)
    return stories_from_payload(parse_generated_json(text))


# ---------------------------------------------------------------------------
# state on disk
# ---------------------------------------------------------------------------


def state_dir(session: Session) -> Path:
    directory = Path(session.workdir) / STATE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_prd(session: Session, task: str, stories: list[Story], iteration: int) -> Path:
    path = state_dir(session) / PRD_NAME
    payload = {
        "task": task,
        "iteration": iteration,
        "stories": [story.to_json() for story in stories],
        "allPassed": all(story.passed for story in stories) if stories else False,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def append_progress(session: Session, lines: list[str]) -> Path:
    path = state_dir(session) / PROGRESS_NAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def ready_stories(stories: list[Story], limit: int) -> list[Story]:
    """The failing stories nothing blocks, at most ``limit`` of them.

    A story is ready when every id in ``depends_on`` already passed.  Stories
    that declare themselves dependent (``independent: false``) are run one at a
    time so their side effects cannot race.
    """
    passed = {story.id for story in stories if story.passed}
    ready = [
        story
        for story in stories
        if not story.passed and all(dep in passed for dep in story.depends_on)
    ]
    if not ready:
        return []
    if not ready[0].independent:
        return ready[:1]
    batch: list[Story] = []
    for story in ready:
        if not story.independent:
            break
        batch.append(story)
        if len(batch) >= limit:
            break
    return batch


def reply_language_for(ctx: CommandContext) -> str:
    """``agent.replyLanguage`` for this run; children cannot see the setting."""
    return reply_language(ctx.core)


def story_task(task: str, story: Story, language: str = "auto") -> str:
    """The brief one implementation subagent receives."""
    acceptance = f"Acceptance criteria: {story.acceptance}\n" if story.acceptance else ""
    verify = (
        "It must make these commands exit zero: " + "; ".join(story.verify) + "\n"
        if story.verify
        else ""
    )
    return workflow_brief(
        "ralph-story",
        reply_language=language,
        TASK=task,
        STORY_ID=story.id,
        STORY_TITLE=story.title,
        ACCEPTANCE=acceptance,
        VERIFY=verify,
    )


async def verify_story(ctx: CommandContext, story: Story) -> tuple[bool, str]:
    """Run a story's verification commands on the session backend."""
    if not story.verify:
        return True, "no verification commands; taking the subagent's word for it"
    from snowpea_core.agent.loop import backend_for

    backend = backend_for(ctx.core, ctx.session)
    for command in story.verify:
        try:
            result = await backend.run(command, timeout=300.0)
        except OSError as exc:
            return False, f"{command}: {type(exc).__name__}: {exc}"
        if result.exit_code != 0 or result.timed_out:
            tail = (result.stderr or result.stdout or "").strip().splitlines()
            return False, f"{command} exited {result.exit_code}: {tail[-1] if tail else ''}"
    return True, "all verification commands exited zero"


async def review(ctx: CommandContext, task: str, stories: list[Story]) -> tuple[bool, str]:
    """Ask a reviewer subagent to sign the work off."""
    manager = get_manager(ctx.core)
    reviewer = REVIEWER_AGENT if manager.definition(ctx.session, REVIEWER_AGENT) else None
    summary = "\n".join(f"- {story.id} {story.title}: {story.note}" for story in stories)
    brief = workflow_brief(
        "ralph-review",
        reply_language=reply_language_for(ctx),
        TASK=task,
        SUMMARY=summary,
        APPROVAL_WORD=APPROVAL_WORD,
    )
    result = await manager.run(ctx.session, brief, agent=reviewer)
    text = result.summary or result.error or ""
    return (APPROVAL_WORD in text.upper()), text


async def cmd_ralph(ctx: CommandContext, args: str) -> None:
    """``/ralph <task>`` — drive a task to a reviewed finish."""
    task = args.strip().strip('"').strip("'").strip()
    if not task:
        await ctx.say(USAGE)
        return

    try:
        stories = await build_prd(ctx, task)
    except Exception as exc:  # noqa: BLE001 - a bad PRD ends the command, not the daemon
        await _fail(ctx, f"could not build a PRD for this task: {exc}")
        return
    if not stories:
        await _fail(ctx, "the model did not return any stories for this task")
        return

    write_prd(ctx.session, task, stories, 0)
    append_progress(ctx.session, [f"# ralph: {task}", "", f"{len(stories)} stories planned."])
    await ctx.say(
        "\n".join(
            [f"ralph: {len(stories)} stories planned.", *(f"  {s.id} {s.title}" for s in stories)]
        )
    )

    manager = get_manager(ctx.core)
    limit = manager.limit_for(ctx.session)
    max_iterations = max(1, int(ctx.core.settings.ralph.max_iterations))

    for iteration in range(1, max_iterations + 1):
        batch = ready_stories(stories, limit)
        if not batch:
            break
        language = reply_language_for(ctx)
        results = await asyncio.gather(
            *(manager.run(ctx.session, story_task(task, story, language)) for story in batch),
            return_exceptions=True,
        )
        lines = ["", f"## iteration {iteration}"]
        for story, result in zip(batch, results, strict=True):
            if isinstance(result, BaseException):
                story.note = f"subagent failed: {result}"
                lines.append(f"- {story.id} {story.title}: {story.note}")
                continue
            if not result.ok:
                story.note = f"subagent failed: {result.error or 'no reason given'}"
                lines.append(f"- {story.id} {story.title}: {story.note}")
                continue
            passed, note = await verify_story(ctx, story)
            story.passed = passed
            story.note = note
            lines.append(f"- {story.id} {story.title}: {'PASS' if passed else 'FAIL'} — {note}")
        write_prd(ctx.session, task, stories, iteration)
        append_progress(ctx.session, lines)
        await ctx.say("\n".join([f"ralph iteration {iteration}:", *lines[2:]]))
        if all(story.passed for story in stories):
            break

    failing = [story for story in stories if not story.passed]
    if failing:
        await _fail(
            ctx,
            "ralph stopped after "
            f"{max_iterations} iterations with {len(failing)} story/stories still failing: "
            + ", ".join(story.id for story in failing),
        )
        return

    approved, verdict = await review(ctx, task, stories)
    append_progress(
        ctx.session, ["", "## review", f"{'APPROVED' if approved else 'REJECTED'}: {verdict}"]
    )
    if not approved:
        await _fail(ctx, f"the reviewer did not approve the work:\n{verdict}")
        return
    await ctx.say(
        f"ralph: all {len(stories)} stories pass and the reviewer approved.\n{verdict}".strip()
    )


async def _fail(ctx: CommandContext, message: str) -> None:
    """End the ralph turn unsuccessfully; only approval earns ``complete``."""
    await ctx.say(f"ralph: {message}")
    await ctx.emit(events.error(errors.INTERNAL, message))
    ctx.handled_turn = True
    await ctx.emit(events.turn_done(ctx.turn_id, "error"))


COMMANDS: tuple[Command, ...] = (
    Command(
        name="ralph",
        summary="Drive a task to a reviewed finish: /ralph <task>.",
        run=cmd_ralph,
        args_schema=RALPH_ARGS_SCHEMA,
    ),
)


__all__ = [
    "APPROVAL_WORD",
    "COMMANDS",
    "MAX_STORIES",
    "PRD_NAME",
    "PROGRESS_NAME",
    "STATE_DIR",
    "USAGE",
    "Story",
    "append_progress",
    "build_prd",
    "cmd_ralph",
    "ready_stories",
    "review",
    "state_dir",
    "stories_from_payload",
    "story_task",
    "verify_story",
    "write_prd",
]
