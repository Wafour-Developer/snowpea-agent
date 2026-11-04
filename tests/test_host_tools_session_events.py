"""R10: the host-tools connection hears a session's turn lifecycle without subscribing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.session.manager import EventHub
from snowpea_core.tools import host_tools as host_tools_mod


class _Conn:
    def __init__(self, surface: str) -> None:
        self.surface_id = surface
        self.client_id = surface
        self.client_kind = "browser"
        self.closed = False
        self.seen: list[tuple[str, str]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.seen.append((method, params["kind"]))


class _Sessions:
    def __init__(self, session: Any) -> None:
        self._s = session

    def get(self, _id: str) -> Any:
        return self._s


def _setup(monkeypatch: pytest.MonkeyPatch, host: _Conn):
    tools = host_tools_mod.HostTools()
    tools._hosts[host.surface_id] = host_tools_mod._Host(conn=host, tools={"repl": object()})
    monkeypatch.setattr(host_tools_mod, "HOST_TOOLS", tools)
    session = SimpleNamespace(
        id="s1", host_tools_from=host.surface_id, origin_conn=None,
        next_seq=lambda: 1, emit_lock=__import__("asyncio").Lock(),
    )
    return EventHub(None, _Sessions(session)), session


@pytest.mark.asyncio
async def test_host_connection_hears_turn_lifecycle_only(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _Conn("browser-1")
    hub, _ = _setup(monkeypatch, host)
    await hub.emit("s1", "turn.started", {"turnId": "t1"})
    await hub.emit("s1", "message.delta", {"text": "x"})
    await hub.emit("s1", "turn.done", {"turnId": "t1", "reason": "complete"})
    assert [k for _, k in host.seen] == ["turn.started", "turn.done"]


@pytest.mark.asyncio
async def test_a_subscribed_host_hears_each_event_once(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _Conn("browser-1")
    hub, _ = _setup(monkeypatch, host)
    hub.subscribe(host, "s1")
    await hub.emit("s1", "turn.done", {"turnId": "t1", "reason": "complete"})
    assert [k for _, k in host.seen] == ["turn.done"]
