"""Which slash commands may run beside a running turn (1.55.0).

A slash command sent while a model turn runs used to start at once and take
over the session's turn bookkeeping (``current_turn``, ``turn_task``): the
running turn could no longer be interrupted and ``session.list`` showed the
chat idle.  Every built-in is now one of two kinds:

``instant``
    Reads, or changes a session setting that is safe mid-turn.  While a turn
    runs it runs beside it: it never touches ``current_turn``/``turn_task``,
    writes nothing to the history (the running turn's tool calls and results
    must stay adjacent), and emits no ``turn.started``/``turn.done`` and no
    ``message.done``: one ``command.result`` session event carries its output
    and, for a setting, from when it applies (:data:`APPLIES_FROM`).  With no
    turn running it runs as before, as a turn of its own.
``turn``
    Starts work (a model turn, a workflow, a question, a rewrite of the
    history, an install).  While a turn runs it waits: it is queued like a
    prompt (``turn.queued``, then ``turn.dequeued`` and its own turn); a
    ``whenBusy: "steer"`` cannot fold a command into a running request, so it
    is queued too.

Commands from skill or command files run the agent loop, so they are ``turn``.
Every built-in must have an entry (``tests/test_command_kinds.py``); an
unknown built-in counts as ``turn``.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable

INSTANT = "instant"
TURN = "turn"

#: When a setting an instant command changes reaches the running turn.
NOW = "now"
NEXT_TOOL_CALL = "next_tool_call"
NEXT_REQUEST = "next_request"
NEXT_TURN = "next_turn"


def _words(args: str) -> list[str]:
    try:
        return shlex.split(args or "")
    except ValueError:
        return (args or "").split()


def _first(args: str) -> str:
    words = _words(args)
    return words[0].lower() if words else ""


def _instant(_args: str) -> str:
    return INSTANT


def _turn(_args: str) -> str:
    return TURN


def _mode_switch(args: str) -> str:
    # ``/plan`` alone switches; ``/plan <request>`` runs the request as a turn
    # (``/plan clear`` archives the plan file: no model, instant).
    rest = (args or "").strip().lower()
    return INSTANT if rest in ("", "clear", "--clear") else TURN


def _reading(*subs: str) -> Callable[[str], str]:
    def judge(args: str) -> str:
        return INSTANT if _first(args) in ("", *subs) else TURN

    return judge


#: Built-in command -> its kind, by its arguments.
COMMAND_KINDS: dict[str, Callable[[str], str]] = {
    "help": _instant,
    "tools": _instant,
    "approvals": _instant,
    "allow": _instant,
    "allowlist": _instant,
    "busy": _instant,
    "model": _instant,
    "models": _instant,
    "vision": _instant,
    "effort": _instant,
    "mode": _instant,
    "plan": _mode_switch,
    "accept": _mode_switch,
    "auto": _mode_switch,
    "delegation": _instant,
    "memory": _instant,
    "schedule": _instant,
    "desktop": _instant,
    # Asks a question to confirm what it adds or removes: waits for the turn.
    "mcp": _reading("list", "show"),
    "agent": _reading("list", "show"),
    "skill": _reading("list", "show"),
    "update": _reading("check"),
    "compact": _turn,
    "goal": _turn,
    "delegate": _turn,
    "ralph": _turn,
    "review": _turn,
    "ultrawork": _turn,
    "deepinit": _turn,
    "init": _turn,
    "setup": _turn,
    "login": _turn,
    "team": _turn,
    "workers": _turn,
    "worker": _turn,
}

#: For an instant command that changes a setting: when the running turn sees it.
APPLIES_FROM: dict[str, str] = {
    # The permission policy reads the mode, the rules and the vision model at
    # every tool call.
    "mode": NEXT_TOOL_CALL,
    "plan": NEXT_TOOL_CALL,
    "accept": NEXT_TOOL_CALL,
    "auto": NEXT_TOOL_CALL,
    "allow": NEXT_TOOL_CALL,
    "allowlist": NEXT_TOOL_CALL,
    "vision": NEXT_TOOL_CALL,
    # A server's tools are synced at every round boundary.
    "mcp": NEXT_REQUEST,
    # The model and its effort are fixed when a turn starts; busy handling and
    # delegation are read for the next prompt.
    "model": NEXT_TURN,
    "models": NEXT_TURN,
    "effort": NEXT_TURN,
    "busy": NEXT_TURN,
    "delegation": NEXT_TURN,
}


def command_kind(name: str, args: str = "", source: str = "builtin") -> str:
    """``instant`` or ``turn`` for ``/name args`` (unknown built-ins: ``turn``)."""
    if source != "builtin":
        return TURN
    judge = COMMAND_KINDS.get(name)
    return judge(args) if judge is not None else TURN


def applies_from(name: str, args: str = "") -> str | None:
    """When a mid-turn ``/name args`` reaches the running turn.

    ``None`` for a read (``/model`` alone, ``/memory``, ``/schedule`` …) or a
    change that does not concern the running turn at all.
    """
    if not (args or "").strip() and name not in ("plan", "accept", "auto"):
        return None  # ``/model`` alone shows; it changes nothing
    return APPLIES_FROM.get(name)


__all__ = [
    "APPLIES_FROM",
    "COMMAND_KINDS",
    "INSTANT",
    "NEXT_REQUEST",
    "NEXT_TOOL_CALL",
    "NEXT_TURN",
    "NOW",
    "TURN",
    "applies_from",
    "command_kind",
]
