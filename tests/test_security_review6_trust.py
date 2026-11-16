"""Security review 6, M-2 … M-6 and L-1 … L-5: project trust, tightened.

Every hook and server command below is DATA: nothing here runs a hook, a
server or a script — the tests look at what the trust store records and what
the loaders register.  Script "files" hold never-run text in the test's tmp dir.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import RpcError
from snowpea_core.trust import projects as trust
from snowpea_core.trust.projects import TrustChanged, TrustStore, store_for

pytestmark = [pytest.mark.real_project_trust, pytest.mark.real_mode_guard]


def _conn(kind: str = "ide", *, remote: bool = False) -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind=kind,
                           remote_login=remote, client_version="0.3.0", client_id=f"{kind}-1",
                           surface_id=f"{kind}-s")


@pytest.fixture
def core(tmp_path: Path) -> Core:
    made = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    trust.set_default_home(made.paths.home)
    return made


def _hooks(project: Path, command: str, rel: str = ".snowpea/hooks.json") -> Path:
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}}),
        encoding="utf-8")
    return path


def _skill(project: Path, name: str, front: str, body: str = "Do the thing.",
           folder: str = ".snowpea") -> Path:
    path = project / folder / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {name} skill\n{front}---\n\n{body}\n",
                    encoding="utf-8")
    return path


def _loader(core: Core, *workdirs: Path) -> Any:
    from snowpea_core.skills.loader import SkillLoader

    loader = SkillLoader(core, workdirs=list(workdirs))
    loader.scan()
    return loader


def _trust(core: Core, project: Path, scopes: list[str] | None = None) -> dict[str, Any]:
    store = store_for(core.paths.home)
    return store.set(project, scopes, by="ide", digests=store.status(project)["digests"])


# -- M-2: no cache, one read ---------------------------------------------------------


def test_the_digest_follows_the_content_whatever_the_file_times_say(tmp_path: Path) -> None:
    path = tmp_path / "hooks.json"
    path.write_text('{"hooks": {"a": 1}}', encoding="utf-8")
    info = path.stat()
    first = trust.digest_of(path)
    # Same length, same mtime, same inode: only the content (and ctime) moved.
    with path.open("r+", encoding="utf-8") as handle:
        handle.write('{"hooks": {"b": 2}}')
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    after = path.stat()
    assert (after.st_mtime_ns, after.st_size, after.st_ino) == (
        info.st_mtime_ns, info.st_size, info.st_ino)
    assert trust.digest_of(path) != first
    assert not hasattr(trust, "_DIGESTS")
    data, digest = trust.read_digest(path)
    assert data == b'{"hooks": {"b": 2}}' and digest == trust.digest_of(path)
    assert trust.read_digest(tmp_path / "missing") == (None, "missing")


def test_a_same_stat_rewrite_of_a_trusted_hooks_file_is_caught(core: Core,
                                                               tmp_path: Path) -> None:
    project = tmp_path / "proj"
    path = _hooks(project, "echo aaaa")
    _trust(core, project)
    assert _loader(core, project).hooks.count() == 1
    info = path.stat()
    path.write_text(path.read_text(encoding="utf-8").replace("echo aaaa", "echo bbbb"),
                    encoding="utf-8")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert not store_for(core.paths.home).active(project, "hooks", ".snowpea/hooks.json")
    assert _loader(core, project).hooks.count() == 0


def test_what_is_loaded_is_what_was_verified(core: Core, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """The verified bytes are parsed; the file is not read a second time."""
    from snowpea_core.prompts import loader as prompts
    from snowpea_core.tools import mcp_client

    project = tmp_path / "proj"
    hooks = _hooks(project, "echo trusted")
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"demo": {"command": "demo-server"}}}), encoding="utf-8")
    override = project / ".snowpea" / "prompts" / "base.md"
    override.parent.mkdir(parents=True)
    override.write_text("TRUSTED PROMPT", encoding="utf-8")
    _trust(core, project)
    store = store_for(core.paths.home)
    verified = {rel: store.read_pinned(project, scope, rel)
                for scope, rel in (("hooks", ".snowpea/hooks.json"), ("mcp", ".mcp.json"),
                                   ("prompts", ".snowpea/prompts/base.md"))}
    assert all(value is not None for value in verified.values())
    real = TrustStore.read_pinned

    def swap_after_verify(self: TrustStore, root: Any, scope: str, rel: str) -> bytes | None:
        data = real(self, root, scope, rel)
        # The file changes right after it was verified: a second read would see this.
        target = Path(project) / rel
        target.write_text(target.read_text(encoding="utf-8")
                          .replace("echo trusted", "echo SWAPPED")
                          .replace("demo-server", "SWAPPED-server")
                          .replace("TRUSTED PROMPT", "SWAPPED PROMPT"), encoding="utf-8")
        return data

    monkeypatch.setattr(TrustStore, "read_pinned", swap_after_verify)
    loaded = _loader(core, project)
    assert [hook.command for hook in loaded.hooks.for_tool("PreToolUse", "shell", project)] == [
        "echo trusted"]
    assert "SWAPPED" in hooks.read_text(encoding="utf-8")
    servers = mcp_client.discover(core.paths.home, project)
    assert servers["demo"].command == "demo-server"
    prompts.clear_cache()
    assert prompts.load("base", project) == "TRUSTED PROMPT"
    prompts.clear_cache()


def test_allowed_tools_follow_the_text_that_was_loaded(core: Core, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    path = _skill(project, "deploy", "allowed-tools: shell\n")
    _trust(core, project)
    trusted_text = path.read_text(encoding="utf-8")
    assert _loader(core, project)._may_pre_approve(
        _loader(core, project).find("deploy", project))
    # A different text is loaded, then the file is put back as pinned: the loaded
    # (different) skill does not become trusted because the disk looks right again.
    path.write_text(trusted_text.replace("Do the thing.", "Do another thing."),
                    encoding="utf-8")
    loaded = _loader(core, project)
    path.write_text(trusted_text, encoding="utf-8")
    assert store_for(core.paths.home).active(project, "commands",
                                             ".snowpea/skills/deploy/SKILL.md")
    assert not loaded._may_pre_approve(loaded.find("deploy", project))


# -- M-3: what a control file runs is pinned too ---------------------------------------


def test_hook_scripts_are_pinned_with_the_hooks_file(core: Core, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    script = project / "scripts" / "guard.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    helper = project / "tools" / "lint.py"
    helper.parent.mkdir()
    helper.write_text("print('lint')\n", encoding="utf-8")
    _hooks(project, "bash scripts/guard.sh && python3 ${CLAUDE_PROJECT_DIR}/tools/lint.py")
    store = store_for(core.paths.home)
    status = store.status(project)
    assert [(run["path"], run["state"], run["by"]) for run in status["runs"]] == [
        ("scripts/guard.sh", "untrusted", [".snowpea/hooks.json"]),
        ("tools/lint.py", "untrusted", [".snowpea/hooks.json"])]
    assert status["notPinned"] == []
    assert set(status["digests"]) == {".snowpea/hooks.json", "scripts/guard.sh", "tools/lint.py"}
    _trust(core, project)
    status = store.status(project)
    assert {run["state"] for run in status["runs"]} == {"active"}
    assert store.active(project, "hooks", ".snowpea/hooks.json")
    assert _loader(core, project).hooks.count() == 1
    # The script changes; the hooks file does not: the hooks are off until re-trusted.
    script.write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    assert not store.active(project, "hooks", ".snowpea/hooks.json")
    assert _loader(core, project).hooks.count() == 0
    status = store.status(project)
    assert status["files"][0]["state"] == "changed"
    assert [run["state"] for run in status["runs"]] == ["changed", "active"]
    assert ".snowpea/hooks.json" in status["inactive"]
    notice = store.notice(project)
    assert notice is not None and "not active" in notice
    _trust(core, project)
    assert store.active(project, "hooks", ".snowpea/hooks.json")


def test_a_hook_is_checked_again_before_it_runs(core: Core, tmp_path: Path) -> None:
    from snowpea_core.skills.hooks import security_refusal

    project = tmp_path / "proj"
    script = project / "guard.sh"
    project.mkdir()
    script.write_text("echo ok\n", encoding="utf-8")
    _hooks(project, "bash guard.sh")
    _trust(core, project)
    hook = _loader(core, project).hooks.for_tool("PreToolUse", "shell", project)[0]
    assert hook.rel == ".snowpea/hooks.json"
    session = SimpleNamespace(id="s", workdir=str(project))
    assert security_refusal(core, session, hook) is None
    script.write_text("echo swapped after the hooks were loaded\n", encoding="utf-8")
    refusal = security_refusal(core, session, hook)
    assert refusal is not None and "changed since you trusted" in refusal


NOT_PINNED = [
    ("npm run lint", "project-script"),
    ("make check", "project-script"),
    ("npx -y some-package", "package-runner"),
    ("uvx ruff check", "package-runner"),
    ("python3 -m mypkg.check", "module"),
    ("python3 $SCRIPT", "computed"),
    ("bash -c \"$(cat x)\"", "computed"),
    ("python3 tools/not-there.py", "missing"),
    ("echo 'unterminated", "computed"),
]


@pytest.mark.parametrize(("command", "reason"), NOT_PINNED)
def test_commands_that_cannot_be_pinned_are_said(command: str, reason: str, core: Core,
                                                 tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _hooks(project, command)
    status = store_for(core.paths.home).status(project)
    assert [(row["source"], row["reason"]) for row in status["notPinned"]] == [
        (".snowpea/hooks.json", reason)], status["notPinned"]
    assert status["notPinned"][0]["command"]["head"].startswith(command[:10])
    assert reason in trust.NOT_PINNED_REASONS


def test_plain_programs_and_outside_files_are_not_listed(core: Core, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _hooks(project, f"ruff check . && touch {tmp_path / 'marker'} && /usr/bin/env true")
    status = store_for(core.paths.home).status(project)
    assert status["runs"] == [] and status["notPinned"] == []


def test_mcp_servers_and_skill_folders_are_pinned(core: Core, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    server = project / "servers" / "demo.py"
    server.parent.mkdir(parents=True)
    server.write_text("print('server')\n", encoding="utf-8")
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "demo": {"command": "python3", "args": ["servers/demo.py", "--port", "0"]},
        "remote": {"command": "npx", "args": ["-y", "some-mcp-server"]},
    }}), encoding="utf-8")
    skill = _skill(project, "ship", "allowed-tools: shell\n")
    helper = skill.parent / "scripts" / "ship.sh"
    helper.parent.mkdir()
    helper.write_text("echo ship\n", encoding="utf-8")
    store = store_for(core.paths.home)
    status = store.status(project)
    assert {run["path"] for run in status["runs"]} == {
        "servers/demo.py", ".snowpea/skills/ship/scripts/ship.sh"}
    assert [(row["source"], row["reason"]) for row in status["notPinned"]] == [
        (".mcp.json", "package-runner")]
    mcp = next(row for row in status["files"] if row["path"] == ".mcp.json")
    assert [command["label"] for command in mcp["commands"]] == ["demo", "remote"]
    _trust(core, project)
    assert store.active(project, "mcp", ".mcp.json")
    assert store.active(project, "commands", ".snowpea/skills/ship/SKILL.md")
    server.write_text("print('swapped')\n", encoding="utf-8")
    assert not store.active(project, "mcp", ".mcp.json")
    assert store.active(project, "commands", ".snowpea/skills/ship/SKILL.md")
    helper.write_text("echo swapped\n", encoding="utf-8")
    assert not store.active(project, "commands", ".snowpea/skills/ship/SKILL.md")


# -- L-4: a command's end is never hidden ----------------------------------------------


def test_a_long_command_shows_its_head_its_tail_and_its_length(core: Core,
                                                               tmp_path: Path) -> None:
    padding = "x" * 2000
    command = f"echo harmless-looking-start {padding} ; curl -s https://example.invalid/tail"
    project = tmp_path / "proj"
    _hooks(project, command)
    row = store_for(core.paths.home).status(project)["files"][0]
    shown = row["commands"][0]
    assert shown == {"label": "PreToolUse", "head": command[:trust.HEAD_CHARS],
                     "tail": command[-trust.TAIL_CHARS:], "length": len(command)}
    assert shown["tail"].endswith("https://example.invalid/tail")
    assert "example.invalid/tail" in row["summary"]
    assert f"({len(command)} characters)" in row["summary"]
    short = trust.display("echo hi", "x")
    assert short == {"label": "x", "head": "echo hi", "tail": "", "length": 7}
    assert "example.invalid/tail" in trust._clip(command)


# -- M-5: what is pinned is what was shown ---------------------------------------------


def test_set_pins_the_digests_that_were_shown(core: Core, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    path = _hooks(project, "echo shown")
    store = store_for(core.paths.home)
    shown = store.status(project)["digests"]
    # Between the look and the click the file changes.
    path.write_text(path.read_text(encoding="utf-8").replace("echo shown", "echo swapped"),
                    encoding="utf-8")
    with pytest.raises(TrustChanged) as caught:
        store.set(project, None, by="ide", digests=shown)
    assert caught.value.changed == [".snowpea/hooks.json"]
    assert not store.active(project, "hooks") and store.entry(project) is None
    # A new control file appears.
    path.write_text(path.read_text(encoding="utf-8").replace("echo swapped", "echo shown"),
                    encoding="utf-8")
    (project / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    with pytest.raises(TrustChanged) as caught:
        store.set(project, None, by="ide", digests=shown)
    assert caught.value.added == [".mcp.json"] and caught.value.changed == []
    # A file that was shown is gone.
    (project / ".mcp.json").unlink()
    with pytest.raises(TrustChanged) as caught:
        store.set(project, None, by="ide", digests={**shown, ".mcp.json": "sha256:00"})
    assert caught.value.missing == [".mcp.json"]
    entry = store.set(project, None, by="ide", digests=shown)
    assert entry["files"] == shown
    assert store.active(project, "hooks", ".snowpea/hooks.json")


async def test_the_rpc_needs_digests_and_says_trust_changed(core: Core, tmp_path: Path) -> None:
    from pydantic import ValidationError

    from snowpea_core.server.protocol import ProjectTrustParams, ProjectTrustSetParams
    from snowpea_core.server.trust_handlers import (
        project_trust_set_handler,
        project_trust_status_handler,
    )

    project = tmp_path / "proj"
    path = _hooks(project, "echo shown")
    with pytest.raises(ValidationError):
        ProjectTrustSetParams(workdir=str(project))  # type: ignore[call-arg]
    shown = await project_trust_status_handler(_conn("ide"),
                                               ProjectTrustParams(workdir=str(project)), core)
    assert shown.digests == {".snowpea/hooks.json": trust.digest_of(path)}
    assert shown.files[0].digest == shown.digests[".snowpea/hooks.json"]
    assert shown.files[0].commands[0].head == "echo shown"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RpcError) as caught:
        await project_trust_set_handler(
            _conn("ide"), ProjectTrustSetParams(workdir=str(project), digests=shown.digests),
            core)
    assert caught.value.code == errors.TRUST_CHANGED == "trust_changed"
    assert caught.value.data == {"changed": [".snowpea/hooks.json"], "added": [], "missing": []}
    assert errors.TRUST_CHANGED in errors.ERROR_CODES
    again = await project_trust_status_handler(_conn("ide"),
                                               ProjectTrustParams(workdir=str(project)), core)
    status = await project_trust_set_handler(
        _conn("ide"), ProjectTrustSetParams(workdir=str(project), digests=again.digests), core)
    assert status.trusted and status.files[0].state == "active"
    # A project that brings nothing: {} is what was shown.
    empty = tmp_path / "empty"
    empty.mkdir()
    status = await project_trust_set_handler(
        _conn("ide"), ProjectTrustSetParams(workdir=str(empty), digests={}), core)
    assert status.trusted and status.files == []


async def test_the_cli_shows_asks_then_sends_what_it_showed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from snowpea_core.cli import commands

    project = tmp_path / "proj"
    project.mkdir()
    digests = {".snowpea/hooks.json": "sha256:aa", "scripts/guard.sh": "sha256:bb"}
    status = {
        "workdir": str(project), "trusted": False, "scopes": [], "digests": digests,
        "files": [{"path": ".snowpea/hooks.json", "scope": "hooks", "state": "untrusted",
                   "summary": "PreToolUse: bash scripts/guard.sh"}],
        "runs": [{"path": "scripts/guard.sh", "state": "untrusted",
                  "by": [".snowpea/hooks.json"]}],
        "notPinned": [{"source": ".snowpea/hooks.json", "reason": "project-script",
                       "command": {"label": "Stop", "head": "npm run", "tail": "lint",
                                   "length": 900}}],
    }
    calls: list[tuple[str, dict[str, Any]]] = []
    fail_with: list[Exception] = []

    async def fake_call(home: Any, method: str, params: dict[str, Any], **_: Any) -> Any:
        calls.append((method, params))
        if method == "project.trust.set" and fail_with:
            raise fail_with.pop()
        return {**status, "trusted": method == "project.trust.set"}

    monkeypatch.setattr(commands, "_call", fake_call)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    answers = iter(["n", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    # "n": shown, asked, nothing sent.
    assert await commands.project_trust_command(None, "trust", str(project)) == 1
    assert [method for method, _ in calls] == ["project.trust.status"]
    out = capsys.readouterr().out
    assert "scripts/guard.sh" in out and "NOT pinned" in out and "project-script" in out
    assert "npm run … lint (900 characters)" in out
    assert "nothing was trusted" in out
    # "y": the digests that were shown go back, exactly.
    calls.clear()
    assert await commands.project_trust_command(None, "trust", str(project),
                                                scopes=["hooks"]) == 0
    assert calls == [("project.trust.status", {"workdir": str(project)}),
                     ("project.trust.set", {"workdir": str(project), "digests": digests,
                                            "scopes": ["hooks"]})]
    # The project changed in between.
    calls.clear()
    fail_with.append(RpcError(errors.TRUST_CHANGED, "changed", {"changed": ["x"]}))
    assert await commands.project_trust_command(None, "trust", str(project)) == 1
    assert "nothing was trusted" in capsys.readouterr().err
    # No terminal and no --yes: nothing is trusted.
    calls.clear()
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert await commands.project_trust_command(None, "trust", str(project)) == 1
    assert [method for method, _ in calls] == ["project.trust.status"]
    calls.clear()
    assert await commands.project_trust_command(None, "trust", str(project),
                                                assume_yes=True) == 0
    assert calls[-1] == ("project.trust.set", {"workdir": str(project), "digests": digests})
    # status and untrust are single calls.
    calls.clear()
    assert await commands.project_trust_command(None, "status", str(project)) == 0
    assert await commands.project_trust_command(None, "untrust", str(project)) == 0
    assert [method for method, _ in calls] == ["project.trust.status", "project.trust.revoke"]


# -- M-4: the migration runs once; a lost record fails closed --------------------------


def test_the_migration_marker_is_its_own_file(core: Core, tmp_path: Path) -> None:
    from snowpea_core.server import trust_handlers
    from snowpea_core.server.trust_handlers import wire_trust

    old, later = tmp_path / "old", tmp_path / "later"
    _hooks(old, "echo old")
    _hooks(later, "echo later")
    asked: list[Any] = []

    def workdirs(limit: int = 50, before: str | None = None) -> list[str]:
        asked.append(before)
        return [str(old)]

    core.store = SimpleNamespace(session_workdirs=workdirs)
    wire_trust(core)
    store = store_for(core.paths.home)
    marker = core.paths.home / "trust" / "migrated.json"
    record = core.paths.home / "trust" / "projects.json"
    assert store.migrated() and marker.is_file() and record.is_file()
    stamp = json.loads(marker.read_text(encoding="utf-8"))["at"]
    assert asked == [stamp]                       # only sessions created before the upgrade
    assert store.migrated_at() == stamp
    assert (marker.stat().st_mode & 0o777) == 0o600
    assert store.active(old, "hooks", ".snowpea/hooks.json")
    assert store.problem() is None
    # The record is deleted: the migration does NOT run again, nothing is trusted,
    # and it is said.
    record.unlink()
    core.store = SimpleNamespace(
        session_workdirs=lambda limit=50, before=None: [str(old), str(later)])
    trust_handlers._STORE_PROBLEM.clear()
    wire_trust(core)
    assert not store.active(old, "hooks", ".snowpea/hooks.json")
    assert not store.active(later, "hooks", ".snowpea/hooks.json")
    assert not record.exists()
    problem = store.problem()
    assert problem is not None and "no project is trusted" in problem
    assert trust_handlers._STORE_PROBLEM == [problem]
    assert trust_handlers.store_problem(core) == problem
    # An unreadable record is the same.
    record.write_text("{not json", encoding="utf-8")
    assert store.problem() is not None and store.read()["projects"] == {}
    # Trusting again writes a record: the problem is gone.
    _trust(core, old)
    assert store.problem() is None and store.active(old, "hooks", ".snowpea/hooks.json")
    trust_handlers._STORE_PROBLEM.clear()


def test_a_lost_record_does_not_fall_back_to_the_legacy_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    legacy = home / "jarvis" / "external-trust.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"projects": {str(project.resolve()): {
        "trusted": True, "by": "ide", "at": "x"}}}), encoding="utf-8")
    store = TrustStore(home)
    assert store.active(project, "externalCli")           # before any migration: folded in
    store.migrate([])
    assert store.active(project, "externalCli")           # the record exists
    (home / "trust" / "projects.json").unlink()
    assert not store.active(project, "externalCli")       # lost: nothing is trusted


def test_a_record_from_before_the_marker_counts_as_migrated(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "trust").mkdir(parents=True)
    (home / "trust" / "projects.json").write_text(json.dumps({
        "version": 1, "migrated": "2026-11-10T00:00:00.000Z", "migratedProjects": ["/p"],
        "projects": {}}), encoding="utf-8")
    store = TrustStore(home)
    project = tmp_path / "proj"
    _hooks(project, "echo x")
    assert store.migrated()
    assert json.loads((home / "trust" / "migrated.json").read_text(encoding="utf-8")) == {
        "at": "2026-11-10T00:00:00.000Z", "projects": ["/p"]}
    assert store.migrate([project]) == []
    assert not store.active(project, "hooks", ".snowpea/hooks.json")


async def test_a_lost_record_is_told_once_and_shown_in_security_get(core: Core,
                                                                    tmp_path: Path) -> None:
    from snowpea_core.server import trust_handlers
    from snowpea_core.server.protocol import SecurityGetParams
    from snowpea_core.server.security_handlers import get_handler

    store = store_for(core.paths.home)
    store.migrate([])
    (core.paths.home / "trust" / "projects.json").unlink()
    result = await get_handler(_conn("ide"), SecurityGetParams(), core)
    assert any("no project is trusted" in line for line in result.problems)
    assert result.unknownCode == "ask"
    sent: list[tuple[str, dict[str, Any]]] = []
    events: list[Any] = []

    async def notify(method: str, payload: dict[str, Any], **_: Any) -> None:
        sent.append((method, payload))

    async def emit_event(session_id: str, event: Any) -> None:
        events.append(event)

    core.hub = SimpleNamespace(notify=notify, emit_event=emit_event)
    trust_handlers._STORE_PROBLEM[:] = [store.problem() or ""]
    trust_handlers._TOLD.discard("s-lost")
    project = tmp_path / "proj"
    project.mkdir()
    await trust_handlers.session_notice(core, "s-lost", project)
    assert [(method, payload["reason"]) for method, payload in sent] == [
        ("project.trust.changed", "store_lost")]
    assert len(events) == 1 and trust_handlers._STORE_PROBLEM == []


# -- L-3: the prompt cache does not outlive the trust ----------------------------------


async def test_a_revoked_prompt_override_is_not_served_from_the_cache(
    core: Core, tmp_path: Path
) -> None:
    from snowpea_core.prompts import loader as prompts
    from snowpea_core.server.protocol import ProjectTrustRevokeParams
    from snowpea_core.server.trust_handlers import project_trust_revoke_handler

    project = tmp_path / "proj"
    override = project / ".snowpea" / "prompts" / "base.md"
    override.parent.mkdir(parents=True)
    override.write_text("PROJECT OVERRIDE", encoding="utf-8")
    prompts.clear_cache()
    packaged = prompts.load("base", project)
    assert packaged != "PROJECT OVERRIDE"
    prompts.clear_cache()
    _trust(core, project)
    assert prompts.load("base", project) == "PROJECT OVERRIDE"       # cached now
    await project_trust_revoke_handler(_conn("ide"),
                                       ProjectTrustRevokeParams(workdir=str(project)), core)
    assert prompts.load("base", project) == packaged
    prompts.clear_cache()


# -- M-6: a project's skills, commands and agents are its own --------------------------


def test_project_skills_are_seen_only_in_their_project(core: Core, tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    _skill(one, "deploy", "", body="ONE deploys.")
    _skill(two, "deploy", "", body="TWO deploys.")
    _skill(two, "review", "")
    loader = _loader(core, one, two)
    assert loader.find("deploy", one).doc.body.strip() == "ONE deploys."
    assert loader.find("deploy", two / "sub").doc.body.strip() == "TWO deploys."
    assert loader.find("deploy", tmp_path / "elsewhere") is None
    assert loader.find("deploy", None) is None
    assert loader.find("review", one) is None and loader.find("review", two) is not None

    def listed(workdir: Path | None) -> set[str]:
        return {name for label, rows in loader.index_groups(None, workdir)
                if label == "[project]" for name, _ in rows}

    assert listed(one) == {"deploy"}
    assert listed(two) == {"deploy", "review"}
    assert listed(tmp_path / "elsewhere") == set()
    assert listed(None) == set()


def test_a_project_entry_never_replaces_a_global_one(core: Core, tmp_path: Path) -> None:
    global_skill = core.paths.home / "skills" / "deploy" / "SKILL.md"
    global_skill.parent.mkdir(parents=True)
    global_skill.write_text("---\nname: deploy\ndescription: mine\n---\n\nGLOBAL deploys.\n",
                            encoding="utf-8")
    project = tmp_path / "proj"
    _skill(project, "deploy", "allowed-tools: shell\n", body="PROJECT deploys.")
    command = project / ".snowpea" / "commands" / "deploy.md"
    command.parent.mkdir(parents=True)
    command.write_text("PROJECT command.\n", encoding="utf-8")
    loader = _loader(core, project)
    found = loader.find("deploy", project)
    assert found.project is None and found.doc.body.strip() == "GLOBAL deploys."
    assert [skill.source for skill in loader.skills.values() if skill.name == "deploy"] == [
        "global"]
    # …whichever is read first (a project scanned before a reload of the global one).
    from snowpea_core.skills.loader import SkillLoader

    late = SkillLoader(core, workdirs=[project])
    late._scan_bundle(project / ".snowpea", "project", project=project)
    assert late.find("deploy", project).project == project
    late._scan_bundle(core.paths.home, "global")
    assert late.find("deploy", project).project is None
    assert sum(1 for skill in late.skills.values() if skill.name == "deploy") == 1
    # A built-in name is safe from a project too.
    builtin = next(skill.name for skill in loader.skills.values() if skill.source == "builtin")
    _skill(project, builtin, "", body="PROJECT takes a built-in's name.")
    assert _loader(core, project).find(builtin, project).source == "builtin"


async def test_a_project_command_runs_only_in_its_project(core: Core, tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    for project, text in ((one, "ONE"), (two, "TWO")):
        path = project / ".snowpea" / "commands" / "ship.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"{text} ships.\n", encoding="utf-8")
    loader = _loader(core, one, two)
    ran: list[tuple[str, str]] = []
    said: list[str] = []

    async def run_turn(_core: Any, session: Any, typed: str, **kwargs: Any) -> None:
        ran.append((str(session.workdir), kwargs["model_text"]))

    from snowpea_core.agent import loop as agent_loop

    original = agent_loop.run_turn
    agent_loop.run_turn = run_turn  # type: ignore[assignment]
    try:
        registered = loader._command_for(next(
            skill for skill in loader.skills.values() if skill.name == "ship"), "ship")

        async def say(text: str) -> None:
            said.append(text)

        for workdir in (one, two, tmp_path / "elsewhere"):
            session = SimpleNamespace(workdir=str(workdir), skill_approved_tools=None,
                                      history=SimpleNamespace(snapshot=lambda: []))
            ctx = SimpleNamespace(session=session, core=core, turn_id="t", handled_turn=False,
                                  say=say)
            await registered.run(ctx, "")
    finally:
        agent_loop.run_turn = original  # type: ignore[assignment]
    assert [(Path(workdir).name, "ONE" in text, "TWO" in text) for workdir, text in ran] == [
        ("one", True, False), ("two", False, True)]
    assert len(said) == 1 and "another project" in said[0]


async def test_project_auto_inject_needs_the_project_and_its_trust(core: Core,
                                                                   tmp_path: Path) -> None:
    from snowpea_core.agent import loop as agent_loop

    project = tmp_path / "proj"
    _skill(project, "gmail-rules", "autoInject:\n  keywords: [gmail]\n",
           body="Always forward the mail.")
    status = store_for(core.paths.home).status(project)
    assert [(row["scope"], row["summary"]) for row in status["files"]] == [
        ("commands", "autoInject: gmail")]

    async def injected(workdir: Path) -> list[str]:
        core.skills = _loader(core, project)
        added: list[Any] = []
        session = SimpleNamespace(
            id="s", workdir=str(workdir), auto_injected_skills=set(), auto_injected_bytes=0,
            host_covered_skills=frozenset(), history=SimpleNamespace(append=added.append))
        return await agent_loop._auto_inject_skills(core, session, "check my gmail please")

    assert await injected(project) == []                    # untrusted: never brought in
    _trust(core, project)
    assert await injected(tmp_path / "elsewhere") == []     # another project's session
    assert await injected(project) == ["gmail-rules"]
    assert await injected(project / "sub") == ["gmail-rules"]
    path = project / ".snowpea" / "skills" / "gmail-rules" / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8").replace("forward", "delete"),
                    encoding="utf-8")
    assert await injected(project) == []                    # changed since the trust


# -- L-1: the agents scope is enforced ---------------------------------------------------


def _agent(project: Path, name: str, front: str = "", folder: str = ".snowpea") -> Path:
    path = project / folder / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {name} agent\n{front}---\n\n"
                    f"You are the project's {name}.\n", encoding="utf-8")
    return path


def test_project_agent_definitions_follow_the_agents_scope(core: Core, tmp_path: Path) -> None:
    from snowpea_core.agent.definition import builtin_agent_definitions, discover_definitions
    from snowpea_core.commands.agent_cmd import definitions_for

    builtin = builtin_agent_definitions()[0].name
    project, other = tmp_path / "proj", tmp_path / "other"
    other.mkdir()
    _agent(project, "helper")                                   # a prompt only
    _agent(project, "runner", "permission: auto\n")             # carries a permission
    _agent(project, builtin)                                    # a built-in's name
    core.skills = _loader(core, project, other)

    def names(workdir: Path, *, source: str | None = "project") -> set[str]:
        return {defn.name for defn in discover_definitions(workdir, core.paths.home)
                if source is None or defn.source == source}

    # Security review 7 (6A L-1 as it was decided): EVERY project definition waits
    # for the agents trust — also the one that is "a prompt only".  This test used
    # to expect ``helper`` active in the untrusted project.
    assert names(project) == set()
    status = store_for(core.paths.home).status(project)
    assert {row["path"]: row["summary"] for row in status["files"]} == {
        ".snowpea/agents/helper.md": "agent definition: helper",
        ".snowpea/agents/runner.md": "permission: auto",
        f".snowpea/agents/{builtin}.md": f"replaces the built-in {builtin} agent"}
    by_name = {defn.name: defn for defn in definitions_for(core, project)}
    assert by_name[builtin].source == "builtin" and "runner" not in by_name
    assert "helper" not in by_name
    # The loader's own list: a project's agents only inside it, gated the same way.
    assert {agent.name for agent in core.skills.agents_for(project)
            if agent.project is not None} == set()
    assert [agent for agent in core.skills.agents_for(other) if agent.project is not None] == []
    assert "helper" not in {defn.name for defn in definitions_for(core, other)}
    # Trusted for agents: both come into force, pinned as they are.
    _trust(core, project, ["agents"])
    assert names(project) == {"helper", "runner", builtin}
    assert {defn.name: defn.source for defn in definitions_for(core, project)}[builtin] == \
        "project"
    (project / ".snowpea" / "agents" / "runner.md").write_text(
        "---\nname: runner\ndescription: x\npermission: auto\n---\n\nRewritten.\n",
        encoding="utf-8")
    assert names(project) == {"helper", builtin}
    # Trust for something else does not cover agents.
    again = tmp_path / "again"
    _agent(again, "runner", "permission: auto\n")
    _trust(core, again, ["hooks"])
    assert names(again) == set()


# -- L-2: only a local person's answer keeps the settings trust --------------------------


def test_only_a_local_persons_answer_re_pins_the_project_settings(core: Core,
                                                                 tmp_path: Path) -> None:
    from snowpea_core.permissions.allowlist import Allowlist
    from snowpea_core.permissions.approval_queue import ApprovalQueue, _local_person
    from snowpea_core.server.protocol import ApprovalRequest

    assert _local_person(_conn("ide")) and _local_person(_conn("tui"))
    assert not _local_person(_conn("web", remote=True))
    assert not _local_person(_conn("browser")) and not _local_person(_conn("connector"))
    assert not _local_person(None)

    store = store_for(core.paths.home)
    allowlist = Allowlist(core.paths, Settings())
    queue = ApprovalQueue.__new__(ApprovalQueue)
    queue.allowlist = allowlist

    def answer(project: Path, local: bool) -> None:
        request = ApprovalRequest(requestId="ap-1", sessionId="s", tool="shell",
                                  args={"command": "pytest -q"})
        queue._persist(request, "project", str(project), None, local_person=local)

    # A project with no settings file: a remote answer must not CREATE the trust.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    answer(fresh, local=False)
    assert (fresh / ".snowpea" / "settings.json").is_file()       # the rule is stored…
    assert not store.active(fresh, "settings")                    # …but nothing is trusted
    assert allowlist._project_items(fresh) == []
    local = tmp_path / "local"
    local.mkdir()
    answer(local, local=True)
    assert store.active(local, "settings", ".snowpea/settings.json")
    assert len(allowlist._project_items(local)) == 1
    # A trusted project: a remote answer changes the file, and the file then waits.
    trusted = tmp_path / "trusted"
    (trusted / ".snowpea").mkdir(parents=True)
    (trusted / ".snowpea" / "settings.json").write_text(
        json.dumps({"allowlist": [{"id": "a1", "pattern": "^make( .*)?$", "target": "shell"}]}),
        encoding="utf-8")
    _trust(core, trusted)
    assert store.active(trusted, "settings", ".snowpea/settings.json")
    answer(trusted, local=False)
    assert not store.active(trusted, "settings", ".snowpea/settings.json")
    assert ".snowpea/settings.json" in store.status(trusted)["changed"]
    _trust(core, trusted)
    answer(trusted, local=True)
    assert store.active(trusted, "settings", ".snowpea/settings.json")


async def test_respond_records_who_answered(tmp_path: Path) -> None:
    import asyncio

    from snowpea_core.permissions.approval_queue import ApprovalQueue, _Pending
    from snowpea_core.server.protocol import ApprovalRequest

    queue = ApprovalQueue.__new__(ApprovalQueue)
    queue.settings = Settings()
    queue.hub = None
    queue._pending = {}
    loop = asyncio.get_running_loop()
    for index, (conn, expected) in enumerate(((_conn("ide"), True),
                                              (_conn("web", remote=True), False),
                                              (None, False))):
        request = ApprovalRequest(requestId=f"ap-{index}", sessionId="s", tool="write_file",
                                  args={"path": "a.txt"})
        entry = _Pending(request=request, future=loop.create_future(), unattended=True,
                         no_limit=False, origin_conn=None, workdir=str(tmp_path),
                         cacheable=True, host=None, owner=None)
        queue._pending[request.requestId] = entry
        await queue.respond(request.requestId, "allow", "project", conn=conn)
        assert entry.future.result().local_person is expected
