"""Shared pytest fixtures.

Every test runs against an isolated ``SNOWPEA_HOME`` so the developer's real
``~/.snowpea`` is never read or written.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import PROVIDER_FIXTURES, env_vars

from snowpea_core.gateway.fake import FakeAdapter


@pytest.fixture(scope="session", autouse=True)
def snowpea_home() -> Iterator[Path]:
    # SNOWPEA_UPDATE_CHECK=0 keeps every daemon the suite starts off the
    # network; tests/test_update.py drives the check explicitly instead.
    with tempfile.TemporaryDirectory(prefix="snowpea-home-") as tmp:
        home = Path(tmp)
        with env_vars(SNOWPEA_HOME=str(home), SNOWPEA_UPDATE_CHECK="0"):
            yield home


@pytest_asyncio.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    """A client session for the tests that talk to a daemon over ``/ws``."""
    async with aiohttp.ClientSession() as session:
        yield session


@pytest_asyncio.fixture
async def gateway_env() -> AsyncIterator[None]:
    """The scripted gateway provider plus the in-process fake chat adapter."""
    with env_vars(
        SNOWPEA_PROVIDER=f"fake:{PROVIDER_FIXTURES / 'gateway.json'}",
        SNOWPEA_GATEWAY_FAKE="1",
    ):
        FakeAdapter.instances.clear()
        try:
            yield None
        finally:
            FakeAdapter.instances.clear()


def pytest_addoption(parser: pytest.Parser) -> None:
    """``--update-golden`` rewrites the prompt snapshots instead of failing."""
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Rewrite tests/golden/** from the current output instead of comparing.",
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))
