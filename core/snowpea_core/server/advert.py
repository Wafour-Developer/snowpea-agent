"""Writing ``$SNOWPEA_HOME/daemon.json``, the daemon's advert."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from snowpea_core.util.atomic import write_atomic


def write_advert(path: Path, payload: dict[str, Any], *, attempts: int = 10) -> None:
    """Replace ``daemon.json`` atomically.

    On Windows the replace fails while another process has the file open for
    reading (Python and PowerShell open without delete sharing), so it is
    tried a few times before giving up.
    """
    text = json.dumps(payload, indent=2) + "\n"
    for attempt in range(attempts):
        try:
            # Security review 8, H8-2: random temp name, owner-only, never via a link.
            write_atomic(path, text)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


__all__ = ["write_advert"]
