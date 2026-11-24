"""M37: integration.list / install / uninstall handlers and integration.changed.

HOME and every CLI config variable point into ``tmp_path`` (monkeypatched) and
each test asserts that before the first install: the real ~/.claude is never touched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from snowpea_core.server import integration_handlers as handlers
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    CAPABILITIES,
    PENDING_CAPABILITIES,
    Empty,
    IntegrationActionParams,
)
from snowpea_core.terminal.integrations import manager


class Hub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def notify(self, name, payload):
        self.events.append((name, payload))


@pytest.fixture
def core(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CURSOR_CONFIG_DIR",
                "ANTIGRAVITY_CLI_CONFIG_DIR", "SNOWPEA_HOME"):
        monkeypatch.delenv(var, raising=False)
    core = SimpleNamespace(paths=SimpleNamespace(home=tmp_path / "sp"), hub=Hub())
    env = handlers._env(core)
    for cli in manager.CLIS:
        assert str(env.config_dir(cli)).startswith(str(tmp_path)), cli
    return core


async def test_list_install_uninstall(core, tmp_path):
    (tmp_path / "home" / ".claude").mkdir()
    listed = await handlers.integration_list_handler(None, Empty(), core)
    assert [r.cli for r in listed.integrations] == list(manager.CLIS)
    assert {r.state for r in listed.integrations} == {"not-installed"}

    result = await handlers.integration_install_handler(
        None, IntegrationActionParams(cli="all"), core)
    rows = {r.cli: r for r in result.integrations}
    assert rows["claude"].state == "current"
    assert rows["claude"].version == str(manager.INTEGRATION_VERSION)
    assert rows["codex"].state == "not-installed"  # not set up: skipped
    settings = tmp_path / "home" / ".claude" / "settings.json"
    assert str(settings) in result.changedFiles
    assert any(f.path == str(settings) and f.exists for f in rows["claude"].files)
    name, payload = core.hub.events[-1]
    assert name == "integration.changed"
    json.dumps(payload)  # wire-ready
    assert {r["cli"]: r["state"] for r in payload["integrations"]}["claude"] == "current"

    events = len(core.hub.events)
    again = await handlers.integration_install_handler(
        None, IntegrationActionParams(cli="claude"), core)
    assert again.changedFiles == [] and len(core.hub.events) == events

    removed = await handlers.integration_uninstall_handler(
        None, IntegrationActionParams(cli="claude"), core)
    assert str(settings) in removed.changedFiles
    assert {r.cli: r.state for r in removed.integrations}["claude"] == "not-installed"


async def test_install_of_a_missing_cli_is_an_error(core):
    with pytest.raises(RpcError) as info:
        await handlers.integration_install_handler(None, IntegrationActionParams(cli="codex"),
                                                   core)
    assert "not found" in str(info.value)


def test_capability_is_advertised():
    assert "cliIntegrations" in CAPABILITIES
    assert "cliIntegrations" not in PENDING_CAPABILITIES


def test_registration():
    registered = {}

    class Dispatcher:
        def register(self, name, handler):
            registered[name] = handler

    handlers.register_integration_handlers(Dispatcher())
    assert set(registered) == set(handlers.HANDLED_METHODS)
