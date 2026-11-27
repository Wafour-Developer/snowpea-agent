"""M54 / 1.82.0: ssh key listing, config hosts, auto key and machine.update connection fields."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from _support import connect, make_daemon

from snowpea_core.cli.remote import RemoteSession, RemoteTarget
from snowpea_core.machines.registry import MachineRegistry, validate_key_path
from snowpea_core.machines.ssh_config_hosts import list_ssh_config_hosts
from snowpea_core.machines.ssh_keys import list_ssh_keys
from snowpea_core.server import errors, settings_gate
from snowpea_core.server.errors import RpcError
from snowpea_core.server.person_gate import method_refusal
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX ssh layout")


def test_protocol_bump_and_capability() -> None:
    assert PROTOCOL_VERSION == "1.82.0"
    assert "machineKeys" in CAPABILITIES


def test_ssh_keys_lists_pub_paired_and_standard_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True, mode=0o700)
    priv = ssh_dir / "deploy"
    pub = ssh_dir / "deploy.pub"
    pub.write_text("ssh-ed25519 AAAAB3 deploy-key\n", encoding="utf-8")
    priv.write_text("secret\n", encoding="utf-8")
    priv.chmod(0o600)
    std = ssh_dir / "id_rsa"
    std.write_text("rsa-secret\n", encoding="utf-8")
    std.chmod(0o600)
    (ssh_dir / "id_rsa.pub").write_text("ssh-rsa AAAAB1 comment line\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    rows = list_ssh_keys(ssh_dir)
    by_name = {row["name"]: row for row in rows}
    assert by_name["deploy"]["type"] == "ssh-ed25519"
    assert by_name["deploy"]["comment"] == "deploy-key"
    assert by_name["deploy"]["modeOk"] is True
    assert by_name["id_rsa"]["type"] == "ssh-rsa"
    assert by_name["id_rsa"]["comment"] == "comment line"


def test_ssh_keys_never_reads_private_bytes(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    priv = ssh_dir / "opaque"
    pub = ssh_dir / "opaque.pub"
    pub.write_text("ssh-ed25519 AAAAB3 y\n", encoding="utf-8")
    priv.write_bytes(b"\x00")
    priv.chmod(0o000)
    opened: list[str] = []
    original_open = open

    def tracking_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        path = str(file)
        if path.endswith("opaque") and not path.endswith(".pub"):
            opened.append(path)
        return original_open(file, *args, **kwargs)

    with patch("builtins.open", side_effect=tracking_open):
        rows = list_ssh_keys(ssh_dir)
    assert [row["name"] for row in rows] == ["opaque"]
    assert not opened


def test_ssh_config_include_match_and_caps(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    (ssh_dir / "config").write_text(
        "Host wild-*\n"
        "    HostName nowhere\n"
        "Match all\n"
        "    Host hidden\n"
        "        HostName secret\n"
        "Host mybox\n"
        "    HostName box.local\n"
        "    User deploy\n"
        "    Port 2222\n"
        "    IdentityFile ~/.ssh/deploy\n"
        "Include extra\n",
        encoding="utf-8",
    )
    (ssh_dir / "extra").write_text(
        "Host nested\n    HostName nested.local\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (ssh_dir / "leak").symlink_to(outside / "config")
    rows = list_ssh_config_hosts(ssh_dir)
    aliases = {row["alias"]: row for row in rows}
    assert "wild-*" not in aliases
    assert "hidden" not in aliases
    assert aliases["mybox"]["hostName"] == "box.local"
    assert aliases["mybox"]["user"] == "deploy"
    assert aliases["mybox"]["port"] == 2222
    assert "deploy" in (aliases["mybox"]["identityFile"] or "")
    assert "nested" in aliases


def test_ssh_config_skips_include_outside_ssh_root(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    outside = tmp_path / "outside"
    ssh_dir.mkdir(mode=0o700)
    outside.mkdir()
    (outside / "evil").write_text("Host evil\n    HostName evil\n", encoding="utf-8")
    (ssh_dir / "config").write_text(f"Include {outside / 'evil'}\n", encoding="utf-8")
    assert list_ssh_config_hosts(ssh_dir) == []


def test_auto_key_ssh_argv_has_no_identity_flag(tmp_path: Path) -> None:
    reg = MachineRegistry(tmp_path)
    rec = reg.add(
        "myalias",
        label="alias",
        key_path=None,
        pasted_key=None,
        remote_cwd=None,
        auto_connect=False,
    )
    session = RemoteSession(
        RemoteTarget(host="myalias"), local_protocol="1.82.0", confirm=lambda _q: True
    )
    session._options = list(reg.ssh_identity(rec))
    session._ssh_extra = list(reg.ssh_extra_args(rec))
    argv = session.ssh_argv()
    assert "-i" not in argv
    assert "IdentitiesOnly=yes" not in argv
    assert argv[-1] == "myalias"


def test_alias_add_keeps_destination_only(tmp_path: Path) -> None:
    reg = MachineRegistry(tmp_path)
    rec = reg.add(
        "mybox", label="x", key_path=None, pasted_key=None, remote_cwd=None, auto_connect=False
    )
    assert rec.host == "mybox"
    assert rec.user is None
    assert rec.port is None


def test_update_connection_fields_and_label_only(tmp_path: Path) -> None:
    reg = MachineRegistry(tmp_path)
    rec = reg.add(
        "a", label="old", key_path=None, pasted_key=None, remote_cwd=None, auto_connect=False
    )
    mid = rec.id
    rec = reg.get(mid)
    assert rec is not None
    rec.label = "new"
    rec.host = "b"
    rec.user = "u"
    rec.port = 2200
    key = tmp_path / "k"
    key.write_text("k\n", encoding="utf-8")
    rec.key_path = validate_key_path(str(key))
    reg.save()
    loaded = reg.get(mid)
    assert loaded is not None
    assert loaded.label == "new" and loaded.host == "b" and loaded.user == "u"
    assert loaded.port == 2200 and loaded.key_path == str(key)


def test_clear_known_host_on_host_change(tmp_path: Path) -> None:
    reg = MachineRegistry(tmp_path)
    rec = reg.add(
        "old.host", label="x", key_path=None, pasted_key=None, remote_cwd=None, auto_connect=False
    )
    kn = reg.known_hosts_file()
    kn.write_text("old.host ssh-ed25519 AAA\n", encoding="utf-8")
    reg.clear_known_host(rec)
    assert "old.host" not in kn.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_rpc_ssh_keys_and_hosts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_ssh = tmp_path / "ssh-home"
    fake_ssh.mkdir(parents=True)
    (fake_ssh / "id_ed25519").write_text("x\n", encoding="utf-8")
    (fake_ssh / "id_ed25519").chmod(0o600)
    (fake_ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAAB3 dev\n", encoding="utf-8")
    (fake_ssh / "config").write_text("Host dev\n    HostName dev\n", encoding="utf-8")
    monkeypatch.setattr(
        "snowpea_core.server.machine_handlers._user_ssh_dir",
        lambda: fake_ssh,
    )
    daemon = await make_daemon(tmp_path / "daemon-home")
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        keys = await client.ok("machine.sshKeys", {})
        hosts = await client.ok("machine.sshHosts", {})
        assert keys["keys"][0]["name"] == "id_ed25519"
        assert hosts["hosts"][0]["alias"] == "dev"
        await client.stop()
    await daemon.stop()


@pytest.mark.asyncio
async def test_update_refuses_while_connected(tmp_path: Path) -> None:
    from snowpea_core.machines.manager import LiveConnection
    from snowpea_core.server.machine_handlers import ensure_manager, machine_update_handler
    from snowpea_core.server.protocol import MachineUpdateParams

    daemon = await make_daemon(tmp_path)
    mgr = ensure_manager(daemon.core)
    rec = mgr.registry.add(
        "me@box",
        label="box",
        key_path=None,
        pasted_key=None,
        remote_cwd=None,
        auto_connect=False,
    )
    mgr._live[rec.id] = LiveConnection(status="connected")
    conn = SimpleNamespace(authenticated=True, closed=False, client_kind="ide", remote_login=False)
    with pytest.raises(RpcError) as exc:
        await machine_update_handler(
            conn,
            MachineUpdateParams(id=rec.id, host="other"),
            daemon.core,
        )
    assert exc.value.code == errors.UNAVAILABLE
    row = await machine_update_handler(
        conn, MachineUpdateParams(id=rec.id, label="renamed"), daemon.core
    )
    assert row.label == "renamed"
    await daemon.stop()


def test_gates_refuse_non_local_person() -> None:
    for kind in ("browser", "connector", "remote"):
        conn = SimpleNamespace(
            authenticated=True,
            closed=False,
            client_kind={"browser": "browser", "connector": "connector", "remote": "web"}[kind],
            remote_login=kind == "remote",
            client_version="0.3.0",
            client_id=f"{kind}-1",
            surface_id=f"{kind}-s",
        )
        for method in ("machine.sshKeys", "machine.sshHosts"):
            with pytest.raises(RpcError) as refused:
                method_refusal(method, conn)
                settings_gate.kind_method_refusal(method, {}, conn, None)
            assert refused.value.code == errors.JARVIS_PERSON_ONLY