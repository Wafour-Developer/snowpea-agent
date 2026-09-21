"""``/agent`` — the agent-definition generator and listing (M6 contract §2).

``/agent create "<description>"`` asks the session's provider for a definition,
writes it to ``<workdir>/.snowpea/agents/<name>.md`` and reloads the skill
loader so the new agent is visible everywhere.  ``/agent list`` prints the
definitions the daemon can see and where each came from.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.definition import (
    AgentDefinition,
    DefinitionError,
    builtin_agent_definitions,
    discover_definitions,
    generate_definition,
    parse_agent_md,
    validate_name,
    write_definition,
)
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.server import errors
from snowpea_core.session import events

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.commands.agent")

AGENT_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create", "list"],
            "description": "'create <description>' generates a definition, 'list' prints them.",
        },
        "description": {
            "type": "string",
            "description": "What the agent should be good at (for 'create').",
        },
    },
}

USAGE = 'Usage: /agent create "<description>"   |   /agent list'


# ---------------------------------------------------------------------------
# the loader, when it is there
# ---------------------------------------------------------------------------


def _loader(core: Core) -> Any:
    """The skill loader (US-017) if this daemon has one, else ``None``."""
    return getattr(core, "skills", None)


def _home(core: Core) -> Path | None:
    paths = getattr(core, "paths", None)
    return Path(paths.home) if paths is not None else None


def _from_loader(core: Core) -> list[AgentDefinition]:
    """Definitions the skill loader knows about, normalised to our dataclass."""
    loader = _loader(core)
    entries = getattr(loader, "agents", None) if loader is not None else None
    if not entries:
        return []
    if isinstance(entries, dict):
        entries = list(entries.values())
    found: list[AgentDefinition] = []
    for entry in entries:
        if isinstance(entry, AgentDefinition):
            found.append(entry)
            continue
        path = entry.get("path") if isinstance(entry, dict) else getattr(entry, "path", None)
        source = entry.get("source") if isinstance(entry, dict) else getattr(entry, "source", None)
        if not path:
            continue
        try:
            found.append(
                parse_agent_md(
                    Path(path),
                    source=_agent_source_label(Path(path), str(source or "project"), core),
                )
            )
        except DefinitionError:  # pragma: no cover - a broken file on disk
            continue
    return found


def _agent_source_label(path: Path, source: str, core: Core) -> str:
    """Label a ``.claude/agents`` definition by the root it was read from.

    ``project`` was the label every discovered definition got, which made an
    agent written for Claude Code in ``~/.claude/agents`` look like one of this
    project's own (validation report §4.4).  Only the label changes here: which
    directories are walked is decided elsewhere.
    """
    parts = path.parts
    if ".claude" not in parts or "agents" not in parts:
        return source
    if source == "claude-plugin" or "plugins" in parts:
        return "claude-plugin"
    for root in (_home(core), Path.home()):
        if root is None:
            continue
        try:
            if path.resolve().is_relative_to((root / ".claude" / "agents").resolve()):
                return "claude-global"
        except OSError:  # pragma: no cover - an unreadable home
            continue
    return "claude-project"


def definitions_for(core: Core, workdir: Path | str | None) -> list[AgentDefinition]:
    """Every agent definition visible here, newest root winning on a name clash.

    Built-in roles are the base catalogue; loader-provided/plugin definitions
    and then on-disk global/project definitions override by name.
    """
    by_name: dict[str, AgentDefinition] = {}
    for defn in builtin_agent_definitions():
        by_name[defn.name] = defn
    builtin_names = frozenset(by_name)
    for source_defns in (_from_loader(core), discover_definitions(workdir, _home(core))):
        for defn in source_defns:
            placed = _without_shadowing(defn, builtin_names)
            if placed is not None:
                by_name[placed.name] = placed
    return sorted(by_name.values(), key=lambda d: d.name)


def _without_shadowing(
    defn: AgentDefinition, builtin_names: frozenset[str]
) -> AgentDefinition | None:
    """``defn`` under a name that does not take a built-in role away.

    A definition the user wrote for snowpea (global, project, a snowpea plugin)
    may replace a built-in on purpose. One that merely came along with Claude
    Code may not: a plugin that ships its own ``architect`` used to become
    snowpea's ``architect``, so the default team ran somebody else's prompt
    (and advertised "Opus" on a machine that has no such model). Such a
    definition stays available as ``<plugin>:<name>`` — the spelling Claude Code
    itself uses, and the one a clashing skill gets (``/<plugin>:<skill>``). The
    colon never reaches the disk: a qualified name only ever comes from a
    plugin's own file, it is not something snowpea writes. With no plugin to
    name it after, the definition is left out.
    """
    if defn.name not in builtin_names or not defn.source.startswith("claude"):
        return defn
    plugin = _claude_plugin_name(defn.path)
    if plugin is None:
        log.info("agent %r (%s) is hidden by the built-in role", defn.name, defn.source)
        return None
    try:
        qualified = f"{validate_name(plugin)}:{defn.name}"
    except DefinitionError:
        return None
    return replace(defn, name=qualified)


def _claude_plugin_name(path: Path | None) -> str | None:
    """``oh-my-claudecode`` from ``…/plugins/cache/<market>/<plugin>/<version>/agents/x.md``."""
    if path is None:
        return None
    parts = path.parts
    if "cache" not in parts:
        return None
    index = len(parts) - 1 - parts[::-1].index("cache")
    return parts[index + 2] if len(parts) > index + 2 else None


async def _maybe_await(value: Any) -> None:
    if hasattr(value, "__await__"):
        await value


async def reload_loader(core: Core) -> None:
    """Re-scan skills and agents; a missing loader is not an error.

    The loader emits ``commands.changed`` itself when it has one, which is why
    nothing is emitted here.
    """
    loader = _loader(core)
    reload_fn = getattr(loader, "reload", None) if loader is not None else None
    if not callable(reload_fn):
        return
    try:
        await _maybe_await(reload_fn())
    except Exception:  # noqa: BLE001 - a broken loader must not fail the command
        log.exception("skill loader reload failed")


async def register_and_reload(core: Core, path: Path) -> None:
    """Tell the skill loader about an agent definition, then reload."""
    loader = _loader(core)
    register = getattr(loader, "register_agent_definition", None) if loader is not None else None
    if callable(register):
        try:
            await _maybe_await(register(path))
        except Exception:  # noqa: BLE001 - registration must not fail the command
            log.exception("skill loader rejected %s", path)
    await reload_loader(core)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def strip_quotes(args: str) -> str:
    """``'"a b"'`` -> ``'a b'``; falls back to the raw text when unbalanced."""
    text = args.strip()
    if not text:
        return ""
    try:
        parts = shlex.split(text)
    except ValueError:
        return text
    return " ".join(parts) if parts else text


async def create_definition(core: Core, session: Any, description: str) -> AgentDefinition:
    """Generate and write one definition; raises :class:`DefinitionError`."""
    provider = core.providers.get(session.provider, session.model)
    defn = await generate_definition(provider, description)
    path = write_definition(defn, session.workdir)
    await register_and_reload(core, path)
    return defn


async def cmd_agent(ctx: CommandContext, args: str) -> None:
    """``/agent create "<description>"`` or ``/agent list``."""
    action, _, rest = args.strip().partition(" ")
    action = action.lower()
    if not action or action == "list":
        await _list(ctx)
        return
    if action != "create":
        await ctx.say(f"Unknown /agent action '{action}'.\n{USAGE}")
        return
    description = strip_quotes(rest)
    if not description:
        await ctx.say(USAGE)
        return
    try:
        defn = await create_definition(ctx.core, ctx.session, description)
    except DefinitionError as exc:
        await ctx.emit(events.error(errors.INVALID_PARAMS, str(exc)))
        await ctx.say(f"Could not create the agent: {exc}")
        return
    await ctx.say(
        f"Created agent '{defn.name}' at {defn.path}.\n"
        f"  description: {defn.description}\n"
        f"  tools: {defn.tools}\n"
        f"  model: {defn.model}"
    )


async def _list(ctx: CommandContext) -> None:
    definitions = definitions_for(ctx.core, ctx.session.workdir)
    if not definitions:
        await ctx.say('No agent definitions yet. Create one with /agent create "<description>".')
        return
    lines = [f"Agents ({len(definitions)}):"]
    for defn in definitions:
        summary = defn.description or "(no description)"
        lines.append(f"  {defn.name} [{defn.source}] — {summary}")
    await ctx.say("\n".join(lines))


COMMANDS: tuple[Command, ...] = (
    Command(
        name="agent",
        summary='Create or list agent definitions: /agent create "<description>" | /agent list.',
        run=cmd_agent,
        args_schema=AGENT_ARGS_SCHEMA,
    ),
)


__all__ = [
    "AGENT_ARGS_SCHEMA",
    "COMMANDS",
    "USAGE",
    "cmd_agent",
    "create_definition",
    "definitions_for",
    "register_and_reload",
    "reload_loader",
    "strip_quotes",
]
