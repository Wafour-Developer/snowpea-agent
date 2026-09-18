"""``/init [--force]`` — a fast, rough ``AGENTS.md`` for a repository.

Claude Code's ``/init`` in one turn: look at the repo root, write ``AGENTS.md``,
and stub ``.snowpea/settings.json`` if the project has none yet. Where
``/deepinit`` fans a subagent out per top-level directory and is the deep,
slow path, ``/init`` is the fast one — a single main-agent turn with a
workflow brief, no subagents, no per-directory documents.

Two things happen in code rather than in the model's turn, because they are
deterministic and worth getting right without hoping the model remembers:

* Whether ``AGENTS.md`` already exists (and, when it does, its current
  content) is read here and folded into the brief, so the model does not have
  to spend one of its ~8 tool calls finding out, and so the merge-vs-overwrite
  instruction is unambiguous instead of implicit.
* ``.snowpea/settings.json`` is a typed project-settings file, not freeform
  prose, so it is written the same way ``/mode ... save`` writes it — through
  :class:`ProjectSettings`, not through the model's ``write_file`` tool — and
  only when the file does not already exist. Plan mode reports what it would
  do instead of writing, matching the mode contract (writes are ``deny`` in
  plan) without needing the model to police itself.

``AGENTS.md`` itself is still the model's job: reading the rest of the repo
and writing a good summary is exactly the kind of judgement a one-shot code
path should not try to replace.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from snowpea_core.agent import loop as agent_loop
from snowpea_core.agent.subagent import SubagentResult, get_manager
from snowpea_core.commands.deepinit import interesting_dirs
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.config.project import ProjectSettings
from snowpea_core.prompts.compose import workflow_brief

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

AGENTS_FILE = "AGENTS.md"

#: Characters of an existing AGENTS.md folded into the brief verbatim, so the
#: model can merge without spending a tool call reading it back.
MAX_EXISTING_CHARS = 6000

INIT_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "force": {
            "type": "boolean",
            "description": "Rewrite AGENTS.md from scratch instead of merging into it.",
        }
    },
}


def _wants_force(args: str) -> bool:
    return any(token in ("--force", "-f") for token in args.split())


def agents_status(agents_path: Path, force: bool) -> str:
    """The brief's instruction for what to do about ``AGENTS.md``."""
    if not agents_path.is_file():
        return "There is no AGENTS.md at the project root yet; write a fresh one."
    if force:
        return (
            "--force was given: rewrite AGENTS.md from scratch, discarding its "
            "current content."
        )
    existing = agents_path.read_text(encoding="utf-8", errors="replace")
    if len(existing) > MAX_EXISTING_CHARS:
        return (
            "AGENTS.md already exists at the project root and is too long to quote "
            "here — read_file it first, then merge your findings into it rather "
            "than discarding anything a human wrote by hand."
        )
    return (
        "AGENTS.md already exists at the project root — merge your findings into "
        "it rather than discarding anything a human wrote by hand. Its current "
        f"content:\n---\n{existing}\n---"
    )


def ensure_project_settings(root: Path, *, plan: bool) -> str:
    """Create ``.snowpea/settings.json`` with a default mode, only if absent.

    Returns the line the brief tells the model, so its own summary reflects
    what actually happened here.
    """
    settings_path = ProjectSettings.path_for(root)
    if settings_path.is_relative_to(root):
        relative = settings_path.relative_to(root)
    else:
        relative = settings_path
    if settings_path.is_file():
        return f"{relative} already exists; it was left alone."
    if plan:
        return (
            f'Plan mode: {relative} would be created with defaultMode "accept", '
            "but configuration is not written in plan mode (AGENTS.md itself is)."
        )
    settings = ProjectSettings.load(root)
    settings.defaultMode = "accept"
    settings.save(root)
    return f"{relative} did not exist and was created with defaultMode \"accept\"."


def explore_dir_task(directory: Path, root: Path) -> str:
    """Brief for an explore subagent reading one directory for /init."""
    relative = directory.relative_to(root)
    return (
        f"Explore the code under {relative} and provide a concise summary.\n"
        "Identify: what this directory is for, its main entry points and key files, "
        "the conventions or tech stack used here, and any important caveats.\n"
        "Do NOT write any files. Keep your summary under 15 lines."
    )


async def cmd_init(ctx: CommandContext, args: str) -> None:
    """``/init [--force]`` — write AGENTS.md and stub .snowpea/settings.json."""
    force = _wants_force(args)
    root = Path(ctx.session.workdir)
    plan = ctx.session.mode == "plan"

    directories = interesting_dirs(root)
    dir_summaries_text = ""
    if directories:
        await ctx.say(
            f"init: exploring {len(directories)} top-level "
            f"director{'y' if len(directories) == 1 else 'ies'} in parallel with explore..."
        )
        manager = get_manager(ctx.core)
        tasks = [
            manager.run(
                ctx.session,
                explore_dir_task(directory, root),
                tools=["read_file", "list_dir", "glob", "grep"],
                title=f"explore {directory.relative_to(root)}",
                prefer=("explore", "explorer"),
            )
            for directory in directories
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        lines: list[str] = []
        for directory, res in zip(directories, results):
            rel = directory.relative_to(root)
            if isinstance(res, SubagentResult) and res.summary:
                lines.append(f"### {rel}\n{res.summary.strip()}")
        if lines:
            dir_summaries_text = (
                "Directory summaries gathered from parallel exploration:\n"
                + "\n\n".join(lines)
            )

    settings_note = ensure_project_settings(root, plan=plan)
    _note_agents_md_was_shown(ctx, root / AGENTS_FILE, force=force)
    brief = workflow_brief(
        "init",
        reply_language=_reply_language(ctx),
        AGENTS_PATH=AGENTS_FILE,
        AGENTS_STATUS=agents_status(root / AGENTS_FILE, force),
        SETTINGS_NOTE=settings_note,
        DIRECTORY_SUMMARIES=dir_summaries_text,
    )

    ctx.handled_turn = True
    await agent_loop.run_turn(
        ctx.core,
        ctx.session,
        brief,
        turn_id=ctx.turn_id,
        unattended=ctx.session.origin_conn is None,
    )


def _note_agents_md_was_shown(ctx: CommandContext, agents_path: Path, *, force: bool) -> None:
    """Satisfy the read-before-write guard for the file this brief folds in.

    ``/init`` hands the model the current ``AGENTS.md`` verbatim precisely so it
    does not have to spend a tool call reading it, and ``--force`` is an
    explicit instruction to discard it. Either way the content is in context,
    so recording the read here keeps the guard (M15 §A3) from refusing the
    write the command exists to make. A file too large to fold in is recorded
    as a partial read, and the model is told to read it itself.
    """
    from snowpea_core.tools import file_state

    if not agents_path.is_file():
        return
    try:
        existing = agents_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    file_state.note_read(
        ctx.core,
        ctx.session,
        str(agents_path),
        existing,
        complete=force or len(existing) <= MAX_EXISTING_CHARS,
    )


def _reply_language(ctx: CommandContext) -> str:
    from snowpea_core.agent.agent import reply_language

    return reply_language(ctx.core)


COMMANDS: tuple[Command, ...] = (
    Command(
        name="init",
        summary="Write a fast, rough AGENTS.md for this project: /init [--force].",
        run=cmd_init,
        args_schema=INIT_ARGS_SCHEMA,
    ),
)


__all__ = [
    "AGENTS_FILE",
    "COMMANDS",
    "INIT_ARGS_SCHEMA",
    "MAX_EXISTING_CHARS",
    "agents_status",
    "cmd_init",
    "ensure_project_settings",
    "explore_dir_task",
]
