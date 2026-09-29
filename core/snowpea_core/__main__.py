"""``python -m snowpea_core`` — run the core daemon."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

from snowpea_core import __version__
from snowpea_core.server.app_server import run_daemon


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snowpea-core", description="Run the snowpea core daemon."
    )
    parser.add_argument(
        "--port", type=int, default=0, help="TCP port on 127.0.0.1 (0 = pick a free one)"
    )
    parser.add_argument("--home", default=None, help="override SNOWPEA_HOME")
    parser.add_argument(
        "--state-dir", dest="home", help="alias of --home (IDE contract: snowpea-core --state-dir)"
    )
    parser.add_argument(
        "--token",
        default=None,
        help="use this pre-issued auth token instead of the one in <home>/token (written there)",
    )
    parser.add_argument("--version", action="version", version=f"snowpea-core {__version__}")
    return parser


def run_hook_file(argv: list[str]) -> int:
    """``snowpea-core --run-hook <file.py> [args…]``: run a plugin hook script.

    A frozen (PyInstaller) core has no ``python3`` of its own on PATH, so hook
    commands written as ``${SNOWPEA_PYTHON} hooks/x.py`` expand to this entry.
    The script runs as ``__main__`` with stdin, stdout and exit status intact.
    """
    import runpy

    if not argv:
        print("snowpea-core: --run-hook needs a script path", file=sys.stderr)
        return 2
    script = argv[0]
    sys.argv = [script, *argv[1:]]
    sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw[:1] == ["--run-hook"]:
        return run_hook_file(raw[1:])
    args = build_parser().parse_args(raw)
    if args.home:
        # Everything that resolves the home lazily — the config-write guard in
        # tools/config_guard, and any command the agent shells out to — reads
        # $SNOWPEA_HOME, so ``--home`` has to reach the environment too.
        os.environ["SNOWPEA_HOME"] = str(args.home)
    from snowpea_core.server.app_server import HOME_LOCKED_EXIT, HomeLocked

    try:
        asyncio.run(run_daemon(port=args.port, home=args.home, token=args.token))
    except HomeLocked as exc:
        print(f"snowpea-core: {exc}", file=sys.stderr)
        return HOME_LOCKED_EXIT
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
