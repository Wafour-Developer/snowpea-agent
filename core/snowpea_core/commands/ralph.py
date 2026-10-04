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
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.agent import session_reply_language
from snowpea_core.agent.definition import DefinitionError, complete_text, parse_generated_json
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

#: Agent definition asked to sign the work off.  A project that ships its own
#: ``architect`` keeps it; otherwise the built-in read-only ``reviewer`` runs
#: the pass, because a review by an agent that can also edit is not a review
#: (M15 §C5).
REVIEWER_AGENT = "architect"
FALLBACK_REVIEWER = "reviewer"

#: The word the reviewer has to say.
APPROVAL_WORD = "APPROVE"
_REVIEW_APPROVED = re.compile(
    r"^\s*(?:verdict\s*:\s*)?[*_`]*(?:APPROVE|APPROVED)[*_`]*(?=\s|[:—-]|$)",
    re.IGNORECASE,
)
_REVIEW_REJECTED = re.compile(
    r"^\s*(?:verdict\s*:\s*)?[*_`]*(?:REJECT|REJECTED|REVISE|CHANGES?\s+REQUESTED)"
    r"[*_`]*(?=\s|[:—-]|$)",
    re.IGNORECASE,
)
_REVIEW_NOT_APPROVED = re.compile(r"\bnot\s+approved?\b", re.IGNORECASE)

#: Ceiling on stories, so a chatty model cannot make the loop unbounded.
MAX_STORIES = 8
MIN_STORIES = 1

PRD_SYSTEM = render(
    "workflows/ralph-prd", MIN_STORIES=MIN_STORIES, MAX_STORIES=MAX_STORIES
)

#: The second PRD request, after a reply that held no usable JSON object.
PRD_RETRY_INSTRUCTION = (
    "Your previous reply could not be used. Reply with only a JSON object, no prose, "
    'no code fences. Shape: {"stories": [{"id": "S1", "title": "<one line>", '
    '"acceptance": "<how we know it is done>", "verify": ["<shell command>"], '
    '"independent": true, "depends_on": []}]}'
)

#: JSON mode for the retry, on vendors whose preset says they take it.
JSON_MODE = {"type": "json_object"}

#: Longest title the single-story fallback PRD takes from the task.
FALLBACK_TITLE_CHARS = 80

#: Asks the model to rewrite one story's verification commands.
CHECK_REPAIR_SYSTEM = (
    "You fix the verification commands of one story in a PRD for an autonomous coding "
    "agent. The commands run from the project root in a POSIX shell; each must exit "
    "non-zero while the story is not done and zero once it is. Reply with only a JSON "
    'object, no prose, no code fences: {"verify": ["<shell command>", ...]}. '
    "Python that needs try/except, with, for, if or def blocks cannot be a one-line "
    "python -c: write it as a script through a heredoc (python3 - <<'PY' ... PY) or as "
    "a temporary file, and use assert for the condition being checked."
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
    #: Normalised signature of the last failed check; ``""`` after a pass.
    failure: str = ""
    #: True when the last failure was the check itself erroring (a syntax
    #: error, a missing command), not the work falling short of it.
    check_broken: bool = False

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


def supports_json_mode(provider: Any) -> bool:
    """True when ``provider``'s vendor preset says it honours JSON mode."""
    return bool(getattr(getattr(provider, "preset", None), "supports_json_mode", False))


def fallback_prd(task: str) -> list[Story]:
    """One story that is the whole task, for when the model gives no PRD."""
    first_line = next((line.strip() for line in task.splitlines() if line.strip()), task)
    title = first_line
    if len(title) > FALLBACK_TITLE_CHARS:
        title = title[: FALLBACK_TITLE_CHARS - 1].rstrip() + "…"
    return [Story(id="S1", title=title or "task", acceptance=task)]


async def build_prd(ctx: CommandContext, task: str) -> tuple[list[Story], str | None]:
    """Ask the session's provider for the story list.

    Returns ``(stories, fallback_reason)``.  A reply with no usable JSON object
    (or no stories) is asked for once more, more strictly and in JSON mode where
    the vendor takes it; when that fails too, the PRD is the task as a single
    story and ``fallback_reason`` says why.  A provider error still raises.
    """
    provider = ctx.core.providers.get(ctx.session.provider, ctx.session.model)
    messages = [
        ChatMessage(role="system", content=PRD_SYSTEM),
        ChatMessage(
            role="user",
            content=(
                "Write the PRD for this task in the project at "
                f"{ctx.session.workdir}. This call has no tools: do not say you will look "
                "at the project first, write the PRD from the task now. Reply with the "
                f"JSON object only.\n\nTask: {task}"
            ),
        ),
    ]
    reason = ""
    for attempt in range(2):
        if attempt:
            messages = [*messages, ChatMessage(role="user", content=PRD_RETRY_INSTRUCTION)]
        text = await complete_text(
            provider,
            messages,
            response_format=JSON_MODE if attempt and supports_json_mode(provider) else None,
        )
        try:
            stories = stories_from_payload(parse_generated_json(text))
        except DefinitionError as exc:
            reason = str(exc)
            log.info("ralph PRD attempt %d unusable: %s", attempt + 1, reason)
            continue
        if stories:
            return stories, None
        reason = "the model did not return any stories"
    return fallback_prd(task), reason


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
    """``agent.replyLanguage`` (or the UI locale) for this run; children cannot see it."""
    return session_reply_language(ctx.core, ctx.session)


def story_task(
    task: str, story: Story, language: str = "auto", reviewer_feedback: str = ""
) -> str:
    """The brief one implementation subagent receives."""
    acceptance = f"Acceptance criteria: {story.acceptance}\n" if story.acceptance else ""
    if reviewer_feedback.strip():
        acceptance += (
            "Reviewer feedback to address before re-verifying: "
            f"{reviewer_feedback.strip()}\n"
        )
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


_PYTHON = re.compile(r"(?:^|/)python(?:\d+(?:\.\d+)?)?$")
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})


def _python_c_code(command: str) -> list[str]:
    """The code of every ``python -c '<code>'`` in ``command``."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    found: list[str] = []
    for index, token in enumerate(tokens):
        if not _PYTHON.search(token):
            continue
        for later in range(index + 1, len(tokens) - 1):
            option = tokens[later]
            if option in _SHELL_SEPARATORS or not option.startswith("-"):
                break
            if option == "-c":
                found.append(tokens[later + 1])
                break
    return found


def check_problem(command: str) -> str | None:
    """Why ``command`` cannot run as written, or ``None`` when it looks valid.

    ``python -c`` code is compiled; the whole line goes through ``bash -n``
    when bash is on this machine.  Neither runs anything.
    """
    for code in _python_c_code(command):
        try:
            compile(code, "<check>", "exec")
        except SyntaxError as exc:
            return f"python -c: {exc.msg} (line {exc.lineno})"
    bash = shutil.which("bash")
    if bash is None:
        return None
    try:
        checked = subprocess.run(
            [bash, "-n", "-c", command], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if checked.returncode != 0:
        lines = (checked.stderr or "").strip().splitlines()
        return f"shell: {lines[-1] if lines else 'syntax error'}"
    return None


def check_is_broken(exit_code: int, output: str) -> bool:
    """True when a failed check failed on its own terms, not on the work.

    Only the unambiguous cases count: the shell could not find or run the
    command, the shell could not parse it, or Python could not compile the
    ``-c`` code (``File "<string>"`` is how Python names that code).  An
    assertion, an import error or a failing test is the work, not the check.
    """
    if exit_code in (126, 127):
        return True
    lowered = output.lower()
    if "command not found" in lowered or "syntax error near unexpected token" in lowered:
        return True
    if "unexpected eof while looking for matching" in lowered:
        return True
    return 'File "<string>"' in output and any(
        name in output for name in ("SyntaxError", "IndentationError", "TabError")
    )


def failure_signature(command: str, exit_code: int, output: str) -> str:
    """A check failure with numbers, addresses and spacing taken out."""
    tail = "\n".join(output.strip().splitlines()[-5:])
    text = f"{command}\x00{exit_code}\x00{tail}".lower()
    text = re.sub(r"0x[0-9a-f]+", "0x", text)
    text = re.sub(r"\d+(?:\.\d+)?", "#", text)
    return " ".join(text.split())


async def verify_story(ctx: CommandContext, story: Story) -> tuple[bool, str]:
    """Run a story's verification commands on the session backend.

    A failure also leaves its signature in :attr:`Story.failure` and whether
    the check itself was at fault in :attr:`Story.check_broken`.
    """
    story.failure = ""
    story.check_broken = False
    if not story.verify:
        return True, "no verification commands; taking the subagent's word for it"
    from snowpea_core.agent.loop import backend_for

    backend = backend_for(ctx.core, ctx.session)
    for command in story.verify:
        try:
            result = await backend.run(command, timeout=300.0)
        except OSError as exc:
            story.failure = failure_signature(command, -1, f"{type(exc).__name__}: {exc}")
            return False, f"{command}: {type(exc).__name__}: {exc}"
        if result.exit_code != 0 or result.timed_out:
            output = "\n".join(part for part in (result.stderr, result.stdout) if part)
            story.failure = failure_signature(command, result.exit_code, output)
            story.check_broken = check_is_broken(result.exit_code, output)
            tail = (result.stderr or result.stdout or "").strip().splitlines()
            return False, f"{command} exited {result.exit_code}: {tail[-1] if tail else ''}"
    return True, "all verification commands exited zero"


async def repair_checks(
    ctx: CommandContext, task: str, story: Story, problem: str
) -> list[str] | None:
    """Ask the model for new verification commands for ``story``; ``None`` on failure."""
    try:
        provider = ctx.core.providers.get(ctx.session.provider, ctx.session.model)
        messages = [
            ChatMessage(role="system", content=CHECK_REPAIR_SYSTEM),
            ChatMessage(
                role="user",
                content=(
                    f"Task: {task}\nStory {story.id}: {story.title}\n"
                    f"Acceptance: {story.acceptance or '(none given)'}\n"
                    "Current verification commands:\n"
                    + "\n".join(f"- {command}" for command in story.verify)
                    + f"\n\nWhat is wrong with them: {problem}"
                ),
            ),
        ]
        text = await complete_text(
            provider,
            messages,
            response_format=JSON_MODE if supports_json_mode(provider) else None,
        )
        verify = _as_list(parse_generated_json(text).get("verify"))
    except Exception as exc:  # noqa: BLE001 - a failed repair keeps the old check
        log.info("ralph could not repair %s's checks: %s", story.id, exc)
        return None
    return verify or None


async def validate_checks(ctx: CommandContext, task: str, stories: list[Story]) -> list[str]:
    """Repair checks that cannot run as written, before any work starts.

    Returns one line per story whose checks were invalid.  A repair that is
    still invalid keeps the original; it then fails as a broken check, gets one
    more repair, and the repeated-failure stop ends the loop if that fails too.
    """
    lines: list[str] = []
    for story in stories:
        problems = [
            f"{command}: {problem}"
            for command in story.verify
            if (problem := check_problem(command)) is not None
        ]
        if not problems:
            continue
        revised = await repair_checks(ctx, task, story, "; ".join(problems))
        if revised and all(check_problem(command) is None for command in revised):
            story.verify = revised
            lines.append(f"- {story.id}: invalid check rewritten ({problems[0]})")
        else:
            lines.append(f"- {story.id}: invalid check could not be repaired ({problems[0]})")
    return lines


def reviewer_agent(manager: Any, session: Any) -> str | None:
    """The definition ``/ralph`` signs off with, or ``None`` for no definition.

    A project (or global) ``architect`` is what the loop has always used and
    still wins; when the only ``architect`` in scope is the built-in role, the
    built-in ``reviewer`` runs instead — same evidence discipline, no write
    tools.
    """
    architect = manager.definition(session, REVIEWER_AGENT)
    if architect is not None and architect.source != "builtin":
        return REVIEWER_AGENT
    if manager.definition(session, FALLBACK_REVIEWER) is not None:
        return FALLBACK_REVIEWER
    return REVIEWER_AGENT if architect is not None else None


def review_approved(text: str) -> bool:
    """True only for an explicit positive reviewer verdict near the top."""
    lines = [line.strip(" \t>*_`-") for line in text.strip().splitlines()[:8]]
    lines = [line for line in lines if line]
    if any(_REVIEW_NOT_APPROVED.search(line) or _REVIEW_REJECTED.match(line) for line in lines):
        return False
    return any(_REVIEW_APPROVED.match(line) for line in lines)


async def review(ctx: CommandContext, task: str, stories: list[Story]) -> tuple[bool, str]:
    """Ask a reviewer subagent to sign the work off."""
    manager = get_manager(ctx.core)
    reviewer = reviewer_agent(manager, ctx.session)
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
    return review_approved(text), text


def history_summary(
    task: str, stories: list[Story], outcome: str, detail: str = ""
) -> str:
    """Concise workflow handoff for the next ordinary model turn."""
    rows = [
        f"Ralph workflow finished with outcome: {outcome}.",
        f"Task: {task}",
        "Stories:",
    ]
    for story in stories:
        status = "PASS" if story.passed else "FAIL" if story.note else "PENDING"
        note = f" — {story.note}" if story.note else ""
        rows.append(f"- {story.id} {story.title}: {status}{note}")
    if detail.strip():
        rows.extend(["", "Final detail:", detail.strip()])
    rows.extend(["", f"State files: {STATE_DIR / PRD_NAME}, {STATE_DIR / PROGRESS_NAME}"])
    return "\n".join(rows)


async def _review_until_approved(
    ctx: CommandContext,
    task: str,
    stories: list[Story],
    *,
    start_iteration: int,
    max_iterations: int,
) -> tuple[bool, str, int]:
    """Run reviewer sign-off, repairing rejected feedback within the iteration cap."""
    manager = get_manager(ctx.core)
    limit = manager.limit_for(ctx.session)
    language = reply_language_for(ctx)
    iteration = start_iteration
    approved, verdict = await review(ctx, task, stories)
    reviewer_feedback = verdict
    append_progress(
        ctx.session, ["", "## review", f"{'APPROVED' if approved else 'REJECTED'}: {verdict}"]
    )
    if approved:
        return True, verdict, iteration

    for story in stories:
        story.passed = False
        story.note = f"review rejected: {verdict}"
    await _progress(ctx, iteration, stories)
    while iteration < max_iterations:
        batch = ready_stories(stories, max(1, limit))
        if not batch:
            verdict = "review feedback repair could not find an unblocked story to run"
            break
        iteration += 1
        await ctx.say(
            "ralph: reviewer requested changes; continuing with reviewer feedback "
            f"(iteration {iteration}/{max_iterations})."
        )
        lines = ["", f"## iteration {iteration} (review feedback)"]
        results = await asyncio.gather(
            *(
                manager.run(
                    ctx.session,
                    story_task(task, story, language, reviewer_feedback=reviewer_feedback),
                    prefer=("executor",),
                )
                for story in batch
            ),
            return_exceptions=True,
        )
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
        await _progress(ctx, iteration, stories)
        if not all(story.passed for story in stories):
            continue
        approved, verdict = await review(ctx, task, stories)
        reviewer_feedback = verdict
        append_progress(
            ctx.session,
            ["", "## review", f"{'APPROVED' if approved else 'REJECTED'}: {verdict}"],
        )
        if approved:
            return True, verdict, iteration
        for story in stories:
            story.passed = False
            story.note = f"review rejected: {verdict}"
        await _progress(ctx, iteration, stories)
    return False, verdict, iteration


#: An HTTP status a provider error names (``HTTP 400``, ``status 400``,
#: ``Error code: 400``). A 4xx other than a timeout or a rate limit is the same
#: answer on every retry (a bad parameter, a refused key), so ralph stops.
_HTTP_STATUS = re.compile(r"(?:http|status|error code)[\s:=]*(\d{3})", re.IGNORECASE)
_RETRYABLE_4XX = frozenset({408, 409, 425, 429})


def deterministic_error(text: str) -> bool:
    """True for a provider error that retrying cannot fix."""
    lowered = text.lower()
    if "rate limit" in lowered or "too many requests" in lowered:
        return False
    for match in _HTTP_STATUS.finditer(text):
        status = int(match.group(1))
        if 400 <= status < 500 and status not in _RETRYABLE_4XX:
            return True
    return "unknown parameter" in lowered or "invalid_request_error" in lowered


async def cmd_ralph(ctx: CommandContext, args: str) -> None:
    """``/ralph <task>`` — drive a task to a reviewed finish."""
    task = args.strip().strip('"').strip("'").strip()
    if not task:
        await ctx.say(USAGE)
        return

    try:
        stories, fallback_reason = await build_prd(ctx, task)
    except Exception as exc:  # noqa: BLE001 - a bad PRD ends the command, not the daemon
        await _progress(ctx, 0, [], outcome="error")
        await _fail(ctx, f"could not build a PRD for this task: {exc}", task=task)
        return
    if fallback_reason is not None:
        await ctx.say(
            f"ralph: no usable PRD from the model ({fallback_reason}); "
            "running the task as a single story."
        )

    repaired = await validate_checks(ctx, task, stories)
    if repaired:
        await ctx.say("\n".join(["ralph: checked the PRD's verification commands:", *repaired]))

    write_prd(ctx.session, task, stories, 0)
    append_progress(ctx.session, [f"# ralph: {task}", "", f"{len(stories)} stories planned."])
    await ctx.say(
        "\n".join(
            [f"ralph: {len(stories)} stories planned.", *(f"  {s.id} {s.title}" for s in stories)]
        )
    )
    await _progress(ctx, 0, stories)

    manager = get_manager(ctx.core)
    limit = manager.limit_for(ctx.session)
    max_iterations = max(1, int(ctx.core.settings.ralph.max_iterations))

    #: The failures of the previous iteration, to notice an error that repeats.
    previous_errors: set[str] = set()
    #: The last iteration that ran, for the final ``command.progress``.
    last = 0
    for iteration in range(1, max_iterations + 1):
        # A story whose check broke gets its check rewritten this round, not
        # its work redone: the work may well be fine.
        broken = [story for story in stories if not story.passed and story.check_broken]
        batch = [story for story in ready_stories(stories, limit) if not story.check_broken]
        if not batch and not broken:
            break
        last = iteration
        lines = ["", f"## iteration {iteration}"]
        #: Stories whose check failed exactly as it did the round before.
        stuck: list[Story] = []
        for story in broken:
            before = story.failure
            revised = await repair_checks(ctx, task, story, story.note)
            if revised:
                story.verify = revised
            passed, note = await verify_story(ctx, story)
            story.passed = passed
            story.note = f"check rewritten; {note}" if revised else note
            if not passed and story.failure and story.failure == before:
                stuck.append(story)
            verdict = "PASS" if passed else "FAIL"
            lines.append(f"- {story.id} {story.title}: {verdict} — {story.note}")
        language = reply_language_for(ctx)
        results = await asyncio.gather(
            *(
                manager.run(
                    ctx.session,
                    story_task(task, story, language),
                    prefer=("executor",),
                )
                for story in batch
            ),
            return_exceptions=True,
        )
        errors_now: set[str] = set()
        for story, result in zip(batch, results, strict=True):
            if isinstance(result, BaseException):
                story.note = f"subagent failed: {result}"
                errors_now.add(str(result))
                lines.append(f"- {story.id} {story.title}: {story.note}")
                continue
            if not result.ok:
                story.note = f"subagent failed: {result.error or 'no reason given'}"
                errors_now.add(str(result.error or "no reason given"))
                lines.append(f"- {story.id} {story.title}: {story.note}")
                continue
            before = story.failure
            passed, note = await verify_story(ctx, story)
            story.passed = passed
            story.note = note
            if not passed and story.failure and story.failure == before:
                stuck.append(story)
            lines.append(f"- {story.id} {story.title}: {'PASS' if passed else 'FAIL'} — {note}")
        write_prd(ctx.session, task, stories, iteration)
        append_progress(ctx.session, lines)
        await ctx.say("\n".join([f"ralph iteration {iteration}:", *lines[2:]]))
        await _progress(ctx, iteration, stories)
        if all(story.passed for story in stories):
            break
        # Every subagent failed, and for a reason retrying cannot change: a
        # deterministic provider error, or the same failure as last time. Ten
        # identical HTTP 400s used to take five minutes to give up.
        all_failed = len(errors_now) > 0 and all(
            isinstance(r, BaseException) or not r.ok for r in results
        )
        if all_failed:
            fatal = next((e for e in errors_now if deterministic_error(e)), None)
            if fatal is not None or errors_now == previous_errors:
                reason = fatal or next(iter(errors_now))
                append_progress(ctx.session, ["", f"stopped early: {reason}"])
                await _progress(ctx, iteration, stories, outcome="stopped")
                await _fail(
                    ctx,
                    f"stopped after iteration {iteration}: every subagent failed with an error "
                    f"retrying will not fix: {reason}",
                    task=task,
                    stories=stories,
                )
                return
        previous_errors = errors_now
        # The same check failing the same way twice in a row will fail that way
        # a third time: a broken check once ran ten times over seven minutes.
        if stuck:
            reason = "; ".join(f"{story.id}: {story.note}" for story in stuck)
            append_progress(ctx.session, ["", f"stopped early: same check failure twice: {reason}"])
            await _progress(ctx, iteration, stories, outcome="stopped")
            await _fail(
                ctx,
                f"stopped after iteration {iteration}: a check failed the same way twice in a "
                f"row: {reason}",
                task=task,
                stories=stories,
            )
            return

    failing = [story for story in stories if not story.passed]
    if failing:
        # Out of iterations, or nothing left runnable (a dependency never passed).
        outcome = "max_iterations" if last >= max_iterations else "stopped"
        await _progress(ctx, last, stories, outcome=outcome)
        await _fail(
            ctx,
            "ralph stopped after "
            f"{max_iterations} iterations with {len(failing)} story/stories still failing: "
            + ", ".join(story.id for story in failing),
            task=task,
            stories=stories,
        )
        return

    approved, verdict, last = await _review_until_approved(
        ctx, task, stories, start_iteration=last, max_iterations=max_iterations
    )
    if not approved:
        await _progress(ctx, last, stories, outcome="rejected")
        await _fail(
            ctx,
            "the reviewer did not approve the work after repair attempts:\n" + verdict,
            task=task,
            stories=stories,
        )
        return
    message = (
        f"ralph: all {len(stories)} stories pass and the reviewer approved.\n{verdict}"
    ).strip()
    ctx.set_history_summary(history_summary(task, stories, "complete", verdict))
    await ctx.say(message)
    await _progress(ctx, last, stories, outcome="complete")


def progress_stories(stories: list[Story]) -> list[dict[str, str]]:
    """Stories as ``command.progress`` rows; one not attempted yet is pending."""
    return [
        {
            "id": story.id,
            "title": story.title,
            "status": "pass" if story.passed else ("fail" if story.note else "pending"),
            "note": story.note,
        }
        for story in stories
    ]


async def _progress(
    ctx: CommandContext, iteration: int, stories: list[Story], outcome: str | None = None
) -> None:
    """Emit ``command.progress`` beside the text lines (snowpea-browser)."""
    await ctx.emit(events.command_progress("ralph", iteration, progress_stories(stories), outcome))


async def _fail(
    ctx: CommandContext,
    message: str,
    *,
    task: str = "",
    stories: list[Story] | None = None,
) -> None:
    """End the ralph turn unsuccessfully; only approval earns ``complete``."""
    if task or stories:
        ctx.set_history_summary(history_summary(task, stories or [], "error", message))
        await ctx.record_history(reason="error")
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
    "FALLBACK_REVIEWER",
    "REVIEWER_AGENT",
    "MAX_STORIES",
    "PRD_NAME",
    "PROGRESS_NAME",
    "STATE_DIR",
    "USAGE",
    "Story",
    "append_progress",
    "build_prd",
    "check_is_broken",
    "check_problem",
    "cmd_ralph",
    "fallback_prd",
    "progress_stories",
    "ready_stories",
    "review",
    "repair_checks",
    "reviewer_agent",
    "state_dir",
    "stories_from_payload",
    "story_task",
    "validate_checks",
    "verify_story",
    "write_prd",
]
