"""snowpea console entry point.

At M0 this is a placeholder: it answers ``--version`` and exits ``2`` for
everything else. M1 replaces it with the daemon launcher + TUI spawn described
in §2.5 of the consensus plan.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from snowpea_core import __version__

PLACEHOLDER = "snowpea: not yet implemented"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("--version", "-V"):
        print(f"snowpea {__version__}")
        return 0
    print(PLACEHOLDER, file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
