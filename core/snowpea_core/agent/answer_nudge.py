"""Answer-now note: a person's question that only reads is told, once, to answer.

A local model asked how something works ("관리자 화면에서 … 어떻게 등록해야
하는지 알려줘") made 36 tool calls in 11 minutes without answering; its own
reasoning said "So the answer: …" seven minutes in.  The round budget (500 for
a person's session) does not catch that, and nothing else says an explanation
ends when it can be given.

So a person's own main turn that has called only read-only tools for
``agent.answerNudgeRounds`` rounds (20; ``0`` turns it off) gets one note
before its next model call.  The note never stops the turn and takes no tool
away.  Whether the message *is* a question is not judged from its words: a
turn that changed anything — a write, an edit, a shell command that is not a
plain inspection, a browser action, a delegation that may write — is work, not
an explanation, and is never nudged.

A browser's host tools keep permission ``read`` because the host gates every
acting call itself, so the tag alone made a booking (``repl`` clicks and form
fills) look like reading, and the note fired mid-task.  A host tool that says
``kind: "act"`` (1.69.0) is work; one that says nothing is work when it is a
``repl`` or a browser-category host tool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger(__name__)

DEFAULT_ROUNDS = 20

NOTE = (
    "[system] You have made {n} rounds of tool calls without changing anything. If you "
    "can answer the question now, answer it now and say what you have not verified; only "
    "continue reading if something essential is still missing."
)

#: Permission tags that only look: a file or search read, a secret read.
_READ_TAGS = frozenset({"read", "secret"})
#: ``network`` tools that only fetch; a browser click or a media job is not one.
_READ_NETWORK_TOOLS = frozenset({"web_search", "web_extract"})
#: Tagged ``read`` but changes how the session works.
_CHANGING_READ_TOOLS = frozenset({"set_mode"})


@dataclass
class AnswerNudge:
    """One turn's state: the round it fires at (0: never), and what happened."""

    rounds: int = 0
    changed: bool = False
    sent: bool = False


def _setting(core: Core) -> int:
    agent = getattr(getattr(core, "settings", None), "agent", None)
    try:
        return max(0, int(getattr(agent, "answerNudgeRounds", DEFAULT_ROUNDS)))
    except (TypeError, ValueError):
        return DEFAULT_ROUNDS


def _eligible(
    core: Core, session: Session, turn_id: str, *, unattended: bool, system: bool
) -> bool:
    """A person's own main-session turn, outside plan mode."""
    if system or unattended or getattr(session, "is_subagent", False):
        return False
    origins = getattr(session, "turn_origins", None) or {}
    if origins.get(turn_id, "human") != "human":
        return False  # Jarvis, schedule, a chat member, an agent, the daemon
    if str(getattr(session, "origin_surface", None) or "").startswith("gateway"):
        return False  # a messenger turn
    if getattr(session, "mode", None) == "plan":
        return False  # plan mode reads by design and ends with its own plan exit
    jarvis = getattr(core, "jarvis", None)
    if jarvis is not None:
        try:
            if jarvis.in_jarvis_family(session):
                return False
        except Exception:  # noqa: BLE001 - when unsure, stay quiet
            return False
    return True


def begin_turn(
    core: Core, session: Session, turn_id: str, *, unattended: bool, system: bool
) -> None:
    rounds = _setting(core)
    if rounds and not _eligible(core, session, turn_id, unattended=unattended, system=system):
        rounds = 0
    session.answer_nudge = AnswerNudge(rounds=rounds)


def _host_acts(name: str, tool: Any) -> bool:
    """A host tool that acts on pages whatever its permission tag says."""
    if tool is None or not str(getattr(tool, "source", "")).startswith("host:"):
        return False
    kind = getattr(tool, "kind", None)
    if kind is not None:
        return kind == "act"
    # A host from before 1.69.0 does not say: its repl and browser tools act.
    return name == "repl" or getattr(tool, "category", None) == "browser"


def _read_only(name: str, tag: str, args: dict[str, Any], tool: Any = None) -> bool:
    if name in _CHANGING_READ_TOOLS or _host_acts(name, tool):
        return False
    if tag in _READ_TAGS:
        return True
    if tag == "network":
        return name in _READ_NETWORK_TOOLS
    if tag == "delegate":
        return bool(args.get("readOnly"))
    if tag == "exec" and name == "shell" and not args.get("background"):
        from snowpea_core.permissions.safe_commands import is_read_only

        return is_read_only(str(args.get("command") or ""))
    return False


def observe_call(
    session: Session, name: str, tag: str, args: dict[str, Any] | None, *, tool: Any = None
) -> None:
    """Note one call the turn attempts; anything but a plain read marks the turn as work."""
    state: AnswerNudge | None = getattr(session, "answer_nudge", None)
    if state is None or state.changed or not state.rounds:
        return
    if not _read_only(name, str(tag), dict(args or {}), tool):
        state.changed = True


def take_due(session: Session) -> str | None:
    """The note when this turn should get it now, at most once; ``None`` otherwise."""
    state: AnswerNudge | None = getattr(session, "answer_nudge", None)
    if state is None or not state.rounds or state.sent or state.changed:
        return None
    if getattr(session, "mode", None) == "plan":
        return None
    if int(getattr(session, "rounds_used", 0) or 0) < state.rounds:
        return None
    state.sent = True
    log.info(
        "answer nudge: session %s made %d read-only tool rounds; asking it to answer",
        getattr(session, "id", "?"),
        session.rounds_used,
    )
    return NOTE.format(n=state.rounds)


__all__ = ["DEFAULT_ROUNDS", "NOTE", "AnswerNudge", "begin_turn", "observe_call", "take_due"]
