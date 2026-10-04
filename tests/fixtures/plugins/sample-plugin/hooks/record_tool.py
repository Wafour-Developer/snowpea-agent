from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    data = json.load(sys.stdin)
    marker = Path(os.environ["SNOWPEA_HOME"]) / "fixture-hook.marker"
    marker.write_text(str(data.get("tool_name", "")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
