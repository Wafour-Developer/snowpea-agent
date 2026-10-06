"""Delegation mode: the lead hands the work to its team instead of doing it.

Off by default — the lead works itself and delegates when it judges a task
worth it.  ``/delegation on`` (or ``agents.delegateByDefault``) turns a
session into an orchestrator: implementation, tests and verification go to the
team's agents, and the lead plans, briefs and relays.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.agent.team_config import default_roster
from snowpea_core.prompts.loader import render

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

#: Who does the work when no team is configured: the built-in roles.
FALLBACK_ROSTER: tuple[str, ...] = (
    "executor",
    "test-engineer",
    "verifier",
    "explorer",
    "architect",
    "writer",
)


def effective_delegation(core: Core | None, session: Session) -> tuple[bool, str]:
    """``(on, source)``: the session's pin, else ``agents.delegateByDefault``."""
    pin = getattr(session, "delegation", None)
    if pin is not None:
        return bool(pin), "session"
    settings = getattr(core, "settings", None)
    on = bool(getattr(getattr(settings, "agents", None), "delegateByDefault", False))
    return on, "default"


def delegation_roster(core: Core | None, session: Session) -> tuple[str, ...]:
    """The agents a delegating lead routes to: the session's team, else the default roster."""
    if session.team_agents:
        return tuple(session.team_agents)
    settings = getattr(core, "settings", None)
    if settings is not None:
        roster = default_roster(settings, session.workdir)
        if roster:
            return tuple(roster)
    return FALLBACK_ROSTER


def delegation_prompt(core: Core | None, session: Session) -> str:
    """The lead's delegation-mode brief, or ``""`` when the mode is off or this is a child."""
    if getattr(session, "is_subagent", False) or getattr(session, "parent_session_id", None):
        return ""
    on, _source = effective_delegation(core, session)
    if not on:
        return ""
    return render(
        "fragments/delegation-lead", TEAM_AGENTS=", ".join(delegation_roster(core, session))
    )


__all__ = [
    "FALLBACK_ROSTER",
    "delegation_prompt",
    "delegation_roster",
    "effective_delegation",
]
