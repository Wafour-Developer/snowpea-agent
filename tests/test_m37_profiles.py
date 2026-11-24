"""M37 T3 §2.6/§2.9: terminal profiles, permission options, launch argv/env, the dangerous gate.

No real CLI is ever started: discovery is replaced (``profiles.cli_state``) by a
pinned fake path and the installed CLI's mode table, and most tests only build
the launch (``build_spec``) without running it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from _support import connect, env_vars, make_daemon

from snowpea_core.external import agy, claude, codex, cursor
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import TerminalCreateParams, TerminalProfile
from snowpea_core.terminal import profiles

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launch argv")

TABLES = {"claude": claude.NATIVE_MODES, "codex": codex.NATIVE_MODES,
          "agy": agy.NATIVE_MODES, "cursor": cursor.NATIVE_MODES}
FORBIDDEN = {
    "claude": (claude.ClaudeAdapter.forbidden_args, claude.ClaudeAdapter.forbidden_pairs),
    "codex": (codex.CodexAdapter.forbidden_args, codex.CodexAdapter.forbidden_pairs),
    "agy": (agy.AgyAdapter.forbidden_args, agy.AgyAdapter.forbidden_pairs),
    "cursor": (cursor.CursorAdapter.forbidden_args, cursor.CursorAdapter.forbidden_pairs),
}


def human(**extra: Any) -> SimpleNamespace:
    """A person's local IDE connection."""
    base = {"authenticated": True, "closed": False, "client_kind": "ide", "client_id": "c1",
            "remote_login": False, "via_tunnel": False}
    base.update(extra)
    return SimpleNamespace(**base)


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bin" / "fake-cli"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)

    async def state(_core: Any, cli: str) -> profiles.CliState:
        forbidden, pairs = FORBIDDEN[cli]
        return profiles.CliState(str(path), TABLES[cli], [], tuple(forbidden), tuple(pairs))

    monkeypatch.setattr(profiles, "cli_state", state)
    return path


@pytest.fixture
async def daemon(tmp_path: Path):
    started = await make_daemon(tmp_path / "home", {"terminals": {"keepShell": True}})
    try:
        yield started
    finally:
        await started.stop()


async def _project(http: aiohttp.ClientSession, daemon: Any, tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    client = await connect(http, daemon)
    try:
        await client.ok("workspace.add", {"path": str(project)})
    finally:
        await client.stop()
    return project.resolve()


def _params(**values: Any) -> TerminalCreateParams:
    return TerminalCreateParams.model_validate(values)


# ---------------------------------------------------------------------------
# options from the mode tables (flags verified against the installed --help)
# ---------------------------------------------------------------------------


def test_options_come_from_the_mode_tables() -> None:
    wire = {cli: {o.id: o for o in profiles.options_for(cli, rows)} for cli, rows in TABLES.items()}
    assert wire["claude"]["bypassPermissions"].args == ("--dangerously-skip-permissions",)
    assert wire["claude"]["bypassPermissions"].dangerous
    assert wire["claude"]["acceptEdits"].args == ("--permission-mode=acceptEdits",)
    assert not wire["claude"]["acceptEdits"].dangerous and wire["claude"]["acceptEdits"].person_only
    assert wire["claude"]["plan"].args == ("--permission-mode=plan",)
    assert not wire["claude"]["plan"].person_only
    assert "default" not in wire["claude"]  # the CLI's own default has no flag
    assert wire["codex"]["full-access"].args == (
        "--dangerously-bypass-approvals-and-sandbox",)
    assert wire["codex"]["read-only"].args == ("--sandbox=read-only",)
    assert "untrusted" not in wire["codex"]  # no flag for it since Codex 0.170
    assert wire["agy"]["dangerously-skip-permissions"].dangerous
    assert wire["agy"]["plan"].args == ("--mode=plan",)
    assert wire["cursor"]["yolo"].args == ("--yolo",) and wire["cursor"]["yolo"].dangerous
    assert wire["cursor"]["ask"].args == ("--mode=ask",)
    # Every dangerous flag is one the bypass regex knows (assert_safe_args refuses it).
    from snowpea_core.external.proc import is_dangerous

    for options in wire.values():
        for option in options.values():
            if option.dangerous:
                assert all(is_dangerous(a) for a in option.args)


def test_launch_argv_runs_the_cli_in_the_interactive_shell() -> None:
    argv = profiles.launch_argv(["/opt/cli", "--resume", "a b"], keep_shell=True,
                                shell="/bin/zsh")
    assert argv[0] == "/bin/zsh" and argv[1] in ("-ic", "-lic")
    assert argv[2] == '"$0" "$@"; exec /bin/zsh -i'
    assert argv[3:] == ["/opt/cli", "--resume", "a b"]  # a list, never one joined string
    closing = profiles.launch_argv(["/opt/cli"], keep_shell=False, shell="/bin/bash")
    assert closing[2] == 'exec "$0" "$@"'
    # fish cannot read "$0" "$@": a POSIX shell wraps it and fish comes back after.
    fish = profiles.launch_argv(["/opt/cli"], keep_shell=True, shell="/usr/bin/fish")
    assert Path(fish[0]).name in ("bash", "sh") and fish[2].endswith("exec /usr/bin/fish -i")


def test_environment_drops_snowpea_secrets() -> None:
    env = profiles.terminal_env({"PATH": "/bin", "SNOWPEA_TOKEN": "secret", "HOME": "/h",
                                 "TELEGRAM_BOT_TOKEN": "x", "CLAUDECODE": "1",
                                 "ANTHROPIC_API_KEY": "k"})
    assert env == {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k"}


# ---------------------------------------------------------------------------
# build_spec on a real daemon
# ---------------------------------------------------------------------------


async def test_cli_argv_new_and_resume(daemon: Any, http: aiohttp.ClientSession,
                                       tmp_path: Path, fake_cli: Path) -> None:
    project = await _project(http, daemon, tmp_path)
    core = daemon.core
    with env_vars(SHELL="/bin/bash", SNOWPEA_TOKEN="daemon-secret"):
        spec = await profiles.build_spec(
            core, human(), _params(profile="claude", cwd=str(project), trust=True,
                                   options=["plan"]), str(project))
        assert spec.argv[3] == str(fake_cli)
        session_id = spec.external["sessionId"]
        assert spec.argv[4:] == ["--permission-mode=plan", "--session-id", session_id]
        assert spec.rules == "claude" and spec.options == ["plan"] and not spec.dangerous
        assert "SNOWPEA_TOKEN" not in spec.env and "daemon-secret" not in spec.env.values()
        assert daemon.token not in json.dumps(spec.argv) + json.dumps(spec.env)
        resumed = await profiles.build_spec(
            core, human(), _params(profile="codex", cwd=str(project),
                                   resumeSessionId="0199-abc", options=["read-only"]),
            str(project))
        assert resumed.argv[3:] == [str(fake_cli), "resume", "--sandbox=read-only", "0199-abc"]
        assert resumed.external == {"cli": "codex", "sessionId": "0199-abc"}
        agy_spec = await profiles.build_spec(
            core, human(), _params(profile="agy", cwd=str(project), resumeSessionId="c1"),
            str(project))
        assert agy_spec.argv[4:] == ["--conversation", "c1"]
        cur = await profiles.build_spec(
            core, human(), _params(profile="cursor", cwd=str(project), resumeSessionId="x1",
                                   options=["ask"]), str(project))
        assert cur.argv[4:] == ["--mode=ask", "--resume", "x1"]
        with pytest.raises(RpcError) as bad:
            await profiles.build_spec(core, human(), _params(
                profile="codex", cwd=str(project), resumeSessionId="-rf"), str(project))
        assert bad.value.code == "invalid_params"
        with pytest.raises(RpcError) as two:
            await profiles.build_spec(core, human(), _params(
                profile="claude", cwd=str(project), options=["plan", "acceptEdits"]),
                str(project))
        assert "one permission option" in two.value.message
        with pytest.raises(RpcError) as unknown:
            await profiles.build_spec(core, human(), _params(
                profile="claude", cwd=str(project), options=["--yolo"]), str(project))
        assert unknown.value.code == "invalid_params"


@pytest.mark.real_trust
async def test_untrusted_project_needs_the_person(daemon: Any, http: aiohttp.ClientSession,
                                                  tmp_path: Path, fake_cli: Path) -> None:
    project = await _project(http, daemon, tmp_path)
    with pytest.raises(RpcError) as untrusted:
        await profiles.build_spec(daemon.core, human(), _params(profile="claude",
                                                                cwd=str(project)), str(project))
    assert untrusted.value.data["reason"] == "untrusted_project"
    with pytest.raises(RpcError) as jarvis:
        await profiles.build_spec(daemon.core, None, _params(profile="claude", cwd=str(project),
                                                             parent="jarvis"), str(project))
    assert jarvis.value.data["reason"] == "untrusted_project_person_only"


async def test_dangerous_refusal_matrix(daemon: Any, http: aiohttp.ClientSession,
                                        tmp_path: Path, fake_cli: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    project = await _project(http, daemon, tmp_path)
    core = daemon.core

    async def build(conn: Any, **extra: Any) -> Any:
        values = {"profile": "claude", "cwd": str(project), "trust": True,
                  "options": ["bypassPermissions"], **extra}
        return await profiles.build_spec(core, conn, _params(**values), str(project))

    # No confirmDangerous: refused before any grant.
    with pytest.raises(RpcError) as unconfirmed:
        await build(human())
    assert unconfirmed.value.data["reason"] == "confirm_dangerous"
    # Jarvis / any tool: no connection.
    with pytest.raises(RpcError) as jarvis:
        await build(None, confirmDangerous=True, parent="jarvis")
    assert jarvis.value.code == "jarvis_person_only"
    # A remote web login is not the local person; a browser is nobody.
    for conn in (human(remote_login=True), human(client_kind="browser")):
        with pytest.raises(RpcError) as remote:
            await build(conn, confirmDangerous=True)
        assert remote.value.data["reason"] == "person_only"
    # Over the tunnel without the unlock.
    with pytest.raises(RpcError) as locked:
        await build(human(via_tunnel=True), confirmDangerous=True)
    assert locked.value.data["reason"] == "terminal_locked"
    assert profiles.dangerous_refusal(core, human(via_tunnel=True)) == "terminal_locked"
    # Unlocked over the tunnel: still a remote web login, never the local person.
    unlocked = human(via_tunnel=True, remote_login=True, tunnel_unlocked=lambda: True)
    assert profiles.dangerous_refusal(core, unlocked) == "person_only"
    # web.remote.maxMode below auto.
    monkeypatch.setattr(core.settings.web.remote, "maxMode", "plan")
    with pytest.raises(RpcError) as capped:
        await build(human(), confirmDangerous=True)
    assert capped.value.data["reason"] == "max_mode"
    assert profiles.dangerous_refusal(core, human()) == "max_mode"
    monkeypatch.setattr(core.settings.web.remote, "maxMode", "auto")
    # The person, confirmed: the grant is in the ledger and the flag gets out.
    spec = await build(human(), confirmDangerous=True)
    assert spec.dangerous and "--dangerously-skip-permissions" in spec.argv
    assert spec.labels["snowpea.grant"]
    ledger = Path(core.paths.home) / "jarvis" / "ledger.jsonl"
    entries = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert entries[-1]["kind"] == "unrestricted" and entries[-1]["decidedBy"] == "person"
    # A person-only mode that is not a bypass: a grant, no confirmDangerous.
    granted = await profiles.build_spec(core, human(), _params(
        profile="claude", cwd=str(project), options=["acceptEdits"]), str(project))
    assert "--permission-mode=acceptEdits" in granted.argv and not granted.dangerous
    with pytest.raises(RpcError):
        await profiles.build_spec(core, None, _params(
            profile="claude", cwd=str(project), options=["acceptEdits"]), str(project))


async def test_profiles_list_and_refusal_per_connection(
    daemon: Any, http: aiohttp.ClientSession, fake_cli: Path
) -> None:
    rows = await profiles.list_profiles(daemon.core, human())
    by_id = {row["id"]: TerminalProfile.model_validate(row) for row in rows}
    assert list(by_id) == ["shell", "snowpea", "claude", "codex", "agy", "cursor"]
    assert by_id["claude"].installed and by_id["claude"].integration == "claude"
    assert by_id["claude"].dangerousRefusal is None
    assert {o.id for o in by_id["claude"].options} >= {"plan", "bypassPermissions"}
    remote = {row["id"]: row for row in await profiles.list_profiles(
        daemon.core, human(remote_login=True))}
    assert remote["codex"]["dangerousRefusal"] == "person_only"
    assert remote["shell"]["dangerousRefusal"] is None  # no dangerous option there
    client = await connect(http, daemon)
    try:
        wire = await client.ok("terminal.profiles", {})
        assert [p["id"] for p in wire["profiles"]][:2] == ["shell", "snowpea"]
    finally:
        await client.stop()


async def test_snowpea_profile_and_reuse(daemon: Any, http: aiohttp.ClientSession,
                                         tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                         ) -> None:
    project = await _project(http, daemon, tmp_path)
    fake = tmp_path / "snowpea-fake"
    fake.write_text("#!/bin/sh\nprintf 'snowpea-tui %s\\n' \"$*\"\nsleep 30\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(profiles, "snowpea_bin", lambda: str(fake))
    client = await connect(http, daemon)
    try:
        created = await client.ok("session.create", {"workdir": str(project)})
        sid = created["sessionId"]
        with env_vars(SHELL="/bin/sh", SNOWPEA_TOKEN="daemon-secret"):
            first = (await client.ok("terminal.create", {"profile": "snowpea",
                                                         "snowpeaSessionId": sid}))["terminal"]
            assert first["snowpeaSessionId"] == sid and first["cwd"] == str(project)
            term = daemon.core.terminals.get(first["terminalId"])
            argv = term.spec.argv
            assert argv[3:] == [str(fake), "--home", str(daemon.paths.home), "--session", sid,
                                "--cwd", str(project)]
            assert daemon.token not in " ".join(argv)
            again = (await client.ok("terminal.create", {"profile": "snowpea",
                                                         "snowpeaSessionId": sid}))["terminal"]
            assert again["terminalId"] == first["terminalId"]
            assert len(daemon.core.terminals.terminals()) == 1
            missing = await client.call("terminal.create", {"profile": "snowpea",
                                                            "snowpeaSessionId": "nope"})
            assert missing["error"]["data"]["code"] == "not_found"
    finally:
        await client.stop()
        await daemon.core.terminals.close_all()
