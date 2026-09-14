"""``/mcp`` — manage MCP servers from inside a session (M14 contract §4).

The command is a thin text surface over the ``mcp.*`` handlers: it parses the
argument string, calls the same functions the RPC dispatcher calls, and prints
the result through ``ctx.say``.  Nothing here reimplements validation, file
writing or process control, so ``/mcp add`` and ``snowpea mcp add`` cannot
drift apart.

Secrets are never echoed: ``--env K=V`` values go into the file and come back
as ``K=•••``.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any

from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.server.errors import RpcError
from snowpea_core.server.mcp_handlers import (
    mcp_add_handler,
    mcp_catalog_handler,
    mcp_list_handler,
    mcp_reload_handler,
    mcp_remove_handler,
    mcp_test_handler,
    mcp_update_handler,
)
from snowpea_core.server.protocol import (
    Empty,
    McpAddParams,
    McpEntryFields,
    McpListParams,
    McpReloadParams,
    McpRemoveParams,
    McpServerInfo,
    McpTestParams,
    McpToolInfo,
    McpUpdateParams,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.session import Session

#: Masked stand-in for anything the API refuses to echo.
MASK = "•••"

ACTIONS = (
    "list",
    "get",
    "add",
    "add-json",
    "remove",
    "test",
    "configure",
    "enable",
    "disable",
    "reload",
    "catalog",
)

USAGE = (
    "Usage: /mcp [list] | /mcp get <name> | "
    "/mcp add <name> [--global] [--env K=V] [--header K=V] [--timeout S] "
    "[--preset <id>] [--permission <tag>] [--force] [--no-test] "
    "(--url <https://…> | -- <command> [args…]) | "
    "/mcp add-json <name> '<json>' | /mcp remove <name> [--global] | /mcp test <name> | "
    "/mcp configure <name> [tool…] | /mcp enable|disable <name> | /mcp reload [name] | /mcp catalog"
)

MCP_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(ACTIONS),
            "description": (
                "'list' shows every configured server; 'add'/'remove'/'update' edit .mcp.json; "
                "'test' probes one; 'enable'/'disable' flip an entry; 'reload' restarts; "
                "'catalog' lists the curated presets."
            ),
        },
        "name": {"type": "string", "description": "Server name the action applies to."},
    },
}


class UsageError(ValueError):
    """The argument string does not parse; the message is shown verbatim."""


def _pairs(values: list[str]) -> dict[str, str]:
    """``["A=1", "B=2"]`` -> ``{"A": "1", "B": "2"}``."""
    out: dict[str, str] = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise UsageError(f"'{item}' is not K=V")
        out[key.strip()] = value
    return out


def parse_add(args: str) -> dict[str, Any]:
    """``add`` / ``test`` arguments -> the keys :class:`McpAddParams` takes.

    Everything after a bare ``--`` is the command and its argv, the way
    ``claude mcp add`` and ``codex mcp add`` spell it; ``--command``/``--args``
    is accepted too.  A shell string is never built.
    """
    tokens = shlex.split(args)
    parsed: dict[str, Any] = {
        "scope": "project",
        "env": {},
        "headers": {},
        "force": False,
        "test": True,
    }
    env: list[str] = []
    headers: list[str] = []
    positional: list[str] = []
    argv: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "--":
            argv = tokens[index:]
            break
        if token in ("--global", "-g"):
            parsed["scope"] = "global"
        elif token == "--project":
            parsed["scope"] = "project"
        elif token == "--force":
            parsed["force"] = True
        elif token == "--no-test":
            parsed["test"] = False
        elif token in ("--env", "-e", "--header", "-H", "--url", "--command", "--timeout",
                       "--tool-timeout", "--preset", "--permission", "--cwd"):
            if index >= len(tokens):
                raise UsageError(f"{token} needs a value")
            value = tokens[index]
            index += 1
            if token in ("--env", "-e"):
                env.append(value)
            elif token in ("--header", "-H"):
                headers.append(value)
            elif token == "--url":
                parsed["url"] = value
            elif token == "--command":
                parsed["command"] = value
            elif token == "--cwd":
                parsed["cwd"] = value
            elif token == "--preset":
                parsed["preset"] = value
            elif token == "--permission":
                parsed["permission"] = value
            elif token == "--timeout":
                parsed["timeoutSec"] = _number(token, value)
            else:
                parsed["toolTimeoutSec"] = _number(token, value)
        elif token == "--args":
            argv = tokens[index:]
            index = len(tokens)
        elif token.startswith("-"):
            raise UsageError(f"unknown option {token}")
        else:
            positional.append(token)

    if not positional:
        raise UsageError("a server name is required")
    parsed["name"] = positional[0]
    rest = positional[1:]
    if rest and not argv and "command" not in parsed and "url" not in parsed:
        # ``/mcp add echo python server.py`` — the tail is the command line.
        parsed["command"] = rest[0]
        argv = rest[1:]
    elif rest:
        raise UsageError(f"unexpected argument '{rest[0]}'")
    if argv:
        if "command" not in parsed:
            parsed["command"] = argv[0]
            argv = argv[1:]
        parsed["args"] = list(argv)
    parsed["env"] = _pairs(env)
    parsed["headers"] = _pairs(headers)
    if not parsed["env"]:
        parsed.pop("env")
    if not parsed["headers"]:
        parsed.pop("headers")
    return parsed


def _number(flag: str, value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise UsageError(f"{flag} takes a number of seconds") from exc


def render_table(servers: list[McpServerInfo]) -> str:
    """The ``/mcp list`` table: name, scope, transport, state, tools."""
    if not servers:
        return "No MCP servers are configured. Add one with /mcp add <name> -- <command> [args…]."
    rows = [
        (
            server.name,
            server.scope,
            server.transport,
            "disabled" if server.disabled else server.state,
            str(server.toolCount),
        )
        for server in servers
    ]
    header = ("name", "scope", "transport", "state", "tools")
    widths = [max(len(row[column]) for row in [header, *rows]) for column in range(5)]
    lines = []
    for row in [header, *rows]:
        cells = (cell.ljust(widths[column]) for column, cell in enumerate(row))
        lines.append("  ".join(cells).rstrip())
    errors = [f"  {server.name}: {server.error}" for server in servers if server.error]
    if errors:
        lines.append("")
        lines.extend(errors)
    return "\n".join(lines)


def render_detail(server: McpServerInfo) -> str:
    """``/mcp get`` — everything about one server, with the values masked."""
    lines = [f"{server.name} ({server.scope}, {server.transport})"]
    if server.plugin:
        lines.append(f"  from plugin: {server.plugin}")
    if server.command:
        lines.append(f"  command: {' '.join([server.command, *server.args])}")
    if server.url:
        lines.append(f"  url: {server.url}")
    if server.cwd:
        lines.append(f"  cwd: {server.cwd}")
    for label, keys in (("env", server.envKeys), ("headers", server.headerKeys)):
        if keys:
            lines.append(f"  {label}: " + ", ".join(f"{key}={MASK}" for key in keys))
    lines.append(f"  permission: {server.permission}")
    lines.append(f"  state: {'disabled' if server.disabled else server.state}")
    if server.error:
        lines.append(f"  error: {server.error}")
    if server.toolsInclude:
        lines.append(f"  tools included: {', '.join(server.toolsInclude)}")
    if server.toolsExclude:
        lines.append(f"  tools excluded: {', '.join(server.toolsExclude)}")
    if server.tools:
        lines.append(f"  tools ({len(server.tools)}):")
        lines.extend(f"    {tool.name} — {tool.description}".rstrip(" —") for tool in server.tools)
    return "\n".join(lines)


def _scope_of(args: str) -> tuple[str, list[str]]:
    """Pull ``--global``/``--project`` out of a short argument list."""
    tokens = shlex.split(args)
    scope = "project"
    rest: list[str] = []
    for token in tokens:
        if token in ("--global", "-g"):
            scope = "global"
        elif token == "--project":
            scope = "project"
        else:
            rest.append(token)
    return scope, rest


def _configure_args(rest: str) -> tuple[str, str, list[str]]:
    """``<name> [--tools a,b] [tool…]`` -> ``(scope, name, tools)``."""
    tokens = shlex.split(rest)
    scope = "project"
    names: list[str] = []
    tools: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token in ("--global", "-g"):
            scope = "global"
        elif token == "--project":
            scope = "project"
        elif token == "--tools":
            if index >= len(tokens):
                raise UsageError("--tools needs a comma-separated list")
            tools.extend(item.strip() for item in tokens[index].split(",") if item.strip())
            index += 1
        elif token.startswith("-"):
            raise UsageError(f"unknown option {token}")
        else:
            names.append(token)
    # Bare tool names after the server name mean the same as --tools.
    tools.extend(names[1:])
    return scope, (names[0] if names else ""), tools


async def _list(ctx: CommandContext, session: Session) -> list[McpServerInfo]:
    result = await mcp_list_handler(None, McpListParams(workdir=str(session.workdir)), ctx.core)
    return result.servers


async def cmd_mcp(ctx: CommandContext, args: str) -> None:
    """``/mcp`` — list, add, remove, test, configure and reload MCP servers."""
    session = ctx.session
    action, _, rest = args.strip().partition(" ")
    action = (action or "list").lower()
    rest = rest.strip()
    if action not in ACTIONS:
        await ctx.say(f"/mcp does not know '{action}'.\n{USAGE}")
        return
    try:
        await _run(ctx, session, action, rest)
    except UsageError as exc:
        await ctx.say(f"{exc}\n{USAGE}")
    except RpcError as exc:
        await ctx.say(f"{exc.code}: {exc.message}")


async def _run(ctx: CommandContext, session: Session, action: str, rest: str) -> None:
    core = ctx.core
    workdir = str(session.workdir)

    if action == "list":
        await ctx.say(render_table(await _list(ctx, session)))
        return

    if action == "catalog":
        lines = ["Curated MCP servers (add one with /mcp add <name> --preset <id>):"]
        catalog = await mcp_catalog_handler(None, Empty(), core)
        width = max(len(entry.id) for entry in catalog.entries)
        for entry in catalog.entries:
            needs = f" (needs {', '.join(entry.needs)})" if entry.needs else ""
            lines.append(f"  {entry.id.ljust(width)}  {entry.description}{needs}")
        await ctx.say("\n".join(lines))
        return

    if action == "reload":
        name = shlex.split(rest)[0] if rest else None
        reloaded = await mcp_reload_handler(
            None, McpReloadParams(name=name, workdir=workdir), core
        )
        await ctx.say(
            f"Reloaded {', '.join(reloaded.servers)}."
            if reloaded.servers
            else "No servers to reload."
        )
        return

    if action == "get":
        name = _one_name(rest)
        for server in await _list(ctx, session):
            if server.name == name:
                await ctx.say(render_detail(server))
                return
        await ctx.say(f"'{name}' is not a configured MCP server.")
        return

    if action == "test":
        name = _one_name(rest)
        probed = await mcp_test_handler(None, McpTestParams(name=name, workdir=workdir), core)
        if not probed.ok:
            await ctx.say(f"{name}: {probed.error} ({probed.elapsedMs} ms)")
            return
        listing = "\n".join(
            f"  {tool.name} — {tool.description}".rstrip(" —") for tool in probed.tools
        )
        await ctx.say(
            f"{name}: ready, {len(probed.tools)} tools in {probed.elapsedMs} ms\n{listing}".rstrip()
        )
        return

    if action == "remove":
        scope, names = _scope_of(rest)
        if not names:
            raise UsageError("a server name is required")
        await mcp_remove_handler(
            None,
            McpRemoveParams(name=names[0], scope=scope, workdir=workdir),  # type: ignore[arg-type]
            core,
        )
        await ctx.say(f"Removed '{names[0]}' from the {scope} .mcp.json.")
        return

    if action in ("enable", "disable"):
        scope, names = _scope_of(rest)
        if not names:
            raise UsageError("a server name is required")
        await mcp_update_handler(
            None,
            McpUpdateParams(
                name=names[0],
                scope=scope,  # type: ignore[arg-type]
                workdir=workdir,
                patch=McpEntryFields(disabled=action == "disable"),
            ),
            core,
        )
        await ctx.say(f"{action.capitalize()}d '{names[0]}'.")
        return

    if action == "configure":
        scope, target, keep = _configure_args(rest)
        if not target:
            raise UsageError("a server name is required")
        await mcp_update_handler(
            None,
            McpUpdateParams(
                name=target,
                scope=scope,  # type: ignore[arg-type]
                workdir=workdir,
                patch=McpEntryFields(toolsInclude=keep),
            ),
            core,
        )
        kept = ", ".join(keep) if keep else "every tool"
        await ctx.say(f"'{target}' now registers {kept}.")
        return

    if action == "add-json":
        import json

        tokens = shlex.split(rest)
        if len(tokens) < 2:
            raise UsageError("/mcp add-json <name> '<json>'")
        try:
            entry = json.loads(tokens[1])
        except json.JSONDecodeError as exc:
            raise UsageError(f"the entry is not valid JSON: {exc}") from exc
        if not isinstance(entry, dict):
            raise UsageError("the entry must be a JSON object")
        await _add(ctx, {"name": tokens[0], "workdir": workdir, **_entry_kwargs(entry)})
        return

    if action == "add":
        if not rest:
            await ctx.say(USAGE)
            return
        await _add(ctx, {**parse_add(rest), "workdir": workdir})
        return


def _entry_kwargs(entry: dict[str, Any]) -> dict[str, Any]:
    """A raw ``.mcp.json`` entry mapped onto :class:`McpAddParams` keys."""
    raw_tools = entry.get("tools")
    tools: dict[str, Any] = raw_tools if isinstance(raw_tools, dict) else {}
    out: dict[str, Any] = {
        key: entry[key]
        for key in ("type", "command", "args", "env", "url", "headers", "cwd", "disabled")
        if key in entry
    }
    for source, target in (("timeoutSec", "timeoutSec"), ("toolTimeoutSec", "toolTimeoutSec")):
        if source in entry:
            out[target] = entry[source]
    if tools.get("include"):
        out["toolsInclude"] = list(tools["include"])
    if tools.get("exclude"):
        out["toolsExclude"] = list(tools["exclude"])
    return out


async def _add(ctx: CommandContext, kwargs: dict[str, Any]) -> None:
    if ctx.session.mode == "plan":
        await ctx.say(
            f"Plan mode: would add MCP server '{kwargs.get('name')}'; nothing is written."
        )
        return
    result = await mcp_add_handler(None, McpAddParams(**kwargs), ctx.core)
    lines = [connected_line(result.tools, result.path)]
    if result.warnings:
        lines.append("Warnings: " + "; ".join(result.warnings))
    if result.error:
        lines.append(f"The server did not start: {result.error}")
    await ctx.say("\n".join(lines))


def connected_line(tools: list[McpToolInfo], path: str) -> str:
    """``Connected — N tools: a, b, c … saved to <path>`` (M14 §4)."""
    names = ", ".join(tool.name for tool in tools)
    listing = f": {names}" if names else ""
    return f"Connected — {len(tools)} tools{listing} … saved to {path}"


def _one_name(rest: str) -> str:
    tokens = shlex.split(rest)
    if not tokens:
        raise UsageError("a server name is required")
    return tokens[0]


COMMANDS: tuple[Command, ...] = (
    Command(
        name="mcp",
        summary="List, add, test and remove MCP servers: /mcp, /mcp add <name> -- <command>.",
        run=cmd_mcp,
        args_schema=MCP_ARGS_SCHEMA,
    ),
)


__all__ = [
    "ACTIONS",
    "COMMANDS",
    "MASK",
    "MCP_ARGS_SCHEMA",
    "USAGE",
    "UsageError",
    "cmd_mcp",
    "connected_line",
    "parse_add",
    "render_detail",
    "render_table",
]
