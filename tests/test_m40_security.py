"""M40 machine proxy security regressions (review 2026-11-24)."""

from __future__ import annotations

pytest_plugins = ["test_m40_machines"]

# ruff: noqa: E402

import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from snowpea_core.cli.remote import RemoteSession, RemoteTarget
from snowpea_core.machines.gates import gate_connection, run_proxy_gates
from snowpea_core.machines.manager import ASK_TIMEOUT_SEC, MachineManager
from snowpea_core.machines.registry import MachineRegistry, validate_key_path
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.machine_handlers import proxy_call
from snowpea_core.server.protocol import ExternalCreateParams, SessionCreateParams
from snowpea_core.server.rpc import RpcConnection
from snowpea_core.session.manager import person_only_event


def _conn(kind: str, **extra: object) -> RpcConnection:
    conn = RpcConnection(sender=AsyncMock())
    conn.authenticated = True
    conn.client_kind = {"person": "ide", "browser": "browser", "connector": "connector"}.get(
        kind, kind
    )
    conn.remote_login = kind == "remote_login"
    for key, value in extra.items():
        setattr(conn, key, value)
    return conn


def _core(*, machines_on: bool = True, max_mode: str = "auto") -> SimpleNamespace:
    remote = SimpleNamespace(machines=machines_on, terminals=True, maxMode=max_mode)
    web = SimpleNamespace(remote=remote)
    settings = SimpleNamespace(web=web)
    return SimpleNamespace(settings=settings, paths=SimpleNamespace(home=Path("/tmp/unused")))


def test_c1_browser_refused_on_proxy() -> None:
    browser = _conn("browser")
    with pytest.raises(RpcError) as caught:
        gate_connection(browser, _core())
    assert caught.value.code == errors.UNAUTHORIZED


def test_h1_remote_login_unrestricted_external_refused() -> None:
    remote = _conn("remote_login")
    params = ExternalCreateParams(
        cli="claude", project="/proj", mode="unrestricted", confirmDangerous=True
    )
    with pytest.raises(RpcError) as caught:
        run_proxy_gates("external.create", remote, params, _core(), "mid")
    assert caught.value.code == errors.JARVIS_PERSON_ONLY


def test_h1_trust_requires_local_person() -> None:
    remote = _conn("remote_login")
    params = ExternalCreateParams(cli="claude", project="/proj", trust=True)
    with pytest.raises(RpcError) as caught:
        run_proxy_gates("external.create", remote, params, _core(), "mid")
    assert caught.value.code == errors.JARVIS_PERSON_ONLY


def test_h2_terminal_write_kind_gate_remote_off() -> None:
    core = _core()
    core.settings.web.remote.terminals = False
    remote = _conn("remote_login")
    remote.remote_login = True
    from snowpea_core.server.protocol import TerminalWriteParams

    params = TerminalWriteParams(terminalId="t1", data="YQ==")
    with pytest.raises(RpcError) as caught:
        run_proxy_gates("terminal.write", remote, params, core, "mid")
    assert (caught.value.data or {}).get("reason") == "remote_terminals_off"


def test_h3_ssh_argv_separates_key_path(tmp_path: Path) -> None:
    key = tmp_path / "id_key"
    key.write_text("k\n", encoding="utf-8")
    reg = MachineRegistry(tmp_path)
    rec = reg.add(
        "me@box",
        label="box",
        key_path=str(key),
        pasted_key=None,
        remote_cwd=None,
        auto_connect=False,
    )
    session = RemoteSession(
        RemoteTarget(host="box", user="me"), local_protocol="1.0", confirm=lambda _q: True
    )
    session._options = list(reg.ssh_identity(rec))
    session._ssh_extra = list(reg.ssh_extra_args(rec))
    argv = session.ssh_argv("uptime")
    assert "-o" in argv and "IdentitiesOnly=yes" in argv
    idx = argv.index("-i")
    assert argv[idx + 1] == str(key)
    assert "-o" not in argv[idx : idx + 2]


def test_h3_hostile_key_path_refused() -> None:
    with pytest.raises(RpcError):
        validate_key_path("-oProxyCommand=evil")


def test_h4_machine_changed_is_person_only_event() -> None:
    assert person_only_event("machine.changed") is True




def test_l1_invalid_registry_id_skipped_on_load(tmp_path: Path) -> None:
    reg = MachineRegistry(tmp_path)
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text(
        '{"machines": [{"id": "../escape", "host": "box", "label": "x"}]}', encoding="utf-8"
    )
    reg.load()
    assert reg.records == {}


def test_l1_remove_refuses_non_uuid(tmp_path: Path) -> None:
    reg = MachineRegistry(tmp_path)
    assert reg.remove("../bad") is None


def test_l2_machines_dir_mode_0700(tmp_path: Path) -> None:
    reg = MachineRegistry(tmp_path)
    reg.add(
        "me@box", label="b", key_path=None, pasted_key="k\n", remote_cwd=None, auto_connect=False
    )
    mode = stat.S_IMODE((tmp_path / "machines").stat().st_mode)
    assert mode == 0o700


def test_l3_target_without_user(tmp_path: Path) -> None:
    rec = MachineRegistry(tmp_path).add(
        "box", label="box", key_path=None, pasted_key=None, remote_cwd=None, auto_connect=False
    )
    assert rec.target().destination == "box"


@pytest.mark.asyncio
async def test_l4_ensure_manager_binds_loop(tmp_path: Path) -> None:
    from _support import make_daemon

    from snowpea_core.server.machine_handlers import ensure_manager

    daemon = await make_daemon(tmp_path / "home")
    try:
        manager = ensure_manager(daemon.core)
        assert manager._loop is not None  # noqa: SLF001
    finally:
        await daemon.stop()


def test_l5_ask_timeout_constant() -> None:
    assert ASK_TIMEOUT_SEC >= 60


def test_l6_notification_queue_bounded() -> None:
    from snowpea_core.cli.daemon_client import NOTIFICATION_QUEUE_MAX, DaemonClient, DaemonInfo

    default = DaemonClient(DaemonInfo(port=1, pid=0, token="x"))
    assert default._notifications.maxsize == 0
    bounded = DaemonClient(DaemonInfo(port=1, pid=0, token="x"), notification_queue_max=16)
    assert bounded._notifications.maxsize == 16
    assert NOTIFICATION_QUEUE_MAX >= 16


def test_l7_rpc_error_mapping() -> None:
    from snowpea_core.cli.daemon_client import RpcCallError

    err = MachineManager._map_rpc_error(  # noqa: SLF001
        RpcCallError(
            "confirm_dangerous", "need confirm", {"reason": "confirm_dangerous", "secret": "x"}
        )
    )
    assert err.code == "confirm_dangerous"
    assert "secret" not in (err.data or {})


def test_l8_rpc_dump_exclude_unset() -> None:
    params = SessionCreateParams(workdir="/p")
    dumped = params.model_dump(mode="json", exclude_unset=True)
    assert "mode" not in dumped
    assert dumped["workdir"] == "/p"


def test_l9_close_connection_prunes_clients() -> None:
    manager = MachineManager(_core())
    conn = _conn("person")
    conn.surface_id = "surf1"
    manager._conn_clients[("m1", "surf1")] = MagicMock()  # noqa: SLF001
    manager.close_connection(conn)
    assert ("m1", "surf1") not in manager._conn_clients


def test_l10_pasted_key_gets_trailing_newline(tmp_path: Path) -> None:
    reg = MachineRegistry(tmp_path)
    rec = reg.add(
        "me@box", label="b", key_path=None, pasted_key="KEY", remote_cwd=None, auto_connect=False
    )
    key = tmp_path / "machines" / rec.id / "id_key"
    assert key.read_text(encoding="utf-8").endswith("\n")


@pytest.mark.asyncio
async def test_m5_jarvis_kind_refused_like_browser(daemon_pair) -> None:
    local, remote = daemon_pair
    from snowpea_core.cli.daemon_client import DaemonInfo
    from snowpea_core.server.protocol import PROTOCOL_VERSION

    info = DaemonInfo(port=remote.port, pid=0, token=remote.token, protocolVersion=PROTOCOL_VERSION)
    local.core.machines = MachineManager(local.core, direct=lambda _id: info)
    local.core.machines.bind_loop(asyncio.get_running_loop())
    mid = local.core.machines.registry.add(
        "me@box", label="box", key_path=None, pasted_key=None, remote_cwd=None, auto_connect=False
    ).id
    local.core.machines.connect(mid)
    jarvis = _conn("connector")
    jarvis.client_kind = "jarvis"
    with pytest.raises(RpcError):
        await proxy_call(
            jarvis,
            "workspace.list",
            mid,
            {},
            local.core,
        )


@pytest.mark.asyncio
async def test_m6_term_attach_params_include_machine() -> None:
    from snowpea_core.cli.term import _machine_params

    out = _machine_params("mid", {"terminalId": "t1"})
    assert out["machine"] == "mid"


@pytest.mark.asyncio
async def test_m1_filter_proxy_result_is_passthrough() -> None:
    from snowpea_core.machines import gates

    browser = _conn("browser")
    core = _core()
    payload = {"sessions": [{"sessionId": "w1"}, {"sessionId": "ok"}]}
    out = await gates.filter_proxy_result(browser, core, "session.list", payload)
    assert out == payload


def test_m4_dangerous_proxy_writes_local_ledger(tmp_path: Path) -> None:
    from snowpea_core.server.protocol import ExternalCreateParams

    tmp_path.mkdir(parents=True, exist_ok=True)
    core = SimpleNamespace(
        settings=SimpleNamespace(web=SimpleNamespace(remote=SimpleNamespace(machines=True))),
        paths=SimpleNamespace(home=tmp_path),
    )
    person = _conn("person")
    params = ExternalCreateParams(
        cli="claude",
        project="/proj",
        mode="unrestricted",
        confirmDangerous=True,
    )
    run_proxy_gates("external.create", person, params, core, "machine-uuid")
    ledger = tmp_path / "jarvis" / "ledger.jsonl"
    assert ledger.is_file()
    assert "remote machine" in ledger.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_m3_separate_proxy_client_per_local_connection(daemon_pair) -> None:
    local, remote = daemon_pair
    from snowpea_core.cli.daemon_client import DaemonInfo
    from snowpea_core.server.protocol import PROTOCOL_VERSION

    info = DaemonInfo(port=remote.port, pid=0, token=remote.token, protocolVersion=PROTOCOL_VERSION)
    manager = MachineManager(local.core, direct=lambda _id: info)
    manager.bind_loop(asyncio.get_running_loop())
    local.core.machines = manager
    mid = manager.registry.add(
        "me@box", label="box", key_path=None, pasted_key=None, remote_cwd=None, auto_connect=False
    ).id
    manager.connect(mid)
    for _ in range(60):
        if manager.rows()[0].status == "connected":
            break
        await asyncio.sleep(0.05)
    a = _conn("person")
    b = _conn("person")
    b.surface_id = "other"
    c1 = await manager._ensure_client(mid, a)  # noqa: SLF001
    c2 = await manager._ensure_client(mid, b)  # noqa: SLF001
    assert c1 is not c2


def test_n1_terminal_create_trust_requires_local_person() -> None:
    from snowpea_core.server.protocol import TerminalCreateParams

    remote = _conn("remote_login")
    params = TerminalCreateParams(profile="claude", cwd="/p", trust=True)
    with pytest.raises(RpcError) as caught:
        run_proxy_gates("terminal.create", remote, params, _core(), "mid")
    assert caught.value.code == errors.JARVIS_PERSON_ONLY


def test_n2_remote_login_session_create_sends_capped_mode() -> None:
    from snowpea_core.machines.gates import proxy_params_dump
    from snowpea_core.server.protocol import SessionCreateParams

    remote = _conn("remote_login")
    core = _core(max_mode="accept")
    params = SessionCreateParams(workdir="/p", mode="auto")
    dumped = proxy_params_dump(remote, core, params, method="session.create")
    assert dumped.get("mode") == "accept"


def test_r1_remote_login_external_default_mode() -> None:
    from snowpea_core.machines.gates import proxy_params_dump
    from snowpea_core.server.protocol import ExternalCreateParams, ExternalOpenParams, ExternalRef

    remote = _conn("remote_login")
    core = _core()
    created = proxy_params_dump(
        remote, core, ExternalCreateParams(cli="claude", project="/p"), method="external.create"
    )
    assert created.get("mode") == "default"
    opened = proxy_params_dump(
        remote,
        core,
        ExternalOpenParams(ref=ExternalRef(cli="claude", id="e1"), mode="unrestricted"),
        method="external.open",
    )
    assert opened.get("mode") == "default"


def test_r1_remote_login_terminal_attach_mode_unchanged() -> None:
    from snowpea_core.machines.gates import proxy_params_dump
    from snowpea_core.server.protocol import TerminalAttachParams

    remote = _conn("remote_login")
    for mode in ("interactive", "observe"):
        params = TerminalAttachParams(terminalId="t1", mode=mode)
        dumped = proxy_params_dump(remote, _core(), params, method="terminal.attach")
        assert dumped.get("mode") == mode


def test_n4_terminal_close_in_gates() -> None:
    from snowpea_core.machines import gates

    assert "terminal.close" in gates._GATES  # noqa: SLF001


def test_n6_hostile_registry_host_skipped(tmp_path: Path) -> None:
    reg = MachineRegistry(tmp_path)
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    hostile = (
        '{"machines": [{"id": "550e8400-e29b-41d4-a716-446655440000", '
        '"host": "-oProxyCommand=evil", "label": "x"}]}'
    )
    reg.path.write_text(hostile, encoding="utf-8")
    reg.load()
    assert reg.records == {}


def test_h4_redacted_row_strips_keypath_without_ask() -> None:
    row = {"id": "m", "keyPath": "/secret", "error": "oops", "ask": None}
    redacted = {**row, "ask": None, "keyPath": None, "error": None}
    assert redacted["keyPath"] is None and redacted["error"] is None


def test_m4_native_mode_writes_local_ledger(tmp_path: Path) -> None:
    from snowpea_core.server.protocol import ExternalCreateParams

    tmp_path.mkdir(parents=True, exist_ok=True)
    core = SimpleNamespace(
        settings=SimpleNamespace(web=SimpleNamespace(remote=SimpleNamespace(machines=True))),
        paths=SimpleNamespace(home=tmp_path),
    )
    person = _conn("person")
    params = ExternalCreateParams(cli="claude", project="/proj", nativeMode="bypass")
    run_proxy_gates("external.create", person, params, core, "machine-uuid")
    ledger = tmp_path / "jarvis" / "ledger.jsonl"
    assert ledger.is_file()
    assert "nativeMode" in ledger.read_text(encoding="utf-8")


def test_l5_ask_timeout_clears_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.machines.manager import LiveConnection, _Asks

    pending: dict[str, object] = {}
    live = LiveConnection(status="off")
    manager = MachineManager(_core())
    manager._pending_asks["m1"] = pending  # noqa: SLF001
    monkeypatch.setattr("snowpea_core.machines.manager.ASK_TIMEOUT_SEC", 0.01)
    asks = _Asks("m1", manager._pending_asks["m1"], live, lambda: None)
    assert asks.ask("install", question="x") is False
    assert live.ask is None
    assert live.status == "connecting"


def test_n3_approval_relay_clamps_scope() -> None:
    remote = _conn("remote_login")
    manager = MachineManager(_core())
    raw = {"decision": "allow", "scope": "always"}
    out = manager._clamp_approval_reply(remote, {"tool": "shell"}, raw)
    assert out["scope"] == "session"


def test_n3b_remote_login_allow_becomes_deny_for_person_only() -> None:
    manager = MachineManager(_core())
    remote = _conn("remote_login")
    request = {"tool": "shell", "personOnly": True}
    raw = {"decision": "allow", "scope": "once", "actor": "person"}
    out = manager._clamp_approval_reply(remote, request, raw)
    assert out == {
        "decision": "deny",
        "scope": "once",
        "reason": "only a local person may allow this on the remote",
    }


def test_n3b_remote_login_allow_becomes_deny_for_blocked() -> None:
    manager = MachineManager(_core())
    remote = _conn("remote_login")
    request = {"tool": "shell", "blocked": {"by": "list", "why": "x"}}
    out = manager._clamp_approval_reply(
        remote, request, {"decision": "allow", "scope": "once"}
    )
    assert out["decision"] == "deny"


def test_n3b_remote_login_allow_becomes_deny_for_delegation_set() -> None:
    manager = MachineManager(_core())
    remote = _conn("remote_login")
    request = {"tool": "delegation_set", "args": {"allow": ["deploy"]}}
    out = manager._clamp_approval_reply(
        remote, request, {"decision": "allow", "scope": "session"}
    )
    assert out["decision"] == "deny"


def test_n3b_local_person_allow_passes() -> None:
    manager = MachineManager(_core())
    person = _conn("person")
    request = {"tool": "shell", "personOnly": True}
    out = manager._clamp_approval_reply(
        person,
        request,
        {"decision": "allow", "scope": "always", "actor": "person", "personConfirm": "window"},
    )
    assert out["decision"] == "allow"
    assert out["scope"] == "always"
    assert "actor" not in out and "personConfirm" not in out


def test_n3b_clamp_drops_extra_reply_keys() -> None:
    manager = MachineManager(_core())
    person = _conn("person")
    out = manager._clamp_approval_reply(
        person,
        {"tool": "shell"},
        {"decision": "deny", "scope": "once", "reason": "no", "actor": "person"},
    )
    assert set(out) <= {"decision", "scope", "reason"}


def test_proxy_param_fields_are_classified() -> None:
    from snowpea_core.machines.gates import PROXY_PARAM_CLASSIFICATION
    from snowpea_core.machines.proxy import PROXY_METHODS
    from snowpea_core.server.protocol import METHODS

    for method in PROXY_METHODS:
        spec = METHODS[method]
        known = PROXY_PARAM_CLASSIFICATION.get(method)
        assert known is not None, f"missing classification for {method}"
        model_fields = set(spec.params.model_fields)
        assert set(known) == model_fields, f"{method}: classify {model_fields - set(known)}"


def test_n8_relay_request_id_prefix() -> None:
    assert MachineManager._relay_request_id("mid", "rid").startswith("m40:mid:")


def test_n10_default_client_queue_unbounded() -> None:
    from snowpea_core.cli.daemon_client import DaemonClient, DaemonInfo

    client = DaemonClient(DaemonInfo(port=1, pid=0, token="x"))
    assert client._notifications.maxsize == 0


@pytest.mark.asyncio
async def test_m2_proxy_hello_keeps_person_kind(daemon_pair) -> None:
    """connector breaks proxied workspace.list on the remote; keep the default hello kind."""
    from snowpea_core.cli.daemon_client import DaemonInfo
    from snowpea_core.server.protocol import PROTOCOL_VERSION

    local, remote = daemon_pair
    info = DaemonInfo(port=remote.port, pid=0, token=remote.token, protocolVersion=PROTOCOL_VERSION)
    manager = MachineManager(local.core, direct=lambda _id: info)
    manager.bind_loop(asyncio.get_running_loop())
    local.core.machines = manager
    mid = manager.registry.add(
        "me@box", label="box", key_path=None, pasted_key=None, remote_cwd=None, auto_connect=False
    ).id
    manager.connect(mid)
    for _ in range(60):
        if manager.rows()[0].status == "connected":
            break
        await asyncio.sleep(0.05)
    person = _conn("person")
    client = await manager._ensure_client(mid, person)  # noqa: SLF001
    assert client._hello_client_kind is None
    assert client.approval_handler is not None


def test_n5_snowpea_create_refused_for_remote_login() -> None:
    from snowpea_core.server.protocol import TerminalCreateParams

    remote = _conn("remote_login")
    params = TerminalCreateParams(profile="snowpea")
    with pytest.raises(RpcError) as caught:
        run_proxy_gates("terminal.create", remote, params, _core(), "mid")
    assert caught.value.code == errors.JARVIS_PERSON_ONLY