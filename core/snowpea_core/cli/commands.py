"""Session-less ``snowpea`` subcommands (plan §3.6).

``tools list`` and ``commands list`` are pure RPC lookups — no session, no LLM —
so they double as the install smoke test.  ``daemon status|stop|start`` drives
the daemon process itself.  ``setup`` runs the M3 wizard (Quick/Full/Blank) and
``provider list|login`` inspect and authorise vendors.  What is left is an M4+
placeholder that exits 2.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import re
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from snowpea_core.cli.daemon_client import (
    CALL_TIMEOUT_SEC,
    DaemonClient,
    DaemonError,
    DaemonInfo,
    RpcCallError,
    ensure_daemon,
    health_ok,
    pid_alive,
    read_daemon_json,
)
from snowpea_core.cli.render import (
    EXIT_AGENT_FAILED,
    EXIT_NO_DAEMON,
    EXIT_OK,
    EXIT_USAGE,
)
from snowpea_core.cli.service import service_command
from snowpea_core.config.paths import Paths, resolve_home
from snowpea_core.gateway.slack import slack_manifest

#: Subcommands whose implementation lands after M1.  ``team`` left this list
#: in US-020; nothing is a placeholder now.
PLACEHOLDER_SUBCOMMANDS: tuple[str, ...] = ()

#: Seconds to wait for ``daemon stop`` to see the process go away.
STOP_TIMEOUT_SEC = 10.0

#: ``job.runNow`` runs a whole turn before it answers, so it gets its own,
#: much longer, RPC deadline.
JOB_RUN_TIMEOUT_SEC = 900.0


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _fail(message: str, code: int) -> int:
    print(f"snowpea: {message}", file=sys.stderr)
    return code


# ---------------------------------------------------------------------------
# tool.list / command.list
# ---------------------------------------------------------------------------


async def _lookup(home: Path | str | None, method: str, key: str) -> list[dict[str, Any]]:
    info = await ensure_daemon(home)
    client = DaemonClient(info)
    await client.connect()
    try:
        result = await client.call(method, {})
    finally:
        await client.close()
    items = result.get(key) or []
    return [item for item in items if isinstance(item, dict)]


async def _call(
    home: Path | str | None,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float | None = CALL_TIMEOUT_SEC,
) -> dict[str, Any]:
    """One RPC round trip against the daemon, started if it is not running."""
    info = await ensure_daemon(home)
    client = DaemonClient(info)
    await client.connect()
    try:
        return await client.call(method, params, timeout=timeout)
    finally:
        await client.close()


async def tools_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea tools list [--json]`` → ``tool.list``."""
    try:
        tools = await _lookup(home, "tool.list", "tools")
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"tool.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(tools)
        return EXIT_OK
    if not tools:
        print("no tools registered")
        return EXIT_OK
    width = max(len(str(tool.get("name", ""))) for tool in tools)
    for tool in sorted(tools, key=lambda item: str(item.get("name", ""))):
        name = str(tool.get("name", ""))
        tag = str(tool.get("permissionTag", "?"))
        state = str(tool.get("state", "active"))
        suffix = "" if state == "active" else f" [{state}]"
        provider = str(tool.get("provider", "") or "")
        if provider:
            suffix += f" [provider: {provider}]"
        print(f"{name:<{width}}  {tag:<8}{suffix} {tool.get('description', '')}".rstrip())
    return EXIT_OK


async def commands_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea commands list [--json]`` → ``command.list``."""
    try:
        commands = await _lookup(home, "command.list", "commands")
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"command.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(commands)
        return EXIT_OK
    if not commands:
        print("no commands registered")
        return EXIT_OK
    width = max(len(str(command.get("name", ""))) for command in commands)
    for command in sorted(commands, key=lambda item: str(item.get("name", ""))):
        print(f"/{str(command.get('name', '')):<{width}}  {command.get('summary', '')}".rstrip())
    return EXIT_OK


async def agents_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea agents [--json]`` → ``agent.list``.

    Poll this while ``/ralph`` runs to watch the subagents: each row has a
    ``kind`` (``definition``, ``named`` or ``subagent``) and, for a subagent, a
    ``status`` of queued, running, done or error (AC-04).
    """
    try:
        agents = await _lookup(home, "agent.list", "agents")
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"agent.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(agents)
        return EXIT_OK
    if not agents:
        print("no agents defined and none running")
        return EXIT_OK
    width = max(len(str(agent.get("name", ""))) for agent in agents)
    for agent in agents:
        name = str(agent.get("name", ""))
        kind = str(agent.get("kind", "definition"))
        status = str(agent.get("status") or "")
        tail = str(agent.get("description") or agent.get("task") or "")
        marker = f"[{kind}{'/' + status if status else ''}]"
        print(f"{name:<{width}}  {marker:<22} {tail}".rstrip())
    return EXIT_OK


# ---------------------------------------------------------------------------
# model profiles (GAP-17) and project teams (GAP-16)
# ---------------------------------------------------------------------------


async def model_profiles(
    home: Path | str | None = None, *, workdir: str | None = None, as_json: bool = False
) -> int:
    """``snowpea model profiles [--json]`` — what ``models.*`` currently says.

    ``models.profiles``, ``models.default`` and ``agents.models`` were
    reachable only through the interactive wizard (GAP-17).  The listing is the
    routing's own view: the project's ``models`` block merged over the global
    one, each row tagged with where it came from.
    """
    try:
        result = await _call(home, "settings.get", {"scope": "global"})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"settings.get failed ({exc.code}): {exc.message}", EXIT_USAGE)
    from snowpea_core.config.project import ProjectSettings

    settings = result.get("settings") or {}
    models = settings.get("models") or {}
    profiles = dict(models.get("profiles") or {})
    assignments = dict((settings.get("agents") or {}).get("models") or {})
    directory = Path(workdir or Path.cwd()).expanduser().resolve()
    # Same merge the daemon routes with: project over global, project wins.
    project = ProjectSettings.load(directory).models
    project_profiles = {
        name: profile.model_dump(mode="json") for name, profile in project.profiles.items()
    }
    profiles.update(project_profiles)
    assignments.update(project.agents)
    effective_default = project.default or models.get("default")
    from snowpea_core.providers import effort as effort_scale

    # The effort each profile would run at, resolved with the same chain the
    # daemon uses, so the listing answers "how hard does this one think?"
    # without a second command (CORE-effort).
    efforts = {
        name: effort_scale.resolve(
            settings, (profile or {}).get("provider"), (profile or {}).get("model")
        )
        for name, profile in profiles.items()
    }
    payload = {
        "workdir": str(directory),
        "default": effective_default,
        "efforts": {name: tier for name, (tier, _source) in efforts.items()},
        "globalDefault": models.get("default"),
        "projectDefault": project.default,
        "profiles": profiles,
        "agents": assignments,
        "project": {
            "profiles": project_profiles,
            "agents": dict(project.agents),
        },
    }
    if as_json:
        _print_json(payload)
        return EXIT_OK
    if not profiles:
        print("no model profiles configured; run `snowpea setup` to add one")
    else:
        width = max(len(name) for name in profiles)
        for name, profile in sorted(profiles.items()):
            marker = "*" if name == effective_default else " "
            scope = "project" if name in project_profiles else "global"
            provider = (profile or {}).get("provider", "")
            model = (profile or {}).get("model", "")
            tier, source = efforts.get(name, ("", ""))
            effort_note = f"  effort {tier}" + (f" ({source})" if source != "default" else "")
            print(f"{marker} {name:<{width}}  {provider}:{model}  [{scope}]{effort_note}")
    if assignments:
        print("agents:")
        for agent, profile in sorted(assignments.items()):
            scope = "project" if agent in project.agents else "global"
            print(f"    {agent} -> {profile}  [{scope}]")
    return EXIT_OK


async def _write_project_models(workdir: str | None, mutate: Any) -> tuple[Path, Any]:
    """Apply ``mutate`` to ``<workdir>/.snowpea/settings.json``'s ``models`` block."""
    from snowpea_core.config.project import ProjectSettings

    directory = Path(workdir or Path.cwd()).expanduser().resolve()
    project = ProjectSettings.load(directory)
    mutate(project.models)
    return project.save(directory), project


async def model_assign(
    agent: str,
    profile: str | None,
    home: Path | str | None = None,
    *,
    project: bool = False,
    workdir: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea model assign <agent> <profileId>`` → ``agents.models``.

    An omitted ``profileId`` clears the assignment; over RPC that rides the
    ``null`` delete sentinel ``settings.set`` gained for exactly this
    (CORE-model-assignment).
    """
    name = agent.strip()
    if not name:
        return _fail("usage: snowpea model assign <agent> [<profileId>]", EXIT_USAGE)
    reference = (profile or "").strip() or None

    if project:
        def mutate(models: Any) -> None:
            if reference is None:
                models.agents.pop(name, None)
            else:
                models.agents[name] = reference

        path, _ = await _write_project_models(workdir, mutate)
        if as_json:
            _print_json({"scope": "project", "agent": name, "profile": reference,
                         "path": str(path)})
            return EXIT_OK
        print(
            f"{name} now uses model profile {reference} in {path}"
            if reference
            else f"cleared {name}'s project model assignment ({path})"
        )
        return EXIT_OK

    patch = {"agents": {"models": {name: reference}}}
    try:
        result = await _call(home, "settings.set", {"scope": "global", "patch": patch})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        # An unknown profile id is rejected by the settings validator itself.
        return _fail(f"settings.set failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result.get("settings") or {})
        return EXIT_OK
    print(
        f"{name} now uses model profile {reference}"
        if reference
        else f"cleared {name}'s model assignment"
    )
    return EXIT_OK


async def model_default(
    profile: str | None = None,
    home: Path | str | None = None,
    *,
    project: bool = False,
    workdir: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea model default [profileId]`` → ``models.default``."""
    if not profile and not project:
        return await model_profiles(home, workdir=workdir, as_json=as_json)
    reference = (profile or "").strip() or None

    if project:
        def mutate(models: Any) -> None:
            models.default = reference

        path, _ = await _write_project_models(workdir, mutate)
        if as_json:
            _print_json({"scope": "project", "default": reference, "path": str(path)})
            return EXIT_OK
        print(
            f"project default model profile is now {reference} ({path})"
            if reference
            else f"cleared the project default model profile ({path})"
        )
        return EXIT_OK

    patch = {"models": {"default": reference}}
    try:
        result = await _call(home, "settings.set", {"scope": "global", "patch": patch})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"settings.set failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result.get("settings") or {})
        return EXIT_OK
    print(
        f"default model profile is now {reference}"
        if reference
        else "cleared the default model profile"
    )
    return EXIT_OK


def _known_agent_names(workdir: Path, home: Path | str | None) -> set[str]:
    """Agent definitions visible from a shell, without a daemon session.

    Plugin-provided definitions are *not* here — they exist only inside a
    running daemon — so an unknown name is refused with that caveat spelled
    out rather than silently accepted.
    """
    from snowpea_core.agent.definition import builtin_agent_definitions, discover_definitions

    resolved_home = resolve_home(home)
    names = {defn.name for defn in builtin_agent_definitions()}
    names |= {defn.name for defn in discover_definitions(workdir, resolved_home)}
    return names


def _team_workdir(workdir: str | None) -> Path:
    return Path(workdir or Path.cwd()).expanduser().resolve()


async def team_list(
    home: Path | str | None = None, *, workdir: str | None = None, as_json: bool = False
) -> int:
    """``snowpea team list`` — global teams merged with this project's (GAP-16).

    The merge mirrors :func:`snowpea_core.agent.team_config.teams_for`: project
    teams win on a name clash, and ``activeTeam`` wins over ``default_team``.
    """
    from snowpea_core.config.project import ProjectSettings

    directory = _team_workdir(workdir)
    try:
        result = await _call(home, "settings.get", {"scope": "global"})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"settings.get failed ({exc.code}): {exc.message}", EXIT_USAGE)
    global_agents = ((result.get("settings") or {}).get("agents")) or {}
    global_teams = {
        name: list(members)
        for name, members in (global_agents.get("teams") or {}).items()
        if isinstance(members, list)
    }
    project = ProjectSettings.load(directory)
    # Same merge rule as ``team_config.teams_for``: project wins on a clash.
    merged = dict(global_teams)
    merged.update({name: list(members) for name, members in project.agents.teams.items()})
    active = project.agents.activeTeam or global_agents.get("default_team")
    if as_json:
        _print_json(
            {
                "workdir": str(directory),
                "active": active,
                "teams": merged,
                "project": {name: list(m) for name, m in project.agents.teams.items()},
                "global": global_teams,
            }
        )
        return EXIT_OK
    if not merged:
        print("no teams configured; snowpea team create <name> <agent...>")
        return EXIT_OK
    for name, members in sorted(merged.items()):
        scope = "project" if name in project.agents.teams else "global"
        marker = "*" if name == active else " "
        print(f"{marker} {name} [{scope}]: {', '.join(members)}")
    return EXIT_OK


async def team_create(
    name: str,
    agents: Sequence[str],
    home: Path | str | None = None,
    *,
    workdir: str | None = None,
    use: bool = False,
    as_json: bool = False,
) -> int:
    """``snowpea team create <name> <agent...>`` — write a project team.

    Project scope only: ``<workdir>/.snowpea/settings.json`` is the file the
    daemon already merges over global teams, and writing it needs no daemon.
    """
    from snowpea_core.config.project import ProjectSettings

    directory = _team_workdir(workdir)
    members = list(dict.fromkeys(agent.strip() for agent in agents if agent.strip()))
    if not members:
        return _fail("usage: snowpea team create <name> <agent...>", EXIT_USAGE)
    known = _known_agent_names(directory, home)
    missing = [member for member in members if member not in known]
    if missing:
        return _fail(
            f"unknown agents: {', '.join(missing)}; available: {', '.join(sorted(known))}"
            " (plugin-provided agents are only visible to /team create in a session)",
            EXIT_USAGE,
        )
    project = ProjectSettings.load(directory)
    project.agents.teams[name] = members
    if use or project.agents.activeTeam is None:
        project.agents.activeTeam = name
    path = project.save(directory)
    if as_json:
        _print_json({"team": name, "agents": members, "active": project.agents.activeTeam,
                     "path": str(path)})
        return EXIT_OK
    print(f"team '{name}': {', '.join(members)} ({path})")
    if project.agents.activeTeam == name:
        print(f"active team is now '{name}'")
    return EXIT_OK


async def team_use(
    name: str, home: Path | str | None = None, *, workdir: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea team use <name>`` — set ``agents.activeTeam`` for this project."""
    from snowpea_core.config.project import ProjectSettings

    directory = _team_workdir(workdir)
    try:
        result = await _call(home, "settings.get", {"scope": "global"})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"settings.get failed ({exc.code}): {exc.message}", EXIT_USAGE)
    global_teams = (((result.get("settings") or {}).get("agents")) or {}).get("teams") or {}
    project = ProjectSettings.load(directory)
    if name not in project.agents.teams and name not in global_teams:
        return _fail(f"unknown team: {name}; see snowpea team list", EXIT_USAGE)
    project.agents.activeTeam = name
    path = project.save(directory)
    if as_json:
        _print_json({"active": name, "path": str(path)})
        return EXIT_OK
    print(f"active team is now '{name}'")
    return EXIT_OK


async def team_delete(
    name: str, home: Path | str | None = None, *, workdir: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea team delete <name>`` — remove a *project* team definition."""
    from snowpea_core.config.project import ProjectSettings

    directory = _team_workdir(workdir)
    project = ProjectSettings.load(directory)
    if name not in project.agents.teams:
        return _fail(f"{name} is not a project team; see snowpea team list", EXIT_USAGE)
    project.agents.teams.pop(name)
    if project.agents.activeTeam == name:
        project.agents.activeTeam = None
    path = project.save(directory)
    if as_json:
        _print_json({"deleted": name, "active": project.agents.activeTeam, "path": str(path)})
        return EXIT_OK
    print(f"deleted team '{name}'; active team: {project.agents.activeTeam or 'none'}")
    return EXIT_OK


async def audio_install(
    engine: str, home: Path | str | None = None, *, as_json: bool = False
) -> int:
    """``snowpea audio install <engine>`` → ``audio.install``.

    The daemon streams its output as ``audio.install.progress`` while it works;
    the CLI is not subscribed to those, so it prints the log the call returns.
    An engine the daemon will not install itself — a system package — exits
    ``2`` with the command to run by hand, which is the useful answer.
    """
    name = (engine or "").strip()
    if not name:
        return _fail("usage: snowpea audio install <engine>", EXIT_USAGE)
    try:
        result = await _call(home, "audio.install", {"engine": name})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"audio.install failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
        return EXIT_OK if result.get("ok") else EXIT_USAGE
    body = str(result.get("log") or "").strip()
    if body:
        print(body)
    if result.get("ok"):
        print(f"{result.get('engine', name)} installed")
        return EXIT_OK
    hint = str(result.get("hint") or "").strip()
    return _fail(
        f"could not install {result.get('engine', name)}" + (f": {hint}" if hint else ""),
        EXIT_USAGE,
    )


async def team_status(
    team_id: str | None = None, home: Path | str | None = None, *, as_json: bool = False
) -> int:
    """``snowpea team status [teamId] [--json]`` → ``team.status`` (AC-16).

    Without a team id the daemon answers for the most recent run.  Each row
    carries the task's state, how many merge conflicts re-queued it and, when
    it ended in ``conflict`` or ``failed``, a one-line summary of the hunks
    that are still attached to it.
    """
    try:
        result = await _call(home, "team.status", {"teamId": team_id or ""})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"team.status failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
        return EXIT_OK
    tasks = [row for row in (result.get("tasks") or []) if isinstance(row, dict)]
    print(
        f"team {result.get('teamId', '')} [{result.get('state', '')}] "
        f"{len(tasks)} tasks, {result.get('workers', 0)} workers"
    )
    if result.get("task"):
        print(f"  task: {result['task']}")
    for path in result.get("worktrees") or []:
        print(f"  worktree: {path}")
    for row in tasks:
        line = f"  {row.get('taskId', '')}  {str(row.get('status', '')):<9}"
        if row.get("agentN") is not None:
            line += f" agent {row['agentN']}"
        if row.get("retries"):
            line += f" retries {row['retries']}"
        line += f"  {row.get('title', '')}"
        print(line.rstrip())
        summary = row.get("conflictSummary") or ""
        if summary:
            print(f"      conflict: {summary}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# provider.* (M3 contract §1–§3)
# ---------------------------------------------------------------------------


async def provider_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea provider list [--json]`` → ``provider.list``."""
    try:
        providers = await _lookup(home, "provider.list", "providers")
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"provider.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(providers)
        return EXIT_OK
    width = max((len(str(item.get("vendor", ""))) for item in providers), default=8)
    for item in providers:
        vendor = str(item.get("vendor", ""))
        mark = "*" if item.get("default") else " "
        state = "configured" if item.get("configured") else "-"
        logins = ",".join(str(m) for m in item.get("authMethods") or [])
        # A named OpenAI-compatible server looks like any other vendor here, so
        # say which rows the user added and can remove again.
        kind = "local (custom)" if item.get("custom") else str(item.get("preset") or vendor)
        print(
            f"{mark} {vendor:<{width}}  {state:<10} {kind:<15} {logins:<22} "
            f"{item.get('defaultModel', '')}".rstrip()
        )
    return EXIT_OK


async def provider_add_local(
    name: str,
    home: Path | str | None = None,
    *,
    url: str = "",
    key: str | None = None,
    server_type: str | None = None,
    model: str | None = None,
    label: str | None = None,
    vision: bool | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea provider add-local <name> --url …`` → ``provider.configure``.

    The block it writes carries ``preset: local``, which is what makes the name
    a vendor: ``<name>:<model>`` then resolves everywhere a built-in vendor id
    does, and several servers can be configured side by side.
    """
    from snowpea_core.providers.presets import LOCAL_VARIANT_IDS, validate_custom_vendor_id

    try:
        vendor = validate_custom_vendor_id(name.strip())
    except ValueError as exc:
        return _fail(str(exc), EXIT_USAGE)
    if not url.strip():
        return _fail("snowpea provider add-local needs --url", EXIT_USAGE)
    if server_type and server_type not in LOCAL_VARIANT_IDS:
        return _fail(
            f"unknown server type: {server_type} (one of {', '.join(LOCAL_VARIANT_IDS)})",
            EXIT_USAGE,
        )
    config: dict[str, Any] = {"preset": "local", "base_url": url.strip()}
    if key:
        config["api_key"] = key
    if server_type:
        config["variant"] = server_type
    if model:
        config["model"] = model
    if label:
        config["label"] = label
    if vision is not None:
        # Settles it for this server rather than letting the try-once probe
        # spend a refused request learning it (CORE-vision).
        config["vision"] = vision
    try:
        await _call(home, "provider.configure", {"vendor": vendor, "config": config})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"provider.configure failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json({"vendor": vendor, "config": {**config, "api_key": "***" if key else None}})
        return EXIT_OK
    print(f"{vendor}: {url.strip()}")
    print(f"pick a model with `snowpea provider models {vendor}`")
    return EXIT_OK


async def provider_remove(
    name: str, home: Path | str | None = None, *, as_json: bool = False
) -> int:
    """``snowpea provider remove <name>`` → ``provider.remove``."""
    vendor = name.strip()
    if not vendor:
        return _fail("usage: snowpea provider remove <name>", EXIT_USAGE)
    try:
        await _call(home, "provider.remove", {"vendor": vendor})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"provider.remove failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json({"vendor": vendor, "removed": True})
        return EXIT_OK
    print(f"{vendor}: removed")
    return EXIT_OK


async def provider_models(
    vendor: str | None = None, home: Path | str | None = None, *, as_json: bool = False
) -> int:
    """``snowpea provider models [vendor] [--json]`` → ``provider.models``."""
    params: dict[str, Any] = {"vendor": vendor} if vendor else {}
    try:
        result = await _call(home, "provider.models", params)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"provider.models failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
        return EXIT_OK
    listed = [str(item) for item in (result.get("models") or [])]
    name = str(result.get("vendor") or vendor or "")
    current = str(result.get("current") or "")
    if not listed:
        print(f"{name}: no models reported; set one with `snowpea setup provider`")
        return EXIT_OK
    # Say which rung answered: a curated fallback and the vendor's own catalog
    # look identical in a plain list, and only one of them is authoritative.
    detail = str(result.get("detail") or "")
    if detail:
        print(f"{name}: {detail}")
    # A model nothing has said anything about gets no badge at all, rather than
    # a crossed-out eye that would claim more than is known.
    raw_vision = result.get("vision")
    vision: dict[str, Any] = raw_vision if isinstance(raw_vision, dict) else {}
    for model in listed:
        seen = vision.get(model)
        badge = "" if seen is None else ("  \N{EYE}" if seen else "  (text only)")
        print(f"{'*' if model == current else ' '} {model}{badge}")
    return EXIT_OK


async def provider_login(
    vendor: str,
    home: Path | str | None = None,
    *,
    token: str | None = None,
    method: str | None = None,
) -> int:
    """``snowpea provider login <vendor>`` → ``provider.loginWeb``.

    OpenAI and Gemini open a browser (falling back to their headless flow on a
    machine without one, or when ``--device-code`` asks for it); OpenRouter
    uses PKCE.  Every other vendor answers ``login_unsupported`` with the
    API-key command.
    """
    if not vendor:
        return _fail("usage: snowpea provider login <vendor>", EXIT_USAGE)
    if token is not None:
        if vendor not in ("openai", "gemini"):
            return _fail(
                f"{vendor} does not expose an OAuth access-token login; use its API key",
                EXIT_USAGE,
            )
        entered = token or getpass.getpass(f"{vendor} OAuth access token: ").strip()
        if not entered:
            return _fail("OAuth access token cannot be empty", EXIT_USAGE)
        problem = await _probe_oauth_token(vendor, entered)
        if problem:
            # A warning, not a refusal: the probe can fail for reasons that
            # have nothing to do with the token (offline, blocked endpoint).
            print(f"warning: {problem}")
        # ``None`` clears whatever the previous auth method left behind, so a
        # stale API key cannot outlive the token that replaced it.
        config = {
            "oauth_token": entered,
            "auth_method": "oauth_token",
            "api_key": None,
            "access_token": None,
        }
        try:
            await _call(home, "provider.configure", {"vendor": vendor, "config": config})
        except DaemonError as exc:
            return _fail(str(exc), EXIT_NO_DAEMON)
        except RpcCallError as exc:
            return _fail(f"{vendor} token login failed ({exc.code}): {exc.message}", EXIT_USAGE)
        print(f"{vendor}: OAuth token saved to settings.json")
        return EXIT_OK
    try:
        info = await ensure_daemon(home)
        client = DaemonClient(info)
        await client.connect()
        try:
            await client.call(
                "provider.loginWeb",
                {"vendor": vendor, "method": method or "web"},
                timeout=900.0,
            )
        finally:
            await client.close()
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"{vendor} login failed ({exc.code}): {exc.message}", EXIT_USAGE)
    print(f"{vendor}: signed in; credentials saved to settings.json")
    return EXIT_OK


async def _probe_oauth_token(vendor: str, token: str) -> str | None:
    """One cheap authenticated call, so a bad paste is caught at entry.

    ``None`` means the token worked — or that the check could not be made,
    which must never be reported as a bad token.
    """
    from snowpea_core.providers import models as model_discovery
    from snowpea_core.providers.presets import PRESETS

    preset = PRESETS.get(vendor)
    if preset is None:  # pragma: no cover - guarded by the caller
        return None
    try:
        listed = await model_discovery.list_models(preset, api_key=token, refresh=True)
    except Exception as exc:  # noqa: BLE001 - a probe never blocks a login
        return f"could not verify the token ({exc}); saving it anyway"
    return None if listed else f"{vendor} accepted the token but listed no models"


# ---------------------------------------------------------------------------
# setup (M3 contract §5)
# ---------------------------------------------------------------------------


async def search_test(query: str, home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea search test "<query>"`` — one real query, and the honest verdict.

    Runs in this process against ``$SNOWPEA_HOME/settings.json``, so it works
    with the daemon down and answers the only question a user has after picking
    a provider: is my choice the one actually being used?
    """
    from snowpea_core.config.settings import Settings
    from snowpea_core.tools import search_providers, web

    if not query.strip():
        return _fail('usage: snowpea search test "<query>"', EXIT_USAGE)
    paths = Paths.create(home)
    settings = Settings.load(paths)
    configured = web.configured_provider(settings)
    if search_providers.get(configured) is None:
        return _fail(f"unknown search provider in settings: {configured}", EXIT_USAGE)

    skipped: list[dict[str, str]] = []
    answered: dict[str, Any] | None = None
    for provider in search_providers.chain(configured):
        pid = provider.meta.id
        provider.bind(settings)
        if not provider.available(settings):
            skipped.append({"provider": pid, "reason": web.unavailable_reason(provider, settings)})
            continue
        try:
            hits = await provider.search(query, limit=5)
        except Exception as exc:  # noqa: BLE001 - report, never raise, at the CLI
            skipped.append({"provider": pid, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if not hits:
            skipped.append({"provider": pid, "reason": "no results"})
            continue
        answered = {
            "provider": pid,
            "results": [
                {"title": hit.title, "url": hit.url, "snippet": hit.snippet} for hit in hits
            ],
        }
        break

    payload = {
        "query": query,
        "configured": configured,
        "provider": answered["provider"] if answered else None,
        "fallback_from": (
            configured if answered and answered["provider"] != configured else None
        ),
        "skipped": skipped,
        "results": answered["results"] if answered else [],
    }
    if as_json:
        _print_json(payload)
        return EXIT_OK if answered else EXIT_USAGE

    print(f"configured provider: {configured}")
    for entry in skipped:
        print(f"  skipped {entry['provider']}: {entry['reason']}")
    if not answered:
        print("no provider could answer; web_search would fail the same way")
        return EXIT_USAGE
    if answered["provider"] != configured:
        print(f"answered by:         {answered['provider']}  (fallback from {configured})")
    else:
        print(f"answered by:         {answered['provider']}")
    for row in answered["results"]:
        print(f"  {row['title']}\n    {row['url']}")
    return EXIT_OK


def setup_command(args: argparse.Namespace, home: Path | str | None = None) -> int:
    """``snowpea setup [--quick|--full|--blank] [flags]`` (M3 contract §5).

    ``--login <vendor>`` is an alias for ``snowpea provider login <vendor>``
    and never runs the wizard.
    """
    from snowpea_core.setup import wizard

    login_vendor = getattr(args, "login", None)
    if login_vendor:
        return wizard.login(str(login_vendor), home)

    mode = "quick"
    if getattr(args, "full", False):
        mode = "full"
    elif getattr(args, "blank", False):
        mode = "blank"

    try:
        result = wizard.run(
            mode,  # type: ignore[arg-type]
            home=home,
            vendor=getattr(args, "vendor", None),
            key=getattr(args, "key", None),
            model=getattr(args, "model", None),
            base_url=getattr(args, "base_url", None),
            search_provider=getattr(args, "search_provider", None),
            search_key=getattr(args, "search_key", None),
            browser_provider=getattr(args, "browser_provider", None),
            browser_key=getattr(args, "browser_key", None),
            tools=getattr(args, "tools", None),
            gateway=getattr(args, "gateway", None),
            token=getattr(args, "token", None),
            user_id=getattr(args, "user_id", None),
            section=getattr(args, "section", None),
        )
    except wizard.SetupError as exc:
        return _fail(str(exc), EXIT_USAGE)

    if getattr(result, "cancelled", False):
        print("setup cancelled — nothing written")
        return 0
    print(f"settings written to {result.settings_path}")
    for line in result.summary():
        print(f"  {line}")
    _reload_running_daemon(home)
    _messenger_next_steps(result, home)
    return EXIT_OK


def _live_daemon(home: Path | str | None) -> DaemonInfo | None:
    """The daemon advertised by ``daemon.json`` when it is really answering.

    Deliberately not :func:`ensure_daemon`: nothing here is worth *starting* a
    daemon for.  A stale advert answers no ``/health`` and is ignored.
    """
    info = read_daemon_json(home)
    if info is None or not pid_alive(info.pid):
        return None
    try:
        alive = _run_blocking(health_ok(info.port))
    except OSError:
        return None
    return info if alive else None


def _reload_running_daemon(home: Path | str | None) -> None:
    """Make a running daemon adopt the settings the wizard just wrote.

    The daemon loads ``settings.json`` once at start, so before this a fresh
    ``--model`` reached the file but not the next prompt, which still went out
    with the old model (CORE-settings-reload).
    """
    if _live_daemon(home) is None:
        return
    try:
        result = _run_blocking(_call_reload_settings(home))
    except (DaemonError, RpcCallError, OSError) as exc:
        print(f"daemon: could not reload settings ({exc}); restart it to pick them up")
        return
    keys = [str(key) for key in (result.get("changedKeys") or [])]
    if not result.get("reloaded") and not keys:
        return
    print("daemon: settings reloaded" + (f" ({', '.join(keys)})" if keys else ""))


async def _call_reload_settings(home: Path | str | None) -> dict[str, Any]:
    """``system.reloadSettings`` against the daemon already advertised."""
    info = await ensure_daemon(home)
    client = DaemonClient(info)
    await client.connect()
    try:
        result = await client.call("system.reloadSettings", {})
    finally:
        await client.close()
    return result if isinstance(result, dict) else {}


def _messenger_next_steps(result: Any, home: Path | str | None) -> None:
    """Say how an enabled messenger goes live, and start it if a daemon is up.

    The bindings live in the daemon, so the wizard's settings write is enough
    on its own only for the *next* start.  When one is already running we ask
    it to reconcile now (``gateway.sync``), so the user can talk to their bot
    without restarting anything.
    """
    enabled = result.state.enabled_gateways()
    if not enabled:
        return
    names = ", ".join(enabled)
    print()
    print(f"messenger {names} enabled — it starts with the daemon:")
    print("  run `snowpea` (or `snowpea daemon start`); send /start to your bot")
    if read_daemon_json(home) is None:
        return
    try:
        changed = _run_blocking(_gateway_sync(home))
    except (DaemonError, RpcCallError, OSError) as exc:
        print(f"  (the running daemon did not pick it up: {exc})")
        return
    live = sorted({*changed.get("added", []), *changed.get("kept", [])})
    for platform in live:
        print(f"  {platform}: listening")


def _run_blocking(coro: Any) -> Any:
    """``asyncio.run`` that also works inside ``dispatch``'s running loop.

    ``setup_command`` is synchronous (it prompts on stdin) but is awaited from
    the async ``dispatch``, so ``asyncio.run`` would raise.  A one-shot worker
    thread gets its own loop; the caller is already blocking either way.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _gateway_sync(home: Path | str | None) -> dict[str, Any]:
    """Ask the running daemon to reconcile its messenger bindings."""
    info = await ensure_daemon(home)
    client = DaemonClient(info)
    await client.connect()
    try:
        result = await client.call("gateway.sync", {})
    finally:
        await client.close()
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# daemon.*
# ---------------------------------------------------------------------------


async def daemon_status(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea daemon status`` — ``system.info`` plus lifecycle counters."""
    try:
        info = await ensure_daemon(home)
        client = DaemonClient(info)
        await client.connect()
        bindings: list[dict[str, Any]] = []
        try:
            result = await client.call("system.info", {})
            # Additive and optional: an older daemon has no gateway.list.
            with contextlib.suppress(RpcCallError):
                listed = await client.call("gateway.list", {})
                bindings = list((listed or {}).get("bindings") or [])
        finally:
            await client.close()
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"system.info failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
        return EXIT_OK
    counters = result.get("counters") or {}
    lifecycle = result.get("lifecycle") or {}
    print(f"port         {result.get('port', info.port)}")
    print(f"pid          {result.get('pid', info.pid)}")
    print(f"startedAt    {result.get('startedAt', info.startedAt)}")
    print(f"version      {result.get('version', '')}")
    print(f"home         {result.get('home', '')}")
    for name in ("sessions", "jobs", "gateway_bindings", "named_agents"):
        print(f"{name:<12} {counters.get(name, 0)}")
    print(f"{'messengers':<12} {_messenger_line(bindings)}")
    if lifecycle:
        remaining = lifecycle.get("secondsUntilExit")
        remaining_text = "-" if remaining is None else f"{float(remaining):.0f}s"
        print(
            f"lifecycle    willExit={lifecycle.get('willExit', False)} "
            f"reason={lifecycle.get('reason', '?')} secondsUntilExit={remaining_text}"
        )
        # Plan §2.6: say in one line whether the daemon is going to exit, and
        # if not, what is keeping it up.
        print(lifecycle.get("summary") or _keepalive_summary(counters, remaining))
    return EXIT_OK


def _messenger_line(bindings: list[dict[str, Any]]) -> str:
    """``telegram (listening), slack (stopped)`` — one word per platform."""
    if not bindings:
        return "(none)"
    seen: dict[str, str] = {}
    for binding in bindings:
        platform = str(binding.get("platform") or "?")
        state = "listening" if binding.get("state") == "active" else "stopped"
        # Listening anywhere beats a stale row for the same platform.
        if seen.get(platform) != "listening":
            seen[platform] = state
    return ", ".join(f"{platform} ({seen[platform]})" for platform in sorted(seen))


#: Singular/plural wording mirrored from ``server/lifecycle.COUNTER_LABELS``,
#: used only when talking to a daemon too old to send ``lifecycle.summary``.
COUNTER_LABELS: tuple[tuple[str, str, str], ...] = (
    ("sessions", "session", "sessions"),
    ("gateway_bindings", "gateway binding", "gateway bindings"),
    ("jobs", "job", "jobs"),
    ("named_agents", "named agent", "named agents"),
)


def _keepalive_summary(counters: dict[str, Any], remaining: Any) -> str:
    """``will exit in 120s`` / ``will not exit: 2 gateway bindings, 1 job``."""
    if remaining is not None:
        return f"will exit in {float(remaining):.0f}s"
    reasons = [
        f"{counters.get(name, 0)} {singular if counters.get(name, 0) == 1 else plural}"
        for name, singular, plural in COUNTER_LABELS
        if counters.get(name, 0) > 0
    ]
    return "will not exit: " + (", ".join(reasons) if reasons else "idle shutdown is off")


async def daemon_start(home: Path | str | None = None) -> int:
    """``snowpea daemon start`` — reuse or spawn, then report where it listens."""
    try:
        info = await ensure_daemon(home)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    print(f"snowpea daemon listening on 127.0.0.1:{info.port} (pid {info.pid})")
    return EXIT_OK


async def daemon_stop(home: Path | str | None = None) -> int:
    """``snowpea daemon stop`` — ``system.shutdown``, then wait for the process."""
    resolved = resolve_home(home)
    info = read_daemon_json(resolved)
    if info is None:
        print("no daemon is running")
        return EXIT_OK
    client = DaemonClient(info)
    try:
        await client.connect()
        try:
            await client.call("system.shutdown", {}, timeout=10.0)
        finally:
            await client.close()
    except DaemonError:
        # Already gone, or unreachable: fall through to the wait below.
        with contextlib.suppress(Exception):
            await client.close()
    except RpcCallError as exc:
        await client.close()
        return _fail(f"system.shutdown failed ({exc.code}): {exc.message}", EXIT_USAGE)

    daemon_json = Paths(home=resolved).daemon_json
    deadline = time.monotonic() + STOP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if not pid_alive(info.pid) and not daemon_json.exists():
            break
        await asyncio.sleep(0.1)
    else:
        return _fail(f"the daemon (pid {info.pid}) did not stop in {STOP_TIMEOUT_SEC:.0f}s", 1)
    with contextlib.suppress(OSError):
        daemon_json.unlink()
    print(f"snowpea daemon stopped (pid {info.pid})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# gateway.* (M5 contract §3)
# ---------------------------------------------------------------------------


def parse_target(raw: str) -> dict[str, Any]:
    """``agent:<name>`` | ``session:<id>`` | ``new:<workdir>`` | raw JSON."""
    text = (raw or "").strip()
    if text.startswith("{"):
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("target JSON must be an object")
        return value
    kind, _, rest = text.partition(":")
    if kind == "agent" and rest:
        return {"agent": rest}
    if kind == "session" and rest:
        return {"session": rest}
    if kind == "new":
        return {"new_session": {"workdir": rest or str(Path.cwd())}}
    raise ValueError("target must be agent:<name>, session:<id>, new:<workdir> or JSON")


async def gateway_bind(
    platform: str,
    credentials_ref: str,
    target: str,
    home: Path | str | None = None,
    *,
    channel_id: str | None = None,
    user_id: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea gateway bind <platform> <credentialsRef> <target>``."""
    try:
        parsed = parse_target(target)
    except (ValueError, json.JSONDecodeError) as exc:
        return _fail(str(exc), EXIT_USAGE)
    params: dict[str, Any] = {
        "platform": platform,
        "credentialsRef": credentials_ref,
        "target": parsed,
    }
    if channel_id:
        params["channelId"] = channel_id
    if user_id:
        params["userId"] = user_id
    try:
        result = await _call(home, "gateway.bind", params)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"gateway.bind failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
    else:
        print(f"bound {platform} -> {target} as {result.get('bindingId')}")
    return EXIT_OK


async def gateway_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea gateway list [--json]``."""
    try:
        result = await _call(home, "gateway.list", {})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"gateway.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    bindings = [item for item in (result.get("bindings") or []) if isinstance(item, dict)]
    if as_json:
        _print_json(bindings)
        return EXIT_OK
    if not bindings:
        print("no gateway bindings")
        return EXIT_OK
    for binding in bindings:
        channel = binding.get("channelId") or "*"
        print(
            f"{binding.get('bindingId')}  {binding.get('platform')}  "
            f"{binding.get('target')}  channel={channel}  [{binding.get('state')}]"
        )
    return EXIT_OK


async def gateway_unbind(binding_id: str, home: Path | str | None = None) -> int:
    """``snowpea gateway unbind <bindingId>``."""
    try:
        await _call(home, "gateway.unbind", {"bindingId": binding_id})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"gateway.unbind failed ({exc.code}): {exc.message}", EXIT_USAGE)
    print(f"unbound {binding_id}")
    return EXIT_OK


def gateway_slack_manifest(*, bot_name: str = "snowpea", as_json: bool = False) -> int:
    """``snowpea gateway slack-manifest [--json]``.

    Slack is the one platform whose slash commands cannot be registered over
    the API: they live in the app manifest.  Printing it here is the whole
    feature — paste it into api.slack.com/apps, either "Create from manifest"
    for a new app or App Manifest on an existing one.  The default is indented
    for reading; ``--json`` prints one line, for piping.
    """
    manifest = slack_manifest(bot_name)
    if as_json:
        print(json.dumps(manifest, ensure_ascii=False))
    else:
        _print_json(manifest)
    return EXIT_OK


# ---------------------------------------------------------------------------
# job.* (M5 contract §2)
# ---------------------------------------------------------------------------


def format_job_line(job: dict[str, Any]) -> str:
    """One line per job for ``snowpea job list``."""
    last = job.get("lastRunAt")
    last_text = f"{last}:{job.get('lastStatus') or '?'}" if last else "-"
    state = job.get("state", "scheduled")
    if not job.get("enabled", True) and state != "cancelled":
        state = f"{state},disabled"
    return (
        f"{job.get('jobId')}  {str(job.get('spec', '')):<20} "
        f"next={job.get('nextRunAt') or '-'}  last={last_text}  "
        f"mode={job.get('mode', '?')}  channel={job.get('channel') or '-'}  [{state}]"
    )


async def job_schedule(
    task: str,
    home: Path | str | None = None,
    *,
    spec: str | None = None,
    mode: str = "accept",
    channel: str | None = None,
    workdir: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea job schedule --in 60s --task "…" [--channel telegram:<id>]``."""
    if not spec:
        return _fail("usage: snowpea job schedule --at <spec> --task <task>", EXIT_USAGE)
    if not task:
        return _fail("snowpea job schedule needs --task", EXIT_USAGE)
    params: dict[str, Any] = {"spec": spec, "task": task, "mode": mode}
    if channel:
        params["channel"] = channel
    params["workdir"] = workdir or str(Path.cwd())
    try:
        result = await _call(home, "job.schedule", params, timeout=JOB_RUN_TIMEOUT_SEC)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"job.schedule failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
    else:
        print(f"scheduled {result.get('jobId')} next at {result.get('nextRunAt') or '-'}")
    return EXIT_OK


async def job_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea job list [--json]`` → ``job.list``."""
    try:
        result = await _call(home, "job.list", {}, timeout=JOB_RUN_TIMEOUT_SEC)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"job.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    jobs = [item for item in (result.get("jobs") or []) if isinstance(item, dict)]
    if as_json:
        _print_json(jobs)
        return EXIT_OK
    if not jobs:
        print("no jobs are scheduled")
        return EXIT_OK
    for job in jobs:
        print(format_job_line(job))
    return EXIT_OK


async def job_cancel(job_id: str, home: Path | str | None = None) -> int:
    """``snowpea job cancel <jobId>``."""
    if not job_id:
        return _fail("usage: snowpea job cancel <jobId>", EXIT_USAGE)
    try:
        await _call(home, "job.cancel", {"jobId": job_id}, timeout=JOB_RUN_TIMEOUT_SEC)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"job.cancel failed ({exc.code}): {exc.message}", EXIT_USAGE)
    print(f"cancelled {job_id}")
    return EXIT_OK


async def job_run(job_id: str, home: Path | str | None = None) -> int:
    """``snowpea job run <jobId>`` → ``job.runNow``, which runs in the daemon."""
    if not job_id:
        return _fail("usage: snowpea job run <jobId>", EXIT_USAGE)
    try:
        await _call(home, "job.runNow", {"jobId": job_id}, timeout=JOB_RUN_TIMEOUT_SEC)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"job.runNow failed ({exc.code}): {exc.message}", EXIT_USAGE)
    print(f"ran {job_id}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# memory.* (M5 contract §1b)
# ---------------------------------------------------------------------------


def format_memory_line(entry: dict[str, Any]) -> str:
    """One line per memory for ``snowpea memory list``."""
    scope = str(entry.get("scope") or "global")
    created = str(entry.get("createdAt") or "")[:10]
    stamp = f" {created}" if created else ""
    tags = entry.get("tags") or []
    suffix = "  #" + " #".join(str(tag) for tag in tags) if tags else ""
    text = " ".join(str(entry.get("text") or "").split())
    return f"{entry.get('id')}  [{scope}]{stamp}  {text}{suffix}"


async def memory_list(
    home: Path | str | None = None,
    *,
    scope: str = "all",
    query: str | None = None,
    workdir: str | None = None,
    limit: int = 100,
    as_json: bool = False,
) -> int:
    """``snowpea memory list|search`` → ``memory.list``."""
    params: dict[str, Any] = {"scope": scope, "limit": limit}
    params["project"] = workdir or str(Path.cwd())
    if query:
        params["query"] = query
    try:
        result = await _call(home, "memory.list", params)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"memory.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    entries = [item for item in (result.get("entries") or []) if isinstance(item, dict)]
    if as_json:
        _print_json(entries)
        return EXIT_OK
    if not entries:
        print("nothing is remembered in this scope yet")
        return EXIT_OK
    for entry in entries:
        print(format_memory_line(entry))
    return EXIT_OK


async def memory_forget(memory_id: str, home: Path | str | None = None) -> int:
    """``snowpea memory forget <id>`` → ``memory.delete``."""
    if not memory_id:
        return _fail("usage: snowpea memory forget <id>", EXIT_USAGE)
    try:
        await _call(home, "memory.delete", {"id": memory_id})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"memory.delete failed ({exc.code}): {exc.message}", EXIT_USAGE)
    print(f"forgot {memory_id}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# skill.* (M6 contract §1)
# ---------------------------------------------------------------------------


def resolve_install_source(source: str) -> str:
    """Make a local install source absolute before it crosses the wire.

    The daemon runs in ``$SNOWPEA_HOME``, so a relative path typed in a project
    checkout — ``./tests/fixtures/plugins/sample-plugin`` (AC-10) — would be
    resolved against the wrong directory.  Anything that is not an existing
    local directory (a git URL, ``<marketplace>/<plugin>``, a shortcut) is
    passed through untouched.
    """
    text = source.strip()
    if not text or text.startswith(("git@", "ssh://", "git://", "http://", "https://")):
        return text
    candidate = Path(text).expanduser()
    if candidate.is_dir():
        return str(candidate.resolve())
    return text


#: ``--source registry`` is the friendly alias for the hosted source label.
SOURCE_ALIASES: dict[str, str] = {"registry": "snowpea-registry", "snowpea": "snowpea-registry"}


async def skill_command(
    action: str,
    argument: str = "",
    home: Path | str | None = None,
    *,
    as_json: bool = False,
    source: str | None = None,
) -> int:
    """``snowpea skill list|search <query>|install <source>|remove <name>``."""
    if action == "install":
        argument = resolve_install_source(argument)
    methods = {
        "list": ("skill.list", {}),
        "search": ("skill.search", {"query": argument}),
        "install": ("skill.install", {"source": argument}),
        "remove": ("skill.remove", {"name": argument}),
    }
    if action not in methods:
        return _fail(
            "usage: snowpea skill list|search <query>|install <source>|remove <name>"
            "|publish <dir>|rate <id> <stars>|sources",
            EXIT_USAGE,
        )
    if action in ("search", "install", "remove") and not argument:
        return _fail(f"snowpea skill {action} needs an argument", EXIT_USAGE)
    method, params = methods[action]
    try:
        result = await _call(home, method, params)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"{method} failed ({exc.code}): {exc.message}", EXIT_USAGE)

    if action in ("install", "remove"):
        if as_json:
            _print_json(result)
        else:
            print(f"{action}ed {argument}" if action == "remove" else f"installed {argument}")
        return EXIT_OK

    skills = [item for item in (result.get("skills") or []) if isinstance(item, dict)]
    unavailable = [str(item) for item in (result.get("unavailable") or [])]
    if action == "search" and source:
        wanted = SOURCE_ALIASES.get(source.strip().lower(), source.strip())
        skills = [skill for skill in skills if str(skill.get("source", "")) == wanted]
    if as_json:
        _print_json({"skills": skills, "unavailable": unavailable} if unavailable else skills)
        return EXIT_OK
    for line in unavailable:
        print(f"snowpea: source unreachable — {line}", file=sys.stderr)
    if not skills:
        if unavailable:
            print(f"no skills found ({len(unavailable)} of the searched sources were unreachable)")
        else:
            print("no skills found")
        return EXIT_OK
    width = max(len(str(skill.get("name", ""))) for skill in skills)
    for skill in skills:
        name = str(skill.get("name", ""))
        kind = str(skill.get("kind", "skill"))
        source_label = str(skill.get("source", ""))
        summary = str(skill.get("summary", ""))
        print(f"{name:<{width}}  {kind:<8} {source_label:<22} {summary}".rstrip())
    return EXIT_OK


# ---------------------------------------------------------------------------
# mcp.* (M14 contract §4)
# ---------------------------------------------------------------------------


def _kv(values: list[str] | None, flag: str) -> dict[str, str]:
    """``--env K=V`` repeated -> a dict; a value is never echoed back."""
    out: dict[str, str] = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"{flag} takes K=V, not {item!r}")
        out[key.strip()] = value
    return out


def mcp_entry_params(args: argparse.Namespace) -> dict[str, Any]:
    """The entry keys shared by ``mcp add`` and ``mcp test``, from the parsed args."""
    rest = [str(item) for item in (getattr(args, "rest", None) or [])]
    command = getattr(args, "command", None)
    argv = [str(item) for item in (getattr(args, "args", None) or [])]
    if rest and not command:
        command, argv = rest[0], rest[1:] + argv
    params: dict[str, Any] = {}
    if command:
        params["command"] = command
        if argv:
            params["args"] = argv
    for flag, key in (
        ("url", "url"),
        ("cwd", "cwd"),
        ("preset", "preset"),
        ("permission", "permission"),
        ("type", "type"),
    ):
        value = getattr(args, flag, None)
        if value:
            params[key] = value
    env = _kv(getattr(args, "env", None), "--env")
    headers = _kv(getattr(args, "header", None), "--header")
    if env:
        params["env"] = env
    if headers:
        params["headers"] = headers
    if getattr(args, "timeout", None):
        params["timeoutSec"] = float(args.timeout)
    if getattr(args, "tool_timeout", None):
        params["toolTimeoutSec"] = float(args.tool_timeout)
    return params


def format_mcp_row(server: dict[str, Any], width: int) -> str:
    """One ``snowpea mcp list`` line."""
    name = str(server.get("name", ""))
    scope = str(server.get("scope", ""))
    transport = str(server.get("transport", ""))
    state = "disabled" if server.get("disabled") else str(server.get("state", ""))
    count = server.get("toolCount", 0)
    return f"{name:<{width}}  {scope:<8} {transport:<6} {state:<8} {count} tools".rstrip()


def _print_mcp_detail(server: dict[str, Any]) -> None:
    print(f"{server.get('name')} ({server.get('scope')}, {server.get('transport')})")
    if server.get("plugin"):
        print(f"  from plugin: {server['plugin']}")
    if server.get("command"):
        argv = " ".join(str(item) for item in server.get("args") or [])
        print(f"  command: {server['command']} {argv}".rstrip())
    if server.get("url"):
        print(f"  url: {server['url']}")
    for label, key in (("env", "envKeys"), ("headers", "headerKeys")):
        keys = server.get(key) or []
        if keys:
            print(f"  {label}: " + ", ".join(f"{name}=***" for name in keys))
    print(f"  permission: {server.get('permission')}")
    print(f"  state: {'disabled' if server.get('disabled') else server.get('state')}")
    if server.get("error"):
        print(f"  error: {server['error']}")
    for tool in server.get("tools") or []:
        print(f"    {tool.get('name')} — {tool.get('description', '')}".rstrip(" —"))


async def mcp_command(
    action: str,
    args: argparse.Namespace,
    home: Path | str | None = None,
    *,
    as_json: bool = False,
) -> int:
    """``snowpea mcp list|get|add|add-json|remove|test|configure|reload|catalog``."""
    workdir = str(Path.cwd())
    name = str(getattr(args, "name", "") or "")
    scope = "global" if getattr(args, "scope_global", False) else str(
        getattr(args, "scope", "project") or "project"
    )
    method: str
    params: dict[str, Any]
    try:
        if action in ("list", "get"):
            method, params = "mcp.list", {"workdir": workdir}
        elif action == "add":
            params = {
                "name": name,
                "scope": scope,
                "workdir": workdir,
                "force": bool(getattr(args, "force", False)),
                "test": not bool(getattr(args, "no_test", False)),
                **mcp_entry_params(args),
            }
            method = "mcp.add"
        elif action == "add-json":
            entry = json.loads(str(getattr(args, "entry", "") or ""))
            if not isinstance(entry, dict):
                return _fail("snowpea mcp add-json needs a JSON object", EXIT_USAGE)
            raw_tools = entry.get("tools")
            tools: dict[str, Any] = raw_tools if isinstance(raw_tools, dict) else {}
            params = {
                "name": name,
                "scope": scope,
                "workdir": workdir,
                "force": bool(getattr(args, "force", False)),
                "test": not bool(getattr(args, "no_test", False)),
            }
            for key in ("type", "command", "args", "env", "url", "headers", "cwd", "disabled"):
                if key in entry:
                    params[key] = entry[key]
            if tools.get("include"):
                params["toolsInclude"] = list(tools["include"])
            if tools.get("exclude"):
                params["toolsExclude"] = list(tools["exclude"])
            method = "mcp.add"
        elif action == "remove":
            method, params = "mcp.remove", {"name": name, "scope": scope, "workdir": workdir}
        elif action == "test":
            params = {"workdir": workdir, **mcp_entry_params(args)}
            # ``snowpea mcp test -- python server.py`` probes a draft; argparse
            # hands the first word to ``name``, so a draft wins over the name.
            if name and not (params.get("command") or params.get("url")):
                params["name"] = name
            method = "mcp.test"
        elif action == "configure":
            keep = [str(item) for item in (getattr(args, "tools", None) or [])]
            for item in str(getattr(args, "tools_csv", "") or "").split(","):
                if item.strip():
                    keep.append(item.strip())
            method = "mcp.update"
            params = {
                "name": name,
                "scope": scope,
                "workdir": workdir,
                "patch": {"toolsInclude": keep},
            }
        elif action in ("enable", "disable"):
            method = "mcp.update"
            params = {
                "name": name,
                "scope": scope,
                "workdir": workdir,
                "patch": {"disabled": action == "disable"},
            }
        elif action == "reload":
            method, params = "mcp.reload", {"workdir": workdir}
            if name:
                params["name"] = name
        elif action == "catalog":
            method, params = "mcp.catalog", {}
        else:
            return _fail(
                "usage: snowpea mcp list|get <name>|add <name> -- <command>|add-json <name> "
                "'<json>'|remove <name>|test <name>|configure <name>|enable <name>|"
                "disable <name>|reload [name]|catalog",
                EXIT_USAGE,
            )
    except (ValueError, json.JSONDecodeError) as exc:
        return _fail(f"snowpea mcp {action}: {exc}", EXIT_USAGE)

    try:
        result = await _call(home, method, params)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"{method} failed ({exc.code}): {exc.message}", EXIT_USAGE)

    if action == "get":
        servers = [row for row in (result.get("servers") or []) if row.get("name") == name]
        if not servers:
            return _fail(f"no MCP server called {name}", EXIT_USAGE)
        if as_json:
            _print_json(servers[0])
        else:
            _print_mcp_detail(servers[0])
        return EXIT_OK
    if as_json:
        _print_json(result)
        return EXIT_OK

    if action == "list":
        servers = list(result.get("servers") or [])
        if not servers:
            print("no MCP servers configured")
            return EXIT_OK
        width = max(len(str(row.get("name", ""))) for row in servers)
        for row in servers:
            print(format_mcp_row(row, width))
        return EXIT_OK
    if action == "catalog":
        entries = list(result.get("entries") or [])
        width = max((len(str(row.get("id", ""))) for row in entries), default=0)
        for row in entries:
            needs = row.get("needs") or []
            suffix = f" (needs {', '.join(str(n) for n in needs)})" if needs else ""
            print(f"{str(row.get('id', '')):<{width}}  {row.get('description', '')}{suffix}")
        return EXIT_OK
    if action == "test":
        if result.get("ok"):
            found = list(result.get("tools") or [])
            print(f"ok — {len(found)} tools in {result.get('elapsedMs', 0)} ms")
            for tool in found:
                print(f"  {tool.get('name')} — {tool.get('description', '')}".rstrip(" —"))
            return EXIT_OK
        return _fail(f"failed: {result.get('error')}", EXIT_USAGE)
    if action in ("add", "add-json"):
        found = list(result.get("tools") or [])
        listing = ", ".join(str(tool.get("name", "")) for tool in found)
        suffix = f": {listing}" if listing else ""
        print(f"Connected — {len(found)} tools{suffix} … saved to {result.get('path')}")
        for warning in result.get("warnings") or []:
            print(f"warning: {warning}", file=sys.stderr)
        return EXIT_OK
    if action == "reload":
        servers = list(result.get("servers") or [])
        names = ", ".join(str(item) for item in servers)
        print(f"reloaded {names}" if servers else "nothing to reload")
        return EXIT_OK
    print(f"{action}d {name}" if action != "configure" else f"configured {name}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# skill publish / skill rate — talk to the hosted registry directly, no daemon
# ---------------------------------------------------------------------------


def _registry_settings(home: Path | str | None):
    from snowpea_core.config.settings import Settings

    paths = Paths.create(home)
    return Settings.load(paths)


async def skill_publish_command(
    directory: str,
    home: Path | str | None = None,
    *,
    registry: str | None = None,
    token: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea skill publish <dir> [--registry <url>] [--token <t>]``."""
    from snowpea_core.skills import publish as publish_mod
    from snowpea_core.skills import registry_client

    if not directory:
        return _fail(
            "usage: snowpea skill publish <dir> [--registry <url>] [--token <t>]", EXIT_USAGE
        )
    try:
        package = publish_mod.load_skill_dir(directory)
    except publish_mod.PublishError as exc:
        return _fail(str(exc), EXIT_USAGE)

    settings = _registry_settings(home)
    url = registry_client.resolve_url(registry, settings)
    resolved_token = registry_client.resolve_token(token, settings)
    if not resolved_token:
        resolved_token = getpass.getpass(f"{url} publisher token: ").strip()
    if not resolved_token:
        return _fail("a publisher token is required to publish", EXIT_USAGE)

    zip_bytes = publish_mod.build_zip(package)
    client = registry_client.HttpRegistryClient(url)
    try:
        result = await client.publish(
            zip_bytes, filename=f"{package.name}.zip", token=resolved_token
        )
    except registry_client.RegistryError as exc:
        return _fail(f"publish failed: {exc}", EXIT_USAGE)

    skill = result.get("skill") if isinstance(result, dict) else None
    install_url = (skill or {}).get("installUrl") if isinstance(skill, dict) else None
    if as_json:
        _print_json(result)
        return EXIT_OK
    created = "published" if result.get("created") else "updated"
    print(f"{created} {package.name}" + (f" — {install_url}" if install_url else ""))
    print(f"install with: snowpea skill install registry:{package.name}")
    return EXIT_OK


async def skill_rate_command(
    identifier: str,
    stars: str,
    comment: str | None,
    home: Path | str | None = None,
    *,
    registry: str | None = None,
    token: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea skill rate <id> <stars> [--comment <text>]``."""
    from snowpea_core.skills import registry_client

    if not identifier or not stars:
        return _fail("usage: snowpea skill rate <id> <1-5> [--comment <text>]", EXIT_USAGE)
    try:
        stars_int = int(stars)
    except ValueError:
        return _fail(f"stars must be an integer 1-5, got {stars!r}", EXIT_USAGE)
    if not 1 <= stars_int <= 5:
        return _fail(f"stars must be 1-5, got {stars_int}", EXIT_USAGE)

    settings = _registry_settings(home)
    url = registry_client.resolve_url(registry, settings)
    resolved_token = registry_client.resolve_token(token, settings)
    client = registry_client.HttpRegistryClient(url)
    try:
        result = await client.rate(identifier, stars_int, comment=comment, token=resolved_token)
    except registry_client.RegistryError as exc:
        return _fail(f"rate failed: {exc}", EXIT_USAGE)

    if as_json:
        _print_json(result)
        return EXIT_OK
    rating = result.get("rating")
    count = result.get("ratingCount")
    print(f"rated {identifier}: {stars_int} stars (average {rating}, {count} ratings)")
    return EXIT_OK


async def skill_sources_command(
    home: Path | str | None = None,
    *,
    registry: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea skill sources`` — every hub the registry federates, and its health."""
    from snowpea_core.skills import registry_client

    settings = _registry_settings(home)
    url = registry_client.resolve_url(registry, settings)
    client = registry_client.HttpRegistryClient(url)
    try:
        sources = await client.sources()
    except registry_client.RegistryError as exc:
        return _fail(f"could not reach {url}: {exc}", EXIT_USAGE)

    if as_json:
        _print_json(sources)
        return EXIT_OK
    if not sources:
        print(f"{url}: no sources reported")
        return EXIT_OK
    width = max(len(str(row.get("id", ""))) for row in sources)
    for row in sources:
        sid = str(row.get("id", ""))
        label = str(row.get("label", ""))
        enabled = row.get("enabled")
        status = "enabled" if enabled else f"disabled ({row.get('disabledReason') or '?'})"
        count = row.get("count")
        last_error = row.get("lastError")
        line = f"{sid:<{width}}  {label:<24} {status:<12}"
        if count is not None:
            line += f" {count} skills"
        if last_error:
            line += f"  — last error: {last_error}"
        print(line.rstrip())
    return EXIT_OK


# ---------------------------------------------------------------------------
# system.checkUpdate / system.update (CORE-update)
# ---------------------------------------------------------------------------

#: An upgrade downloads and builds a package, so it gets a long deadline.
UPDATE_TIMEOUT_SEC = 900.0


def format_update_line(answer: dict[str, Any]) -> str:
    """One human line for a ``system.checkUpdate`` answer."""
    current = str(answer.get("current", ""))
    latest = str(answer.get("latest", ""))
    if answer.get("error"):
        return f"snowpea v{current} — could not check for updates: {answer['error']}"
    if not answer.get("available"):
        return f"snowpea v{current} is up to date."
    channel = str(answer.get("channel", ""))
    return f"snowpea v{current} — update available: v{latest} (from {channel})"


async def update_cli(
    home: Path | str | None = None, *, check_only: bool = False, as_json: bool = False
) -> int:
    """``snowpea update [--check]`` — report, and unless ``--check``, install."""
    try:
        info = await ensure_daemon(home)
        client = DaemonClient(info)
        await client.connect()
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)

    try:
        try:
            answer = await client.call("system.checkUpdate", {"force": True})
        except RpcCallError as exc:
            return _fail(f"system.checkUpdate failed ({exc.code}): {exc.message}", EXIT_USAGE)

        if as_json and check_only:
            _print_json(answer)
        else:
            print(format_update_line(answer))
        if check_only or not answer.get("available"):
            return EXIT_OK

        try:
            started = await client.call("system.update", {}, timeout=UPDATE_TIMEOUT_SEC)
        except RpcCallError as exc:
            return _fail(f"system.update failed ({exc.code}): {exc.message}", EXIT_USAGE)
        if as_json:
            _print_json(started)
        if not started.get("started"):
            return _fail(str(started.get("error") or "the update did not start"), EXIT_USAGE)
        print(f"running {started.get('command')}")
        print(f"log: {started.get('log')}")

        phase, message = await _await_update(client)
        if phase != "done":
            return _fail(message or "the update failed", EXIT_AGENT_FAILED)
        print(message or "update finished")
    finally:
        await client.close()

    await daemon_stop(home)
    print("restart snowpea to run the new version")
    return EXIT_OK


async def _await_update(
    client: DaemonClient, *, timeout: float = UPDATE_TIMEOUT_SEC
) -> tuple[str, str]:
    """Wait for ``system.updateProgress`` to report ``done`` or ``failed``.

    Bounded: the upgrade subprocess is detached, so an installer that never
    finishes must not leave ``snowpea update`` blocked forever.
    """

    async def _listen() -> tuple[str, str]:
        async for frame in client.notifications():
            if frame.get("method") != "system.updateProgress":
                continue
            params = frame.get("params") or {}
            phase = str(params.get("phase", ""))
            if phase in ("done", "failed"):
                return phase, str(params.get("message", ""))
        return "failed", "the daemon closed the connection before the update finished"

    try:
        return await asyncio.wait_for(_listen(), timeout=timeout)
    except TimeoutError:
        return "failed", f"the update did not report back within {timeout:.0f}s"
    except DaemonError as exc:  # pragma: no cover - socket died mid-upgrade
        return "failed", str(exc)


# ---------------------------------------------------------------------------
# placeholders
# ---------------------------------------------------------------------------


def placeholder(name: str) -> int:
    """Print the M2+ notice and return the usage exit code."""
    print(f"snowpea {name}: not yet implemented", file=sys.stderr)
    return EXIT_USAGE


# ---------------------------------------------------------------------------
# session context / compaction (CORE-context)
# ---------------------------------------------------------------------------


def format_context_line(summary: dict[str, Any]) -> str:
    """``"s-abc  12.3k / 200.0k (6.2%)"`` for one ``session.list`` row."""
    from snowpea_core.cli.render import format_tokens

    used = int(summary.get("contextUsed", 0) or 0)
    window = summary.get("contextWindow")
    model = str(summary.get("model") or "?")
    if window:
        percent = round(used * 100.0 / int(window), 1)
        size = f"{format_tokens(used)} / {format_tokens(int(window))} ({percent}%)"
    else:
        size = f"{format_tokens(used)} / ?"
    return f"{summary.get('sessionId', '?')}  {size}  {model}"


async def session_context(
    session_id: str | None = None, home: Path | str | None = None, *, as_json: bool = False
) -> int:
    """``snowpea session context [id] [--json]`` → the ``session.list`` rows."""
    try:
        sessions = await _lookup(home, "session.list", "sessions")
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"session.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if session_id:
        sessions = [row for row in sessions if str(row.get("sessionId")) == session_id]
        if not sessions:
            return _fail(f"no such session: {session_id}", EXIT_USAGE)
    if as_json:
        _print_json(sessions)
        return EXIT_OK
    if not sessions:
        print("no live sessions")
        return EXIT_OK
    for row in sessions:
        print(format_context_line(row))
    return EXIT_OK


async def session_list(
    home: Path | str | None = None,
    *,
    include_closed: bool = False,
    workdir: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea session list [--include-closed] [--workdir DIR]`` → ``session.list``.

    Saved sessions used to be reachable only from the TUI's ``/resume`` picker,
    so a headless user could neither see what had accumulated under
    ``$SNOWPEA_HOME`` nor pick an id to resume (GAP-14).
    """
    params: dict[str, Any] = {"includeClosed": include_closed}
    if workdir:
        params["workdir"] = str(Path(workdir).expanduser().resolve())
    try:
        result = await _call(home, "session.list", params)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"session.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    sessions = [row for row in (result.get("sessions") or []) if isinstance(row, dict)]
    if as_json:
        _print_json(sessions)
        return EXIT_OK
    if not sessions:
        print("no saved sessions" if include_closed else "no live sessions")
        return EXIT_OK
    width = max(len(str(row.get("sessionId", ""))) for row in sessions)
    for row in sessions:
        prompt = str(row.get("lastPrompt") or "").replace("\n", " ").strip()
        if len(prompt) > 60:
            prompt = prompt[:57] + "..."
        print(
            f"{str(row.get('sessionId', '')):<{width}}  {str(row.get('mode', '')):<6}"
            f"  {row.get('createdAt', '')}  {row.get('workdir', '')}"
            + (f"  {prompt}" if prompt else "")
        )
    return EXIT_OK


async def session_delete(
    home: Path | str | None = None,
    *,
    session_id: str | None = None,
    workdir: str | None = None,
    all_dirs: bool = False,
    as_json: bool = False,
) -> int:
    """``snowpea session delete <id>`` / ``session clear`` → ``session.deleteSaved``.

    A live session is never deleted; the daemon skips those.  Deleting also
    removes the session's attachments and audio from disk.
    """
    params: dict[str, Any] = {}
    if session_id:
        params["sessionId"] = session_id
    elif all_dirs:
        params["all"] = True
    else:
        params["workdir"] = str(Path(workdir or Path.cwd()).expanduser().resolve())
    try:
        result = await _call(home, "session.deleteSaved", params)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"session.deleteSaved failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
        return EXIT_OK
    deleted = int(result.get("deleted") or 0)
    print(f"deleted {deleted} saved session{'' if deleted == 1 else 's'}")
    return EXIT_OK


async def session_compact(
    session_id: str,
    home: Path | str | None = None,
    *,
    instructions: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea session compact <id> [instructions]`` → ``session.compact``."""
    from snowpea_core.cli.render import format_tokens

    params: dict[str, Any] = {"sessionId": session_id}
    if instructions:
        params["instructions"] = instructions
    try:
        result = await _call(home, "session.compact", params, timeout=None)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"session.compact failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
        return EXIT_OK
    before = format_tokens(int(result.get("before", 0) or 0))
    after = format_tokens(int(result.get("after", 0) or 0))
    print(f"compacted {session_id}: ~{before} → ~{after} tokens")
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser wiring
# ---------------------------------------------------------------------------


def add_subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[Any]:
    """Attach every ``snowpea <subcommand>`` to ``parser``."""
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>")

    tools = sub.add_parser("tools", help="inspect the tool registry")
    tools_sub = tools.add_subparsers(dest="action", metavar="<action>")
    tools_list_parser = tools_sub.add_parser("list", help="list registered tools")
    tools_list_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    commands = sub.add_parser("commands", help="inspect the slash-command registry")
    commands_sub = commands.add_subparsers(dest="action", metavar="<action>")
    commands_list_parser = commands_sub.add_parser("list", help="list registered commands")
    commands_list_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )

    session_parser = sub.add_parser("session", help="inspect live sessions")
    session_sub = session_parser.add_subparsers(dest="action", metavar="<action>")
    session_context_parser = session_sub.add_parser(
        "context", help="show how full each session's context window is"
    )
    session_context_parser.add_argument(
        "session_id", nargs="?", default=None, help="only this session"
    )
    session_context_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    session_compact_parser = session_sub.add_parser(
        "compact", help="summarise a session's conversation and replace it"
    )
    session_compact_parser.add_argument("session_id", help="session to compact")
    session_compact_parser.add_argument(
        "instructions", nargs="?", default=None, help="what the summary must keep"
    )
    session_compact_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    session_list_parser = session_sub.add_parser(
        "list", help="list live sessions, and saved ones with --include-closed"
    )
    session_list_parser.add_argument(
        "--include-closed",
        dest="include_closed",
        action="store_true",
        help="include persisted, already closed sessions",
    )
    session_list_parser.add_argument(
        "--workdir", default=None, help="only sessions rooted in this directory"
    )
    session_list_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    session_delete_parser = session_sub.add_parser(
        "delete", help="delete one saved session and the files it owned"
    )
    session_delete_parser.add_argument("session_id", help="saved session to delete")
    session_delete_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    session_clear_parser = session_sub.add_parser(
        "clear", help="delete the saved sessions of one directory, or all of them"
    )
    session_clear_parser.add_argument(
        "--all", dest="clear_all", action="store_true", help="every directory, not just this one"
    )
    session_clear_parser.add_argument(
        "--workdir", default=None, help="directory to clear (default: the current one)"
    )
    session_clear_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )

    model_parser = sub.add_parser("model", help="inspect and assign model profiles")
    model_sub = model_parser.add_subparsers(dest="action", metavar="<action>")
    model_profiles_parser = model_sub.add_parser(
        "profiles", help="list configured model profiles and per-agent assignments"
    )
    model_profiles_parser.add_argument(
        "--workdir", default=None, help="project whose models block to merge in"
    )
    model_profiles_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    model_assign_parser = model_sub.add_parser(
        "assign", help="route one agent to a model profile"
    )
    model_assign_parser.add_argument("agent", help="agent name, e.g. executor")
    model_assign_parser.add_argument(
        "profile", nargs="?", default=None, help="profile id; omit to clear the assignment"
    )
    model_assign_parser.add_argument(
        "--project",
        action="store_true",
        help="write it to this project instead of global settings",
    )
    model_assign_parser.add_argument("--workdir", default=None, help="project to write it to")
    model_assign_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    model_default_parser = model_sub.add_parser(
        "default", help="show or set the default model profile"
    )
    model_default_parser.add_argument(
        "profile", nargs="?", default=None, help="profile id to make the default"
    )
    model_default_parser.add_argument(
        "--project",
        action="store_true",
        help="set this project's default instead of the global one",
    )
    model_default_parser.add_argument("--workdir", default=None, help="project to write it to")
    model_default_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )

    provider = sub.add_parser("provider", help="inspect and log into chat providers")
    provider_sub = provider.add_subparsers(dest="action", metavar="<action>")
    provider_list_parser = provider_sub.add_parser("list", help="list known vendors")
    provider_list_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    provider_models_parser = provider_sub.add_parser(
        "models", help="ask a vendor which models it serves"
    )
    provider_models_parser.add_argument(
        "vendor", nargs="?", default=None, help="vendor to query (default: the configured one)"
    )
    provider_models_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    provider_add_local_parser = provider_sub.add_parser(
        "add-local", help="add a named OpenAI-compatible server (vLLM, Ollama, LM Studio)"
    )
    provider_add_local_parser.add_argument("name", help="name for this server, e.g. hon2")
    provider_add_local_parser.add_argument(
        "--url", required=True, help="base URL, e.g. http://hon2:8000/v1"
    )
    provider_add_local_parser.add_argument("--key", default=None, help="API key, if it needs one")
    provider_add_local_parser.add_argument(
        "--type",
        dest="server_type",
        default=None,
        choices=["vllm", "ollama", "lmstudio", "generic"],
        help="which server software this is",
    )
    provider_add_local_parser.add_argument(
        "--model", default=None, help="default model id for this server"
    )
    provider_add_local_parser.add_argument(
        "--label", default=None, help="human-readable name for pickers"
    )
    provider_add_local_parser.add_argument(
        "--vision",
        dest="vision",
        action="store_true",
        default=None,
        help="this server's model can be sent images",
    )
    provider_add_local_parser.add_argument(
        "--no-vision",
        dest="vision",
        action="store_false",
        help="this server's model is text-only; never send images",
    )
    provider_add_local_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    provider_remove_parser = provider_sub.add_parser(
        "remove", help="forget a provider and the model profiles that used it"
    )
    provider_remove_parser.add_argument("name", help="provider to remove")
    provider_remove_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    provider_login_parser = provider_sub.add_parser(
        "login", help="browser login, or enter an OAuth token on a remote machine"
    )
    provider_login_parser.add_argument("vendor", help="vendor to log into")
    provider_login_parser.add_argument(
        "--device-code",
        dest="device_code",
        action="store_true",
        help="use the headless flow (device code / gcloud) instead of a browser",
    )
    provider_login_parser.add_argument(
        "--token",
        nargs="?",
        const="",
        default=None,
        metavar="TOKEN",
        help="use an OAuth access token; omit TOKEN for a hidden prompt",
    )

    search = sub.add_parser("search", help="check the web-search provider")
    search_sub = search.add_subparsers(dest="action", metavar="<action>")
    search_test_parser = search_sub.add_parser(
        "test", help="run one query with the configured provider and say which one answered"
    )
    search_test_parser.add_argument("query", help="what to search for")
    search_test_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )

    setup_parser = sub.add_parser("setup", help="configure providers, search, tools")
    setup_parser.add_argument(
        "section",
        nargs="?",
        default=None,
        choices=["provider", "providers", "search", "browser", "tools", "gateway"],
        help="configure one section only, e.g. `snowpea setup search` (Hermes-style)",
    )
    setup_mode = setup_parser.add_mutually_exclusive_group()
    setup_mode.add_argument(
        "--quick", action="store_true", help="ask for the LLM provider only (default)"
    )
    setup_mode.add_argument(
        "--full", action="store_true", help="walk every screen (providers → … → done)"
    )
    setup_mode.add_argument("--blank", action="store_true", help="ask nothing, write the defaults")
    setup_parser.add_argument("--vendor", default=None, help="LLM vendor id, e.g. anthropic")
    setup_parser.add_argument("--key", default=None, help="API key for --vendor")
    setup_parser.add_argument("--model", default=None, help="default model for --vendor")
    setup_parser.add_argument("--base-url", dest="base_url", default=None, help="API base URL")
    setup_parser.add_argument(
        "--search-provider", dest="search_provider", default=None, help="web-search provider id"
    )
    setup_parser.add_argument(
        "--search-key",
        dest="search_key",
        default=None,
        help="API key for --search-provider (saved under search.credentials)",
    )
    setup_parser.add_argument(
        "--browser-provider", dest="browser_provider", default=None, help="browser provider id"
    )
    setup_parser.add_argument(
        "--browser-key",
        dest="browser_key",
        default=None,
        help="API key for --browser-provider (saved under browser.credentials)",
    )
    setup_parser.add_argument(
        "--tools", default=None, help="tool categories: 'vision,-git' enables and disables"
    )
    setup_parser.add_argument("--gateway", default=None, help="gateway to enable")
    setup_parser.add_argument("--token", default=None, help="bot token for --gateway")
    setup_parser.add_argument(
        "--user-id",
        dest="user_id",
        default=None,
        help="your user id on --gateway; only this account may approve from chat",
    )
    setup_parser.add_argument(
        "--login", default=None, metavar="VENDOR", help="browser login (alias of provider login)"
    )

    skill = sub.add_parser("skill", help="find, install and list skills and plugins")
    skill_sub = skill.add_subparsers(dest="action", metavar="<action>")
    for name, help_text, argument in (
        ("list", "list installed skills, agents, commands and plugins", None),
        ("search", "search the skill marketplaces", "query"),
        ("install", "install a path, git URL, <marketplace>/<plugin> or shortcut", "source"),
        ("remove", "delete an installed plugin", "name"),
    ):
        skill_action = skill_sub.add_parser(name, help=help_text)
        if argument:
            skill_action.add_argument(argument)
        skill_action.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
        if name == "search":
            skill_action.add_argument(
                "--source",
                default=None,
                help="only show hits from this source, e.g. registry (snowpea-registry)",
            )

    skill_publish = skill_sub.add_parser(
        "publish", help="zip a skill directory and publish it to the registry"
    )
    skill_publish.add_argument("dir", help="path to the skill directory (holds SKILL.md)")
    skill_publish.add_argument("--registry", default=None, help="registry base URL override")
    skill_publish.add_argument("--token", default=None, help="publisher token override")
    skill_publish.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    skill_rate = skill_sub.add_parser("rate", help="rate a registry skill 1-5 stars")
    skill_rate.add_argument("id", help="registry skill id")
    skill_rate.add_argument("stars", help="1-5")
    skill_rate.add_argument("--comment", default=None, help="optional review text")
    skill_rate.add_argument("--registry", default=None, help="registry base URL override")
    skill_rate.add_argument("--token", default=None, help="publisher token override (optional)")
    skill_rate.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    skill_sources = skill_sub.add_parser(
        "sources", help="list the hubs the registry federates, and their health"
    )
    skill_sources.add_argument("--registry", default=None, help="registry base URL override")
    skill_sources.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    mcp = sub.add_parser("mcp", help="add, inspect and test MCP servers")
    mcp_sub = mcp.add_subparsers(dest="action", metavar="<action>")

    def _mcp_scope(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--scope",
            choices=("project", "global"),
            default="project",
            help="which .mcp.json to write: the project's (default) or $SNOWPEA_HOME's",
        )
        target.add_argument(
            "--global",
            dest="scope_global",
            action="store_true",
            help="shorthand for --scope global",
        )

    def _mcp_entry(target: argparse.ArgumentParser) -> None:
        target.add_argument("--command", default=None, help="executable for a stdio server")
        target.add_argument("--args", nargs="*", default=None, help="arguments for --command")
        target.add_argument("--url", default=None, help="endpoint for an http or sse server")
        target.add_argument(
            "--type", choices=("stdio", "http", "sse"), default=None, help="force a transport"
        )
        target.add_argument(
            "--env", action="append", default=None, metavar="K=V", help="environment variable"
        )
        target.add_argument(
            "--header", action="append", default=None, metavar="K=V", help="HTTP header"
        )
        target.add_argument("--cwd", default=None, help="working directory for a stdio server")
        target.add_argument("--timeout", default=None, help="startup cap in seconds")
        target.add_argument("--tool-timeout", default=None, help="per-call cap in seconds")

    mcp_list = mcp_sub.add_parser("list", help="list every configured MCP server")
    mcp_list.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    mcp_get = mcp_sub.add_parser("get", help="show one server, with its tools")
    mcp_get.add_argument("name")
    mcp_get.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    mcp_add = mcp_sub.add_parser("add", help="write a server into .mcp.json and start it")
    mcp_add.add_argument("name")
    mcp_add.add_argument(
        "rest", nargs="*", help="the command and its arguments, after a bare --"
    )
    _mcp_scope(mcp_add)
    _mcp_entry(mcp_add)
    mcp_add.add_argument("--preset", default=None, help="catalog id to start from")
    mcp_add.add_argument("--permission", default=None, help="permission tag for the server's tools")
    mcp_add.add_argument("--force", action="store_true", help="replace an entry, accept warnings")
    mcp_add.add_argument(
        "--no-test", dest="no_test", action="store_true", help="save without probing first"
    )
    mcp_add.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    mcp_add_json = mcp_sub.add_parser("add-json", help="write a raw .mcp.json entry")
    mcp_add_json.add_argument("name")
    mcp_add_json.add_argument("entry", help="the entry as a JSON object")
    _mcp_scope(mcp_add_json)
    mcp_add_json.add_argument("--force", action="store_true", help="replace an existing entry")
    mcp_add_json.add_argument(
        "--no-test", dest="no_test", action="store_true", help="save without probing first"
    )
    mcp_add_json.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    mcp_remove = mcp_sub.add_parser("remove", help="delete an entry and stop the server")
    mcp_remove.add_argument("name")
    _mcp_scope(mcp_remove)
    mcp_remove.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    mcp_test = mcp_sub.add_parser("test", help="probe a saved server or an unsaved draft")
    mcp_test.add_argument(
        "name",
        nargs="?",
        default="",
        help="a saved server; omit it to probe a draft given after -- or with --command/--url",
    )
    _mcp_entry(mcp_test)
    mcp_test.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    mcp_configure = mcp_sub.add_parser("configure", help="choose which tools a server registers")
    mcp_configure.add_argument("name")
    mcp_configure.add_argument("tools", nargs="*", help="tool names to keep; none means all")
    mcp_configure.add_argument(
        "--tools",
        dest="tools_csv",
        default=None,
        metavar="a,b",
        help="comma-separated tool names to keep",
    )
    _mcp_scope(mcp_configure)
    mcp_configure.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    for verb, help_text in (
        ("enable", "start a disabled server again"),
        ("disable", "keep the entry but never start it"),
    ):
        action_parser = mcp_sub.add_parser(verb, help=help_text)
        action_parser.add_argument("name")
        _mcp_scope(action_parser)
        action_parser.add_argument(
            "--json", dest="sub_json", action="store_true", help="emit JSON"
        )

    mcp_reload = mcp_sub.add_parser("reload", help="restart one server, or all of them")
    mcp_reload.add_argument("name", nargs="?", default="")
    mcp_reload.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    mcp_catalog = mcp_sub.add_parser("catalog", help="list the curated MCP server presets")
    mcp_catalog.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    daemon = sub.add_parser("daemon", help="control the core daemon")
    daemon_sub = daemon.add_subparsers(dest="action", metavar="<action>")
    status_parser = daemon_sub.add_parser("status", help="show the running daemon")
    status_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
    daemon_sub.add_parser("start", help="start the daemon if it is not running")
    daemon_sub.add_parser("stop", help="ask the daemon to shut down")

    gateway = sub.add_parser("gateway", help="attach chat platforms to agents or sessions")
    gateway_sub = gateway.add_subparsers(dest="action", metavar="<action>")
    bind_parser = gateway_sub.add_parser("bind", help="attach a platform account")
    bind_parser.add_argument("platform", help="telegram, discord or slack")
    bind_parser.add_argument("credentials_ref", help="credentials.json key or env var name")
    bind_parser.add_argument(
        "target", help="agent:<name>, session:<id>, new:<workdir> or a JSON object"
    )
    bind_parser.add_argument("--channel", dest="channel_id", help="restrict to one chat id")
    bind_parser.add_argument("--user", dest="user_id", help="platform user who may approve")
    bind_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
    gateway_list_parser = gateway_sub.add_parser("list", help="list live bindings")
    gateway_list_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    unbind_parser = gateway_sub.add_parser("unbind", help="detach a binding")
    unbind_parser.add_argument("binding_id", help="binding id from `gateway list`")
    slack_manifest_parser = gateway_sub.add_parser(
        "slack-manifest", help="print a Slack app manifest with the chat commands"
    )
    slack_manifest_parser.add_argument(
        "--name", dest="bot_name", default="snowpea", help="bot display name"
    )
    slack_manifest_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )

    job = sub.add_parser("job", help="schedule prompts to run unattended")
    job_sub = job.add_subparsers(dest="action", metavar="<action>")
    job_schedule_parser = job_sub.add_parser("schedule", help="register a job")
    job_schedule_parser.add_argument(
        "--at",
        "--in",
        "--every",
        "--cron",
        "--spec",
        dest="spec",
        help='when to run: "0 9 * * *", "60s", "10m", "매일 09:00"',
    )
    job_schedule_parser.add_argument("--task", default="", help="prompt to run on each firing")
    job_schedule_parser.add_argument(
        "--mode", default="accept", choices=["plan", "accept", "auto"], help="mode for the run"
    )
    job_schedule_parser.add_argument("--channel", default=None, help="e.g. telegram:12345 or log")
    job_schedule_parser.add_argument("--workdir", default=None, help="directory the job runs in")
    job_schedule_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    job_list_parser = job_sub.add_parser("list", help="list scheduled jobs")
    job_list_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
    job_cancel_parser = job_sub.add_parser("cancel", help="cancel a job")
    job_cancel_parser.add_argument("job_id", help="job id from `job list`")
    job_run_parser = job_sub.add_parser("run", help="fire a job now, in the daemon")
    job_run_parser.add_argument("job_id", help="job id from `job list`")

    memory_parser = sub.add_parser("memory", help="inspect and prune long-term memory")
    memory_sub = memory_parser.add_subparsers(dest="action", metavar="<action>")
    for name, help_text in (
        ("list", "list stored memories, newest first"),
        ("search", "search stored memories"),
    ):
        memory_action = memory_sub.add_parser(name, help=help_text)
        if name == "search":
            memory_action.add_argument("query", nargs="?", default="", help="what to look for")
        memory_action.add_argument(
            "--scope",
            default="all",
            choices=["project", "global", "agent", "all"],
            help="which scope to read (default: all)",
        )
        memory_action.add_argument(
            "--project",
            dest="memory_project",
            action="store_const",
            const="project",
            help="shorthand for --scope project",
        )
        memory_action.add_argument(
            "--global",
            dest="memory_global",
            action="store_const",
            const="global",
            help="shorthand for --scope global",
        )
        memory_action.add_argument("--limit", type=int, default=100, help="maximum rows")
        memory_action.add_argument(
            "--json", dest="sub_json", action="store_true", help="emit JSON"
        )
    memory_forget_parser = memory_sub.add_parser("forget", help="delete one memory by id")
    memory_forget_parser.add_argument("memory_id", help="memory id from `memory list`")

    service = sub.add_parser("service", help="run the daemon as a login service")
    service_sub = service.add_subparsers(dest="action", metavar="<action>")
    service_sub.add_parser("install", help="register the daemon with systemd/launchd/schtasks")
    service_sub.add_parser("uninstall", help="deregister it again")
    service_sub.add_parser("status", help="is it registered, is it running")

    update_parser = sub.add_parser("update", help="check for a newer snowpea and install it")
    update_parser.add_argument(
        "--check", dest="check_only", action="store_true", help="report only, install nothing"
    )
    update_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    agents_parser = sub.add_parser("agents", help="list agent definitions and running subagents")
    agents_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    init_parser = sub.add_parser(
        "init", help="write a fast, rough AGENTS.md for the project in --cwd (or the cwd)"
    )
    init_parser.add_argument(
        "--force", action="store_true", help="rewrite AGENTS.md from scratch instead of merging"
    )

    audio = sub.add_parser("audio", help="install and inspect the local voice engines")
    audio_sub = audio.add_subparsers(dest="action", metavar="<action>")
    audio_install_parser = audio_sub.add_parser(
        "install", help="install a local voice engine on the daemon's machine"
    )
    audio_install_parser.add_argument(
        "engine",
        help="faster-whisper (or local-whisper), piper, edge-tts; a system package prints a hint",
    )
    audio_install_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )

    workers = sub.add_parser(
        "workers", help="inspect a run of N identical workers in git worktrees"
    )
    workers_sub = workers.add_subparsers(dest="action", metavar="<action>")
    workers_status = workers_sub.add_parser("status", help="show the worker task board")
    workers_status.add_argument(
        "team_id", nargs="?", default=None, help="run id; defaults to the most recent one"
    )
    workers_status.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    team = sub.add_parser("team", help="inspect a parallel worktree team run")
    team_sub = team.add_subparsers(dest="action", metavar="<action>")
    team_status_parser = team_sub.add_parser("status", help="show a team's task board")
    team_status_parser.add_argument(
        "team_id", nargs="?", default=None, help="team id; defaults to the most recent run"
    )
    team_status_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    team_list_parser = team_sub.add_parser("list", help="list configured agent teams")
    team_list_parser.add_argument(
        "--workdir", default=None, help="project whose teams to read (default: the current one)"
    )
    team_list_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
    team_create_parser = team_sub.add_parser(
        "create", help="define a reusable agent team for this project"
    )
    team_create_parser.add_argument("name", help="team name")
    team_create_parser.add_argument("agents", nargs="+", help="agent names, in delegation order")
    team_create_parser.add_argument(
        "--workdir", default=None, help="project to write it to (default: the current one)"
    )
    team_create_parser.add_argument(
        "--use", action="store_true", help="also make it the active team"
    )
    team_create_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    team_use_parser = team_sub.add_parser("use", help="make a team the active one")
    team_use_parser.add_argument("name", help="team to activate")
    team_use_parser.add_argument("--workdir", default=None, help="project to write it to")
    team_use_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
    team_delete_parser = team_sub.add_parser("delete", help="remove a team definition")
    team_delete_parser.add_argument("name", help="team to delete")
    team_delete_parser.add_argument("--workdir", default=None, help="project to write it to")
    team_delete_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )

    for name in PLACEHOLDER_SUBCOMMANDS:
        placeholder_parser = sub.add_parser(name, help=f"{name} (not yet implemented)")
        placeholder_parser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return sub


def _job_spec(args: argparse.Namespace) -> str | None:
    """The spec as typed. ``--in 60s`` and ``--every 10m`` keep their own wording."""
    raw = getattr(args, "spec", None)
    if not raw:
        return None
    text = str(raw).strip()
    # argparse folds --in/--every/--cron/--at into one dest, so a bare duration
    # like "60s" is read as "in 60s" and "10m" after --every stays an interval
    # only if the user spelled it that way.  Bare durations mean "from now".
    if re.fullmatch(r"\d+\s*[a-z가-힣]+", text) and not text.startswith(("in ", "every ")):
        return f"in {text}"
    return text


async def dispatch(args: argparse.Namespace, home: Path | str | None = None) -> int:
    """Run the parsed subcommand; returns its exit code."""
    subcommand = args.subcommand
    as_json = bool(getattr(args, "sub_json", False) or getattr(args, "json", False))
    action = getattr(args, "action", None)

    if subcommand in PLACEHOLDER_SUBCOMMANDS:
        return placeholder(subcommand)
    if subcommand == "setup":
        return setup_command(args, home)
    if subcommand == "service":
        return service_command(str(action or ""), home)
    if subcommand == "tools":
        if action != "list":
            return _fail("usage: snowpea tools list [--json]", EXIT_USAGE)
        return await tools_list(home, as_json=as_json)
    if subcommand == "search":
        if action != "test":
            return _fail('usage: snowpea search test "<query>" [--json]', EXIT_USAGE)
        return await search_test(str(getattr(args, "query", "") or ""), home, as_json=as_json)
    if subcommand == "commands":
        if action != "list":
            return _fail("usage: snowpea commands list [--json]", EXIT_USAGE)
        return await commands_list(home, as_json=as_json)
    if subcommand == "agents":
        return await agents_list(home, as_json=as_json)
    if subcommand == "session":
        if action == "context":
            return await session_context(
                getattr(args, "session_id", None) or None, home, as_json=as_json
            )
        if action == "compact":
            return await session_compact(
                str(getattr(args, "session_id", "") or ""),
                home,
                instructions=getattr(args, "instructions", None) or None,
                as_json=as_json,
            )
        if action == "list":
            return await session_list(
                home,
                include_closed=bool(getattr(args, "include_closed", False)),
                workdir=getattr(args, "workdir", None),
                as_json=as_json,
            )
        if action == "delete":
            return await session_delete(
                home, session_id=str(getattr(args, "session_id", "") or ""), as_json=as_json
            )
        if action == "clear":
            return await session_delete(
                home,
                workdir=getattr(args, "workdir", None),
                all_dirs=bool(getattr(args, "clear_all", False)),
                as_json=as_json,
            )
        return _fail(
            "usage: snowpea session context [id] [--json] | compact <id> [instructions]"
            " | list [--include-closed] [--workdir DIR] | delete <id> | clear [--all]",
            EXIT_USAGE,
        )
    if subcommand == "model":
        if action == "profiles":
            return await model_profiles(
                home, workdir=getattr(args, "workdir", None), as_json=as_json
            )
        if action == "assign":
            return await model_assign(
                str(getattr(args, "agent", "") or ""),
                getattr(args, "profile", None),
                home,
                project=bool(getattr(args, "project", False)),
                workdir=getattr(args, "workdir", None),
                as_json=as_json,
            )
        if action == "default":
            return await model_default(
                getattr(args, "profile", None),
                home,
                project=bool(getattr(args, "project", False)),
                workdir=getattr(args, "workdir", None),
                as_json=as_json,
            )
        return _fail(
            "usage: snowpea model profiles [--json] | assign <agent> [<profileId>]"
            " | default [profileId]  (both accept --project)",
            EXIT_USAGE,
        )
    if subcommand == "update":
        return await update_cli(
            home, check_only=bool(getattr(args, "check_only", False)), as_json=as_json
        )
    if subcommand == "workers":
        if action in (None, "status"):
            return await team_status(getattr(args, "team_id", None), home, as_json=as_json)
        return _fail("usage: snowpea workers status [runId]", EXIT_USAGE)

    if subcommand == "audio":
        if action == "install":
            return await audio_install(
                str(getattr(args, "engine", "") or ""), home, as_json=as_json
            )
        return _fail("usage: snowpea audio install <engine>", EXIT_USAGE)

    if subcommand == "team":
        if action == "status":
            return await team_status(getattr(args, "team_id", None), home, as_json=as_json)
        if action == "list":
            return await team_list(
                home, workdir=getattr(args, "workdir", None), as_json=as_json
            )
        if action == "create":
            return await team_create(
                str(getattr(args, "name", "") or ""),
                list(getattr(args, "agents", []) or []),
                home,
                workdir=getattr(args, "workdir", None),
                use=bool(getattr(args, "use", False)),
                as_json=as_json,
            )
        if action == "use":
            return await team_use(
                str(getattr(args, "name", "") or ""),
                home,
                workdir=getattr(args, "workdir", None),
                as_json=as_json,
            )
        if action == "delete":
            return await team_delete(
                str(getattr(args, "name", "") or ""),
                home,
                workdir=getattr(args, "workdir", None),
                as_json=as_json,
            )
        return _fail(
            "usage: snowpea team status [teamId] | list | create <name> <agent...>"
            " | use <name> | delete <name>",
            EXIT_USAGE,
        )
    if subcommand == "provider":
        if action == "list":
            return await provider_list(home, as_json=as_json)
        if action == "models":
            return await provider_models(
                getattr(args, "vendor", None) or None, home, as_json=as_json
            )
        if action == "add-local":
            return await provider_add_local(
                str(getattr(args, "name", "") or ""),
                home,
                url=str(getattr(args, "url", "") or ""),
                key=getattr(args, "key", None),
                server_type=getattr(args, "server_type", None),
                model=getattr(args, "model", None),
                label=getattr(args, "label", None),
                vision=getattr(args, "vision", None),
                as_json=as_json,
            )
        if action == "remove":
            return await provider_remove(
                str(getattr(args, "name", "") or ""), home, as_json=as_json
            )
        if action == "login":
            vendor_name = str(getattr(args, "vendor", "") or "")
            headless = {"openai": "device_code", "gemini": "google_adc"}
            return await provider_login(
                vendor_name,
                home,
                token=getattr(args, "token", None),
                method=headless.get(vendor_name) if getattr(args, "device_code", False) else None,
            )
        return _fail(
            "usage: snowpea provider list|models|login <vendor>"
            " | add-local <name> --url <url> | remove <name>",
            EXIT_USAGE,
        )
    if subcommand == "gateway":
        if action == "bind":
            return await gateway_bind(
                str(getattr(args, "platform", "") or ""),
                str(getattr(args, "credentials_ref", "") or ""),
                str(getattr(args, "target", "") or ""),
                home,
                channel_id=getattr(args, "channel_id", None),
                user_id=getattr(args, "user_id", None),
                as_json=as_json,
            )
        if action == "list":
            return await gateway_list(home, as_json=as_json)
        if action == "unbind":
            return await gateway_unbind(str(getattr(args, "binding_id", "") or ""), home)
        if action == "slack-manifest":
            return gateway_slack_manifest(
                bot_name=str(getattr(args, "bot_name", "") or "snowpea"),
                as_json=as_json,
            )
        return _fail(
            "usage: snowpea gateway bind <platform> <credentialsRef> <target>"
            " | list | unbind <bindingId> | slack-manifest",
            EXIT_USAGE,
        )
    if subcommand == "job":
        if action == "schedule":
            return await job_schedule(
                str(getattr(args, "task", "") or ""),
                home,
                spec=_job_spec(args),
                mode=str(getattr(args, "mode", "accept") or "accept"),
                channel=getattr(args, "channel", None),
                workdir=getattr(args, "workdir", None),
                as_json=as_json,
            )
        if action == "list":
            return await job_list(home, as_json=as_json)
        if action == "cancel":
            return await job_cancel(str(getattr(args, "job_id", "") or ""), home)
        if action == "run":
            return await job_run(str(getattr(args, "job_id", "") or ""), home)
        return _fail(
            'usage: snowpea job schedule --in 60s --task "…" | list | cancel <id> | run <id>',
            EXIT_USAGE,
        )
    if subcommand == "memory":
        if action == "forget":
            return await memory_forget(str(getattr(args, "memory_id", "") or ""), home)
        if action in {"list", "search"}:
            scope = (
                getattr(args, "memory_project", None)
                or getattr(args, "memory_global", None)
                or str(getattr(args, "scope", "all") or "all")
            )
            return await memory_list(
                home,
                scope=scope,
                query=str(getattr(args, "query", "") or "") or None,
                workdir=getattr(args, "cwd", None),
                limit=int(getattr(args, "limit", 100) or 100),
                as_json=as_json,
            )
        return _fail(
            "usage: snowpea memory list | search <query> | forget <id>"
            " [--project|--global|--scope S]",
            EXIT_USAGE,
        )
    if subcommand == "skill":
        if action == "publish":
            return await skill_publish_command(
                str(getattr(args, "dir", "") or ""),
                home,
                registry=getattr(args, "registry", None),
                token=getattr(args, "token", None),
                as_json=as_json,
            )
        if action == "rate":
            return await skill_rate_command(
                str(getattr(args, "id", "") or ""),
                str(getattr(args, "stars", "") or ""),
                getattr(args, "comment", None),
                home,
                registry=getattr(args, "registry", None),
                token=getattr(args, "token", None),
                as_json=as_json,
            )
        if action == "sources":
            return await skill_sources_command(
                home, registry=getattr(args, "registry", None), as_json=as_json
            )
        if action == "install":
            argument = getattr(args, "source", None) or ""
        elif action == "search":
            argument = getattr(args, "query", None) or ""
        else:
            argument = getattr(args, "name", None) or ""
        return await skill_command(
            str(action or ""),
            str(argument),
            home,
            as_json=as_json,
            source=getattr(args, "source", None) if action == "search" else None,
        )
    if subcommand == "mcp":
        return await mcp_command(str(action or ""), args, home, as_json=as_json)
    if subcommand == "daemon":
        if action == "status":
            return await daemon_status(home, as_json=as_json)
        if action == "start":
            return await daemon_start(home)
        if action == "stop":
            return await daemon_stop(home)
        return _fail("usage: snowpea daemon status|start|stop", EXIT_USAGE)
    return _fail(f"unknown subcommand: {subcommand}", EXIT_USAGE)


__all__ = [
    "COUNTER_LABELS",
    "JOB_RUN_TIMEOUT_SEC",
    "PLACEHOLDER_SUBCOMMANDS",
    "STOP_TIMEOUT_SEC",
    "UPDATE_TIMEOUT_SEC",
    "add_subparsers",
    "agents_list",
    "commands_list",
    "daemon_start",
    "daemon_status",
    "daemon_stop",
    "dispatch",
    "gateway_bind",
    "gateway_list",
    "gateway_slack_manifest",
    "gateway_unbind",
    "format_job_line",
    "format_update_line",
    "job_cancel",
    "job_list",
    "job_run",
    "job_schedule",
    "mcp_command",
    "mcp_entry_params",
    "format_mcp_row",
    "parse_target",
    "placeholder",
    "provider_add_local",
    "provider_list",
    "service_command",
    "provider_login",
    "provider_models",
    "provider_remove",
    "resolve_install_source",
    "search_test",
    "setup_command",
    "skill_command",
    "skill_publish_command",
    "skill_rate_command",
    "skill_sources_command",
    "team_status",
    "tools_list",
    "update_cli",
]
