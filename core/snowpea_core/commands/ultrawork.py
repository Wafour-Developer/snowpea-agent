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
from typing import Any

from snowpea_core.agent.definition import complete_text, parse_generated_json
from snowpea_core.agent.subagent import get_manager
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.providers.base import ChatMessage

log = logging.getLogger("snowpea.commands.ultrawork")

USAGE = 'Usage: /ultrawork "<task>"'

#: Never fan out wider than this, however many subtasks the model invents.
MAX_SUBTASKS = 8

SPLIT_SYSTEM = (
    "You split a development task into independent subtasks that can run at "
    "the same time without touching the same files.\n"
    "Answer with a single JSON object and nothing else.\n"
    'Shape: {"subtasks": [{"id": "T1", "title": "<one line>", '
    '"task": "<self-contained brief for one agent>"}]}\n'
    f"Give at most {MAX_SUBTASKS} subtasks. If the task cannot be split, "
    "return exactly one."
)

ULTRAWORK_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "What to split across parallel subagents."}
    },
    "required": ["task"],
}


def subtasks_from_payload(data: dict[str, Any], fallback: str) -> list[tuple[str, str, str]]:
    """``(id, title, brief)`` triples from the splitter's JSON."""
    raw = data.get("subtasks")
    if not isinstance(raw, list):
        raw = []
    out: list[tuple[str, str, str]] = []
    for index, entry in enumerate(raw[:MAX_SUBTASKS], start=1):
        if isinstance(entry, str):
            out.append((f"T{index}", entry.strip(), entry.strip()))
            continue
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or entry.get("task") or "").strip()
        brief = str(entry.get("task") or entry.get("title") or "").strip()
        if not brief:
            continue
        out.append((str(entry.get("id") or f"T{index}"), title or brief, brief))
    return out or [("T1", fallback, fallback)]


async def split(ctx: CommandContext, task: str) -> list[tuple[str, str, str]]:
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
        return subtasks_from_payload(parse_generated_json(text), task)
    except Exception as exc:  # noqa: BLE001 - an unsplittable task is still a task
        log.info("ultrawork could not split the task (%s); running it whole", exc)
        return [("T1", task, task)]


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
                *(f"  {sid} {title}" for sid, title, _ in subtasks),
            ]
        )
    )

    manager = get_manager(ctx.core)
    results = await asyncio.gather(
        *(manager.run(ctx.session, brief) for _, _, brief in subtasks),
        return_exceptions=True,
    )

    lines: list[str] = [f"ultrawork: merged {len(subtasks)} subtask reports."]
    failures = 0
    for (sid, title, _), result in zip(subtasks, results, strict=True):
        if isinstance(result, BaseException):
            failures += 1
            lines.append(f"\n### {sid} {title} — failed\n{result}")
            continue
        if not result.ok:
            failures += 1
            lines.append(f"\n### {sid} {title} — failed\n{result.error or 'no reason given'}")
            continue
        lines.append(f"\n### {sid} {title}\n{result.summary or '(no report)'}")
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
    "cmd_ultrawork",
    "split",
    "subtasks_from_payload",
]
