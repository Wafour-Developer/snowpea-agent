"""The project's current plan, on disk, with per-step progress (CORE-plan-continuity).

``/plan`` wrote a plan file, ``/ralplan`` and ``/deep-interview`` left theirs in
the conversation, and ``/ralph``, ``/team`` and ``/ultrawork`` planned from
nothing but their own argument.  So ``/ralplan`` followed by ``/ralph 실행해줘``
planned the words "실행해줘" — one real run turned an agreed plan into "add
main.py and a Makefile".  And once a conversation was compacted, the plan body
was gone while the user's "그냥 구현해줘" survived.

This module is the one place a plan lives:

* ``<workdir>/.snowpea/plans/current.md`` — the plan, as the planner wrote it;
* ``<workdir>/.snowpea/plans/current.json`` — its id, title, source, status and
  steps (``S1``…), each ``pending``/``in_progress``/``done``/``blocked``;
* ``<workdir>/.snowpea/plans/archive/<stamp>-<slug>.{md,json}`` — every plan a
  newer one replaced.

Claude Code keeps approved plans in a plans directory and re-injects "a plan
file exists, continue it" after compaction; OMC's ralph and team read
``.omc/plans/`` and ``prd.json``.  Same principle here: the commands read the
plan when told to just carry on, the main agent sees one summary line of it
every turn, and whoever finishes a step marks it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snowpea_core.config.paths import utc_now

log = logging.getLogger("snowpea.agent.plan_store")

#: Where the current plan lives, relative to the workdir.
PLANS_DIR = Path(".snowpea") / "plans"
CURRENT_MD = "current.md"
CURRENT_JSON = "current.json"
ARCHIVE_DIR = "archive"
#: The path every prompt line and event shows.
CURRENT_PATH = (PLANS_DIR / CURRENT_MD).as_posix()

SOURCES = ("plan", "ralplan", "deep-interview", "manual")
STATUSES = ("active", "done", "archived")
STEP_STATUSES = ("pending", "in_progress", "done", "blocked")

#: A plan longer than this is a backlog, not a plan; the rest is dropped.
MAX_STEPS = 30
#: Longest step title kept; the markdown still has the whole line.
MAX_STEP_TITLE = 160
#: How much of the markdown a command's planner prompt carries.
MAX_PROMPT_MARKDOWN = 12000


class PlanError(ValueError):
    """A plan operation that cannot be done; the message is what the model is told."""


@dataclass
class Step:
    id: str
    title: str
    status: str = "pending"
    note: str = ""
    updatedAt: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "note": self.note,
            "updatedAt": self.updatedAt,
        }


@dataclass
class Plan:
    id: str
    title: str
    source: str = "manual"
    createdAt: str = ""
    updatedAt: str = ""
    status: str = "active"
    steps: list[Step] = field(default_factory=list)
    #: The plan text; read from ``current.md``, not stored in the JSON.
    markdown: str = ""
    #: Hash of the markdown the steps were last read against, so a hand edit
    #: of ``current.md`` is noticed (:func:`load_current`).
    markdownHash: str = ""
    #: True when the steps came from the markdown rather than the caller; only
    #: those are re-derived after a hand edit.
    stepsDerived: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "createdAt": self.createdAt,
            "updatedAt": self.updatedAt,
            "status": self.status,
            "markdownHash": self.markdownHash,
            "stepsDerived": self.stepsDerived,
            "steps": [step.to_json() for step in self.steps],
        }

    def step(self, step_id: str) -> Step | None:
        wanted = str(step_id).strip().upper()
        return next((step for step in self.steps if step.id.upper() == wanted), None)


# ---------------------------------------------------------------------------
# deriving steps from markdown
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
#: ``**Steps**`` or ``Steps:`` on a line of its own reads as a heading too: plan
#: mode's template names its sections that way and models copy it.
_BOLD_HEADING = re.compile(r"^\s*\*\*([^*]{1,60})\*\*\s*:?\s*$")
_COLON_HEADING = re.compile(r"^\s*([^\s:|`-][^:|`]{0,40}):\s*$")
#: ``Steps — numbered …``: plan mode's own section lines.
_DASH_HEADING = re.compile(r"^\s*([A-Za-z가-힣][\w가-힣 ]{0,30}?)\s+[—–-]\s+\S")
_NUMBERED = re.compile(r"^\s{0,3}(\d{1,3})[.)]\s+(.+?)\s*$")
_STEP_WORDS = re.compile(
    r"\b(?:steps?|plan|implementation|tasks?|milestones?)\b|단계|계획|구현|작업",
    re.IGNORECASE,
)
_SPEC_WORDS = re.compile(
    r"\bacceptance\b|\bcriteria\b|완료\s*조건|수용\s*기준|인수\s*조건", re.IGNORECASE
)


def _heading(line: str) -> str | None:
    for pattern in (_HEADING, _BOLD_HEADING, _COLON_HEADING):
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return None


def _clean_title(text: str) -> str:
    text = re.sub(r"[*_`]+", "", text).strip()
    if len(text) > MAX_STEP_TITLE:
        text = text[: MAX_STEP_TITLE - 1].rstrip() + "…"
    return text


def _sections(markdown: str) -> list[tuple[str, list[str]]]:
    """``(heading, numbered items directly under it)`` in document order."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        numbered = _NUMBERED.match(line)
        if numbered:
            sections[-1][1].append(_clean_title(numbered.group(2)))
            continue
        heading = _heading(line)
        if heading is None:
            dash = _DASH_HEADING.match(line)
            heading = dash.group(1).strip() if dash and _STEP_WORDS.search(dash.group(1)) else None
        if heading is not None:
            sections.append((heading, []))
    return sections


def derive_steps(markdown: str, title: str = "") -> list[Step]:
    """Steps from the plan's own numbered list.

    The numbered list under a "Steps"/"Plan"/"단계" heading wins; a spec has
    none, so its "Acceptance criteria" list is next; then the first numbered
    list anywhere.  A plan with no numbered list at all is one step: the plan.
    """
    sections = [(heading, items) for heading, items in _sections(markdown) if items]
    chosen: list[str] = []
    for words in (_STEP_WORDS, _SPEC_WORDS):
        found = next((items for heading, items in sections if words.search(heading)), None)
        if found:
            chosen = found
            break
    if not chosen and sections:
        chosen = sections[0][1]
    if not chosen:
        chosen = [_clean_title(title) or "carry out the plan"]
    now = utc_now()
    return [
        Step(id=f"S{index}", title=text, updatedAt=now)
        for index, text in enumerate(chosen[:MAX_STEPS], start=1)
        if text
    ]


def _steps_from_args(raw: Any) -> list[Step]:
    """Caller-given steps (``[{id?, title}]`` or plain strings), ids filled in."""
    if not isinstance(raw, list):
        return []
    now = utc_now()
    steps: list[Step] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw[:MAX_STEPS], start=1):
        if isinstance(entry, str):
            entry = {"title": entry}
        if not isinstance(entry, dict):
            continue
        title = _clean_title(str(entry.get("title") or entry.get("content") or ""))
        if not title:
            continue
        step_id = str(entry.get("id") or "").strip().upper() or f"S{index}"
        while step_id in seen:
            step_id = f"{step_id}x"
        seen.add(step_id)
        status = str(entry.get("status") or "pending").strip()
        steps.append(
            Step(
                id=step_id,
                title=title,
                status=status if status in STEP_STATUSES else "pending",
                updatedAt=now,
            )
        )
    return steps


# ---------------------------------------------------------------------------
# on disk
# ---------------------------------------------------------------------------


def plans_dir(workdir: Path | str) -> Path:
    return Path(workdir) / PLANS_DIR


def _slug(title: str) -> str:
    text = re.sub(r"[^\w]+", "-", title.casefold()).strip("-_")
    return text[:40].rstrip("-_") or "plan"


def _read(workdir: Path | str) -> Plan | None:
    directory = plans_dir(workdir)
    try:
        data = json.loads((directory / CURRENT_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not str(data.get("title") or "").strip():
        return None
    steps: list[Step] = []
    for entry in data.get("steps") or []:
        if not isinstance(entry, dict) or not str(entry.get("id") or "").strip():
            continue
        status = str(entry.get("status") or "pending")
        steps.append(
            Step(
                id=str(entry["id"]).strip(),
                title=str(entry.get("title") or "").strip(),
                status=status if status in STEP_STATUSES else "pending",
                note=str(entry.get("note") or ""),
                updatedAt=str(entry.get("updatedAt") or ""),
            )
        )
    status = str(data.get("status") or "active")
    try:
        markdown = (directory / CURRENT_MD).read_text(encoding="utf-8")
    except OSError:
        markdown = ""
    return Plan(
        id=str(data.get("id") or ""),
        title=str(data["title"]).strip(),
        source=str(data.get("source") or "manual"),
        createdAt=str(data.get("createdAt") or ""),
        updatedAt=str(data.get("updatedAt") or ""),
        status=status if status in STATUSES else "active",
        steps=steps,
        markdown=markdown,
        markdownHash=str(data.get("markdownHash") or ""),
        stepsDerived=bool(data.get("stepsDerived", False)),
    )


def _hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]


def _rederive(plan: Plan) -> None:
    """Steps re-read from an edited markdown, each keeping the status of its namesake."""
    old = {step.title.casefold(): step for step in plan.steps}
    fresh = derive_steps(plan.markdown, plan.title)
    for step in fresh:
        before = old.get(step.title.casefold())
        if before is not None:
            step.status, step.note, step.updatedAt = before.status, before.note, before.updatedAt
    plan.steps = fresh


def _replace(target: Path, text: str) -> None:
    """Write ``text`` whole and rename it into place: never half a file."""
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(target)


def _write(workdir: Path | str, plan: Plan, *, markdown: bool = False) -> None:
    directory = plans_dir(workdir)
    directory.mkdir(parents=True, exist_ok=True)
    if markdown:
        _replace(directory / CURRENT_MD, plan.markdown)
    # A reader in another turn must never see half a step list.
    _replace(
        directory / CURRENT_JSON, json.dumps(plan.to_json(), ensure_ascii=False, indent=2) + "\n"
    )


def load_current(workdir: Path | str) -> Plan | None:
    """The current plan; ``None`` when there is none, it is unreadable, or archived.

    A ``current.md`` edited by hand since the steps were read has its steps
    re-derived (when they came from the markdown in the first place), each
    keeping the status of the step with the same title.
    """
    plan = _read(workdir)
    if plan is None or plan.status == "archived":
        return None
    digest = _hash(plan.markdown)
    if plan.markdown.strip() and digest != plan.markdownHash:
        if plan.stepsDerived:
            _rederive(plan)
        plan.markdownHash = digest
        try:
            _write(workdir, plan)
        except OSError:
            log.debug("could not record the edited plan in %s", workdir, exc_info=True)
    return plan


def active_plan(workdir: Path | str) -> Plan | None:
    """The current plan while it still has work left; what the commands run."""
    plan = load_current(workdir)
    if plan is None or plan.status != "active" or not pending_steps(plan):
        return None
    return plan


def _copy_to_archive(workdir: Path | str) -> Path | None:
    """Copy the current plan into ``archive/`` (status ``archived``); it stays current."""
    directory = plans_dir(workdir)
    plan = _read(workdir)
    if plan is None:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = directory / ARCHIVE_DIR
    archive.mkdir(parents=True, exist_ok=True)
    base = archive / f"{stamp}-{_slug(plan.title)}"
    plan.status = "archived"
    plan.updatedAt = utc_now()
    base.with_suffix(".json").write_text(
        json.dumps(plan.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    base.with_suffix(".md").write_text(plan.markdown, encoding="utf-8")
    return base.with_suffix(".md")


def archive_current(workdir: Path | str) -> Path | None:
    """Abandon the current plan: archive it and leave no current plan.

    Returns the archived markdown path, or ``None`` when there was no plan.
    """
    directory = plans_dir(workdir)
    try:
        archived = _copy_to_archive(workdir)
        if archived is None:
            return None
        (directory / CURRENT_JSON).unlink(missing_ok=True)
        (directory / CURRENT_MD).unlink(missing_ok=True)
    except OSError:
        log.warning("could not archive the current plan in %s", directory, exc_info=True)
        return None
    return archived


def save_plan(
    workdir: Path | str,
    title: str,
    markdown: str,
    steps: Any = None,
    source: str = "manual",
) -> Plan:
    """Make this the current plan, archiving the one it replaces.

    ``steps`` is ``[{id?, title}]``; without it the steps come from the
    markdown's numbered list (:func:`derive_steps`).
    """
    title = " ".join(str(title or "").split())
    markdown = str(markdown or "")
    if not title:
        heading = next((h for h in (_heading(line) for line in markdown.splitlines()) if h), "")
        title = re.sub(r"^(?:plan|계획)\s*:\s*", "", heading, flags=re.IGNORECASE).strip()
    if not title:
        raise PlanError("a plan needs a title")
    if not markdown.strip():
        raise PlanError("a plan needs its markdown")
    now = utc_now()
    given = _steps_from_args(steps)
    markdown = markdown if markdown.endswith("\n") else markdown + "\n"
    plan = Plan(
        # The random tail keeps two plans saved in the same second apart: the
        # commands key their step marks to this id.
        id=f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_slug(title)}-{uuid.uuid4().hex[:6]}",
        title=title,
        source=source if source in SOURCES else "manual",
        createdAt=now,
        updatedAt=now,
        status="active",
        steps=given or derive_steps(markdown, title),
        markdown=markdown,
        markdownHash=_hash(markdown),
        stepsDerived=not given,
    )
    finish_if_done(plan)
    # The old plan is copied to the archive first and the new one then
    # replaces it atomically: a failure at any point leaves one whole plan
    # current, never none.
    _copy_to_archive(workdir)
    _write(workdir, plan, markdown=True)
    return plan


def mark_step(workdir: Path | str, step_id: str, status: str, note: str = "") -> Plan:
    """Set one step's status (and note); the plan is ``done`` once every step is."""
    if status not in STEP_STATUSES:
        raise PlanError(f"status must be one of {', '.join(STEP_STATUSES)}; got {status!r}")
    plan = load_current(workdir)
    if plan is None:
        raise PlanError(f"there is no current plan ({CURRENT_PATH} is missing)")
    step = plan.step(step_id)
    if step is None:
        known = ", ".join(step.id for step in plan.steps) or "none"
        raise PlanError(f"the current plan has no step {step_id!r}; its steps are {known}")
    now = utc_now()
    step.status = status
    if note or status == "done":
        step.note = str(note or "").strip()
    step.updatedAt = now
    plan.updatedAt = now
    if not finish_if_done(plan) and plan.status == "done" and status != "done":
        # A step re-opened after the plan was finished puts it back to work.
        plan.status = "active"
    _write(workdir, plan)
    return plan


def pending_steps(plan: Plan) -> list[Step]:
    """Every step not yet done, in plan order."""
    return [step for step in plan.steps if step.status != "done"]


def finish_if_done(plan: Plan) -> bool:
    """Mark an active plan ``done`` once every step is; True when it changed."""
    if plan.status == "active" and plan.steps and not pending_steps(plan):
        plan.status = "done"
        return True
    return False


def _next(plan: Plan) -> Step | None:
    pending = pending_steps(plan)
    return next((step for step in pending if step.status == "in_progress"), None) or (
        pending[0] if pending else None
    )


def age(plan: Plan, now: datetime | None = None) -> str:
    """How long ago the plan was saved: ``just now``, ``5m``, ``3h``, ``2d``."""
    try:
        created = datetime.fromisoformat(plan.createdAt.replace("Z", "+00:00"))
    except ValueError:
        return "at an unknown time"
    seconds = ((now or datetime.now(UTC)) - created).total_seconds()
    if seconds < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"  # pragma: no cover - unreachable


def provenance(plan: Plan, now: datetime | None = None) -> str:
    """``saved 3d ago by ralplan`` — so a stale plan reads as one."""
    return f"saved {age(plan, now)} by {plan.source}"


def describe(plan: Plan) -> str:
    """``"<title>" (saved 3d ago by ralplan)``, for what a command says it runs."""
    return f'"{plan.title}" ({provenance(plan)})'


def summary_line(plan: Plan, now: datetime | None = None) -> str:
    """``Current plan: <title> (saved 3d ago by ralplan) — 2/7 steps done; next: S3 …``."""
    done = sum(step.status == "done" for step in plan.steps)
    nxt = _next(plan)
    progress = f"{done}/{len(plan.steps)} steps done"
    if nxt is not None:
        progress += f"; next: {nxt.id} {nxt.title}"
    return (
        f"Current plan: {plan.title} ({provenance(plan, now)}) — {progress} ({CURRENT_PATH})"
    )


def steps_text(plan: Plan) -> str:
    """One line per step with its status, for a planner prompt."""
    return "\n".join(
        f"- {step.id} [{step.status}] {step.title}" + (f" — {step.note}" if step.note else "")
        for step in plan.steps
    )


def prompt_markdown(plan: Plan) -> str:
    text = plan.markdown.strip()
    if len(text) > MAX_PROMPT_MARKDOWN:
        text = text[:MAX_PROMPT_MARKDOWN].rstrip() + "\n… (truncated; read the file for the rest)"
    return text


def prompt_line(workdir: Path | str) -> str:
    """The volatile-tier line for an active plan, or ``""``."""
    try:
        plan = load_current(workdir)
    except Exception:  # noqa: BLE001 - a broken plan file must never break a turn
        return ""
    if plan is None or plan.status != "active":
        return ""
    return summary_line(plan)


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def event_for(plan: Plan) -> Any:
    from snowpea_core.session import events

    nxt = _next(plan)
    return events.plan_updated(
        plan.title,
        plan.status,
        sum(step.status == "done" for step in plan.steps),
        len(plan.steps),
        f"{nxt.id} {nxt.title}" if nxt is not None else None,
        CURRENT_PATH,
    )


async def publish(core: Any, session: Any, plan: Plan) -> None:
    """Emit ``plan.updated`` for ``session``; never fails the caller."""
    hub = getattr(core, "hub", None)
    if hub is None or session is None:
        return
    try:
        await hub.emit_event(session.id, event_for(plan))
    except Exception:  # noqa: BLE001 - a dead listener must not fail the work
        log.debug("could not emit plan.updated for %s", getattr(session, "id", ""), exc_info=True)


async def mark_steps(
    workdir: Path | str,
    changes: list[tuple[str, str, str]],
    emit: Any = None,
    *,
    plan_id: str,
) -> Plan | None:
    """Apply ``(step_id, status, note)`` changes; emit once through ``emit(event)``.

    For the commands, keyed to the plan they started from: when the current
    plan is a different one (replaced mid-run) or no longer active, nothing is
    touched — a run of the old plan must never mark the new plan's ``S1`` —
    and a finished plan is never reopened.  Unknown steps are skipped, and
    nothing is written when nothing changes.
    """
    plan = load_current(workdir)
    if plan is None or plan.id != plan_id or plan.status != "active":
        return None
    changed = False
    now = utc_now()
    for step_id, status, note in changes:
        step = plan.step(step_id)
        if status not in STEP_STATUSES or step is None or plan.status != "active":
            continue
        if step.status == status and (not note or step.note == note):
            continue
        step.status = status
        if note or status == "done":
            step.note = str(note or "").strip()
        step.updatedAt = plan.updatedAt = now
        finish_if_done(plan)
        changed = True
    if changed:
        _write(workdir, plan)
    if changed and emit is not None:
        try:
            await emit(event_for(plan))
        except Exception:  # noqa: BLE001 - progress events are best-effort
            log.debug("could not emit plan.updated", exc_info=True)
    return plan if changed else None


# ---------------------------------------------------------------------------
# plan mode: a plan written as a file
# ---------------------------------------------------------------------------


def is_plan_document(workdir: Path | str, path: str) -> bool:
    """A .md/.markdown/.txt file in the project, but not the store's own files.

    Called only for writes made in PLAN mode, where documents are all a
    planner may write: whichever one it wrote last is its plan.
    """
    try:
        root = Path(workdir).resolve()
        target = Path(path)
        if not target.is_absolute():
            target = root / target
        relative = target.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    if relative.suffix.lower() not in (".md", ".markdown", ".txt"):
        return False
    parts = relative.parts
    if parts[:2] == PLANS_DIR.parts:
        return len(parts) == 3 and parts[2] != CURRENT_MD
    return bool(parts)


def note_written(session: Any, tool: str, args: dict[str, Any]) -> None:
    """Remember a plan document written in PLAN mode, for :func:`adopt_written_plan`."""
    if tool not in ("write_file", "patch") or getattr(session, "mode", None) != "plan":
        return
    path = str(args.get("path") or "").strip()
    if not path or not is_plan_document(session.workdir, path):
        return
    files = session.plan_files
    if path in files:
        files.remove(path)
    files.append(path)


def adopt_written_plan(session: Any) -> Plan | None:
    """Register the newest plan file this session wrote, when ``plan_save`` never ran.

    Called as a session leaves PLAN mode: a planner that wrote its plan as a
    file but forgot ``plan_save`` still leaves a plan ``/ralph`` can run.
    """
    if getattr(session, "plan_saved", False) or not getattr(session, "plan_files", None):
        return None
    root = Path(session.workdir)
    for path in reversed(session.plan_files):
        target = Path(path) if Path(path).is_absolute() else root / path
        try:
            markdown = target.read_text(encoding="utf-8")
        except OSError:
            continue
        if not markdown.strip():
            continue
        title = next(
            (h for h in (_heading(line) for line in markdown.splitlines()) if h),
            target.stem.replace("-", " ").replace("_", " "),
        )
        title = re.sub(r"^(?:plan|계획)\s*:\s*", "", title, flags=re.IGNORECASE).strip()
        try:
            plan = save_plan(session.workdir, title or target.stem, markdown, source="plan")
        except (PlanError, OSError):
            continue
        session.plan_saved = True
        return plan
    return None


def _looks_like_a_plan(text: str) -> bool:
    """A heading over a numbered list: what a finished plan or spec looks like."""
    return any(
        items and (_STEP_WORDS.search(heading) or _SPEC_WORDS.search(heading))
        for heading, items in _sections(text)
    )


def adopt_from_history(session: Any, messages: list[Any], source: str) -> Plan | None:
    """Save the newest plan-shaped assistant message of a turn as the current plan.

    The fallback for ``/ralplan`` and ``/deep-interview`` turns that ended
    without ``plan_save``: the plan was written into the conversation, and a
    conversation is exactly where it got lost before.
    """
    for message in reversed(messages):
        if getattr(message, "role", None) != "assistant":
            continue
        content = getattr(message, "content", "")
        text = content if isinstance(content, str) else ""
        if not text.strip() or not _looks_like_a_plan(text):
            continue
        title = next((h for h in (_heading(line) for line in text.splitlines()) if h), "")
        title = re.sub(r"^(?:plan|계획|spec)\s*:\s*", "", title, flags=re.IGNORECASE).strip()
        try:
            return save_plan(session.workdir, title or "plan", text, source=source)
        except (PlanError, OSError):
            return None
    return None


__all__ = [
    "CURRENT_PATH",
    "MAX_STEPS",
    "PLANS_DIR",
    "SOURCES",
    "STEP_STATUSES",
    "Plan",
    "PlanError",
    "Step",
    "active_plan",
    "adopt_from_history",
    "adopt_written_plan",
    "age",
    "describe",
    "archive_current",
    "derive_steps",
    "event_for",
    "finish_if_done",
    "is_plan_document",
    "load_current",
    "mark_step",
    "mark_steps",
    "note_written",
    "pending_steps",
    "plans_dir",
    "prompt_line",
    "prompt_markdown",
    "provenance",
    "publish",
    "save_plan",
    "steps_text",
    "summary_line",
]
