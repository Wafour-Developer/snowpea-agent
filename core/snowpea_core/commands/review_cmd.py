"""``/review [what]`` — one reviewer pass over the current diff (M15 §C5).

A reviewer subagent runs **only when it is asked for**: this command, ``/ralph``
at the end of its loop, and a user who says "check this". Nothing else starts
one, which is the decision taken with the user on 2026-09-14 — an automatic
review after every change costs a model turn the user did not ask for.

What the reviewer gets is the working tree's own diff plus the list of files it
touches, never a summary of the change: a review of what someone says the change
does is worthless. The command relays the verdict; the child's report is the
deliverable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.agent.subagent import get_manager
from snowpea_core.agent.team import git
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.prompts.compose import workflow_brief

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

USAGE = "Usage: /review [what to look at]"

#: The definition the pass runs as; a project file of the same name wins.
REVIEWER_AGENT = "reviewer"

#: How much diff the child is handed inline. Past this it reads the files.
MAX_DIFF_CHARS = 24000

REVIEW_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "what": {
            "type": "string",
            "description": "Optional focus: an area, a risk, or a question to answer.",
        }
    },
}


async def working_diff(workdir: str) -> tuple[str, list[str]]:
    """``(diff, changed files)`` for the tree, staged and unstaged together."""
    names = await git(workdir, "diff", "HEAD", "--name-only")
    diff = await git(workdir, "diff", "HEAD")
    if not names.ok:  # no commit yet: everything that is tracked or added
        names = await git(workdir, "diff", "--name-only")
        diff = await git(workdir, "diff")
    files = [line.strip() for line in names.stdout.splitlines() if line.strip()]
    untracked = await git(workdir, "ls-files", "--others", "--exclude-standard")
    files += [line.strip() for line in untracked.stdout.splitlines() if line.strip()]
    return diff.stdout, sorted(dict.fromkeys(files))


def reviewer_for(core: Core, session: Session) -> str | None:
    """``"reviewer"`` when that definition resolves here, else no definition."""
    manager = get_manager(core)
    return REVIEWER_AGENT if manager.definition(session, REVIEWER_AGENT) else None


async def cmd_review(ctx: CommandContext, args: str) -> None:
    """``/review [what]`` — delegate one reviewer pass and relay its verdict."""
    from snowpea_core.agent.agent import reply_language

    focus = args.strip().strip('"').strip("'").strip()
    workdir = str(ctx.session.workdir)
    diff, files = await working_diff(workdir)
    if not diff.strip() and not files:
        await ctx.say(
            "review: the working tree has no uncommitted change to review. "
            "Make the change first, or ask a reviewer about a specific file."
        )
        return

    trimmed = diff[:MAX_DIFF_CHARS]
    if len(diff) > MAX_DIFF_CHARS:
        trimmed += (
            f"\n… [{len(diff) - MAX_DIFF_CHARS} more characters of diff omitted — "
            "read the changed files themselves]"
        )
    brief = workflow_brief(
        "review",
        reply_language=reply_language(ctx.core),
        WORKDIR=workdir,
        FOCUS=(f"The user asked you to look at: {focus}" if focus else ""),
        FILES="\n".join(f"- {name}" for name in files) or "(none reported by git)",
        DIFF=trimmed or "(git reported no textual diff; the files above are new)",
    )
    await ctx.say(f"review: {len(files)} changed file(s) going to the reviewer.")
    result = await get_manager(ctx.core).run(
        ctx.session,
        brief,
        agent=reviewer_for(ctx.core, ctx.session),
        title="Review the current diff",
    )
    verdict = (result.summary or result.error or "").strip()
    await ctx.say(verdict or "review: the reviewer returned nothing.")


COMMANDS: tuple[Command, ...] = (
    Command(
        name="review",
        summary="Review the current diff with a read-only reviewer agent: /review [what].",
        run=cmd_review,
        args_schema=REVIEW_ARGS_SCHEMA,
    ),
)


__all__ = [
    "COMMANDS",
    "MAX_DIFF_CHARS",
    "REVIEWER_AGENT",
    "USAGE",
    "cmd_review",
    "reviewer_for",
    "working_diff",
]
