"""M19 WP5: process safety for external CLIs (``external/proc.py``)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from _external_adapter import FAKE_CLAUDE, FAKE_CLIS
from _support import env_vars

from snowpea_core.external import proc
from snowpea_core.external.proc import (
    BypassArgs,
    ExternalError,
    PersonConfirmation,
    PinnedExecutable,
    ProcessSupervisor,
    UnrestrictedGrant,
)
from snowpea_core.server import errors

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

DANGEROUS = [
    "--dangerously-skip-permissions",
    "--yolo",
    "--permission-mode=bypassPermissions",
    "bypassPermissions",
    "danger-full-access",
    "--sandbox=danger-full-access",
    "--dangerously-bypass-approvals-and-sandbox",
    "--approve-for-me",
    "--YOLO",
]
BASE = ["-p", "--output-format", "stream-json", "--resume", "abc-123"]


class Conn:
    def __init__(self, kind: str | None, *, authenticated: bool = True) -> None:
        self.client_kind = kind
        self.client_id = "c1"
        self.authenticated = authenticated
        self.closed = False
        self.on_close: list[Any] = []


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not proc.pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _reap_zombie(pid: int) -> None:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


# -- argument checks -------------------------------------------------------


@pytest.mark.parametrize("flag", DANGEROUS)
@pytest.mark.parametrize("position", [0, 2, 5])
def test_bypass_flags_refused_in_every_position(flag: str, position: int) -> None:
    args = list(BASE)
    args.insert(position, flag)
    with pytest.raises(ExternalError) as caught:
        proc.assert_safe_args(args)
    assert caught.value.code == errors.INVALID_PARAMS
    with pytest.raises(ExternalError):
        proc.build_argv(PinnedExecutable("/bin/true"), args)


def test_cli_specific_tokens_and_pairs() -> None:
    with pytest.raises(ExternalError):
        proc.assert_safe_args(["-p", "-f", "x"], forbidden=("-f", "--force"))
    with pytest.raises(ExternalError):
        proc.assert_safe_args(["--force=true"], forbidden=("--force",))
    for args in (["-a", "never"], ["exec", "-a", "NEVER"], ["-a=never"]):
        with pytest.raises(ExternalError):
            proc.assert_safe_args(args, forbidden_pairs=(("-a", "never"),))
    proc.assert_safe_args(["-a", "on-request"], forbidden_pairs=(("-a", "never"),))
    with pytest.raises(ExternalError):
        proc.assert_safe_args(["ok", "bad\x00"])
    with pytest.raises(ExternalError):
        proc.assert_safe_args(["ok", 3])  # type: ignore[list-item]


def test_json_payloads_are_scanned() -> None:
    proc.scan_payload({"cwd": "/w", "approvalPolicy": "on-request", "sandbox": "workspace-write"})
    for payload in (
        {"sandbox": "danger-full-access"},
        {"config": {"x": ["ok", "bypassPermissions"]}},
        {"dangerouslyBypass": True},
    ):
        with pytest.raises(ExternalError):
            proc.scan_payload(payload)


def test_ids_models_and_modes_are_validated() -> None:
    for good in ("72b3e6e6-81b3-49e4-9824-9c99d396912e", "01a1df17_x", "a"):
        assert proc.validate_session_id(good) == good
    for bad in ("", "-rf", "--resume", "../etc", "a b", "x;rm", "a/b", "a.b", "x" * 200, "é"):
        with pytest.raises(ExternalError):
            proc.validate_session_id(bad)
    for good in ("haiku", "gpt-5.3-codex", "openai/gpt-5", "claude-opus-4@2026", None):
        assert proc.validate_model(good) == good
    for bad in ("-m", "--yolo", "a b", "../x", "x\n", "danger-model", ""):
        with pytest.raises(ExternalError):
            proc.validate_model(bad)
    assert proc.validate_mode("readonly") == "readonly"
    with pytest.raises(ExternalError):
        proc.validate_mode("yolo")
    with pytest.raises(ExternalError):
        proc.validate_mode("default", ("readonly", "unrestricted"))


def test_executable_is_pinned_and_absolute(tmp_path: Path) -> None:
    assert proc.resolve_executable("x", str(FAKE_CLAUDE)) == str(FAKE_CLAUDE.resolve())
    with pytest.raises(ExternalError):
        proc.resolve_executable("x", "relative/claude")
    plain = tmp_path / "not-exec"
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    assert proc.resolve_executable("x", str(plain)) is None
    assert proc.resolve_executable("definitely-not-a-binary-snowpea") is None
    with pytest.raises(ExternalError):
        proc.build_argv(PinnedExecutable("claude"), ["-p"])


async def test_version_probe() -> None:
    assert await proc.probe_version(str(FAKE_CLAUDE)) == "2.1.325 (Claude Code, fake)"


def test_prompt_argument_goes_after_end_of_options() -> None:
    argv = proc.build_argv(PinnedExecutable("/bin/echo"), ["-p"], prompt_arg="--yolo do it")
    assert argv == ["/bin/echo", "-p", "--", "--yolo do it"]


# -- environment and working directory ---------------------------------------


def test_env_is_scrubbed() -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "SNOWPEA_HOME": "/s",
        "SNOWPEA_REGISTRY_TOKEN": "t",
        "snowpea_lower": "t",
        "TELEGRAM_BOT_TOKEN": "t",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SIMPLE": "1",
        "ANTHROPIC_API_KEY": "only-when-passEnv-names-it",
    }
    env = proc.scrubbed_env(base=base)
    # An allowlist since the security review (L3): provider keys stay behind
    # unless jarvis.external.clis.<id>.passEnv names them.
    assert env == {"PATH": "/usr/bin", "HOME": "/home/x"}
    named = proc.scrubbed_env(base=base, pass_env=["ANTHROPIC_API_KEY"])
    assert named["ANTHROPIC_API_KEY"] == "only-when-passEnv-names-it"
    assert proc.scrubbed_env({"NO_COLOR": "1"}, base={})["NO_COLOR"] == "1"
    with pytest.raises(ExternalError):
        proc.scrubbed_env({"SNOWPEA_TOKEN": "x"}, base={})


def test_cwd_must_be_a_registered_project(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "other"
    outside.mkdir()
    assert proc.validate_cwd(root, [str(root)]) == str(root.resolve())
    assert proc.validate_cwd(root / "sub", [str(root)]) == str((root / "sub").resolve())
    for bad in (outside, tmp_path / "missing", root / "sub" / ".." / ".." / "other"):
        with pytest.raises(ExternalError) as caught:
            proc.validate_cwd(bad, [str(root)])
        assert caught.value.code == errors.OUTSIDE_ROOT
    link = root / "escape"
    link.symlink_to(outside)
    with pytest.raises(ExternalError):
        proc.validate_cwd(link, [str(root)])
    # The home folder as a "project" only matches itself.
    home = Path.home()
    assert proc.validate_cwd(home, [str(home)]) == str(home.resolve())
    with pytest.raises(ExternalError):
        proc.validate_cwd(tmp_path, [str(home), "/"])
    assert proc.project_of(str(root / "sub"), [str(tmp_path), str(root)]) == str(root)


# -- person confirmation, grants, ledger ---------------------------------------


def test_only_a_human_connection_confirms() -> None:
    for kind in ("ide", "desktop", "tui", "web", "cli", "app"):
        assert proc.confirm_person(Conn(kind)).client_kind == kind
    # The desktop app and the TUI send no clientKind: the delegation engine's rule.
    assert proc.confirm_person(Conn(None)).client_kind == "local"
    assert proc.is_human_connection(Conn(None))
    remote = Conn("web")
    remote.remote_login = True  # type: ignore[attr-defined]
    assert proc.is_human_connection(remote)  # a person, who may send...
    with pytest.raises(ExternalError) as caught:
        proc.confirm_person(remote)  # ...but not open unrestricted mode
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    for conn in (None, Conn("browser"), Conn("connector"), Conn("ide", authenticated=False)):
        with pytest.raises(ExternalError) as caught:
            proc.confirm_person(conn)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    closed = Conn("tui")
    closed.closed = True
    with pytest.raises(ExternalError):
        proc.confirm_person(closed)


def test_tokens_cannot_be_forged(tmp_path: Path) -> None:
    with pytest.raises(ExternalError):
        PersonConfirmation(client_kind="ide", client_id=None, surface="ide", ts="t")
    confirmation = proc.confirm_person(Conn("ide"))
    with pytest.raises(ExternalError):
        UnrestrictedGrant(cli="claude", session_id=None, project=None, action="create",
                          confirmation=confirmation, ledger_id="x")
    with pytest.raises(ExternalError):
        BypassArgs(flags=("--yolo",))
    with pytest.raises(ExternalError):
        proc.bypass_args(object(), ["--yolo"])  # type: ignore[arg-type]
    with pytest.raises(ExternalError):
        proc.unrestricted_values(object(), {"approvalPolicy": "never"})  # type: ignore[arg-type]
    with pytest.raises(ExternalError):
        proc.grant_unrestricted(object(), cli="claude", session_id=None,  # type: ignore[arg-type]
                                project=None, action="create", home=tmp_path)


def test_grant_writes_the_ledger_and_releases_flags(tmp_path: Path) -> None:
    confirmation = proc.confirm_person(Conn("tui"))
    grant = proc.grant_unrestricted(confirmation, cli="claude", session_id="s-1",
                                    project="/w", action="setMode", home=tmp_path)
    entries = [json.loads(line) for line in
               (tmp_path / "jarvis" / "ledger.jsonl").read_text().splitlines()]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "unrestricted" and entry["decidedBy"] == "person"
    assert entry["external"] == {"cli": "claude", "id": "s-1"} and entry["project"] == "/w"
    assert entry["id"] == grant.ledger_id
    assert oct((tmp_path / "jarvis" / "ledger.jsonl").stat().st_mode & 0o777) == "0o600"
    flags = proc.bypass_args(grant, ["--dangerously-skip-permissions"])
    argv = proc.build_argv(PinnedExecutable("/bin/true"), ["-p"], bypass=flags)
    assert argv == ["/bin/true", "-p", "--dangerously-skip-permissions"]
    assert proc.unrestricted_values(grant, {"approvalPolicy": "never"}) == {
        "approvalPolicy": "never"
    }


def test_ledger_hook_seam(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    proc.set_ledger_hook(seen.append)
    try:
        proc.grant_unrestricted(proc.confirm_person(Conn("ide")), cli="codex", session_id=None,
                                project=None, action="create", home=tmp_path)
        assert seen and seen[0]["kind"] == "unrestricted"
        assert not (tmp_path / "jarvis").exists()

        def broken(_entry: dict[str, Any]) -> None:
            raise OSError("disk full")

        proc.set_ledger_hook(broken)
        with pytest.raises(ExternalError):
            proc.grant_unrestricted(proc.confirm_person(Conn("ide")), cli="codex",
                                    session_id=None, project=None, action="create",
                                    home=tmp_path)
    finally:
        proc.set_ledger_hook(None)


# -- processes ---------------------------------------------------------------------


def _argv(*extra: str) -> list[str]:
    return proc.build_argv(
        PinnedExecutable(str(FAKE_CLAUDE)),
        ["-p", "--input-format", "stream-json", "--session-id",
         "11111111-2222-3333-4444-555555555555", *extra],
    )


async def test_child_env_is_scrubbed_end_to_end(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    supervisor = ProcessSupervisor(tmp_path / "home")
    with env_vars(SNOWPEA_SECRET_TEST="leak", TELEGRAM_BOT_TOKEN="leak", FAKE_CLI_LOG=str(log)):
        child = await supervisor.spawn(_argv(), cwd=str(tmp_path), env=proc.scrubbed_env(),
                                       cli="claude")
    for _ in range(200):
        if log.exists() and '"start"' in log.read_text():
            break
        await asyncio.sleep(0.05)
    await child.terminate(grace=1)
    start = next(json.loads(line) for line in log.read_text().splitlines()
                 if json.loads(line)["kind"] == "start")
    names = start["data"]["env"]
    assert "FAKE_CLI_LOG" in names
    assert not [n for n in names if n.startswith("SNOWPEA_") or n.startswith("TELEGRAM_")]
    assert start["data"]["bypass"] == []


async def test_concurrency_cap(tmp_path: Path) -> None:
    supervisor = ProcessSupervisor(tmp_path / "home", max_processes=lambda: 2)
    env = proc.scrubbed_env()
    first = await supervisor.spawn(_argv(), cwd=str(tmp_path), env=env, cli="claude")
    second = await supervisor.spawn(_argv(), cwd=str(tmp_path), env=env, cli="claude")
    try:
        with pytest.raises(ExternalError) as caught:
            await supervisor.spawn(_argv(), cwd=str(tmp_path), env=env, cli="claude")
        assert caught.value.code == errors.EXTERNAL_UNAVAILABLE
        assert len(list((tmp_path / "home" / "external").glob("*.json"))) == 2
        await first.terminate(grace=1)
        third = await supervisor.spawn(_argv(), cwd=str(tmp_path), env=env, cli="claude")
        await third.terminate(grace=1)
    finally:
        await supervisor.shutdown(grace=1)
    assert not list((tmp_path / "home" / "external").glob("*.json"))
    assert not second.alive


async def test_terminate_kills_the_whole_tree_even_when_sigterm_is_ignored(
    tmp_path: Path,
) -> None:
    pidfile = tmp_path / "child.pid"
    supervisor = ProcessSupervisor(tmp_path / "home")
    with env_vars(FAKE_CLI_SCENARIO="hang", FAKE_CLI_CHILD_PIDFILE=str(pidfile)):
        child = await supervisor.spawn(_argv(), cwd=str(tmp_path), env=proc.scrubbed_env(),
                                       cli="claude")
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
    grandchild = int(pidfile.read_text())
    assert proc.pid_alive(child.pid) and proc.pid_alive(grandchild)
    assert child.pgid == child.pid  # its own process group
    await child.terminate(grace=0.5)
    assert not child.alive
    assert await asyncio.to_thread(_wait_dead, grandchild)


async def test_orphans_of_a_dead_daemon_are_reaped(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pidfile = tmp_path / "child.pid"
    first = ProcessSupervisor(home)
    with env_vars(FAKE_CLI_CHILD_PIDFILE=str(pidfile)):
        orphan = await first.spawn(_argv(), cwd=str(tmp_path), env=proc.scrubbed_env(),
                                   cli="claude")
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
    grandchild = int(pidfile.read_text())
    record = json.loads((home / "external" / f"{orphan.pid}.json").read_text())
    assert record["pid"] == orphan.pid and record["startTime"]
    # A pid file whose pid now belongs to someone else (start time differs) is
    # removed, and that process is left alone.
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (home / "external" / f"{bystander.pid}.json").write_text(
            json.dumps({"pid": bystander.pid, "pgid": bystander.pid, "startTime": "1"})
        )
        (home / "external" / "garbage.json").write_text("{not json")
        # The next daemon on this home starts and sweeps.
        second = ProcessSupervisor(home)
        assert await asyncio.to_thread(second.reap_orphans) == 1
        assert await asyncio.to_thread(_wait_dead, orphan.pid)
        assert await asyncio.to_thread(_wait_dead, grandchild)
        assert bystander.poll() is None
        assert not list((home / "external").glob("*.json"))
    finally:
        bystander.kill()
        bystander.wait()
        await orphan.terminate(grace=0.2)
        _reap_zombie(orphan.pid)


async def test_line_reader_survives_garbage(tmp_path: Path) -> None:
    supervisor = ProcessSupervisor(tmp_path / "home")
    with env_vars(FAKE_CLI_SCENARIO="garbage"):
        child = await supervisor.spawn(_argv(), cwd=str(tmp_path), env=proc.scrubbed_env(),
                                       cli="claude")
    await child.write_line({"type": "user", "message": {"role": "user", "content": "hi"}})
    frames: list[dict[str, Any]] = []

    async def collect() -> None:
        async for frame in child.json_lines():
            frames.append(frame)
            if frame.get("type") == "result":
                await child.close_stdin()

    await asyncio.wait_for(collect(), 10)
    await child.terminate(grace=1)
    assert frames[-1]["type"] == "result" and frames[-1]["result"] == "OK"
    assert child.garbage_lines == 5  # "not json", "{broken", "[1, 2, 3]", escape, "42"
    with pytest.raises(ExternalError):
        await child.write_line("two\nlines")
    with pytest.raises(ExternalError):
        await child.write_line("after exit")


def test_fake_clis_are_executable() -> None:
    for name in ("fake_claude.py", "fake_codex.py", "fake_agy.py", "fake_cursor.py"):
        assert os.access(FAKE_CLIS / name, os.X_OK), name
