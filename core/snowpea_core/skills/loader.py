"""Plugin / skill loader (M6 contract §1).

Search roots, in the order they are scanned — a later root wins a name clash:

1. built-ins      ``core/snowpea_core/builtin_skills/<name>/SKILL.md``      (``builtin``)
2. global         ``$SNOWPEA_HOME/{skills,agents,commands}``                (``global``)
3. claude-global  ``~/.claude/{skills,agents,commands}``                    (``claude-global``)
4. plugins        ``$SNOWPEA_HOME/plugins/<plugin>/…``                      (``plugin:<name>``)
5. claude-plugin  ``~/.claude/plugins/cache/…``                             (``claude-plugin``)
6. project        ``<workdir>/.claude/…`` then ``<workdir>/.snowpea/…``     (``project``)

Claude Code plugins installed under ``~/.claude/plugins/installed_plugins.json``
are scanned as ``claude-plugin``. Snowpea-installed plugins and skills with the
same name win over Claude copies and the duplicate is skipped with a debug log.
Hooks from Claude plugins are NOT registered (only skills, commands, agents).

A *bundle* is any directory that may hold ``skills/<name>/SKILL.md``,
``agents/*.md``, ``commands/*.md``, ``hooks/hooks.json`` and ``.mcp.json``; a
plugin is a bundle with a ``plugin.json`` (in the root or in
``.claude-plugin/``).  Every user-invocable skill and every ``commands/*.md``
becomes a slash command whose ``run`` injects the body — with ``$ARGUMENTS``
substituted — and starts a normal turn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.server.protocol import SkillInfo, SkillKind
from snowpea_core.skills import marketplace
from snowpea_core.skills.hooks import HookRegistry
from snowpea_core.skills.skill_md import SkillDoc, load_skill_md

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.skills")

SOURCE_BUILTIN = "builtin"
SOURCE_GLOBAL = "global"
SOURCE_PROJECT = "project"
SOURCE_CLAUDE_GLOBAL = "claude-global"
SOURCE_CLAUDE_PROJECT = "claude-project"
SOURCE_CLAUDE_PLUGIN = "claude-plugin"

#: Project-local bundles, read in this order (``.snowpea`` wins).
PROJECT_DIRS: tuple[tuple[str, str], ...] = (
    (".claude", SOURCE_CLAUDE_PROJECT),
    (".snowpea", SOURCE_PROJECT),
)

#: Ceiling on the project directories one scan walks (M15 §B5a).
MAX_SCANNED_WORKDIRS = 50

#: Placeholders a plugin may use in ``.mcp.json`` and in hook commands.
ROOT_VARS: tuple[str, ...] = ("CLAUDE_PLUGIN_ROOT", "SNOWPEA_PLUGIN_ROOT")

#: ``${SNOWPEA_PYTHON}`` in a plugin's ``.mcp.json`` is the interpreter running
#: the daemon, which is the one that has this project's dependencies.
PYTHON_VAR = "SNOWPEA_PYTHON"


#: Aliases so the annotations below still mean the builtin ``list`` even
#: though :class:`SkillLoader` defines a method called ``list``.
SkillInfos = list[SkillInfo]
Strings = list[str]

#: One index group: its bracketed label and its ``(name, description)`` rows.
IndexGroups = list[tuple[str, list[tuple[str, str]]]]

#: How much of a description the skills index shows before it elides (M15 §B1).
INDEX_DESCRIPTION_CHARS = 60

#: Group order in the index: the most specific origin first, so a project skill
#: is the first thing the model reads.
INDEX_GROUP_ORDER: tuple[str, ...] = (SOURCE_PROJECT, SOURCE_GLOBAL)


@dataclass
class LoadedSkill:
    """A skill or a Claude Code command file, ready to become a command."""

    doc: SkillDoc
    source: str
    kind: SkillKind = "skill"
    plugin: str = ""

    @property
    def name(self) -> str:
        return self.doc.name

    @property
    def description(self) -> str:
        return self.doc.description


@dataclass
class LoadedAgent:
    """An ``agents/<name>.md`` definition; US-018 gives it behaviour."""

    name: str
    description: str
    source: str
    path: Path
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""


@dataclass
class LoadedPlugin:
    """One installed plugin directory."""

    name: str
    version: str
    description: str
    root: Path
    source: str


@dataclass
class ReloadReport:
    """What a :meth:`SkillLoader.reload` found."""

    skills: int = 0
    agents: int = 0
    commands: int = 0
    plugins: int = 0
    hooks: int = 0
    mcp_tools: list[str] = field(default_factory=list)


def _clip(text: str, limit: int) -> str:
    """``text`` on one line, elided with ``…`` once it passes ``limit``."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: max(1, limit - 1)].rstrip() + "…"


def builtin_root() -> Path:
    """``core/snowpea_core/builtin_skills``; may not exist in a trimmed install."""
    return Path(__file__).resolve().parent.parent / "builtin_skills"


class SkillLoader:
    """Scans the search roots and keeps the command registry in step."""

    def __init__(self, core: Core) -> None:
        self.core = core
        self.skills: dict[str, LoadedSkill] = {}
        self.agents: list[LoadedAgent] = []
        self.plugins: list[LoadedPlugin] = []
        self.hooks = HookRegistry()
        #: Servers merged out of plugin ``.mcp.json`` files, by server name.
        self.mcp_servers: dict[str, dict[str, Any]] = {}
        #: Which plugin each of those came from, so ``mcp.list`` can say
        #: "from plugin X" on a row it refuses to edit (M14 §2).
        self.mcp_server_plugins: dict[str, str] = {}
        self._registered: set[str] = set()
        self._lock = asyncio.Lock()
        #: ``(label, reason)`` hubs the registry switched off during the last
        #: :meth:`search` — surfaced by ``skill.search`` as ``notIncluded``.
        self.last_not_included: list[tuple[str, str]] = []
        #: Cached :meth:`index_groups` result, dropped by every scan (M15 §B1).
        self._index_groups: IndexGroups | None = None
        #: Plugin names detected in Claude Code's installed_plugins.json.
        self.claude_plugin_names: set[str] = set()

    # -- paths ---------------------------------------------------------
    @property
    def claude_root(self) -> Path:
        """Where Claude Code keeps its own tree (skills, agents, plugins).

        ``$CLAUDE_CONFIG_DIR`` when set, else the user's ``~/.claude``.  A
        ``.claude`` directly under the snowpea home is honoured first so a
        self-contained home (tests, a sandboxed daemon) can carry one; the
        production daemon's home is ``~/.snowpea``, which has none, and that is
        exactly why Claude Code's installed plugins used to be invisible.
        """
        local = self.home / ".claude"
        if local.is_dir():
            return local
        override = os.environ.get("CLAUDE_CONFIG_DIR")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".claude"

    @property
    def home(self) -> Path:
        return Path(self.core.paths.home)

    @property
    def plugins_dir(self) -> Path:
        return self.home / "plugins"

    def workdirs(self) -> list[Path]:
        """Project directories to scan (M15 §B5a).

        Every live session's workdir first, then the workdirs of sessions the
        store still remembers — closed ones included — so a daemon that has
        just started already knows the skills of every project the user has
        worked in, rather than discovering them the first time a session opens
        there.  Capped at :data:`MAX_SCANNED_WORKDIRS` and filtered to
        directories that still exist, because a scan walks each one.
        """
        seen: list[Path] = []

        def add(raw: str | Path) -> None:
            path = Path(raw)
            if path in seen or len(seen) >= MAX_SCANNED_WORKDIRS:
                return
            seen.append(path)

        for row in self.core.sessions.list():
            add(row.workdir)
        store = getattr(self.core, "store", None)
        if store is not None and hasattr(store, "session_workdirs"):
            try:
                stored = store.session_workdirs(limit=MAX_SCANNED_WORKDIRS)
            except Exception:  # noqa: BLE001 - a bad store must not stop the scan
                log.debug("could not list stored session workdirs", exc_info=True)
            else:
                for raw in stored:
                    path = Path(raw)
                    if path.is_dir():
                        add(path)
        if not seen:
            seen.append(Path.cwd())
        return seen

    # -- scanning ------------------------------------------------------
    def scan(self) -> ReloadReport:
        """Re-read every root into memory.  Pure filesystem work."""
        self.skills = {}
        self.agents = []
        self.plugins = []
        self.hooks = HookRegistry()
        self.mcp_servers = {}
        self.mcp_server_plugins = {}
        self._index_groups = None
        self.claude_plugin_names = set()

        self._scan_builtins()
        self._scan_bundle(self.home, SOURCE_GLOBAL)
        self._scan_bundle(self.claude_root, SOURCE_CLAUDE_GLOBAL)
        self._scan_plugins()
        self._scan_claude_plugins()
        for workdir in self.workdirs():
            for name, source in PROJECT_DIRS:
                self._scan_bundle(workdir / name, source)

        commands = sum(1 for skill in self.skills.values() if skill.doc.user_invocable)
        return ReloadReport(
            skills=sum(1 for s in self.skills.values() if s.kind == "skill"),
            agents=len(self.agents),
            commands=commands,
            plugins=len(self.plugins),
            hooks=self.hooks.count(),
        )

    def _scan_builtins(self) -> None:
        root = builtin_root()
        if not root.is_dir():
            return
        for entry in sorted(root.iterdir()):
            self._add_skill_dir(entry, SOURCE_BUILTIN)

    def _scan_plugins(self) -> None:
        root = self.plugins_dir
        if not root.is_dir():
            return
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            manifest = marketplace.read_plugin_json(entry)
            name = str(manifest.get("name") or entry.name)
            source = f"plugin:{name}"
            self.plugins.append(
                LoadedPlugin(
                    name=name,
                    version=str(manifest.get("version") or ""),
                    description=str(manifest.get("description") or ""),
                    root=entry,
                    source=source,
                )
            )
            self._scan_bundle(entry, source, plugin=name)

    def _scan_claude_plugins(self) -> None:
        """Scan plugins installed by Claude Code in ``~/.claude/plugins/``.

        Reads ``~/.claude/plugins/installed_plugins.json`` and respects
        ``enabledPlugins`` in ``~/.claude/settings.json``. A snowpea-installed
        plugin or skill with the same name wins and the Claude copy is skipped
        with a debug log (no duplicate commands). Hooks from Claude plugins are
        NOT registered (only skills, commands, agents).
        """
        installed_file = self.claude_root / "plugins" / "installed_plugins.json"
        if not installed_file.is_file():
            return
        import json

        try:
            data = json.loads(installed_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        raw_plugins = data.get("plugins")
        if not isinstance(raw_plugins, dict):
            return

        enabled_plugins: dict[str, Any] = {}
        settings_file = self.claude_root / "settings.json"
        if settings_file.is_file():
            try:
                settings_data = json.loads(settings_file.read_text(encoding="utf-8"))
                if isinstance(settings_data, dict):
                    raw_enabled = settings_data.get("enabledPlugins")
                    if isinstance(raw_enabled, dict):
                        enabled_plugins = raw_enabled
            except (OSError, json.JSONDecodeError):
                pass

        known_workdirs = {w.resolve() for w in self.workdirs()}
        snowpea_plugin_names = {p.name for p in self.plugins if p.source != SOURCE_CLAUDE_PLUGIN}

        for plugin_key, raw_entries in sorted(raw_plugins.items(), key=lambda item: str(item[0])):
            key_str = str(plugin_key)
            plugin_id = key_str.partition("@")[0]
            if enabled_plugins.get(key_str) is False or enabled_plugins.get(plugin_id) is False:
                continue

            if isinstance(raw_entries, dict):
                entries = [raw_entries]
            elif isinstance(raw_entries, list):
                entries = [e for e in raw_entries if isinstance(e, dict)]
            else:
                continue

            user_entry: dict[str, Any] | None = None
            matching_proj_entry: dict[str, Any] | None = None
            for e in entries:
                scope = e.get("scope")
                if scope == "user":
                    user_entry = e
                    break
                if scope == "project":
                    proj_path = e.get("projectPath")
                    if proj_path:
                        try:
                            if Path(proj_path).resolve() in known_workdirs:
                                if matching_proj_entry is None:
                                    matching_proj_entry = e
                        except OSError:
                            continue

            chosen = user_entry if user_entry is not None else matching_proj_entry
            if chosen is None:
                continue

            install_path_str = chosen.get("installPath")
            if not install_path_str:
                continue
            install_path = Path(install_path_str)
            if not install_path.is_dir():
                continue

            manifest = marketplace.read_plugin_json(install_path)
            manifest_name = manifest.get("name")
            name = str(manifest_name if manifest_name else plugin_id)
            version = str(manifest.get("version") or chosen.get("version") or "")
            description = str(manifest.get("description") or "")

            self.claude_plugin_names.add(name)
            self.claude_plugin_names.add(plugin_id)

            if (
                name in snowpea_plugin_names
                or any(p.name == name for p in self.plugins)
                or (self.plugins_dir / name).is_dir()
            ):
                log.debug("Claude plugin %s skipped: snowpea-installed copy wins", name)
                continue
            if name in self.skills and self.skills[name].source != SOURCE_CLAUDE_PLUGIN:
                log.debug("Claude plugin %s skipped: snowpea-installed skill wins", name)
                continue

            self.plugins.append(
                LoadedPlugin(
                    name=name,
                    version=version,
                    description=description,
                    root=install_path,
                    source=SOURCE_CLAUDE_PLUGIN,
                )
            )
            self._scan_bundle(
                install_path,
                SOURCE_CLAUDE_PLUGIN,
                plugin=name,
                load_hooks=False,
            )

    def _scan_bundle(
        self, root: Path, source: str, plugin: str = "", load_hooks: bool = True
    ) -> None:
        """Read ``skills/``, ``agents/``, ``commands/``, hooks and ``.mcp.json``.

        Hooks from Claude plugins are NOT registered (only skills, commands, agents).
        """
        if not root.is_dir():
            return
        # A bundle that *is* one skill: ``<root>/SKILL.md`` with no skills/
        # directory around it.  ``~/.snowpea/plugins/flux/SKILL.md`` is shaped
        # this way and used to load as nothing at all (M15 §B5c).
        if (root / "SKILL.md").is_file():
            self._add_skill_dir(root, source, plugin=plugin)
            if plugin and source != SOURCE_CLAUDE_PLUGIN:
                log.info(
                    "plugin %s is a bare skill directory (only SKILL.md); it is registered "
                    "as a skill — install it under %s/skills/%s to keep it out of plugins/",
                    plugin,
                    self.home,
                    root.name,
                )
        skills_dir = root / "skills"
        if skills_dir.is_dir():
            for entry in sorted(skills_dir.iterdir()):
                self._add_skill_dir(entry, source, plugin=plugin)
        agents_dir = root / "agents"
        if agents_dir.is_dir():
            for entry in sorted(agents_dir.glob("*.md")):
                self.register_agent_definition(entry, source)
        commands_dir = root / "commands"
        if commands_dir.is_dir():
            for entry in sorted(commands_dir.glob("*.md")):
                doc = load_skill_md(entry, default_name=entry.stem)
                if doc is None:
                    continue
                doc.name = entry.stem
                if source == SOURCE_CLAUDE_PLUGIN and doc.name in self.skills:
                    log.debug("Claude command %s skipped: snowpea copy wins", doc.name)
                    continue
                self.skills[doc.name] = LoadedSkill(
                    doc=doc, source=source, kind="command", plugin=plugin
                )
        if load_hooks:
            for candidate in (root / "hooks" / "hooks.json", root / "hooks.json"):
                if candidate.is_file():
                    self.hooks.load_file(candidate, plugin=plugin or source, root=root)
        if plugin and source != SOURCE_CLAUDE_PLUGIN:
            self._read_mcp(root / ".mcp.json", root, plugin)

    def _add_skill_dir(self, entry: Path, source: str, plugin: str = "") -> None:
        if not entry.is_dir():
            return
        path = entry / "SKILL.md"
        if not path.is_file():
            return
        doc = load_skill_md(path, default_name=entry.name)
        if doc is None:
            return
        if source == SOURCE_CLAUDE_PLUGIN and doc.name in self.skills:
            log.debug("Claude skill %s skipped: snowpea copy wins", doc.name)
            return
        self.skills[doc.name] = LoadedSkill(doc=doc, source=source, kind="skill", plugin=plugin)

    def register_agent_definition(
        self, path: Path | str, source: str = SOURCE_PROJECT
    ) -> LoadedAgent | None:
        """Parse one ``agents/<name>.md`` and remember its front matter."""
        target = Path(path)
        doc = load_skill_md(target, default_name=target.stem)
        if doc is None:
            return None
        agent_name = doc.name or target.stem
        if source == SOURCE_CLAUDE_PLUGIN and any(
            existing.name == agent_name for existing in self.agents
        ):
            log.debug("Claude agent %s skipped: snowpea copy wins", agent_name)
            return None
        agent = LoadedAgent(
            name=agent_name,
            description=doc.description,
            source=source,
            path=target,
            frontmatter=dict(doc.frontmatter),
            body=doc.body,
        )
        self.agents = [existing for existing in self.agents if existing.name != agent.name]
        self.agents.append(agent)
        return agent

    def _read_mcp(self, path: Path, root: Path, plugin: str = "") -> None:
        """Merge a plugin's ``.mcp.json`` servers, expanding the root variables."""
        import json

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        servers = raw.get("mcpServers") if isinstance(raw, dict) else None
        if not isinstance(servers, dict):
            return
        for name, entry in servers.items():
            if isinstance(entry, dict):
                self.mcp_servers[str(name)] = expand_tree(entry, root)
                if plugin:
                    self.mcp_server_plugins[str(name)] = plugin

    # -- applying ------------------------------------------------------
    async def reload(self) -> ReloadReport:
        """Re-scan, re-register commands and MCP servers, announce the change."""
        async with self._lock:
            report = self.scan()
            self._apply_commands()
            report.mcp_tools = await self._apply_mcp()
            await self._announce()
            log.info(
                "skills reloaded: %d skills, %d agents, %d commands, %d plugins, %d hooks",
                report.skills,
                report.agents,
                report.commands,
                report.plugins,
                report.hooks,
            )
            return report

    async def reload_workdir(self, workdir: Path | str) -> ReloadReport:
        """Register one project's skills, commands and agents, now (M15 §B5b).

        ``session.create`` and ``session.restore`` call this before the first
        turn, so a project whose ``.snowpea/skills`` the daemon has never seen
        still has its ``/commands`` in the palette on the very first prompt.
        Incremental: it scans only ``<workdir>/.claude`` and
        ``<workdir>/.snowpea`` and adds to what is already loaded, rather than
        re-walking every root.
        """
        root = Path(workdir)
        async with self._lock:
            before = len(self.skills), len(self.agents)
            for name, source in PROJECT_DIRS:
                self._scan_bundle(root / name, source)
            self._index_groups = None
            self._apply_commands()
            report = ReloadReport(
                skills=len(self.skills) - before[0],
                agents=len(self.agents) - before[1],
                commands=sum(1 for s in self.skills.values() if s.doc.user_invocable),
                plugins=len(self.plugins),
                hooks=self.hooks.count(),
            )
            await self._announce()
            return report

    def load_sync(self) -> ReloadReport:
        """Scan and register commands without touching the network or MCP.

        ``wire_core`` is synchronous, so the daemon starts with the command
        table already filled in; :meth:`reload` then does the full pass.
        """
        report = self.scan()
        self._apply_commands()
        return report

    def _apply_commands(self) -> None:
        registry = self.core.commands
        for name in self._registered:
            registry.unregister(name)
        self._registered = set()
        protected: frozenset[str] = getattr(registry, "protected", frozenset())
        for skill in self.skills.values():
            if not skill.doc.user_invocable:
                continue
            name = skill.name
            if name in protected:
                # A core command keeps its name. The skill stays reachable the
                # way Claude Code spells it, ``/<plugin>:<skill>``; one with no
                # plugin to qualify it is left out, and said so.
                if not skill.plugin:
                    log.info(
                        "skill %r (%s) is hidden by the built-in /%s", name, skill.source, name
                    )
                    continue
                name = f"{skill.plugin}:{skill.name}"
            registry.register(replace(self._command_for(skill), name=name))
            self._registered.add(name)

    def _command_for(self, skill: LoadedSkill) -> Command:
        doc = skill.doc
        summary = doc.description or f"{skill.kind} {doc.name}"
        if doc.argument_hint:
            summary = f"{summary} {doc.argument_hint}".strip()

        async def run(ctx: CommandContext, args: str) -> None:
            from snowpea_core.agent import loop as agent_loop

            session = ctx.session
            previous = getattr(session, "allowed_tools", None)
            if doc.allowed_tools:
                session.allowed_tools = set(doc.allowed_tools)
            ctx.handled_turn = True
            try:
                await agent_loop.run_turn(
                    ctx.core, session, instruction(doc, args), turn_id=ctx.turn_id
                )
            finally:
                session.allowed_tools = previous

        return Command(
            name=doc.name,
            summary=summary,
            run=run,
            args_schema={
                "type": "object",
                "properties": {"arguments": {"type": "string", "description": doc.argument_hint}},
            },
            source=skill.source,
        )

    async def _apply_mcp(self) -> list[str]:
        """Register the plugins' MCP servers through the M2 client."""
        from snowpea_core.tools import mcp_client

        registered: list[str] = []
        for name, entry in self.mcp_servers.items():
            config = mcp_client.McpServerConfig.parse(name, entry)
            if config is None:
                continue
            try:
                registered.extend(await mcp_client.register_config(self.core, config))
            except Exception as exc:  # noqa: BLE001 - one bad server is not fatal
                log.info("plugin mcp server %s did not start: %s", name, exc)
        return registered

    async def _announce(self) -> None:
        """Tell every connected client that the command table moved."""
        # A reload can add or drop a skill's tools and its index entry, both of
        # which the cached prompt fragments quote (CORE-round-cost).
        from snowpea_core.agent import agent as agent_mod

        agent_mod.invalidate_tools()
        hub = getattr(self.core, "hub", None)
        if hub is None:
            return
        payload = {
            "commands": [info.model_dump(mode="json") for info in self.core.commands.list()],
            "reason": "reload",
        }
        try:
            await hub.notify("commands.changed", payload)
        except Exception:  # noqa: BLE001 - a dead socket must not fail the reload
            log.debug("could not announce commands.changed", exc_info=True)

    # -- queries -------------------------------------------------------
    def index_groups(self) -> IndexGroups:
        """Visible skills for the prompt index, grouped by origin (M15 §B1).

        ``[project]`` first, then ``[global]``, then each ``[plugin:<name>]``
        alphabetically, then each ``[claude-plugin:<name>]``, then ``[builtin]`` —
        the order a reader would guess. Only ``kind == "skill"`` entries are
        listed: a ``commands/*.md`` file is a slash command the user runs, not
        something ``skill_view`` explains. Cached until the next scan, because
        the index is rebuilt into the context tier of every prompt.
        """
        if self._index_groups is not None:
            return self._index_groups
        buckets: dict[str, list[tuple[str, str]]] = {}
        for skill in self.skills.values():
            if skill.kind != "skill" or not skill.doc.user_invocable:
                continue
            group_key = (
                f"claude-plugin:{skill.plugin}"
                if skill.source == SOURCE_CLAUDE_PLUGIN and skill.plugin
                else skill.source
            )
            buckets.setdefault(group_key, []).append(
                (skill.name, _clip(skill.description, INDEX_DESCRIPTION_CHARS))
            )
        plugins = sorted(name for name in buckets if name.startswith("plugin:"))
        claude_plugins = sorted(name for name in buckets if name.startswith("claude-plugin:"))
        order = [
            SOURCE_PROJECT,
            SOURCE_CLAUDE_PROJECT,
            SOURCE_GLOBAL,
            SOURCE_CLAUDE_GLOBAL,
            *plugins,
            *claude_plugins,
        ]
        if SOURCE_CLAUDE_PLUGIN in buckets and SOURCE_CLAUDE_PLUGIN not in order:
            order.append(SOURCE_CLAUDE_PLUGIN)
        order.append(SOURCE_BUILTIN)
        for key in sorted(buckets):
            if key not in order:
                order.append(key)
        groups: IndexGroups = [
            (f"[{source}]", sorted(buckets[source]))
            for source in order
            if buckets.get(source)
        ]
        self._index_groups = groups
        return groups

    def list(self) -> SkillInfos:
        """Everything loaded, as protocol ``SkillInfo`` (``skill.list``)."""
        out: SkillInfos = []
        for plugin in self.plugins:
            out.append(
                SkillInfo(
                    name=plugin.name,
                    kind="plugin",
                    summary=plugin.description,
                    source=plugin.source,
                    installed=True,
                )
            )
        for skill in self.skills.values():
            out.append(
                SkillInfo(
                    name=skill.name,
                    kind=skill.kind,
                    summary=skill.description,
                    source=skill.source,
                    installed=True,
                )
            )
        for agent in self.agents:
            out.append(
                SkillInfo(
                    name=agent.name,
                    kind="agent",
                    summary=agent.description,
                    source=agent.source,
                    installed=True,
                )
            )
        return out

    async def search(self, query: str) -> tuple[SkillInfos, Strings]:
        """Aggregated marketplace search: ``(hits, unreachable sources)``.

        The second element names every source that could not be reached, so an
        empty result is never mistaken for "nothing matched".
        """
        installed = {skill.name for skill in self.skills.values()}
        installed |= {plugin.name for plugin in self.plugins}
        report = await marketplace.search(query, self.home)
        hits = [
            SkillInfo(
                id=hit.id,
                name=hit.name,
                kind="skill",
                summary=hit.description,
                source=hit.source,
                installSpec=hit.install_spec,
                installed=hit.name in installed,
                rating=hit.rating,
                downloads=hit.downloads,
            )
            for hit in report.hits
        ]
        self.last_not_included = list(report.not_included)
        return hits, list(report.unavailable)

    async def install(self, source: str) -> Path:
        """Install a plugin and reload; returns where it landed."""
        target = await marketplace.install(source, self.plugins_dir, self.home)
        await self.reload()
        return target

    async def remove(self, name: str) -> bool:
        """Delete an installed plugin or global skill directory and reload.

        ``skills/`` is checked too: a bare skill installs there rather than
        into ``plugins/`` (M15 §B5d), and it must still be removable.
        """
        for target in (self.plugins_dir / name, self.home / "skills" / name):
            if target.is_dir():
                shutil.rmtree(target)
                await self.reload()
                return True
        return False


def instruction(doc: SkillDoc, args: str) -> str:
    """The text a skill command injects into the session before the turn."""
    body = doc.render(args)
    header = f"Follow these instructions for /{doc.name}"
    if doc.description:
        header = f"{header} — {doc.description}"
    return f"{header}:\n\n{body}".strip()


def expand_tree(value: Any, root: Path) -> Any:
    """Expand ``${CLAUDE_PLUGIN_ROOT}``-style variables through a JSON tree."""
    if isinstance(value, dict):
        return {key: expand_tree(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_tree(item, root) for item in value]
    if isinstance(value, str):
        text = value
        for name in ROOT_VARS:
            text = text.replace(f"${{{name}}}", str(root)).replace(f"${name}", str(root))
        return text.replace(f"${{{PYTHON_VAR}}}", sys.executable).replace(
            f"${PYTHON_VAR}", sys.executable
        )
    return value


__all__ = [
    "INDEX_DESCRIPTION_CHARS",
    "INDEX_GROUP_ORDER",
    "MAX_SCANNED_WORKDIRS",
    "PROJECT_DIRS",
    "PYTHON_VAR",
    "ROOT_VARS",
    "SOURCE_BUILTIN",
    "SOURCE_CLAUDE_GLOBAL",
    "SOURCE_CLAUDE_PLUGIN",
    "SOURCE_CLAUDE_PROJECT",
    "SOURCE_GLOBAL",
    "SOURCE_PROJECT",
    "IndexGroups",
    "LoadedAgent",
    "LoadedPlugin",
    "LoadedSkill",
    "ReloadReport",
    "SkillLoader",
    "builtin_root",
    "expand_tree",
    "instruction",
]
