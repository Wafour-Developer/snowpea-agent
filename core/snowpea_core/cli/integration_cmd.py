"""``snowpea integration install|uninstall|status [claude|codex|cursor|agy|all]`` (m37 §2.7).

Works on the files directly, with or without a running daemon: the hook script
under ``$SNOWPEA_HOME/hooks`` and each CLI's own hook config.  ``all`` on
install / uninstall skips a CLI whose config folder does not exist (it is not
installed) and reports every other failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def add_arguments(sub: Any) -> None:
    """Register ``snowpea integration …`` on the top-level subparsers."""
    parser = sub.add_parser(
        "integration",
        help="install, remove or check the hooks that report a CLI's state to snowpea terminals",
    )
    actions = parser.add_subparsers(dest="action", metavar="<action>")
    for name, text in (
        ("install", "add snowpea's hook to the CLI's hook config (repeatable, changes no "
                    "bytes when current)"),
        ("uninstall", "remove exactly what install added"),
        ("status", "not-installed / current / outdated / modified for each CLI"),
    ):
        action = actions.add_parser(name, help=text)
        action.add_argument("cli", nargs="?", default="all",
                            choices=["claude", "codex", "cursor", "agy", "all"],
                            help="which CLI (default: all)")
        action.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")


def _row_line(row: Any) -> str:
    line = f"{row.cli:<7} {row.status:<14} {row.configDir}"
    if row.detail:
        line += f"  ({row.detail})"
    if not row.cliInstalled:
        line += "  [CLI not found on PATH]"
    return line


def integration_command(args: argparse.Namespace, home: Path | str | None) -> int:
    from snowpea_core.config.paths import resolve_home
    from snowpea_core.terminal.integrations import manager

    action = getattr(args, "action", None)
    as_json = bool(getattr(args, "sub_json", False))
    if action not in ("install", "uninstall", "status"):
        print("usage: snowpea integration install|uninstall|status "
              "[claude|codex|cursor|agy|all]", file=sys.stderr)
        return EXIT_USAGE
    env = manager.Env(snowpea_home=resolve_home(home))
    target = getattr(args, "cli", None) or "all"
    clis = manager.resolve_targets(target)

    if action == "status":
        rows = [manager.status(cli, env) for cli in clis]
        if as_json:
            print(json.dumps([r.as_dict() for r in rows], indent=2))
        else:
            for row in rows:
                print(_row_line(row))
        return EXIT_OK

    results: list[dict[str, Any]] = []
    failed = False
    verb = manager.install if action == "install" else manager.uninstall
    for cli in clis:
        if (target == "all" and action == "install"
                and not env.config_dir(cli).is_dir()):
            results.append({"cli": cli, "skipped": "config folder not found"})
            if not as_json:
                print(f"{cli:<7} skipped: {manager.LABELS[cli]} is not set up here "
                      f"({env.config_dir(cli)} does not exist)")
            continue
        try:
            result = verb(cli, env)
        except (manager.IntegrationError, OSError) as exc:
            failed = True
            results.append({"cli": cli, "error": str(exc)})
            if not as_json:
                print(f"{cli:<7} failed: {exc}", file=sys.stderr)
            continue
        results.append(result.as_dict())
        if not as_json:
            parts = []
            if result.changed:
                parts.append("changed " + ", ".join(result.changed))
            if result.removed:
                parts.append("removed " + ", ".join(result.removed))
            what = "; ".join(parts) or "nothing to change"
            print(f"{cli:<7} {result.row.status if result.row else ''}: {what}")
            for backup in result.backups:
                print(f"        backup kept at {backup}")
    if as_json:
        print(json.dumps(results, indent=2))
    return EXIT_FAIL if failed else EXIT_OK
