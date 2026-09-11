"""``python -m snowpea_core`` — placeholder until M1."""

from __future__ import annotations

import sys

from snowpea_core.cli.main import PLACEHOLDER


def main() -> None:
    print(PLACEHOLDER, file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
