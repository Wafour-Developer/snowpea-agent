"""Shared pytest fixtures.

Every test runs against an isolated ``SNOWPEA_HOME`` so the developer's real
``~/.snowpea`` is never read or written.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def snowpea_home() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="snowpea-home-") as tmp:
        home = Path(tmp)
        previous = os.environ.get("SNOWPEA_HOME")
        os.environ["SNOWPEA_HOME"] = str(home)
        try:
            yield home
        finally:
            if previous is None:
                os.environ.pop("SNOWPEA_HOME", None)
            else:
                os.environ["SNOWPEA_HOME"] = previous
