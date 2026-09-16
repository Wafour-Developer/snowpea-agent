"""``skill_search``, ``skill_view``, ``skill_list``, ``skill_install``, ``skill_remove``.

Finding and installing a skill used to be a CLI-only errand: the user left the
session, ran ``snowpea skill search``, read the output, ran ``snowpea skill
install``, and came back.  These four tools put the same four RPC calls in the
model's hands, so "find me a pdf skill" is answered in the conversation that
asked it.

They are thin on purpose — every one of them goes through
:class:`~snowpea_core.skills.loader.SkillLoader`, the same object the
``skill.*`` RPC handlers use, so the CLI and the agent can never drift apart.
Installing therefore reloads the registry as part of the call, and the tool
reports what the reload actually added: new ``/commands``, agents and MCP
servers are usable in the same session, with no daemon restart.

Permissions: ``skill_install`` and ``skill_remove`` carry ``exec`` — they clone
or unpack code into ``$SNOWPEA_HOME/plugins`` and then run it as part of the
session, which is exactly what ``shell`` is gated for, and plan mode must not
be able to install anything.  ``skill_search`` and ``skill_list`` are ``read``:
a catalogue lookup writes nothing, and making the user approve a search would
defeat the point of asking for one.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from snowpea_core.prompts.tool_descriptions import SKILL_VIEW
from snowpea_core.skills.marketplace import InstallError
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

log = logging.getLogger("snowpea.tools.skills")

#: How many search hits the model is shown; more than this is noise it has to
#: summarise anyway, and the user picks from a list they can read.
MAX_HITS = 20

#: Sibling files listed under a skill body before the list itself is elided.
MAX_SKILL_FILES = 40

#: What a repeat view of an unchanged skill returns instead of the body.
UNCHANGED_STUB = (
    "{name}: unchanged since your last view in this session — the body is already "
    "above in this conversation. Scroll back to it; do not view it again unless a "
    "[SKILL_PRUNED] marker has replaced it."
)


class LoaderMissing(RuntimeError):
    """The daemon has no skill loader wired — a broken install, not a user error."""


def _loader(ctx: ToolContext) -> Any:
    loader = getattr(ctx.core, "skills", None)
    if loader is None:
        raise LoaderMissing("the skill loader is not wired into this daemon")
    return loader


def _kinds(loader: Any) -> dict[str, set[str]]:
    """A snapshot of what is loaded, so an install can report the difference."""
    return {
        "commands": {name for name, skill in loader.skills.items() if skill.doc.user_invocable},
        "skills": set(loader.skills),
        "agents": {agent.name for agent in loader.agents},
        "plugins": {plugin.name for plugin in loader.plugins},
        "mcp": set(loader.mcp_servers),
    }


def _added(before: dict[str, set[str]], after: dict[str, set[str]]) -> list[str]:
    """``["commands: /a, /b", "agents: c"]`` for everything the reload gained."""
    lines: list[str] = []
    for key, label, prefix in (
        ("commands", "new commands", "/"),
        ("agents", "new agents", ""),
        ("mcp", "new MCP servers", ""),
    ):
        gained = sorted(after[key] - before[key])
        if gained:
            lines.append(f"{label}: " + ", ".join(f"{prefix}{name}" for name in gained))
    return lines


def _popularity(hit: Any) -> str:
    """``" (4.6 stars, 120 downloads)"`` — empty when the source published none."""
    parts: list[str] = []
    rating = float(getattr(hit, "rating", 0.0) or 0.0)
    downloads = int(getattr(hit, "downloads", 0) or 0)
    if rating:
        parts.append(f"{rating:g} stars")
    if downloads:
        parts.append(f"{downloads} downloads")
    return f" ({', '.join(parts)})" if parts else ""


def _resolve_spec(ctx: ToolContext, spec: str) -> str:
    """Make a local path absolute against the session workdir.

    The daemon's own cwd is ``$SNOWPEA_HOME``, so a relative path typed in a
    session would otherwise resolve somewhere the user never meant — the same
    trap ``resolve_install_source`` fixes for the CLI.
    """
    text = spec.strip()
    if not text or text.startswith(("http://", "https://", "git@")) or ":" in text[:12]:
        return text
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = Path(ctx.session.workdir) / candidate
    return str(candidate.resolve()) if candidate.is_dir() else text


async def skill_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Search every registered source, and say which ones did not answer."""
    query = str(args.get("query", "") or "").strip()
    if not query:
        return ToolResult(ok=False, error="query is required, e.g. 'pdf'")
    wanted = {str(name).strip().lower() for name in args.get("sources") or [] if str(name).strip()}

    try:
        hits, unavailable = await _loader(ctx).search(query)
    except LoaderMissing as exc:
        return ToolResult(ok=False, error=str(exc))
    if wanted:
        hits = [hit for hit in hits if str(hit.source).lower() in wanted]

    shown = hits[:MAX_HITS]
    trimmed = f", showing {len(shown)}" if len(shown) < len(hits) else ""
    lines = [f'{len(hits)} result(s) for "{query}"{trimmed}']
    for index, hit in enumerate(shown, start=1):
        mark = " [already installed]" if hit.installed else ""
        lines.append(f"{index}. {hit.name} [{hit.source}]{_popularity(hit)}{mark}")
        if hit.summary:
            lines.append(f"   {hit.summary}")
        lines.append(f"   install spec: {hit.installSpec or hit.id or hit.name}")
    if unavailable:
        lines.append("sources that could not be reached: " + "; ".join(unavailable))
        if not hits:
            lines.append("No hits, but the search was incomplete — this is offline, not empty.")
    elif not hits:
        lines.append("Nothing matched. Every source answered, so there really is no such skill.")
    lines.append(
        "Show these to the user with their install spec and let them pick; "
        "then call skill_install with the spec of the one they chose."
    )
    return ToolResult(
        ok=True,
        output="\n".join(lines),
        meta={"query": query, "count": len(hits), "unavailable": unavailable},
    )


async def skill_list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Everything installed: plugins first, then what they contribute."""
    try:
        loader = _loader(ctx)
    except LoaderMissing as exc:
        return ToolResult(ok=False, error=str(exc))

    lines: list[str] = []
    if loader.plugins:
        lines.append(f"{len(loader.plugins)} installed plugin(s):")
        for plugin in loader.plugins:
            version = f" {plugin.version}" if plugin.version else ""
            lines.append(f"- {plugin.name}{version} — {plugin.description or 'no description'}")
            lines.append(f"  path: {plugin.root}")
    else:
        lines.append("No plugins are installed.")

    for kind in ("skill", "command"):
        entries = [entry for entry in loader.skills.values() if entry.kind == kind]
        if entries:
            lines.append(f"{kind}s:")
            lines.extend(
                f"- {entry.name} [{entry.source}] — {entry.description or 'no description'}"
                for entry in sorted(entries, key=lambda item: item.name)
            )
    if loader.agents:
        lines.append("agents:")
        lines.extend(
            f"- {agent.name} [{agent.source}] — {agent.description or 'no description'}"
            for agent in sorted(loader.agents, key=lambda item: item.name)
        )
    lines.append("Read any skill in full with skill_view(name); run one as a command with /name.")
    return ToolResult(ok=True, output="\n".join(lines), meta={"plugins": len(loader.plugins)})


def _skill_files(root: Path, limit: int = MAX_SKILL_FILES) -> list[str]:
    """Sibling files that ship with a skill, as paths relative to its directory.

    ``SKILL.md`` itself is left out — it *is* the body — and so is anything
    under a dot directory, which is packaging rather than content.
    """
    found: list[str] = []
    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name == "SKILL.md":
                continue
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            found.append(relative.as_posix())
            if len(found) >= limit:
                break
    except OSError:  # pragma: no cover - an unreadable skill directory
        return found
    return found


async def skill_view(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """The SKILL.md body of one installed skill, plus what ships beside it."""
    name = str(args.get("name") or "").strip().lstrip("/")
    if not name:
        return ToolResult(ok=False, error="name is required: an installed skill, from skill_list")
    try:
        loader = _loader(ctx)
    except LoaderMissing as exc:
        return ToolResult(ok=False, error=str(exc))

    skill = loader.skills.get(name)
    if skill is None:
        lowered = name.lower()
        skill = next(
            (item for key, item in loader.skills.items() if key.lower() == lowered), None
        )
    if skill is None:
        known = ", ".join(sorted(loader.skills)[:MAX_HITS]) or "none"
        return ToolResult(ok=False, error=f"no skill named {name!r} (installed: {known})")

    doc = skill.doc
    text = (doc.body or "").strip()
    if not text and doc.path is not None:
        try:
            text = doc.path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return ToolResult(ok=False, error=f"{skill.name}: could not read SKILL.md ({exc})")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    views = getattr(ctx.session, "skill_views", None)
    if isinstance(views, dict) and views.get(skill.name) == digest:
        return ToolResult(
            ok=True,
            output=UNCHANGED_STUB.format(name=skill.name),
            meta={"name": skill.name, "unchanged": True},
        )

    files = _skill_files(doc.path.parent) if doc.path is not None else []
    header = f"# skill: {skill.name} [{skill.source}]"
    lines = [header]
    if doc.description:
        lines.append(doc.description)
    lines.append("")
    lines.append(text)
    if files:
        lines.append("")
        lines.append("Files that ship with this skill (read one with read_file):")
        base = doc.path.parent if doc.path is not None else Path()
        lines.extend(f"- {(base / item).as_posix()}" for item in files)
    if isinstance(views, dict):
        views[skill.name] = digest
    log.info("skill_view %s (%d chars)", skill.name, len(text))
    return ToolResult(
        ok=True,
        output="\n".join(lines),
        path=str(doc.path) if doc.path is not None else None,
        meta={"name": skill.name, "source": skill.source, "files": files},
    )


async def skill_install(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Install one plugin and report what the reload made available."""
    spec = str(args.get("spec") or args.get("source") or "").strip()
    if not spec:
        return ToolResult(
            ok=False,
            error="spec is required: an install spec from skill_search, a git URL or a local path",
        )
    try:
        loader = _loader(ctx)
    except LoaderMissing as exc:
        return ToolResult(ok=False, error=str(exc))

    claude_plugins = set(getattr(loader, "claude_plugin_names", set()))
    plugin_name = ""
    if "@" in spec:
        plugin_name = spec.split("@", 1)[1].strip()

    before = _kinds(loader)
    try:
        target = await loader.install(_resolve_spec(ctx, spec))
    except InstallError as exc:
        return ToolResult(ok=False, error=str(exc))
    after = _kinds(loader)

    target_name = Path(target).name
    already_in_claude = bool(
        (plugin_name and plugin_name in claude_plugins)
        or (target_name in claude_plugins)
    )

    installed = sorted(after["plugins"] - before["plugins"]) or [target_name]
    lines = [f"installed {', '.join(installed)} from {spec} into {target}"]
    if already_in_claude:
        lines.append(
            "already available from Claude Code's plugin cache; installing a snowpea copy anyway"
        )
    gained = _added(before, after)
    lines.extend(gained)
    lines.append(
        "It is live in this session already — no restart."
        if gained
        else "The registry reloaded; this plugin adds no new commands, agents or MCP servers."
    )
    log.info("skill_install %s -> %s", spec, target)
    return ToolResult(
        ok=True, output="\n".join(lines), path=str(target), meta={"spec": spec, "added": gained}
    )


async def skill_remove(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Delete an installed plugin and reload, so its commands go away at once."""
    name = str(args.get("name") or "").strip()
    if not name:
        return ToolResult(ok=False, error="name is required: the installed plugin to remove")
    try:
        loader = _loader(ctx)
    except LoaderMissing as exc:
        return ToolResult(ok=False, error=str(exc))

    before = _kinds(loader)
    if not await loader.remove(name):
        known = ", ".join(sorted(before["plugins"])) or "none"
        return ToolResult(
            ok=False, error=f"no installed plugin named {name!r} (installed: {known})"
        )
    after = _kinds(loader)
    gone = sorted(before["commands"] - after["commands"])
    lines = [f"removed {name}"]
    if gone:
        lines.append("commands that are gone: " + ", ".join(f"/{command}" for command in gone))
    return ToolResult(ok=True, output="\n".join(lines), meta={"name": name, "removed": gone})


_SPEC_DESCRIPTION = (
    "What to install: an install spec from skill_search (registry:<id>, "
    "clawhub:<id>, github:<owner>/<repo>, <marketplace>/<plugin>), a git URL, "
    "or a local directory path."
)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="skill_search",
        category="skills",
        description=(
            "Search the skill marketplaces and the hosted registry for an installable "
            "skill or plugin. Use this when the user asks whether a skill exists or asks "
            "you to find one — do not tell them to run the snowpea CLI. Show the "
            "candidates with their install spec and let the user choose before installing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What the skill should do, e.g. pdf."},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keep only these source labels; omit for every source.",
                },
            },
            "required": ["query"],
        },
        permission="read",
        run=skill_search,
    ),
    Tool(
        name="skill_list",
        category="skills",
        description=(
            "List the plugins, skills, commands and agents installed here, with their "
            "version, source and path. Use it before installing, to see whether the user "
            "already has what they are asking for."
        ),
        input_schema={"type": "object", "properties": {}},
        permission="read",
        run=skill_list,
    ),
    Tool(
        name="skill_view",
        category="skills",
        description=SKILL_VIEW,
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Installed skill name, as it appears in the skills index.",
                }
            },
            "required": ["name"],
        },
        permission="read",
        run=skill_view,
    ),
    Tool(
        name="skill_install",
        category="skills",
        description=(
            "Install a skill or plugin and reload the registry, so its commands, agents "
            "and MCP servers work in this session without a restart. Only call it once "
            "the user has picked one of the candidates from skill_search; it downloads "
            "and runs third-party code, so it always needs their approval."
        ),
        input_schema={
            "type": "object",
            "properties": {"spec": {"type": "string", "description": _SPEC_DESCRIPTION}},
            "required": ["spec"],
        },
        permission="exec",
        run=skill_install,
    ),
    Tool(
        name="skill_remove",
        category="skills",
        description=(
            "Delete an installed plugin and reload, so its commands disappear at once. "
            "Only call it when the user asked to remove that plugin by name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Installed plugin name, from skill_list."}
            },
            "required": ["name"],
        },
        permission="exec",
        run=skill_remove,
    ),
)


__all__ = [
    "MAX_HITS",
    "MAX_SKILL_FILES",
    "UNCHANGED_STUB",
    "TOOLS",
    "LoaderMissing",
    "skill_install",
    "skill_list",
    "skill_remove",
    "skill_search",
    "skill_view",
]
