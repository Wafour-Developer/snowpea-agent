"""Project-aware reusable agent teams."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings


@dataclass(frozen=True)
class ActiveTeam:
    name: str
    agents: tuple[str, ...]


def teams_for(settings: Settings, workdir: Path | str) -> dict[str, list[str]]:
    teams = {name: list(members) for name, members in settings.agents.teams.items()}
    project_teams = ProjectSettings.load(workdir).agents.teams
    teams.update({name: list(members) for name, members in project_teams.items()})
    return teams


def teams_with_source(settings: Settings, workdir: Path | str) -> dict[str, tuple[list[str], str]]:
    """Every visible team as ``name -> (members, "global"|"project")``.

    The merge rule is :func:`teams_for`'s — a project team wins on a name
    clash — but the winner's origin is kept, because a client offering the
    user a team to pick has to say where it came from.
    """
    out: dict[str, tuple[list[str], str]] = {
        name: (list(members), "global") for name, members in settings.agents.teams.items()
    }
    for name, members in ProjectSettings.load(workdir).agents.teams.items():
        out[name] = (list(members), "project")
    return out


def active_team(settings: Settings, workdir: Path | str) -> ActiveTeam | None:
    """The team a session in ``workdir`` runs under, or ``None``.

    Only a team the project chose (``/team create`` / ``/team use`` write
    ``agents.activeTeam``) restricts delegation.  The global
    ``agents.default_team`` the setup wizard writes is a *roster* — what
    ``/team`` starts from when no name is given — not a whitelist: reading it
    here turned every session into a team session and hid project agents,
    named agents and the built-in ``explore``/``reviewer`` from
    ``delegate_task``, ``/delegate`` and ``$name`` completion (M15 C).
    """
    project = ProjectSettings.load(workdir)
    name = project.agents.activeTeam
    members = teams_for(settings, workdir).get(name or "")
    if not name or not members:
        return None
    return ActiveTeam(name=name, agents=tuple(dict.fromkeys(members)))


def default_roster(settings: Settings, workdir: Path | str) -> list[str]:
    """The roster ``/team`` starts from when no team is named."""
    name = settings.agents.default_team
    return list(teams_for(settings, workdir).get(name or "", []))


__all__ = [
    "ActiveTeam",
    "active_team",
    "default_roster",
    "teams_for",
    "teams_with_source",
]
