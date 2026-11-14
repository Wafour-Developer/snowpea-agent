"""Goals: keep working toward an objective in rounds until it is really done (M16 P1.6).

A session may carry one goal: an objective, a phase (``active``, ``paused``,
``blocked``, ``complete``), a round cap and a revision.  While it is active and
*armed*, each time a turn ends normally the driver starts another round with a
goal-round message.  The model ends it with the ``goal_update`` tool:

* ``complete`` needs evidence — what was run or looked at that shows the
  objective is met.  "I think it works" is not evidence;
* ``blocked`` is accepted only after the same blocking condition was reported
  in :data:`BLOCKED_STREAK` consecutive rounds, so one failure is a reason to
  try another way, not to stop;
* at the round cap the goal becomes ``blocked`` with code ``round-limit``.

"Armed" is runtime only: after a restart or a resume the goal is shown but no
round starts until a person resumes it (dsh ``packages/goal``).  Every change
bumps ``revision``; the model's update must name the revision it read, so a
stale round cannot overwrite a newer decision.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

Phase = Literal["active", "paused", "blocked", "complete"]
PHASES: tuple[str, ...] = ("active", "paused", "blocked", "complete")

#: Rounds a goal may run when the caller names no cap.
DEFAULT_MAX_ROUNDS = 20
#: Hard ceiling on any cap.
MAX_ROUNDS_CEILING = 256
#: Consecutive rounds the same blocking condition must be reported.
BLOCKED_STREAK = 3
#: Shortest evidence accepted for ``complete``.
MIN_EVIDENCE_CHARS = 20

ROUND_LIMIT = "round-limit"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Goal:
    objective: str
    id: str = field(default_factory=lambda: f"g-{uuid.uuid4().hex[:10]}")
    revision: int = 1
    phase: str = "active"
    max_rounds: int = DEFAULT_MAX_ROUNDS
    rounds: int = 0
    blocked_code: str | None = None
    blocked_message: str | None = None
    #: The condition being counted toward ``blocked``: ``{code, count}``.
    streak_code: str | None = None
    streak_count: int = 0
    evidence: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def touch(self) -> None:
        self.revision += 1
        self.updated_at = _now()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str | None) -> Goal | None:
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict) or not data.get("objective"):
            return None
        known = {key: value for key, value in data.items() if key in cls.__dataclass_fields__}
        goal = cls(**known)
        if goal.phase not in PHASES:
            goal.phase = "paused"
        return goal

    def view(self, armed: bool = False) -> dict[str, Any]:
        """The protocol shape (``goal.changed``, ``goal.get``)."""
        blocked = (
            {"code": self.blocked_code, "message": self.blocked_message or ""}
            if self.phase == "blocked" and self.blocked_code
            else None
        )
        return {
            "id": self.id,
            "revision": self.revision,
            "objective": self.objective,
            "phase": self.phase,
            "maxRounds": self.max_rounds,
            "rounds": self.rounds,
            "armed": armed,
            "blockedReason": blocked,
            "evidence": self.evidence or None,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


def clamp_rounds(value: Any) -> int:
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROUNDS
    return max(1, min(MAX_ROUNDS_CEILING, rounds))


# ---------------------------------------------------------------------------
# state transitions (pure; the caller persists and announces)
# ---------------------------------------------------------------------------


def start(session: Any, objective: str, max_rounds: Any = None) -> Goal:
    text = " ".join(str(objective or "").split())
    if not text:
        raise ValueError("a goal needs an objective")
    goal = Goal(objective=text, max_rounds=clamp_rounds(max_rounds or DEFAULT_MAX_ROUNDS))
    session.goal = goal
    session.goal_armed = True
    return goal


def clear(session: Any) -> None:
    session.goal = None
    session.goal_armed = False


def pause(session: Any) -> Goal | None:
    goal = getattr(session, "goal", None)
    if goal is not None and goal.phase == "active":
        goal.phase = "paused"
        goal.touch()
    session.goal_armed = False
    return goal


def resume(session: Any, extra_rounds: int = 0) -> Goal | None:
    """Re-arm a paused or blocked goal (a person decided to go on)."""
    goal = getattr(session, "goal", None)
    if goal is None or goal.phase == "complete":
        return goal
    if extra_rounds > 0:
        goal.max_rounds = clamp_rounds(goal.max_rounds + extra_rounds)
    elif goal.rounds >= goal.max_rounds:
        goal.max_rounds = clamp_rounds(goal.rounds + DEFAULT_MAX_ROUNDS)
    goal.phase = "active"
    goal.blocked_code = goal.blocked_message = None
    goal.streak_code, goal.streak_count = None, 0
    goal.touch()
    session.goal_armed = True
    return goal


def due(session: Any) -> bool:
    """True when the driver should start another round now."""
    goal = getattr(session, "goal", None)
    return bool(
        goal is not None
        and goal.phase == "active"
        and getattr(session, "goal_armed", False)
        and not getattr(session, "is_subagent", False)
    )


def begin_round(session: Any) -> tuple[Goal, str] | None:
    """Count the next round and return its message, or hit the cap (``None``)."""
    goal = getattr(session, "goal", None)
    if goal is None:
        return None
    if goal.rounds >= goal.max_rounds:
        goal.phase = "blocked"
        goal.blocked_code = ROUND_LIMIT
        goal.blocked_message = f"stopped after {goal.max_rounds} rounds without completing"
        goal.touch()
        session.goal_armed = False
        return None
    goal.rounds += 1
    goal.touch()
    return goal, round_message(goal)


def round_message(goal: Goal) -> str:
    return (
        f"<goal_round>\nRound {goal.rounds}/{goal.max_rounds} of the goal (revision "
        f"{goal.revision}).\nObjective: {goal.objective}\n\n"
        "Treat the workspace as the source of truth, not earlier messages. Check what is "
        "actually done, do the next piece of work, and verify it by running or looking at "
        "it. When the objective is met, call goal_update with phase \"complete\" and the "
        "evidence (commands run and their results, files checked). If something outside "
        "your control blocks you, call goal_update with phase \"blocked\", a short code and "
        "what you need; it is accepted after the same condition holds for "
        f"{BLOCKED_STREAK} rounds, so first try another way.\n</goal_round>"
    )


def update_from_model(
    session: Any,
    *,
    revision: Any,
    phase: str,
    evidence: str = "",
    code: str = "",
    message: str = "",
) -> tuple[bool, str]:
    """Apply a ``goal_update`` call. ``(changed, text for the model)``."""
    goal = getattr(session, "goal", None)
    if goal is None:
        return False, "There is no goal in this session."
    try:
        named = int(revision)
    except (TypeError, ValueError):
        named = -1
    if named != goal.revision:
        return False, (
            f"Stale goal revision {revision}; the goal is now at revision {goal.revision} "
            f"(phase {goal.phase}). Re-read it before updating."
        )
    if goal.phase != "active":
        return False, f"The goal is {goal.phase}; only an active goal can be updated."
    if phase == "complete":
        proof = " ".join(str(evidence or "").split())
        if len(proof) < MIN_EVIDENCE_CHARS:
            return False, (
                "Not marked complete: give the evidence — the commands you ran and what they "
                "printed, or the files and output you checked — not an opinion."
            )
        goal.phase = "complete"
        goal.evidence = proof
        goal.streak_code, goal.streak_count = None, 0
        goal.touch()
        session.goal_armed = False
        return True, "Goal marked complete. Tell the user what was achieved and how you checked."
    if phase == "blocked":
        key = " ".join(str(code or "").split()).lower() or "unspecified"
        if goal.streak_code == key:
            goal.streak_count += 1
        else:
            goal.streak_code, goal.streak_count = key, 1
        if goal.streak_count < BLOCKED_STREAK:
            goal.touch()
            return True, (
                f"Not blocked yet: '{key}' reported {goal.streak_count}/{BLOCKED_STREAK} "
                "rounds in a row. Try a different approach in the next round."
            )
        goal.phase = "blocked"
        goal.blocked_code = key
        goal.blocked_message = " ".join(str(message or "").split()) or key
        goal.touch()
        session.goal_armed = False
        return True, "Goal marked blocked. Tell the user exactly what you need from them."
    if phase == "active":
        goal.streak_code, goal.streak_count = None, 0
        goal.touch()
        return True, "Noted; the goal stays active."
    return False, f"Unknown phase {phase!r}; use complete, blocked or active."


__all__ = [
    "BLOCKED_STREAK",
    "DEFAULT_MAX_ROUNDS",
    "MAX_ROUNDS_CEILING",
    "MIN_EVIDENCE_CHARS",
    "PHASES",
    "ROUND_LIMIT",
    "Goal",
    "begin_round",
    "clamp_rounds",
    "clear",
    "due",
    "pause",
    "resume",
    "round_message",
    "start",
    "update_from_model",
]
