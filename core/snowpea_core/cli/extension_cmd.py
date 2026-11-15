"""``snowpea extension list|info|enable|disable|reload|new`` (M16 Phase 3).

Everything but ``new`` talks to the daemon through the ``extension.*`` RPCs
(started if it is not running); ``new`` only writes files into
``$SNOWPEA_HOME/extensions/<name>/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from snowpea_core.cli.daemon_client import DaemonClient, DaemonError, RpcCallError, ensure_daemon
from snowpea_core.cli.render import EXIT_AGENT_FAILED, EXIT_NO_DAEMON, EXIT_OK, EXIT_USAGE
from snowpea_core.config.paths import resolve_home

USAGE = (
    "usage: snowpea extension list | info <name> | enable <name> | disable <name> | "
    "reload [<name>] | new <name>"
)


def add_arguments(sub: Any) -> None:
    """Attach ``snowpea extension …`` to the top-level subparsers."""
    parser = sub.add_parser(
        "extension", help="list, enable, reload and create extensions (Python, in the daemon)"
    )
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    def json_flag(target: argparse.ArgumentParser) -> None:
        target.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    json_flag(actions.add_parser("list", help="list installed extensions and their state"))
    info = actions.add_parser(
        "info", help="show one extension: state, error, config, schema and what it registered"
    )
    info.add_argument("name")
    json_flag(info)
    for verb, text in (
        ("enable", "enable and load an extension"),
        ("disable", "unload an extension and keep it off"),
    ):
        action = actions.add_parser(verb, help=text)
        action.add_argument("name")
        json_flag(action)
    reload = actions.add_parser(
        "reload", help="load an extension again from disk, or every enabled one"
    )
    reload.add_argument("name", nargs="?", default="")
    json_flag(reload)
    new = actions.add_parser(
        "new", help="create $SNOWPEA_HOME/extensions/<name>/ from the starter template"
    )
    new.add_argument("name")
    json_flag(new)


def _fail(message: str, code: int) -> int:
    print(f"snowpea: {message}", file=sys.stderr)
    return code


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


class _Session:
    """One daemon connection for the few calls a command makes."""

    def __init__(self, home: Path | str | None) -> None:
        self.home = home
        self.client: DaemonClient | None = None

    async def __aenter__(self) -> _Session:
        info = await ensure_daemon(self.home)
        self.client = DaemonClient(info)
        await self.client.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.client is not None:
            await self.client.close()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.client is not None
        return await self.client.call(method, params)


def _row(ext: dict[str, Any], width: int) -> str:
    state = str(ext.get("state", ""))
    version = str(ext.get("version") or "-")
    effects = int(ext.get("effects") or 0)
    line = f"{ext.get('name', ''):<{width}}  {state:<12} {version:<9} {effects:>3} registered"
    if ext.get("error"):
        line += f"  {ext['error']}"
    elif ext.get("description"):
        line += f"  {ext['description']}"
    return line


def _print_list(extensions: list[dict[str, Any]], root: Path) -> None:
    if not extensions:
        print(f"no extensions in {root} (create one with `snowpea extension new <name>`)")
        return
    width = max(len(str(ext.get("name", ""))) for ext in extensions)
    for ext in extensions:
        print(_row(ext, width))


def _print_info(detail: dict[str, Any]) -> None:
    ext = detail["extension"]
    print(f"{ext['name']} {ext.get('version') or ''}".rstrip())
    if ext.get("description"):
        print(f"  {ext['description']}")
    print(f"  folder: {ext.get('path')}")
    print(f"  state: {ext.get('state')}{'' if ext.get('enabled', True) else ' (disabled)'}")
    if ext.get("error"):
        print(f"  error: {ext['error']}")
    print(f"  registrations: {ext.get('effects', 0)}")
    for tool in detail.get("tools") or []:
        print(f"    tool {tool.get('name')} ({tool.get('permissionTag')})")
    for command in detail.get("commands") or []:
        print(f"    command /{command.get('name')}")
    for server in detail.get("mcpServers") or []:
        print(f"    mcp server {server.get('name')} ({server.get('state')})")
    if ext.get("config"):
        print("  config: " + json.dumps(ext["config"], ensure_ascii=False))
    if ext.get("configSchema"):
        print("  configSchema: " + json.dumps(ext["configSchema"], ensure_ascii=False))


async def _info(conn: _Session, name: str) -> dict[str, Any] | None:
    listing = await conn.call("extension.list", {})
    found = [row for row in listing.get("extensions") or [] if row.get("name") == name]
    if not found:
        return None
    source = f"extension:{name}"
    tools = (await conn.call("tool.list", {})).get("tools") or []
    commands = (await conn.call("command.list", {})).get("commands") or []
    servers = (await conn.call("mcp.list", {"workdir": str(Path.cwd())})).get("servers") or []
    return {
        "extension": found[0],
        "tools": [row for row in tools if row.get("source") == source],
        "commands": [row for row in commands if row.get("source") == source],
        "mcpServers": [row for row in servers if row.get("plugin") == name],
    }


def _new(home: Path | str | None, name: str, *, as_json: bool) -> int:
    from snowpea_core.extensions.manager import scaffold

    try:
        folder = scaffold(resolve_home(home), name)
    except (ValueError, FileExistsError) as exc:
        return _fail(f"snowpea extension new: {exc}", EXIT_USAGE)
    files = sorted(path.name for path in folder.iterdir())
    if as_json:
        _print_json({"name": name, "path": str(folder), "files": files})
        return EXIT_OK
    print(f"created {folder}")
    for file_name in files:
        print(f"  {file_name}")
    print(f"next: edit plugin.py, then `snowpea extension reload {name}`")
    return EXIT_OK


async def extension_command(
    action: str,
    args: argparse.Namespace,
    home: Path | str | None = None,
    *,
    as_json: bool = False,
) -> int:
    """Run one ``snowpea extension`` action; returns the exit code.

    ``reload``, ``enable`` and ``info`` exit 1 when the extension ends up in
    ``error`` or ``incompatible``, so a script notices a broken edit.
    """
    name = str(getattr(args, "name", "") or "")
    if action == "new":
        return _new(home, name, as_json=as_json)
    if action not in ("list", "info", "enable", "disable", "reload"):
        return _fail(USAGE, EXIT_USAGE)
    if action in ("info", "enable", "disable") and not name:
        return _fail(f"usage: snowpea extension {action} <name>", EXIT_USAGE)
    try:
        async with _Session(home) as conn:
            if action == "list":
                listing = await conn.call("extension.list", {})
                rows = list(listing.get("extensions") or [])
                if as_json:
                    _print_json(rows)
                else:
                    _print_list(rows, resolve_home(home) / "extensions")
                return EXIT_OK
            if action == "info":
                detail = await _info(conn, name)
                if detail is None:
                    return _fail(f"no extension called {name}", EXIT_USAGE)
                if as_json:
                    _print_json(detail)
                else:
                    _print_info(detail)
                return _exit_for([detail["extension"]])
            if action in ("enable", "disable"):
                result = await conn.call(f"extension.{action}", {"name": name})
                rows = [result["extension"]]
            elif name:
                rows = [(await conn.call("extension.reload", {"name": name}))["extension"]]
            else:
                listing = await conn.call("extension.list", {})
                rows = [
                    (await conn.call("extension.reload", {"name": row["name"]}))["extension"]
                    for row in listing.get("extensions") or []
                    if row.get("enabled", True)
                ]
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"extension.{action} failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(rows if action == "reload" and not name else rows[0])
    elif not rows:
        print("no enabled extensions to reload")
    else:
        width = max(len(str(row.get("name", ""))) for row in rows)
        for row in rows:
            print(_row(row, width))
    return _exit_for(rows)


def _exit_for(rows: list[dict[str, Any]]) -> int:
    failed = any(row.get("state") in ("error", "incompatible") for row in rows)
    return EXIT_AGENT_FAILED if failed else EXIT_OK


__all__ = ["add_arguments", "extension_command"]
