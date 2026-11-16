"""What each slash command persists, for the browser gate (security review 3).

``command.run`` from a browser connection is judged by what the command would
write, with the same rules as the equivalent RPC (``server/settings_gate``):

``session``
    Changes only the session (or only reads): no marker needed.
``host``
    Persists what a browser may write unmarked, like ``job.*`` / ``memory.delete``.
``person``
    Persists a marker-required key (providers, models, setup, MCP servers,
    allowlist rules, project files, skills, …): a browser 0.3.0+ needs
    ``actor: "person"``.

``jarvis_on`` adds that the change is part of Jarvis's authority chain while
Jarvis mode is on (refused from a browser with ``details.reason: "jarvis_on"``).

Every built-in command must have an entry (``tests/test_security_review3.py``
checks it); an unknown built-in is treated as ``person`` at runtime.  Commands
from skill or command files only send a prompt, so they are ``session``.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Persists:
    level: str  # "session" | "host" | "person"
    #: Part of Jarvis's authority chain while Jarvis mode is on.
    jarvis_on: bool = False
    #: The models/providers key it changes (checked against Jarvis's chain).
    key: tuple[str, ...] | None = None


SESSION = Persists("session")
HOST = Persists("host")
PERSON = Persists("person")


def _words(args: str) -> list[str]:
    try:
        return shlex.split(args or "")
    except ValueError:
        return (args or "").split()


def _first(args: str) -> str:
    words = _words(args)
    return words[0].lower() if words else ""


def _model(args: str) -> Persists:
    words = _words(args)
    if not words:
        return SESSION
    if words[0].lower() == "default":
        return Persists("person", key=("models", "default"))
    return Persists("person", key=("models",))


def _mcp(args: str) -> Persists:
    sub = _first(args)
    if sub in ("", "list", "show"):
        return SESSION
    words = _words(args)
    read = any(w == "--permission" and i + 1 < len(words) and words[i + 1] == "read"
               for i, w in enumerate(words)) or "--permission=read" in words
    return Persists("person", jarvis_on=sub == "add" and read)


def _memory(args: str) -> Persists:
    sub = _first(args)
    if sub in ("forget", "delete"):
        return HOST
    return SESSION


def _allowlist(args: str) -> Persists:
    return PERSON if _first(args) == "remove" else SESSION


def _mode(args: str) -> Persists:
    return PERSON if "save" in {w.lower() for w in _words(args)} else SESSION


def _agent(args: str) -> Persists:
    return PERSON if _first(args) in ("create", "new", "add", "remove", "delete") else SESSION


def _skill(args: str) -> Persists:
    return SESSION if _first(args) in ("", "list", "show") else PERSON


def _busy(args: str) -> Persists:
    return SESSION if not _words(args) else PERSON


#: Built-in command -> what it persists, by its arguments.
COMMAND_PERSISTS: dict[str, Callable[[str], Persists]] = {
    "accept": lambda _a: SESSION,
    "auto": lambda _a: SESSION,
    "plan": lambda _a: SESSION,
    "mode": _mode,
    "agent": _agent,
    "allow": lambda _a: Persists("person", jarvis_on=True),  # rules pre-approve before routing
    "allowlist": _allowlist,
    "approvals": lambda _a: SESSION,
    "busy": _busy,
    "compact": lambda _a: SESSION,
    "deepinit": lambda _a: PERSON,  # writes AGENTS.md files in the project
    "delegate": lambda _a: SESSION,
    "delegation": lambda _a: SESSION,
    "desktop": lambda _a: PERSON,  # launches or installs a program
    "effort": lambda _a: SESSION,
    "goal": lambda _a: SESSION,
    "help": lambda _a: SESSION,
    "init": lambda _a: PERSON,
    "login": lambda _a: PERSON,  # provider sign-in (provider.loginWeb)
    "mcp": _mcp,
    "memory": _memory,
    "model": _model,
    "models": lambda _a: SESSION,
    "ralph": lambda _a: SESSION,
    "review": lambda _a: SESSION,
    "schedule": lambda _a: HOST,  # like job.* (a jarvis-task job is a person's anyway)
    "setup": lambda _a: PERSON,
    "skill": _skill,
    "team": lambda _a: PERSON,  # project roster and team files
    "tools": lambda _a: SESSION,
    "ultrawork": lambda _a: SESSION,
    "update": lambda _a: PERSON,  # installs software
    "vision": lambda _a: Persists("person", key=("models",)),
    "worker": lambda _a: SESSION,
    "workers": lambda _a: SESSION,
}


#: Where a project's own commands and skills come from (``skills/loader.py``).
PROJECT_SOURCES: frozenset[str] = frozenset({"project", "claude-project", "codex-project"})


def persists(name: str, args: str, source: str = "builtin") -> Persists:
    """What running ``/name args`` would persist (unknown built-ins: ``person``).

    A command from a skill or command file the person installed (global,
    plugins, Claude Code's and Codex's own) only sends a prompt: ``session``,
    judged by its tool calls.  A PROJECT's own command is ``person`` for every
    non-person caller (security review 5, M2): the project folder wrote its
    body and its ``allowed-tools``, so a browser needs the marker and a
    connector or an unknown in-process caller may not start it.
    """
    if source != "builtin":
        return PERSON if source in PROJECT_SOURCES else SESSION
    judge = COMMAND_PERSISTS.get(name)
    return judge(args) if judge is not None else PERSON


__all__ = ["COMMAND_PERSISTS", "Persists", "persists"]
