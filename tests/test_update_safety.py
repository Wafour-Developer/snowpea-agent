"""An update check must authorize the exact install, never a downgrade fallback."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from snowpea_core.server import update_handlers
from snowpea_core.server.protocol import Empty


@pytest.mark.parametrize(
    "answer",
    [
        {"available": False, "current": "0.1.2", "latest": "0.1.1", "source": "old-tag"},
        {"available": False, "current": "0.1.2", "latest": "0.1.2", "source": "same-version"},
        {"available": True, "error": "offline", "source": "fallback"},
    ],
)
async def test_update_does_not_install_without_a_successful_newer_check(monkeypatch, answer):
    monkeypatch.setattr(update_handlers.update_mod, "check_update", AsyncMock(return_value=answer))
    install = Mock(side_effect=AssertionError("must not construct an install command"))
    monkeypatch.setattr(update_handlers.update_mod, "update_command", install)
    core = SimpleNamespace(paths=SimpleNamespace(update_log="/tmp/update.log"), settings=None)
    result = await update_handlers.update_handler(None, Empty(), core)
    assert result.started is False
    assert result.error
    install.assert_not_called()
