"""``python -m snowpea_core`` — run the core daemon."""

from __future__ import annotations

import argparse
import asyncio
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
    parser.add_argument("--version", action="version", version=f"snowpea-core {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        asyncio.run(run_daemon(port=args.port, home=args.home))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
