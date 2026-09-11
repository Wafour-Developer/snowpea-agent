"""Session-less ``snowpea`` subcommands (plan §3.6).

``tools list`` and ``commands list`` are pure RPC lookups — no session, no LLM —
so they double as the install smoke test.  ``daemon status|stop|start`` drives
the daemon process itself.  Everything else is an M2+ placeholder that exits 2.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from snowpea_core.cli.daemon_client import (
    DaemonClient,
    DaemonError,
    RpcCallError,
    ensure_daemon,
    pid_alive,
    read_daemon_json,
)
from snowpea_core.cli.render import EXIT_NO_DAEMON, EXIT_OK, EXIT_USAGE
from snowpea_core.config.paths import Paths, resolve_home

#: Subcommands whose implementation lands after M1.
PLACEHOLDER_SUBCOMMANDS: tuple[str, ...] = (
    "setup",
    "skill",
    "service",
    "agents",
    "team",
    "job",
)

#: Seconds to wait for ``daemon stop`` to see the process go away.
STOP_TIMEOUT_SEC = 10.0


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
        print(
            f"{mark} {vendor:<{width}}  {state:<10} {logins:<22} "
            f"{item.get('defaultModel', '')}".rstrip()
        )
    return EXIT_OK


async def provider_login(vendor: str, home: Path | str | None = None) -> int:
    """``snowpea provider login <vendor>`` → ``provider.loginWeb``.

    Only OpenAI (device code) and OpenRouter (OAuth PKCE) have a browser login;
    every other vendor answers ``login_unsupported`` with the API-key command.
    """
    if not vendor:
        return _fail("usage: snowpea provider login <vendor>", EXIT_USAGE)
    try:
        info = await ensure_daemon(home)
        client = DaemonClient(info)
        await client.connect()
        try:
            await client.call(
                "provider.loginWeb", {"vendor": vendor, "method": "web"}, timeout=900.0
            )
        finally:
            await client.close()
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"{vendor} login failed ({exc.code}): {exc.message}", EXIT_USAGE)
    print(f"{vendor}: signed in; credentials saved to settings.json")
    return EXIT_OK


# ---------------------------------------------------------------------------
# daemon.*
# ---------------------------------------------------------------------------


async def daemon_status(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea daemon status`` — ``system.info`` plus lifecycle counters."""
    try:
        info = await ensure_daemon(home)
        client = DaemonClient(info)
        await client.connect()
        try:
            result = await client.call("system.info", {})
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
    if lifecycle:
        remaining = lifecycle.get("secondsUntilExit")
        remaining_text = "-" if remaining is None else f"{float(remaining):.0f}s"
        print(
            f"lifecycle    willExit={lifecycle.get('willExit', False)} "
            f"reason={lifecycle.get('reason', '?')} secondsUntilExit={remaining_text}"
        )
    return EXIT_OK


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
# placeholders
# ---------------------------------------------------------------------------


def placeholder(name: str) -> int:
    """Print the M2+ notice and return the usage exit code."""
    print(f"snowpea {name}: not yet implemented", file=sys.stderr)
    return EXIT_USAGE


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

    provider = sub.add_parser("provider", help="inspect and log into chat providers")
    provider_sub = provider.add_subparsers(dest="action", metavar="<action>")
    provider_list_parser = provider_sub.add_parser("list", help="list known vendors")
    provider_list_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    provider_login_parser = provider_sub.add_parser(
        "login", help="browser login (openai, openrouter)"
    )
    provider_login_parser.add_argument("vendor", help="vendor to log into")

    daemon = sub.add_parser("daemon", help="control the core daemon")
    daemon_sub = daemon.add_subparsers(dest="action", metavar="<action>")
    status_parser = daemon_sub.add_parser("status", help="show the running daemon")
    status_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
    daemon_sub.add_parser("start", help="start the daemon if it is not running")
    daemon_sub.add_parser("stop", help="ask the daemon to shut down")

    for name in PLACEHOLDER_SUBCOMMANDS:
        placeholder_parser = sub.add_parser(name, help=f"{name} (not yet implemented)")
        placeholder_parser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return sub


async def dispatch(args: argparse.Namespace, home: Path | str | None = None) -> int:
    """Run the parsed subcommand; returns its exit code."""
    subcommand = args.subcommand
    as_json = bool(getattr(args, "sub_json", False) or getattr(args, "json", False))
    action = getattr(args, "action", None)

    if subcommand in PLACEHOLDER_SUBCOMMANDS:
        return placeholder(subcommand)
    if subcommand == "tools":
        if action != "list":
            return _fail("usage: snowpea tools list [--json]", EXIT_USAGE)
        return await tools_list(home, as_json=as_json)
    if subcommand == "commands":
        if action != "list":
            return _fail("usage: snowpea commands list [--json]", EXIT_USAGE)
        return await commands_list(home, as_json=as_json)
    if subcommand == "provider":
        if action == "list":
            return await provider_list(home, as_json=as_json)
        if action == "login":
            return await provider_login(str(getattr(args, "vendor", "") or ""), home)
        return _fail("usage: snowpea provider list|login <vendor>", EXIT_USAGE)
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
    "PLACEHOLDER_SUBCOMMANDS",
    "STOP_TIMEOUT_SEC",
    "add_subparsers",
    "commands_list",
    "daemon_start",
    "daemon_status",
    "daemon_stop",
    "dispatch",
    "placeholder",
    "provider_list",
    "provider_login",
    "tools_list",
]
