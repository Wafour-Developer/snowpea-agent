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

from snowpea_core.external import proc as external_proc
from snowpea_core.gateway.fake import FakeAdapter

# The fake CLIs are steered by FAKE_* variables; an external CLI child gets
# only allow-listed variables (security review L3), so the suite adds its own.
if "FAKE_" not in external_proc.EXTRA_ALLOW_PREFIXES:
    external_proc.EXTRA_ALLOW_PREFIXES.append("FAKE_")


@pytest.fixture(autouse=True)
def _projects_trusted_for_external_clis(request: pytest.FixtureRequest) -> Iterator[None]:
    """External CLI tests run in projects the person already trusted (security review M4).

    Before the review any registered project was enough; the suites written
    then exercise sessions, not the trust step.  Tests marked ``real_trust``
    (tests/test_security_review2_*.py) see the real rule.  Patched by hand,
    not through ``monkeypatch``: an autouse fixture that requested it would
    make every test's own monkeypatching outlive its other fixtures.
    """
    if request.node.get_closest_marker("real_trust") is not None:
        yield
        return
    from snowpea_core.external.trust import ProjectTrust

    original = ProjectTrust.trusted
    ProjectTrust.trusted = lambda self, root: True  # type: ignore[method-assign]
    try:
        yield
    finally:
        ProjectTrust.trusted = original  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _tests_may_set_modes_directly(request: pytest.FixtureRequest) -> Iterator[None]:
    """A test that builds a session in a mode calls the manager itself (security review 4).

    In production every caller of ``SessionManager.create(mode=…)`` and
    ``set_mode`` names who it is (``session/mode_guard.py``); one that names no
    one may only keep or lower the mode.  The suites written before that
    review set up sessions in auto from the test body, as the test harness;
    that harness is named here.  Tests marked ``real_mode_guard`` see the real
    rule.  Patched by hand, as ``_projects_trusted_for_external_clis`` is.
    """
    if request.node.get_closest_marker("real_mode_guard") is not None:
        yield
        return
    from snowpea_core.session.manager import SessionManager
    from snowpea_core.session.mode_guard import ModeCaller

    harness = ModeCaller.internal("test-harness")
    original_create = SessionManager.create
    original_set_mode = SessionManager.set_mode

    async def create(self, *args, caller=None, **kwargs):  # type: ignore[no-untyped-def]
        return await original_create(self, *args, caller=caller or harness, **kwargs)

    async def set_mode(self, *args, caller=None, **kwargs):  # type: ignore[no-untyped-def]
        return await original_set_mode(self, *args, caller=caller or harness, **kwargs)

    SessionManager.create = create  # type: ignore[method-assign]
    SessionManager.set_mode = set_mode  # type: ignore[method-assign]
    try:
        yield
    finally:
        SessionManager.create = original_create  # type: ignore[method-assign]
        SessionManager.set_mode = original_set_mode  # type: ignore[method-assign]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "real_trust: external CLI project trust is not pre-granted (M4)"
    )
    config.addinivalue_line(
        "markers", "real_mode_guard: in-process mode raises are not pre-granted (review 4)"
    )


@pytest.fixture(autouse=True)
def _no_legacy_settings_carry_over() -> Iterator[None]:
    """Legacy settings values one test loads never raise another test's escalation."""
    from snowpea_core.config.settings import LEGACY_VALUES

    LEGACY_VALUES.clear()
    yield
    LEGACY_VALUES.clear()


@pytest.fixture(scope="session", autouse=True)
def snowpea_home() -> Iterator[Path]:
    # SNOWPEA_UPDATE_CHECK=0 keeps every daemon the suite starts off the
    # network; tests/test_update.py drives the check explicitly instead.
    # SNOWPEA_MODELS_DEV=0 does the same for the public models.dev catalog the
    # curated model rung merges in: tests/test_model_resolution.py drives that
    # path with an httpx.MockTransport instead of reaching the internet.
    with tempfile.TemporaryDirectory(prefix="snowpea-home-") as tmp:
        home = Path(tmp)
        # CLAUDE_CONFIG_DIR keeps the developer's real ~/.claude (its plugins,
        # skills, agents) out of every test; a test that wants a Claude tree
        # creates one under its own home, which the loader prefers.
        # CODEX_HOME does the same for ~/.codex.
        with env_vars(
            SNOWPEA_HOME=str(home),
            SNOWPEA_UPDATE_CHECK="0",
            SNOWPEA_MODELS_DEV="0",
            # Never hold a real sleep inhibitor on the machine running the suite.
            SNOWPEA_KEEP_AWAKE="0",
            CLAUDE_CONFIG_DIR=str(home / "claude-config"),
            CODEX_HOME=str(home / "codex-home"),
        ):
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
