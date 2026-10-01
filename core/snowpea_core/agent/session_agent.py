"""An agent definition applied to a human session (addendum 17).

``session.create {agent}`` used to set only the label and the model route, so
a definition's prompt, tool allowlist and round budget shaped subagents and
named agents but never the chat a person types into — the snowpea-browser
plugin's ``browser`` rules (agent tab only, page safety, no settings changes)
did not reach the browser's own chat. :func:`apply_agent` applies all three;
``session.setAgent`` and ``/agent use`` switch it later.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.agent.definition import AgentDefinition
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.agent.session_agent")


class UnknownAgent(ValueError):
    """No definition of that name is visible from the session's project."""


def find_definition(core: Any, session: Session, name: str) -> AgentDefinition | None:
    from snowpea_core.commands.agent_cmd import definitions_for

    for defn in definitions_for(core, session.workdir):
        if defn.name == name:
            return defn
    return None


def apply_agent(core: Any, session: Session, name: str | None) -> AgentDefinition | None:
    """Give ``session`` the persona, tools and round budget of ``name``.

    ``None`` clears them: the session goes back to the plain assistant. Raises
    :class:`UnknownAgent` when ``name`` has no definition here. The prompt is
    the persona layer on top of the base rules, as for a named agent; a
    ``tools: "*"`` definition allows every tool.
    """
    if not name:
        session.agent = None
        session.system_prompt = None
        session.allowed_tools = None
        session.tool_rounds = None
        session.agent_applied = True
        return None
    defn = find_definition(core, session, name)
    if defn is None:
        raise UnknownAgent(f"no agent definition named {name!r} for {session.workdir}")
    session.agent = defn.name
    session.system_prompt = defn.prompt.strip() or None
    tools = defn.tool_list()
    session.allowed_tools = set(tools) if tools is not None else None
    session.tool_rounds = defn.max_tool_rounds or defn.tool_rounds or None
    session.agent_applied = True
    return defn


def ensure_applied(core: Any, session: Session) -> None:
    """Re-apply a human session's stored agent once after a resume or restart."""
    if getattr(session, "agent_applied", False) or session.kind != "chat":
        return
    if not session.agent:
        session.agent_applied = True
        return
    try:
        apply_agent(core, session, session.agent)
    except UnknownAgent:
        log.warning("session %s: agent %s is gone; running without it", session.id, session.agent)
        session.agent_applied = True


async def switch_agent(core: Any, session: Session, name: str | None) -> str | None:
    """Apply ``name`` now, persist it, and tell every surface (``session.setAgent``)."""
    from snowpea_core.session import events

    apply_agent(core, session, name)
    if getattr(core, "store", None) is not None:
        await core.store.update_agent(session.id, session.agent)
    await core.hub.emit_event(session.id, events.agent_changed(session.agent))
    await core.sessions.announce_sessions_changed("agent", session.id)
    return session.agent


__all__ = ["UnknownAgent", "apply_agent", "ensure_applied", "find_definition", "switch_agent"]
