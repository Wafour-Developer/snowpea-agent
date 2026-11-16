from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    data = json.load(sys.stdin)
    # In a working area of the home: its root is closed to tools (review 8, H8-2).
    marker = Path(os.environ["SNOWPEA_HOME"]) / "extension-data" / "fixture-hook.marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(data.get("tool_name", "")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
