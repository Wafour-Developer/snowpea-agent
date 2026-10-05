"""``/ultrawork <task>`` — split, fan out, merge (M7 contract §4).

Three steps, all of them code: ask the provider to cut the task into
independent subtasks, run them all through subagents at once (the semaphore in
:mod:`snowpea_core.agent.subagent` is what actually bounds the parallelism),
then merge the reports into one answer.

Differences from the OMC original
---------------------------------
* OMC's ultrawork fans out by asking the model to emit several ``Task`` calls
  in one assistant turn and relies on the harness to run them together; here
  the fan-out is an ``asyncio.gather`` so the concurrency limit is enforced by
  the daemon rather than by the model's formatting.
* OMC merges by asking the model to summarise; snowpea prints each subagent's
  own report under its subtask heading, so nothing is lost in a second
  summarisation pass, and adds a model-written merge only when the provider
  produces one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from snowpea_core.agent import plan_store
from snowpea_core.agent.definition import complete_text, parse_generated_json
from snowpea_core.agent.subagent import get_manager
from snowpea_core.commands import plan_gate
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.prompts.loader import render
from snowpea_core.providers.base import ChatMessage
from snowpea_core.session import events

log = logging.getLogger("snowpea.commands.ultrawork")

USAGE = 'Usage: /ultrawork "<task>"'

#: Never fan out wider than this, however many subtasks the model invents.
MAX_SUBTASKS = 8

SPLIT_SYSTEM = render("workflows/ultrawork-split", MAX_SUBTASKS=MAX_SUBTASKS)

ULTRAWORK_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "What to split across parallel subagents."}
    },
    "required": ["task"],
}


@dataclass
class Subtask:
    """One slice of an ``/ultrawork`` run and the files it claims."""

    id: str
    title: str
    brief: str
    files: tuple[str, ...] = ()
    #: Ids merged into this one because they claimed the same files.
    merged: tuple[str, ...] = ()
    #: Current-plan steps this subtask implements (CORE-plan-continuity).
    plan_step: tuple[str, ...] = ()


def _files(entry: dict[str, Any]) -> tuple[str, ...]:
    """The ``files`` list of one splitter entry, normalised for comparison."""
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


def subtasks_from_payload(data: dict[str, Any], fallback: str) -> list[Subtask]:
    """:class:`Subtask` entries from the splitter's JSON."""
    raw = data.get("subtasks")
    if not isinstance(raw, list):
        raw = []
    out: list[Subtask] = []
    for index, entry in enumerate(raw[:MAX_SUBTASKS], start=1):
        if isinstance(entry, str):
            text = entry.strip()
            if text:
                out.append(Subtask(f"T{index}", text, text))
            continue
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or entry.get("task") or "").strip()
        brief = str(entry.get("task") or entry.get("title") or "").strip()
        if not brief:
            continue
        out.append(
            Subtask(
                str(entry.get("id") or f"T{index}"),
                title or brief,
                brief,
                _files(entry),
                plan_step=plan_gate.step_ids(entry.get("plan_step")),
            )
        )
    return out or [Subtask("T1", fallback, fallback)]


def merge_overlapping(subtasks: list[Subtask]) -> list[Subtask]:
    """Fold subtasks that claim the same file into one brief (M15 §C4).

    Two agents editing one file without a worktree each is how a fan-out ends
    with half of one change and half of the other; the split is a plan, not a
    promise, so it is validated here rather than trusted.  Subtasks that share
    no file are left exactly as the model wrote them.
    """
    owner: dict[str, int] = {}
    groups: list[Subtask] = []
    for task in subtasks:
        target = next(
            (owner[name] for name in task.files if name in owner),
            None,
        )
        if target is None:
            owner.update({name: len(groups) for name in task.files})
            groups.append(task)
            continue
        head = groups[target]
        shared = sorted(name for name in task.files if owner.get(name) == target)
        groups[target] = Subtask(
            id=head.id,
            title=head.title,
            brief=(
                f"{head.brief}\n\n"
                "These two pieces of work were merged because they change the same "
                f"file(s) ({', '.join(shared)}); do both, in one pass, yourself:\n\n"
                f"{task.brief}"
            ),
            files=tuple(dict.fromkeys((*head.files, *task.files))),
            merged=(*head.merged, task.id),
            plan_step=tuple(dict.fromkeys((*head.plan_step, *task.plan_step))),
        )
        owner.update({name: target for name in task.files})
        log.info(
            "ultrawork merged %s into %s: both claim %s",
            task.id,
            head.id,
            ", ".join(shared) or "the same files",
        )
    return groups


async def split(ctx: CommandContext, task: str) -> list[Subtask]:
    """Ask the provider how to cut ``task`` up; one subtask is a valid answer."""
    provider = ctx.core.providers.get(ctx.session.provider, ctx.session.model)
    messages = [
        ChatMessage(role="system", content=SPLIT_SYSTEM),
        ChatMessage(
            role="user",
            content=(
                f"Split this task for the project at {ctx.session.workdir}. "
                f"Reply with the JSON object only.\n\nTask: {task}"
            ),
        ),
    ]
    try:
        text = await complete_text(provider, messages)
        parsed = subtasks_from_payload(parse_generated_json(text), task)
    except Exception as exc:  # noqa: BLE001 - an unsplittable task is still a task
        log.info("ultrawork could not split the task (%s); running it whole", exc)
        return [Subtask("T1", task, task)]
    return merge_overlapping(parsed)


async def cmd_ultrawork(ctx: CommandContext, args: str) -> None:
    """``/ultrawork <task>`` — parallel fan-out over subagents.

    With an active current plan, an empty or execute-only argument splits the
    plan's pending steps instead of the words typed; a real task is split
    with the plan as context (CORE-plan-continuity).
    """
    task, forced = plan_gate.strip_bypass(args)
    task = task.strip().strip('"').strip("'").strip()
    plan = plan_store.active_plan(ctx.session.workdir)
    from_plan = plan is not None and plan_gate.is_execute_request(task, plan)
    if not task and not from_plan:
        await ctx.say(USAGE)
        return
    if plan is None and not await plan_gate.ask_before_running(
        ctx, "ultrawork", task, forced=forced
    ):
        return

    if from_plan:
        assert plan is not None
        only = plan_gate.target_step(task, plan)
        await ctx.say(
            f"ultrawork: splitting the current plan {plan_store.describe(plan)}"
            + (f", step {only} only." if only else ".")
        )
        subtasks = await split(ctx, plan_gate.plan_request(plan, unit="subtask", only=only))
        known = {only} if only else {step.id.upper() for step in plan.steps}
        for part in subtasks:
            part.plan_step = tuple(step for step in part.plan_step if step in known)
        if len(subtasks) == 1 and not subtasks[0].plan_step:
            subtasks[0].plan_step = (
                (only,) if only else tuple(step.id for step in plan_store.pending_steps(plan))
            )
            if subtasks[0].title.startswith("Carry out the user's current plan"):
                subtasks[0].title = plan.title
    else:
        subtasks = await split(ctx, plan_gate.plan_context(task, plan) if plan else task)
    await ctx.say(
        "\n".join(
            [
                f"ultrawork: {len(subtasks)} subtasks, running in parallel.",
                *(f"  {part.id} {part.title}" for part in subtasks),
            ]
        )
    )

    await ctx.emit(
        events.command_progress(
            "ultrawork",
            0,
            [
                {"id": part.id, "title": part.title, "status": "pending", "note": ""}
                for part in subtasks
            ],
        )
    )

    merged = [part for part in subtasks if part.merged]
    if merged:
        await ctx.say(
            "\n".join(
                f"  {part.id} absorbed {', '.join(part.merged)}: they claim the same files."
                for part in merged
            )
        )

    manager = get_manager(ctx.core)
    results = await asyncio.gather(
        *(
            manager.run(
                ctx.session,
                part.brief,
                title=part.title,
                prefer=("executor",),
            )
            for part in subtasks
        ),
        return_exceptions=True,
    )

    lines: list[str] = [f"ultrawork: merged {len(subtasks)} subtask reports."]
    rows: list[dict[str, str]] = []
    failures = 0
    for part, result in zip(subtasks, results, strict=True):
        heading = f"{part.id} {part.title}"
        if isinstance(result, BaseException):
            failures += 1
            lines.append(f"\n### {heading} — failed\n{result}")
            rows.append({"id": part.id, "title": part.title, "status": "fail", "note": str(result)})
            continue
        if not result.ok:
            failures += 1
            reason = result.error or "no reason given"
            lines.append(f"\n### {heading} — failed\n{reason}")
            rows.append({"id": part.id, "title": part.title, "status": "fail", "note": reason})
            continue
        lines.append(f"\n### {heading}\n{result.summary or '(no report)'}")
        rows.append({"id": part.id, "title": part.title, "status": "pass", "note": ""})
    if failures:
        lines.append(f"\n{failures} of {len(subtasks)} subtasks did not finish cleanly.")
    await ctx.say("\n".join(lines))
    if from_plan and plan is not None:
        await _sync_plan(ctx, subtasks, rows, plan.id)
    # One round, so its progress is also the last: it carries the outcome.
    await ctx.emit(
        events.command_progress(
            "ultrawork", 1, rows, outcome="partial" if failures else "complete"
        )
    )


async def _sync_plan(
    ctx: CommandContext, subtasks: list[Subtask], rows: list[dict[str, str]], plan_id: str
) -> None:
    """Mark the plan steps the subtasks implemented: done when every one finished."""
    status: dict[str, list[dict[str, str]]] = {}
    for part, row in zip(subtasks, rows, strict=True):
        for step in part.plan_step:
            status.setdefault(step, []).append(row)
    changes = [
        (step, "done", "ultrawork: subtask finished")
        if all(row["status"] == "pass" for row in done)
        else (
            step,
            "in_progress",
            ("ultrawork: " + next(r["note"] for r in done if r["status"] != "pass"))[:300],
        )
        for step, done in status.items()
    ]
    if changes:
        await plan_store.mark_steps(ctx.session.workdir, changes, emit=ctx.emit, plan_id=plan_id)


COMMANDS: tuple[Command, ...] = (
    Command(
        name="ultrawork",
        summary="Split a task, run the parts in parallel, merge the reports: /ultrawork <task>.",
        run=cmd_ultrawork,
        args_schema=ULTRAWORK_ARGS_SCHEMA,
    ),
)


__all__ = [
    "COMMANDS",
    "MAX_SUBTASKS",
    "USAGE",
    "Subtask",
    "cmd_ultrawork",
    "merge_overlapping",
    "split",
    "subtasks_from_payload",
]
