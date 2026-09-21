"""Team guides: persona and routing rules stored as markdown files."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from snowpea_core.agent.definition import FENCE, DefinitionError, split_frontmatter, validate_name
from snowpea_core.config.paths import resolve_home
from snowpea_core.prompts.loader import render

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.agent.team_guide")

MAX_PERSONA_CHARS = 8000
MAX_ROUTING_RULES = 30
MAX_WHEN_CHARS = 200
TRUNCATED_MARKER = "[team guide truncated]"
ROUTING_SECTION = re.compile(
    r"^##[ \t]+(?:Routing|라우팅|역할[ \t]+분담)[ \t]*$", re.IGNORECASE | re.MULTILINE
)
H2_SECTION = re.compile(r"^##[ \t]+.+$", re.MULTILINE)
ROUTING_AGENT = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)
UNKNOWN_AGENT_NOTE = "(unknown agent — ignore this rule)"
ROUTING_FOOTER = (
    "Follow this guide when you split work and choose an agent. When a rule "
    "matches the task, use that agent; when none matches, choose as you normally would."
)
WORKER_LINE_TEMPLATE = "You are working as part of team {name}."


@dataclass(frozen=True)
class RoutingRule:
    when: str
    agent: str
    known: bool = True


@dataclass(frozen=True)
class TeamGuide:
    team: str
    description: str
    persona: str
    routing: tuple[RoutingRule, ...]
    path: Path | None
    source: Literal["project", "global"]


def _teams_dir(base: Path) -> Path:
    return base / "teams"


def _guide_path(base: Path, team: str) -> Path:
    return _teams_dir(base) / f"{team}.md"


def _reject_path_like(name: str) -> str:
    if "/" in name or "\\" in name or name in (".", ".."):
        raise DefinitionError(f"team name is not usable as a file name: {name!r}")
    return validate_name(name)


def _safe_read(path: Path, root: Path) -> str | None:
    """Read ``path`` only when it is a regular file inside ``root/teams``."""
    teams_root = _teams_dir(root)
    try:
        if not path.is_file():
            return None
        resolved = path.resolve()
        resolved.relative_to(teams_root.resolve())
    except (OSError, ValueError):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _parse_routing_body(body: str) -> list[tuple[str, str]]:
    match = ROUTING_SECTION.search(body)
    if match is None:
        return []
    section = body[match.end() :]
    next_heading = H2_SECTION.search(section)
    if next_heading is not None:
        section = section[: next_heading.start()]
    rules: list[tuple[str, str]] = []
    for line in section.splitlines():
        text = line.strip()
        if not text.startswith("-"):
            continue
        text = text[1:].strip()
        if "->" in text:
            when, agent = text.rsplit("->", 1)
        elif "→" in text:
            when, agent = text.rsplit("→", 1)
        elif ":" in text:
            when, agent = text.rsplit(":", 1)
        else:
            continue
        when, agent = when.strip(), agent.strip()
        if when and ROUTING_AGENT.fullmatch(agent):
            rules.append((when, agent))
    return rules


def _parse_routing_frontmatter(meta: dict[str, Any]) -> list[tuple[str, str]]:
    raw = meta.get("routing")
    if not isinstance(raw, list):
        return []
    rules: list[tuple[str, str]] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        patterns = (
            r"^(.+?)\s*->\s*([a-z0-9][a-z0-9._-]*)$",
            r"^(.+?)\s*:\s*([a-z0-9][a-z0-9._-]*)$",
        )
        for pattern in patterns:
            hit = re.match(pattern, text, re.IGNORECASE)
            if hit:
                rules.append((hit.group(1).strip(), hit.group(2).strip()))
                break
    return rules


def _strip_routing_section(body: str) -> str:
    match = ROUTING_SECTION.search(body)
    if match is None:
        return body.strip()
    return body[: match.start()].strip()


def _clip_persona(text: str) -> str:
    stripped = text.strip()
    if len(stripped) <= MAX_PERSONA_CHARS:
        return stripped
    log.warning(
        "team guide persona truncated from %s to %s chars",
        len(stripped),
        MAX_PERSONA_CHARS,
    )
    return f"{stripped[:MAX_PERSONA_CHARS].rstrip()}\n\n{TRUNCATED_MARKER}"


def _clip_when(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_WHEN_CHARS:
        return text
    return text[:MAX_WHEN_CHARS].rstrip()


def _merge_routing(
    front: list[tuple[str, str]], body: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    merged: dict[str, tuple[str, str]] = {}
    for when, agent in front:
        merged[when.lower()] = (when, agent)
    for when, agent in body:
        merged[when.lower()] = (when, agent)
    return list(merged.values())[:MAX_ROUTING_RULES]


def _flag_rules(
    rules: list[tuple[str, str]],
    *,
    known_agents: set[str],
    roster: set[str] | None,
) -> tuple[RoutingRule, ...]:
    out: list[RoutingRule] = []
    for when, agent in rules[:MAX_ROUTING_RULES]:
        known = agent in known_agents
        if roster is not None and agent not in roster:
            known = False
        out.append(RoutingRule(when=_clip_when(when), agent=agent, known=known))
    return tuple(out)


def _known_agents(core: Core | None, workdir: Path | str) -> set[str]:
    if core is None:
        return set()
    from snowpea_core.commands.agent_cmd import definitions_for

    return {definition.name for definition in definitions_for(core, workdir)}


def _roster_for_team(core: Core | None, workdir: Path | str, team: str) -> set[str] | None:
    if core is None:
        return None
    from snowpea_core.agent.team_config import active_team, teams_for

    members = teams_for(core.settings, workdir).get(team)
    if members:
        return set(members)
    selected = active_team(core.settings, workdir)
    if selected and selected.name == team:
        return set(selected.agents)
    return None


def _parse_guide_text(
    text: str,
    *,
    team: str,
    path: Path | None,
    source: Literal["project", "global"],
    workdir: Path | str,
    home: Path,
    core: Core | None = None,
    roster: set[str] | None = None,
) -> TeamGuide:
    meta, body = split_frontmatter(text)
    description = str(meta.get("description") or "").strip()
    persona_body = _strip_routing_section(body)
    persona = _clip_persona(persona_body)
    front_rules = _parse_routing_frontmatter(meta)
    body_rules = _parse_routing_body(body)
    merged = _merge_routing(front_rules, body_rules)
    known = _known_agents(core, workdir)
    if roster is None and core is not None:
        roster = _roster_for_team(core, workdir, team)
    routing = _flag_rules(merged, known_agents=known, roster=roster)
    return TeamGuide(
        team=team,
        description=description,
        persona=persona,
        routing=routing,
        path=path,
        source=source,
    )


@lru_cache(maxsize=64)
def _cached_load(
    path_str: str, mtime_ns: int, size: int, team: str, source: str
) -> tuple[str, ...] | None:
    path = Path(path_str)
    root = path.parent.parent
    text = _safe_read(path, root)
    if text is None:
        return None
    # Store serialized pieces; full parse needs workdir/core at call site.
    return (text, team, source)


def _load_from_path(
    path: Path,
    *,
    team: str,
    source: Literal["project", "global"],
    root: Path,
    workdir: Path | str,
    home: Path,
    core: Core | None = None,
) -> TeamGuide | None:
    try:
        stat = path.stat()
        mtime_ns = stat.st_mtime_ns
        size = stat.st_size
    except OSError:
        return None
    cached = _cached_load(str(path.resolve()), mtime_ns, size, team, source)
    if cached is None:
        return None
    text = cached[0]
    return _parse_guide_text(
        text,
        team=team,
        path=path,
        source=source,
        workdir=workdir,
        home=home,
        core=core,
    )


def load_guide(
    home: Path | str | None,
    workdir: Path | str,
    team: str,
    *,
    core: Core | None = None,
) -> TeamGuide | None:
    """Load ``<team>.md`` from the project, else from the snowpea home."""
    name = _reject_path_like(team)
    resolved_home = resolve_home(home)
    project_root = Path(workdir).expanduser().resolve()
    project_path = _guide_path(project_root / ".snowpea", name)
    guide = _load_from_path(
        project_path,
        team=name,
        source="project",
        root=project_root / ".snowpea",
        workdir=workdir,
        home=resolved_home,
        core=core,
    )
    if guide is not None:
        return guide
    global_path = _guide_path(resolved_home, name)
    return _load_from_path(
        global_path,
        team=name,
        source="global",
        root=resolved_home,
        workdir=workdir,
        home=resolved_home,
        core=core,
    )


def guide_exists(home: Path | str | None, workdir: Path | str, team: str) -> bool:
    resolved_home = resolve_home(home)
    project_root = Path(workdir).expanduser().resolve()
    name = _reject_path_like(team)
    for base in (project_root / ".snowpea", resolved_home):
        path = _guide_path(base, name)
        try:
            if path.is_file():
                return True
        except OSError:
            continue
    return False


def _session_has_active_team(session: Session, core: Core | None) -> bool:
    if session.team:
        return True
    parent_id = getattr(session, "parent_session_id", None)
    if parent_id and core is not None:
        parent = core.sessions.get(parent_id)
        if parent is not None and parent.team:
            return True
    return False


def _effective_team_name(session: Session, core: Core | None) -> str:
    if session.team:
        return session.team
    parent_id = getattr(session, "parent_session_id", None)
    if parent_id and core is not None:
        parent = core.sessions.get(parent_id)
        if parent is not None and parent.team:
            return parent.team
    return "default"


def guide_for_session(core: Core | None, session: Session) -> TeamGuide | None:
    """Guide for this session's team context, or ``None`` when none applies."""
    if core is None:
        return None
    home = core.paths.home
    workdir = session.workdir
    if _session_has_active_team(session, core):
        name = _effective_team_name(session, core)
        return load_guide(home, workdir, name, core=core)
    return load_guide(home, workdir, "default", core=core)


def list_guides(
    home: Path | str | None, workdir: Path | str, *, core: Core | None = None
) -> list[TeamGuide]:
    resolved_home = resolve_home(home)
    project_root = Path(workdir).expanduser().resolve()
    names: dict[str, Literal["project", "global"]] = {}
    locations: tuple[tuple[Path, Literal["project", "global"]], ...] = (
        (resolved_home, "global"),
        (project_root / ".snowpea", "project"),
    )
    for base, source in locations:
        directory = _teams_dir(base)
        try:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.md")):
                try:
                    name = _reject_path_like(path.stem)
                except DefinitionError:
                    continue
                names[name] = source
        except OSError:
            continue
    guides: list[TeamGuide] = []
    for name in sorted(names):
        guide = load_guide(resolved_home, workdir, name, core=core)
        if guide is not None:
            guides.append(guide)
    return guides


def _render_routing_lines(rules: tuple[RoutingRule, ...]) -> str:
    lines: list[str] = []
    for rule in rules:
        agent = f"`{rule.agent}`"
        if not rule.known:
            agent = f"`{rule.agent}` {UNKNOWN_AGENT_NOTE}"
        lines.append(f"- {rule.when} → delegate to {agent}")
    return "\n".join(lines)


def render_team_guide(guide: TeamGuide | None, *, audience: Literal["lead", "worker"]) -> str:
    if guide is None or (not guide.persona.strip() and not guide.routing and not guide.description):
        return ""
    if audience == "worker":
        if not guide.persona.strip():
            return ""
        return render(
            "fragments/team-guide-worker",
            TEAM_PERSONA=guide.persona.strip(),
            WORKER_LINE=WORKER_LINE_TEMPLATE.format(name=guide.team),
        ).strip()
    description = guide.description.strip()
    description_block = f"{description}\n" if description else ""
    routing_lines = _render_routing_lines(guide.routing)
    if not routing_lines:
        routing_lines = "(no routing rules yet)"
    return render(
        "fragments/team-guide",
        TEAM_NAME=guide.team,
        TEAM_DESCRIPTION=description_block,
        TEAM_PERSONA=guide.persona.strip(),
        ROUTING_LINES=routing_lines,
        ROUTING_FOOTER=ROUTING_FOOTER,
    ).strip()


def _serialize_guide(guide: TeamGuide) -> str:
    lines: list[str] = []
    if guide.description:
        lines.append(FENCE)
        lines.append(f"description: {guide.description}")
        lines.append(FENCE)
        lines.append("")
    if guide.persona.strip():
        lines.append(guide.persona.strip())
        lines.append("")
    if guide.routing:
        lines.append("## Routing")
        for rule in guide.routing:
            lines.append(f"- {rule.when} -> {rule.agent}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def route_guide(
    home: Path | str | None,
    workdir: Path | str,
    team: str,
    *,
    scope: Literal["project", "global"],
    agent: str,
    when: str,
) -> Path:
    """Append one routing rule while preserving the rest of a guide."""
    existing = load_guide(home, workdir, team)
    routing = [(rule.when, rule.agent) for rule in existing.routing] if existing else []
    routing.append((when, agent))
    return save_guide(
        home,
        workdir,
        team,
        scope=scope,
        description=existing.description if existing else "",
        persona=existing.persona if existing else "",
        routing=routing,
    )


def unroute_guide(
    home: Path | str | None,
    workdir: Path | str,
    team: str,
    *,
    scope: Literal["project", "global"],
    target: str,
) -> Path | None:
    """Remove routing rules by agent name or by one-based index."""
    existing = load_guide(home, workdir, team)
    if existing is None:
        return None
    routing = [(rule.when, rule.agent) for rule in existing.routing]
    if target.isdigit():
        index = int(target)
        if index < 1 or index > len(routing):
            raise DefinitionError(f"route index out of range: {target}")
        routing.pop(index - 1)
    else:
        routing = [row for row in routing if row[1] != target]
    return save_guide(
        home,
        workdir,
        team,
        scope=scope,
        description=existing.description,
        persona=existing.persona,
        routing=routing,
    )


def save_guide(
    home: Path | str | None,
    workdir: Path | str,
    team: str,
    *,
    scope: Literal["project", "global"],
    description: str,
    persona: str,
    routing: Sequence[tuple[str, str] | RoutingRule],
) -> Path:
    name = _reject_path_like(team)
    rules = [
        (rule.when, rule.agent) if isinstance(rule, RoutingRule) else (rule[0], rule[1])
        for rule in routing
    ]
    guide = TeamGuide(
        team=name,
        description=description.strip(),
        persona=_clip_persona(persona),
        routing=_flag_rules(rules[:MAX_ROUTING_RULES], known_agents=set(), roster=None),
        path=None,
        source="project" if scope == "project" else "global",
    )
    if scope == "project":
        base = Path(workdir).expanduser().resolve() / ".snowpea"
    else:
        base = resolve_home(home)
    directory = _teams_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    path = _guide_path(base, name)
    path.write_text(_serialize_guide(guide), encoding="utf-8")
    _cached_load.cache_clear()
    return path


def delete_guide(
    home: Path | str | None,
    workdir: Path | str,
    team: str,
    *,
    scope: Literal["project", "global"],
) -> bool:
    name = _reject_path_like(team)
    if scope == "project":
        base = Path(workdir).expanduser().resolve() / ".snowpea"
    else:
        base = resolve_home(home)
    path = _guide_path(base, name)
    try:
        if not path.is_file():
            return False
        path.unlink()
    except OSError:
        return False
    _cached_load.cache_clear()
    return True


def to_info(guide: TeamGuide) -> dict[str, Any]:
    return {
        "team": guide.team,
        "description": guide.description,
        "persona": guide.persona,
        "routing": [
            {"when": rule.when, "agent": rule.agent, "known": rule.known} for rule in guide.routing
        ],
        "source": guide.source,
        "path": str(guide.path) if guide.path else None,
    }


__all__ = [
    "RoutingRule",
    "TeamGuide",
    "TRUNCATED_MARKER",
    "delete_guide",
    "guide_exists",
    "guide_for_session",
    "list_guides",
    "load_guide",
    "render_team_guide",
    "route_guide",
    "save_guide",
    "to_info",
    "unroute_guide",
]
