"""Goals in the running daemon (M16 P1.6): persistence, the tool, the round driver.

:mod:`agent.goals` holds the rules; this module stores a goal with its session
row, announces ``goal.changed``, gives the model ``goal_update``, puts the goal
into the runtime-context snapshot, and starts the next round when a turn ended
normally and nothing else is queued.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from snowpea_core.agent import goals
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.goals")

#: Turn outcomes after which an armed goal goes on to its next round.
CONTINUE_AFTER = frozenset({"complete"})
#: Outcomes that disarm the goal: a person stopped it, or it could not go on.
DISARM_AFTER = frozenset({"interrupted", "denied", "error", "budget", "empty_response"})


def view(session: Any) -> dict[str, Any] | None:
    goal = getattr(session, "goal", None)
    return goal.view(armed=bool(getattr(session, "goal_armed", False))) if goal else None


async def publish(core: Core, session: Session) -> None:
    """Store the goal with the session row and announce it."""
    from snowpea_core.session import events

    goal = getattr(session, "goal", None)
    store = getattr(core, "store", None)
    if store is not None and hasattr(store, "update_goal"):
        try:
            await store.update_goal(session.id, goal.to_json() if goal else None)
        except Exception:  # noqa: BLE001 - the goal still works in memory
            log.warning("could not store the goal of %s", session.id, exc_info=True)
    await core.hub.emit_event(session.id, events.goal_changed(view(session)))


def start_round(core: Core, session: Session, who: Any = None) -> bool:
    """Count and start the next round now, or block at the cap. True if one started.

    ``who``: the caller snapshot of the RPC that armed or resumed the goal — the
    rounds run for that caller (security review 7, H-1).
    """
    from snowpea_core.agent import loop

    began = goals.begin_round(session)
    if began is None:
        return False
    _goal, text = began
    loop.start_turn(core, session, text, initiator="system", reason="goal",
                    **({"who": who} if who is not None else {}))
    return True


async def after_turn(core: Core, session: Session) -> str | None:
    """What the drain loop runs next for the goal: a round's text, or ``None``.

    Called when a turn ended and no prompt is queued.  An armed goal after a
    normal end gets its next round; a stop, a denial or an error disarms it so a
    person decides.
    """
    goal = getattr(session, "goal", None)
    if goal is None or goal.phase != "active" or not getattr(session, "goal_armed", False):
        return None
    if getattr(session, "is_subagent", False):
        return None
    reason = getattr(session, "last_turn_reason", None)
    if reason in DISARM_AFTER or session.interrupt.is_set():
        session.goal_armed = False
        await publish(core, session)
        return None
    if reason not in CONTINUE_AFTER:
        return None
    began = goals.begin_round(session)
    await publish(core, session)
    if began is None:
        return None
    return began[1]


# ---------------------------------------------------------------------------
# the model's side
# ---------------------------------------------------------------------------


def runtime_text(core: Any, session: Any) -> str:
    """The goal line of the runtime-context snapshot (M16 P1.2 provider)."""
    goal = getattr(session, "goal", None)
    if goal is None or getattr(session, "is_subagent", False):
        return ""
    if goal.phase == "active":
        return (
            f"Goal (revision {goal.revision}, round {goal.rounds}/{goal.max_rounds}, active): "
            f"{goal.objective}. When it is met, call goal_update with phase \"complete\", "
            f"revision {goal.revision} and the evidence."
        )
    if goal.phase == "blocked":
        return f"Goal blocked ({goal.blocked_code}): {goal.objective}. {goal.blocked_message}"
    if goal.phase == "paused":
        return f"Goal paused: {goal.objective}. Do not work on it unless the user asks."
    return ""


async def goal_update(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    session = ctx.session
    changed, text = goals.update_from_model(
        session,
        revision=args.get("revision"),
        phase=str(args.get("phase") or "").strip().lower(),
        evidence=str(args.get("evidence") or ""),
        code=str(args.get("code") or ""),
        message=str(args.get("message") or ""),
    )
    if changed:
        await publish(ctx.core, session)
    return ToolResult(ok=changed, output=text if changed else "", error=None if changed else text)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="goal_update",
        category="interaction",
        description=(
            "Only when the session has a goal (shown in the runtime context): report it "
            "complete with evidence, blocked with a short code and what you need, or active "
            "after progress. Name the revision you were shown."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "revision": {"type": "integer", "description": "The goal revision you read."},
                "phase": {"type": "string", "enum": ["complete", "blocked", "active"]},
                "evidence": {
                    "type": "string",
                    "description": "complete: commands run and their results, files checked.",
                },
                "code": {
                    "type": "string",
                    "enum": [code for code in goals.BLOCKED_CODES if code != goals.ROUND_LIMIT],
                    "description": (
                        "blocked: needs-input (a decision or answer from the user), "
                        "no-credentials (a key or login), no-access (a permission or "
                        "resource), external (a service or tool outside your control), other."
                    ),
                },
                "message": {"type": "string", "description": "blocked: what you need."},
            },
            "required": ["revision", "phase"],
        },
        permission="read",
        run=goal_update,
    ),
)


__all__ = [
    "TOOLS",
    "after_turn",
    "goal_update",
    "publish",
    "runtime_text",
    "start_round",
    "view",
]
