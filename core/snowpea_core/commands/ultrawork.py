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

from snowpea_core.agent.definition import complete_text, parse_generated_json
from snowpea_core.agent.subagent import get_manager
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.prompts.loader import render
from snowpea_core.providers.base import ChatMessage

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
            Subtask(str(entry.get("id") or f"T{index}"), title or brief, brief, _files(entry))
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
    """``/ultrawork <task>`` — parallel fan-out over subagents."""
    task = args.strip().strip('"').strip("'").strip()
    if not task:
        await ctx.say(USAGE)
        return

    subtasks = await split(ctx, task)
    await ctx.say(
        "\n".join(
            [
                f"ultrawork: {len(subtasks)} subtasks, running in parallel.",
                *(f"  {part.id} {part.title}" for part in subtasks),
            ]
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
    failures = 0
    for part, result in zip(subtasks, results, strict=True):
        heading = f"{part.id} {part.title}"
        if isinstance(result, BaseException):
            failures += 1
            lines.append(f"\n### {heading} — failed\n{result}")
            continue
        if not result.ok:
            failures += 1
            lines.append(f"\n### {heading} — failed\n{result.error or 'no reason given'}")
            continue
        lines.append(f"\n### {heading}\n{result.summary or '(no report)'}")
    if failures:
        lines.append(f"\n{failures} of {len(subtasks)} subtasks did not finish cleanly.")
    await ctx.say("\n".join(lines))


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
