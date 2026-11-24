"""M37 T0: the protocol 1.73.0 surface (terminals, integrations, tunnel), additive only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors, protocol
from snowpea_core.server.app_server import Core, build_dispatcher
from snowpea_core.server.errors import RpcError
from snowpea_core.server.person_gate import PERSON_METHODS

TERMINAL_METHODS = (
    "terminal.profiles", "terminal.list", "terminal.create", "terminal.attach",
    "terminal.detach", "terminal.write", "terminal.resize", "terminal.ack", "terminal.read",
    "terminal.send", "terminal.keys", "terminal.interrupt", "terminal.wait", "terminal.close",
    "terminal.seen", "terminal.unlock", "terminal.report",
)
INTEGRATION_METHODS = ("integration.list", "integration.install", "integration.uninstall")
TUNNEL_METHODS = (
    "web.tunnel.status", "web.tunnel.start", "web.tunnel.stop", "web.tunnel.setToken",
    "web.tunnel.install", "web.tunnel.unlock",
)
EVENTS = (
    "terminal.data", "terminal.status", "terminal.created", "terminal.exit", "terminal.title",
    "terminal.closed", "integration.changed", "web.tunnel.changed",
)
M37_CAPABILITIES = {
    "terminalSessions", "terminalStatus", "cliIntegrations", "externalPty",
    "tuiSessionAttach", "webTunnel",
}


def _version() -> tuple[int, ...]:
    return tuple(int(part) for part in protocol.PROTOCOL_VERSION.split("."))


def test_version_and_registry() -> None:
    assert _version() >= (1, 73, 0)
    for name in (*TERMINAL_METHODS, *INTEGRATION_METHODS, *TUNNEL_METHODS):
        assert name in protocol.METHODS, name
        assert protocol.METHODS[name].direction == "c2s"
    for name in EVENTS:
        assert name in protocol.EVENTS, name


def test_capabilities_are_pending_not_advertised() -> None:
    assert M37_CAPABILITIES <= set(protocol.PENDING_CAPABILITIES) | set(protocol.CAPABILITIES)
    pending = M37_CAPABILITIES & set(protocol.PENDING_CAPABILITIES)
    assert not (pending & set(protocol.CAPABILITIES))
    # T0 advertises none of them; a package that lands moves its name across.
    for name in pending:
        assert name not in protocol.dump_schema()["capabilities"]


def test_models_round_trip() -> None:
    status = protocol.TerminalStatus(state="blocked", kind="permission", turn=2, stuck=True)
    info = protocol.TerminalInfo(
        terminalId="t1", profile="claude", status=status, alive=True, pid=5,
        external=protocol.TerminalExternalRef(cli="claude", sessionId="abc"),
        options=["bypass"], dangerous=True, labels={"a": "b"},
    )
    again = protocol.TerminalInfo.model_validate(info.model_dump(mode="json"))
    assert again == info
    attach = protocol.TerminalAttachResult(
        repaint="AA==", seq=3, cols=80, rows=24, status=status, title="x", info=info
    )
    assert protocol.TerminalAttachResult.model_validate(attach.model_dump()) == attach
    profile = protocol.TerminalProfile(
        id="codex", label="Codex", installed=True,
        options=[protocol.TerminalOption(id="yolo", label="yolo", args=["--x"], dangerous=True)],
    )
    assert protocol.TerminalProfilesResult(profiles=[profile]).profiles[0].options[0].dangerous
    row = protocol.IntegrationRow(
        cli="codex", label="Codex", state="current",
        files=[protocol.IntegrationFile(path="/a", exists=True)],
    )
    assert protocol.IntegrationListResult(integrations=[row]).integrations[0].state == "current"
    tunnel = protocol.TunnelStatus(provider="ngrok", state="running", url="https://x.example")
    assert protocol.TunnelStatus.model_validate(tunnel.model_dump()) == tunnel


def test_models_reject_unknown_values() -> None:
    with pytest.raises(ValidationError):
        protocol.TerminalStatus(state="sleeping")
    with pytest.raises(ValidationError):
        protocol.TerminalCreateParams(profile="vim")
    with pytest.raises(ValidationError):
        protocol.TerminalCreateParams(profile="shell", surprise=1)
    with pytest.raises(ValidationError):
        protocol.TerminalWaitParams(terminalId="t", timeout=999)


def test_old_clients_keep_structured_external_sessions() -> None:
    ref = {"cli": "claude", "id": "s1"}
    opened = protocol.ExternalOpenParams(ref=ref)
    assert opened.transport is None and opened.options == [] and opened.confirmDangerous is False
    created = protocol.ExternalCreateParams(cli="codex", project="/w")
    assert created.transport == "structured" and created.options == []
    pty_open = protocol.ExternalOpenParams(ref=ref, transport="pty", options=["bypass"])
    assert pty_open.transport == "pty"
    pty_create = protocol.ExternalCreateParams(cli="claude", project="/w", transport="pty")
    assert pty_create.transport == "pty"
    # An old row (tmux, all fields given) still validates and gains no required field.
    tmux = protocol.ExternalTerminal(kind="tmux", pane="%1", session="s", window="0")
    assert tmux.terminalId is None and tmux.status is None
    pty = protocol.ExternalTerminal(
        kind="pty", terminalId="t1", status=protocol.TerminalStatus(state="working")
    )
    assert pty.pane == ""


def test_settings_defaults_and_person_local_rules() -> None:
    settings = Settings()
    assert settings.terminals.max == 16 and settings.terminals.keepShell is True
    assert (settings.terminals.headlessCols, settings.terminals.headlessRows) == (160, 48)
    assert settings.terminals.sounds.blocked is True
    assert settings.ui.sessionView == "chat"
    assert settings.jarvis.external.view == "terminal"
    tunnel = settings.web.remote.tunnel
    assert tunnel.provider == "off" and tunnel.autoStart is True
    assert tunnel.hostname == "" and tunnel.binary == ""
    with pytest.raises(ValidationError):
        Settings.model_validate({"ui": {"sessionView": "split"}})
    from snowpea_core.server import settings_gate

    assert ("terminals",) in settings_gate.PERSON_LOCAL_ONLY
    assert ("web",) in settings_gate.PERSON_LOCAL_ONLY  # web.remote.tunnel
    assert ("ui",) in settings_gate.REMOTE_LOGIN_KEYS


def test_person_only_methods() -> None:
    for name in (*INTEGRATION_METHODS, "web.tunnel.status", "web.tunnel.start",
                 "web.tunnel.stop", "web.tunnel.setToken", "web.tunnel.install"):
        assert PERSON_METHODS[name] is True, name
    # A remote login re-enters its password itself; terminals are not local-only.
    assert "web.tunnel.unlock" not in PERSON_METHODS
    assert not any(name in PERSON_METHODS for name in TERMINAL_METHODS)


@pytest.mark.parametrize("name", [*TERMINAL_METHODS, *INTEGRATION_METHODS, *TUNNEL_METHODS])
def test_stubs_answer_not_implemented_cleanly(name: str, tmp_path: Path) -> None:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    dispatcher = build_dispatcher(core)
    assert dispatcher.has(name)
    handler = dispatcher._handlers[name]  # noqa: SLF001
    import asyncio

    async def call() -> Any:
        return await handler(None, None, core)

    with pytest.raises(RpcError) as caught:
        asyncio.run(call())
    assert caught.value.code == errors.NOT_IMPLEMENTED
    assert "not available yet" in str(caught.value)
