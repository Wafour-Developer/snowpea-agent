"""Security review 8, part A: H8-2 (one atomic no-follow writer, loaders refuse
links), M8-1 (an agent definition is pinned as rendered, and only for a local
person's own request) and M8-3 (the written-files record never forgets a running
session).

Links and files are made ONLY inside pytest's ``tmp_path`` with a throwaway home
there; nothing runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import snowpea_core
from snowpea_core.config.credentials import CredentialStore
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.security import written
from snowpea_core.util import atomic
from snowpea_core.util.atomic import UnsafePath, read_owned, write_atomic

pytestmark = [pytest.mark.real_project_trust]


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths.create(tmp_path / "home")


def _bait(tmp_path: Path, name: str = "stolen") -> Path:
    bait = tmp_path / name
    bait.write_text("untouched", encoding="utf-8")
    return bait


# -- the helper --------------------------------------------------------------------


def test_the_helper_never_writes_through_a_link(tmp_path: Path) -> None:
    target = tmp_path / "file.json"
    bait = _bait(tmp_path)
    target.symlink_to(bait)
    with pytest.raises(UnsafePath):
        write_atomic(target, "secret")
    assert bait.read_text(encoding="utf-8") == "untouched"
    target.unlink()
    info = write_atomic(target, "secret")
    assert target.read_text(encoding="utf-8") == "secret" and info.st_size == 6
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["file.json", "stolen"]  # no temp left


def test_reading_an_authority_file_refuses_a_link(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert read_owned(real) == b"{}"
    with pytest.raises(UnsafePath):
        read_owned(link)
    with pytest.raises(FileNotFoundError):
        read_owned(tmp_path / "missing")


def test_appending_to_a_log_never_follows_a_link(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    bait = _bait(tmp_path)
    log.symlink_to(bait)
    with pytest.raises(OSError):
        atomic.append_text(log, "line\n")
    assert bait.read_text(encoding="utf-8") == "untouched"


# -- the writers that used a fixed ``name.tmp`` ---------------------------------------


def test_credentials_are_never_written_to_a_planted_temp_link(paths: Paths,
                                                            tmp_path: Path) -> None:
    bait = _bait(tmp_path)
    (paths.home / "credentials.json.tmp").symlink_to(bait)
    CredentialStore(paths).set("tg_main", "123:ABC")
    assert bait.read_text(encoding="utf-8") == "untouched"
    assert json.loads(paths.credentials_json.read_text(encoding="utf-8")) == {"tg_main": "123:ABC"}
    assert oct(paths.credentials_json.stat().st_mode & 0o777) == "0o600"


def test_a_linked_credentials_file_is_neither_read_nor_written(paths: Paths,
                                                               tmp_path: Path) -> None:
    elsewhere = tmp_path / "theirs.json"
    elsewhere.write_text(json.dumps({"tg_main": "planted"}), encoding="utf-8")
    paths.credentials_json.symlink_to(elsewhere)
    store = CredentialStore(paths)
    assert store.load() == {}
    with pytest.raises(UnsafePath):
        store.set("x", "y")
    assert json.loads(elsewhere.read_text(encoding="utf-8")) == {"tg_main": "planted"}


def test_settings_are_never_written_to_a_planted_temp_link(paths: Paths,
                                                         tmp_path: Path) -> None:
    bait = _bait(tmp_path, "s.json")
    (paths.home / "settings.json.tmp").symlink_to(bait)
    Settings().save(paths)
    assert bait.read_text(encoding="utf-8") == "untouched"
    assert not paths.settings_json.is_symlink()
    assert oct(paths.settings_json.stat().st_mode & 0o777) == "0o600"


def test_a_linked_settings_file_that_leads_nowhere_is_not_taken(paths: Paths,
                                                               tmp_path: Path) -> None:
    # Security review 9, F2: a link to the user's own regular file is followed
    # (tests/test_security_review9.py); a link to nothing or a folder is not.
    paths.settings_json.symlink_to(tmp_path / "missing.json")
    assert Settings.load(paths).degraded_sections() == ["*"]
    from snowpea_core.config.settings import SettingsUnreadable

    with pytest.raises(SettingsUnreadable):
        Settings().save(paths)
    assert not (tmp_path / "missing.json").exists()


def test_project_settings_never_follow_a_planted_link(tmp_path: Path) -> None:
    from snowpea_core.config.project import ProjectSettings

    project = tmp_path / "proj"
    (project / ".snowpea").mkdir(parents=True)
    bait = _bait(tmp_path)
    (project / ".snowpea" / "settings.json.tmp").symlink_to(bait)
    ProjectSettings().save(project)
    assert bait.read_text(encoding="utf-8") == "untouched"
    (project / ".snowpea" / "settings.json").unlink()
    (project / ".snowpea" / "settings.json").symlink_to(bait)
    with pytest.raises(UnsafePath):
        ProjectSettings().save(project)
    assert bait.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("name", ["gateway-chats.json", "token", "daemon.json"])
def test_other_home_writers_never_follow_a_planted_temp_link(paths: Paths, tmp_path: Path,
                                                             name: str) -> None:
    from snowpea_core.gateway.chat import ChatSessionMemory
    from snowpea_core.server.advert import write_advert
    from snowpea_core.server.auth import write_token

    bait = _bait(tmp_path)
    (paths.home / f"{name}.tmp").symlink_to(bait)
    if name == "gateway-chats.json":
        ChatSessionMemory(paths.home / name).remember("b", "c", "s")
    elif name == "token":
        write_token(paths, "t0ken")
    else:
        write_advert(paths.daemon_json, {"port": 1, "token": "x"})
    assert bait.read_text(encoding="utf-8") == "untouched"
    assert (paths.home / name).is_file() and not (paths.home / name).is_symlink()


def test_linked_authority_files_are_not_loaded(paths: Paths, tmp_path: Path) -> None:
    from snowpea_core.cli.daemon_client import read_daemon_json
    from snowpea_core.skills.hooks import HookRegistry
    from snowpea_core.tools.mcp_client import _read_config

    planted = tmp_path / "planted.json"
    planted.write_text(json.dumps({
        "mcpServers": {"x": {"command": "planted"}},
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "planted"}]}]},
        "port": 1, "token": "x", "pid": 1, "version": "0", "protocol": "1"}), encoding="utf-8")
    mcp = paths.home / ".mcp.json"
    mcp.symlink_to(planted)
    assert _read_config(mcp, "global") == {}
    hooks = paths.home / "hooks.json"
    hooks.symlink_to(planted)
    assert HookRegistry().load_file(hooks) == 0
    paths.daemon_json.symlink_to(planted)
    assert read_daemon_json(paths.home) is None


#: Modules whose writes under the home or a project's ``.snowpea`` go through the helper.
_CONVERTED = (
    "config/credentials.py", "config/settings.py", "config/project.py", "gateway/chat.py",
    "gateway/router.py", "tools/mcp_oauth.py", "tools/mcp_config.py", "server/auth.py",
    "server/advert.py", "server/host_handlers.py", "update.py", "jarvis/presets.py",
    "jarvis/intake.py", "jarvis/projects.py", "jarvis/escalations.py", "external/proc.py",
    "attachments/store.py", "agent/plan_store.py", "agent/team_guide.py",
    "agent/definition.py", "providers/models.py", "providers/context_windows.py",
    "providers/vision.py", "lsp/servers.py", "extensions/manager.py", "skills/marketplace.py",
    "skills/generate.py", "commands/skill_cmd.py", "server/skill_handlers.py",
    "security/written.py", "memory/mirror.py", "session/checkpoints.py",
    "tools/output_spill.py", "tools/tool_content_store.py", "commands/ralph.py",
    "audio/stt_models.py", "audio/install.py", "server/audio_handlers.py", "audio/tts.py",
    "permissions/approval_queue.py", "security/enforce.py", "machine.py",
    "trust/projects.py", "security/tamper.py", "cli/daemon_client.py",
)
#: What may stay: a write into the WORKING TREE restoring a checkpoint (the
#: project's own files, not ``.snowpea``), and a process's stdin.
_ALLOWED = {("session/checkpoints.py", "target.write_bytes(content)"),
            ("session/checkpoints.py", "await session.backend.write_bytes(rel, content)"),
            ("external/proc.py", 'await self.write_bytes((text + "\\n").encode("utf-8"))')}


def test_the_converted_writers_have_no_direct_write_left() -> None:
    root = Path(snowpea_core.__file__).parent
    pattern = re.compile(r"[\w\)\]]\.(?:write_text|write_bytes)\(|\.open\(\s*[\"'][wa]|"
                         r"os\.open\([^)]*O_TRUNC|[\w\)]\.replace\(tmp|tmp\.replace\(")
    found = []
    for rel in _CONVERTED:
        for line in (root / rel).read_text(encoding="utf-8").splitlines():
            if pattern.search(line) and (rel, line.strip()) not in _ALLOWED:
                found.append(f"{rel}: {line.strip()}")
    assert found == []


# -- M8-1: /agent create ---------------------------------------------------------------


def _session(project: Path, origin: str, who: Any) -> Any:
    return SimpleNamespace(workdir=str(project), provider="p", model="m",
                           turn_origins={"t1": origin}, turn_callers={"t1": who})


def _who(kind: str, origin: str | None = None) -> Any:
    from snowpea_core.server.person_gate import CallerSnapshot

    return CallerSnapshot(kind=kind, origin=origin)  # type: ignore[arg-type]


@pytest.mark.parametrize(("origin", "kind", "pins"), [
    ("human", "person", True),
    ("agent", "person", False),      # ask_user.queue_command
    ("jarvis", "person", False),     # Jarvis session_prompt
    ("schedule", "remote_login", False),
    ("member", "person", False),
    ("system", "person", False),
    ("human", "remote_login", False),
    ("human", "none", False),
])
def test_only_a_local_persons_own_command_line_pins(tmp_path: Path, origin: str, kind: str,
                                                    pins: bool,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.agent.definition import AgentDefinition
    from snowpea_core.commands import agent_cmd

    project = tmp_path / "proj"
    project.mkdir()
    pinned: list[bool] = []

    async def generate(_provider: Any, _description: str) -> Any:
        return AgentDefinition(name="helper", description="d", prompt="Be brief.")

    def write(defn: Any, workdir: Any, *, pin: bool = False) -> Path:
        pinned.append(pin)
        return Path(workdir) / "x.md"

    async def nothing(*_: Any) -> None:
        return None

    monkeypatch.setattr(agent_cmd, "generate_definition", generate)
    monkeypatch.setattr(agent_cmd, "write_definition", write)
    monkeypatch.setattr(agent_cmd, "register_and_reload", nothing)
    said: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    who = _who(kind)
    ctx = SimpleNamespace(core=SimpleNamespace(providers=SimpleNamespace(get=lambda *a: None)),
                          session=_session(project, origin, who), turn_id="t1", who=who,
                          say=say, emit=nothing)
    asyncio.run(agent_cmd.cmd_agent(ctx, 'create "a helper"'))
    assert pinned == [pins]
    assert ("not active until you trust" in said[-1]) is (not pins)


def test_the_pin_is_the_digest_of_the_rendered_bytes(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.agent.definition import AgentDefinition, render_agent_md, write_definition
    from snowpea_core.trust import projects as trust
    from snowpea_core.trust.projects import store_for

    home = tmp_path / "home"
    trust.set_default_home(home)
    project = tmp_path / "proj"
    project.mkdir()
    defn = AgentDefinition(name="mine", description="mine", prompt="Be brief.")
    rendered = render_agent_md(defn)
    real = atomic.write_atomic

    def racing(path: Any, data: Any, **kw: Any) -> Any:
        info = real(path, data, **kw)
        if str(path).endswith("mine.md"):
            Path(path).write_text(rendered.replace("Be brief.", "Planted."), encoding="utf-8")
        return info

    monkeypatch.setattr(atomic, "write_atomic", racing)
    write_definition(defn, project, pin=True)
    entry = store_for(home).entry(project) or {}
    import hashlib

    assert entry["files"][".snowpea/agents/mine.md"] == (
        "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest())
    # What is on disk now is not what was pinned: not active.
    assert not store_for(home).active(project, "agents", ".snowpea/agents/mine.md")


def test_no_path_but_a_persons_own_reaches_the_pin() -> None:
    """Every caller passes ``pin`` on from ``person_typed`` or a local-person check;
    the agent's and Jarvis's own ways to start a command carry their origin."""
    root = Path(snowpea_core.__file__).parent
    calls = re.compile(r"\b(?:write_definition|create_definition|rename_definition)\(([^)]*)\)")
    sources = {}
    for path in root.rglob("*.py"):
        if "vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in calls.finditer(text):
            args = match.group(1)
            if "pin=" in args:
                assert re.search(r"pin=pin\b", args), (path, args)
                sources.setdefault(str(path.relative_to(root)), text)
    assert set(sources) == {"commands/agent_cmd.py", "server/agent_handlers.py",
                            "agent/named.py"}
    assert "pin = person_typed(ctx)" in sources["commands/agent_cmd.py"]
    assert "pin = is_person(conn, local=True)" in sources["server/agent_handlers.py"]
    assert 'origin="agent"' in (root / "tools" / "ask_user.py").read_text(encoding="utf-8")
    assert 'origin="jarvis"' in (root / "jarvis" / "tools.py").read_text(encoding="utf-8")


# -- M8-3: the written-files record ---------------------------------------------------------


def test_a_running_sessions_paths_are_never_forgotten(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    written.reset()
    home = tmp_path / "home"
    record = home / "security" / "written.log"
    record.parent.mkdir(parents=True)
    old = "".join(f"old\t/x/old-{index}\n" for index in range(30))
    record.write_text(old, encoding="utf-8")
    monkeypatch.setattr(written, "MAX_PATHS", 40)
    written.set_home(home)                         # an earlier daemon's session, not running
    mine = [str(tmp_path / f"mine-{index}") for index in range(35)]
    written.note(mine[:20], "now")
    written.note(mine[20:], "now")
    assert all(written.contains(path) for path in mine)
    assert not written.contains("/x/old-0")        # the old session went, whole
    lines = record.read_text(encoding="utf-8").splitlines()
    assert lines and all(line.startswith("now\t") for line in lines)
    written.reset()


def test_a_session_that_writes_too_much_counts_everything_recent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written.reset()
    monkeypatch.setattr(written, "MAX_PER_SESSION", 5)
    before = tmp_path / "before.sh"
    before.write_text("old\n", encoding="utf-8")
    os.utime(before, (1_000_000_000, 1_000_000_000))
    written.note([str(tmp_path / f"f{index}") for index in range(6)], "busy")
    later = tmp_path / "later.sh"
    later.write_text("new\n", encoding="utf-8")
    assert written.contains(later)                 # not listed, but changed since: the agent's
    assert written.contains(tmp_path / "f0")
    assert written.under(tmp_path)
    written.reset()
