"""Pick a team/builtin agent for delegated work (role → general → none).

Commands and ``delegate_task`` share one rule:

1. Prefer a role suited to the task (``prefer``).
2. Else the configured general agent (default ``executor``), when it is on
   the roster / defined.
3. Else ``None`` — the caller either spawns an anonymous child, or runs the
   work on the parent (main) agent, depending on ``agents.missingRole``.

A session with an active/default team only draws from that roster, so
``/team use`` is respected.  With no team, any matching definition counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.agent.subagent import SubagentManager
    from snowpea_core.session.session import Session

#: Default generalist when no specialised role fits.
DEFAULT_GENERAL_AGENTS: tuple[str, ...] = ("executor",)

MissingRole = Literal["general", "anonymous", "parent"]

#: ``SubagentResult.reason`` when the runtime asks the parent to do the work.
PARENT = "parent"


def general_agents_for(core: Any) -> tuple[str, ...]:
    """``agents.generalAgent`` — a name, a list, or the executor default."""
    agents = getattr(getattr(core, "settings", None), "agents", None)
    raw = getattr(agents, "generalAgent", None)
    if raw is None or raw == "":
        return DEFAULT_GENERAL_AGENTS
    if isinstance(raw, str):
        name = raw.strip()
        return (name,) if name else DEFAULT_GENERAL_AGENTS
    if isinstance(raw, (list, tuple)):
        names = tuple(str(item).strip() for item in raw if str(item).strip())
        return names or DEFAULT_GENERAL_AGENTS
    return DEFAULT_GENERAL_AGENTS


def missing_role_policy(core: Any) -> MissingRole:
    """``agents.missingRole``: ``general`` | ``anonymous`` | ``parent``."""
    agents = getattr(getattr(core, "settings", None), "agents", None)
    raw = str(getattr(agents, "missingRole", "general") or "general").strip().lower()
    if raw in ("general", "anonymous", "parent"):
        return raw  # type: ignore[return-value]
    return "general"


def _available(session: Session, manager: SubagentManager, name: str) -> bool:
    if manager.definition(session, name) is not None:
        return True
    # Persistent named agents the user invented.
    try:
        return bool(manager._is_named(name))  # noqa: SLF001 - shared picker
    except Exception:  # noqa: BLE001
        return False


def pick_agent(
    session: Session,
    manager: SubagentManager,
    prefer: Sequence[str] = (),
    *,
    core: Any = None,
    allow_general: bool = True,
) -> str | None:
    """Resolve a role name, or ``None`` when nothing suitable is available.

    ``allow_general=False`` skips the generalist fallback (used by deepinit
    when only a writing role will do — falling through to anonymous+tools).
    """
    roster = tuple(session.team_agents) if session.team_agents else None

    def _first(names: Sequence[str]) -> str | None:
        for name in names:
            name = str(name).strip()
            if not name:
                continue
            if roster is not None and name not in roster:
                continue
            if _available(session, manager, name):
                return name
        return None

    chosen = _first(prefer)
    if chosen:
        return chosen
    if not allow_general:
        return None
    core = core if core is not None else getattr(manager, "core", None)
    return _first(general_agents_for(core))


__all__ = [
    "DEFAULT_GENERAL_AGENTS",
    "PARENT",
    "MissingRole",
    "general_agents_for",
    "missing_role_policy",
    "pick_agent",
]
