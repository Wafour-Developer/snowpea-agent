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


def active_team(settings: Settings, workdir: Path | str) -> ActiveTeam | None:
    project = ProjectSettings.load(workdir)
    name = project.agents.activeTeam or settings.agents.default_team
    members = teams_for(settings, workdir).get(name or "")
    if not name or not members:
        return None
    return ActiveTeam(name=name, agents=tuple(dict.fromkeys(members)))


__all__ = ["ActiveTeam", "active_team", "teams_for"]
