"""``snowpea machine list|add|remove|connect|disconnect`` (M40)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from snowpea_core.cli.daemon_client import DaemonClient, DaemonError, RpcCallError, ensure_daemon

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_DAEMON = 3


def add_parser(sub: argparse._SubParsersAction[Any]) -> None:
    machine = sub.add_parser("machine", help="remote snowpea machines over ssh (beta)")
    actions = machine.add_subparsers(dest="action", metavar="<action>")
    actions.add_parser("list", help="machines registered on this computer")
    add = actions.add_parser("add", help="register user@host[:port]")
    add.add_argument("target")
    add.add_argument("--label")
    add.add_argument("--key", dest="key_path", metavar="PATH")
    add.add_argument("--json", action="store_true")
    for name, help_text in (
        ("remove", "forget a machine"),
        ("connect", "open ssh and a tunnel"),
        ("disconnect", "close the tunnel"),
    ):
        p = actions.add_parser(name, help=help_text)
        p.add_argument("id")
        p.add_argument("--json", action="store_true")


async def _client(home: Path | None) -> DaemonClient:
    info = await ensure_daemon(home)
    client = DaemonClient(info)
    await client.connect()
    return client


async def run(args: argparse.Namespace, home: Path | None) -> int:
    action = getattr(args, "action", None)
    if not action:
        print("snowpea: usage: snowpea machine list|add|remove|connect|disconnect", file=sys.stderr)
        return EXIT_USAGE
    try:
        client = await _client(home)
    except DaemonError as exc:
        print(f"snowpea: {exc}", file=sys.stderr)
        return EXIT_NO_DAEMON
    try:
        if action == "list":
            result = await client.call("machine.list", {})
        elif action == "add":
            params = {"target": args.target}
            if args.label:
                params["label"] = args.label
            if args.key_path:
                params["keyPath"] = args.key_path
            result = await client.call("machine.add", params)
        else:
            method = f"machine.{action}"
            result = await client.call(method, {"id": args.id})
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif action == "list":
            for row in result.get("machines", []):
                print(f"{row['id'][:8]}…  {row.get('label')}  [{row.get('status')}]")
        else:
            print(json.dumps(result, ensure_ascii=False))
        return EXIT_OK
    except RpcCallError as exc:
        print(f"snowpea: {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        await client.close()