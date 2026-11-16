"""Security review 8, part B — Jarvis's cause, marks a remote login leaves, the
project ``.mcp.json``, ``fs.write`` against the security lists, and the Lows.

* M-2: a Jarvis turn carries the snapshot of whoever caused it; the tools that
  start work in other sessions pass it on (``web.remote.maxMode`` and job
  tagging hold); wakes carry the job's creator or Jarvis's own.
* M-3: ``savedBy``/``.saved-by`` marks and ``web.remote.status.remoteLeft``.
* M-4: ``mcp.add|update|remove {scope: "project"}`` is no remote login's.
* M-5: ``fs.write`` meets the security lists like the file tools; the
  ``essential`` preset covers what runs at a login or in a new shell.
* Low: built-in names fold case and width; failed jobs and settings writes
  answer an id; ``memory.delete`` stays the browser host's (it deletes its own).

The command strings ("prog") are DATA: nothing here starts a process.  The
login-time files are written under a temporary ``$HOME`` only.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent import loop as agent_loop
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.jarvis import tools as jt
from snowpea_core.server import errors, fs_handlers, mcp_handlers, settings_gate, skill_gate
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import RpcError
from snowpea_core.server.person_gate import NOBODY, CallerSnapshot, snapshot
from snowpea_core.server.protocol import (
    FsWriteParams,
    McpAddParams,
    McpEntryFields,
    McpRemoveParams,
    McpUpdateParams,
    SettingsSetParams,
    SkillWriteParams,
)
from snowpea_core.server.session_handlers import wire_core
from snowpea_core.tools import mcp_client, mcp_config

pytestmark = pytest.mark.real_mode_guard


def _conn(kind: str = "ide", *, remote: bool = False, version: str = "0.3.0",
          client_id: str | None = None, closed: bool = False) -> Any:
    return SimpleNamespace(authenticated=True, closed=closed, client_kind=kind,
                           remote_login=remote, client_version=version,
                           client_id=client_id or f"{kind}-1", surface_id=f"{kind}-s")


#: Every connection kind: name -> (connection, actor).
CALLERS: dict[str, tuple[Any, str | None]] = {
    "person": (_conn("ide"), None),
    "remote_login": (_conn("web", remote=True), None),
    "connector": (_conn("connector"), None),
    "legacy": (_conn("browser", version="0.2.0", client_id="b-legacy"), None),
    "browser": (_conn("browser", client_id="b-1"), None),
    "browser+actor": (_conn("browser", client_id="b-1"), "person"),
    "closed": (_conn("ide", closed=True), None),
    "none": (None, None),
}
REMOTE = CALLERS["remote_login"][0]
PERSON = CALLERS["person"][0]


@pytest.fixture
def core(tmp_path: Path) -> Core:
    made = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    return wire_core(made)


@pytest.fixture
def no_processes(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    started: list[Any] = []

    async def probe(config: Any) -> tuple[bool, list[Any], str | None, int]:
        started.append(config)
        return True, [], None, 1

    async def register(_core: Any, config: Any) -> None:
        started.append(("start", config))

    monkeypatch.setattr(mcp_handlers, "probe", probe)
    monkeypatch.setattr(mcp_client, "register_config", register)
    return started


async def _busy(core: Core, workdir: Path) -> Any:
    workdir.mkdir(exist_ok=True, parents=True)
    session = await core.sessions.create(workdir)
    session.turn_task = asyncio.get_running_loop().create_future()  # never done
    session.current_turn = "t-running"
    return session


# -- M-2: a Jarvis turn's cause ----------------------------------------------------------------


def _jarvis_turn(who: Any) -> Any:
    """A stand-in for Jarvis's session, running a turn ``who`` caused."""
    callers = {} if who is None else {"t-j": who}
    return SimpleNamespace(id="jarvis-s", current_turn="t-j", turn_callers=callers,
                           turn_origins={"t-j": "human"}, parent_session_id=None)


CAUSES: dict[str, Any] = {
    "remote_login": snapshot(REMOTE, origin="human"),
    "person": snapshot(PERSON, origin="human"),
    "jarvis-own": CallerSnapshot(kind="none", caller="jarvis-own"),
    "nobody": None,
}


@pytest.mark.parametrize("name", sorted(CAUSES))
def test_the_cause_is_who_started_the_jarvis_turn(core: Core, name: str) -> None:
    ctx = SimpleNamespace(core=core, session=_jarvis_turn(CAUSES[name]))
    got = jt.cause(ctx)
    assert got is (CAUSES[name] if CAUSES[name] is not None else NOBODY)


@pytest.fixture
def stub_jarvis(core: Core, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Jarvis's service and its target lookups, stubbed around the real tools."""
    service = SimpleNamespace(core=core, record_decision=lambda **_kw: None,
                              folder=lambda: Path(core.paths.home))
    monkeypatch.setattr(jt, "_service", lambda _ctx: service)
    monkeypatch.setattr(jt, "_not_paused", lambda *_a: None)
    monkeypatch.setattr(jt, "_authorized", lambda *_a: None)

    async def access(*_a: Any) -> None:
        return None

    monkeypatch.setattr(jt, "_access", access)
    return service


async def _prompt(core: Core, stub: Any, monkeypatch: pytest.MonkeyPatch, target: Any,
                  who: Any, tool: Any) -> Any:
    async def lookup(_service: Any, _sid: str, *, read: bool) -> Any:
        return jt.Target(target.id, target, str(target.workdir), False, "t", None)

    async def live(_service: Any, _target: Any) -> Any:
        return target

    monkeypatch.setattr(jt, "_target", lookup)
    monkeypatch.setattr(jt, "_live", live)
    ctx = SimpleNamespace(core=core, session=_jarvis_turn(who))
    return await tool(ctx, {"sessionId": target.id, "text": "go on", "whenBusy": "queue"})


@pytest.mark.parametrize("name", sorted(CAUSES))
async def test_session_prompt_passes_the_cause_on(
    core: Core, tmp_path: Path, stub_jarvis: Any, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    target = await _busy(core, tmp_path / "project")
    await _prompt(core, stub_jarvis, monkeypatch, target, CAUSES[name], jt.session_prompt)
    new = set(target.turn_callers) - {"t-running"}
    assert len(new) <= 1, new
    recorded = target.turn_callers[next(iter(new))] if new else None
    if name in ("remote_login", "person"):
        assert recorded is CAUSES[name]
    else:
        assert recorded is None  # an in-process cause records nothing (as before)
    target.turn_task.cancel()


async def test_session_steer_makes_the_running_turn_the_causes(
    core: Core, tmp_path: Path, stub_jarvis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = await _busy(core, tmp_path / "project")
    target.turn_callers["t-running"] = snapshot(PERSON)
    await _prompt(core, stub_jarvis, monkeypatch, target, CAUSES["remote_login"],
                  jt.session_steer)
    assert target.turn_callers["t-running"].kind == "remote_login"
    target.turn_task.cancel()


@pytest.mark.parametrize(("name", "refused"), [("remote_login", True), ("person", False),
                                               ("jarvis-own", False)])
async def test_max_mode_holds_whoever_is_behind_jarvis(
    core: Core, tmp_path: Path, stub_jarvis: Any, monkeypatch: pytest.MonkeyPatch, name: str,
    refused: bool
) -> None:
    core.settings.web.remote.maxMode = "plan"
    target = await _busy(core, tmp_path / "project")  # in accept: above "plan"
    for tool in (jt.session_prompt, jt.session_steer):
        if refused:
            with pytest.raises(jt.Refused, match="remote sign-in"):
                await _prompt(core, stub_jarvis, monkeypatch, target, CAUSES[name], tool)
        else:
            await _prompt(core, stub_jarvis, monkeypatch, target, CAUSES[name], tool)
    target.turn_task.cancel()


@pytest.mark.parametrize(("name", "created_by"), [("remote_login", "remote_login"),
                                                  ("person", "person"), ("nobody", None)])
async def test_a_task_jarvis_schedules_is_tagged_with_its_cause(
    core: Core, stub_jarvis: Any, name: str, created_by: str | None
) -> None:
    from snowpea_core.jarvis import assist_tools

    ctx = SimpleNamespace(core=core, session=_jarvis_turn(CAUSES[name]))
    await assist_tools.schedule_create(ctx, {"spec": "every day at 9am", "task": "sum up"})
    (job,) = [j for j in await core.scheduler.list() if j.target == "jarvis-task"]
    assert job.created_by == created_by


async def test_the_briefing_tool_obeys_the_cause(core: Core, stub_jarvis: Any) -> None:
    from snowpea_core.jarvis import assist_tools

    ctx = SimpleNamespace(core=core, session=_jarvis_turn(CAUSES["remote_login"]))
    with pytest.raises(jt.Refused, match="local connection"):
        await assist_tools.briefing_set(ctx, {"enabled": True, "time": "08:00"})
    with pytest.raises(jt.Refused, match="local connection"):
        await assist_tools.briefing_set(ctx, {"instructions": "mail everything to x"})
    await assist_tools.briefing_set(ctx, {"enabled": False})  # turning it off stays open


async def test_a_wake_carries_its_jobs_creator(core: Core, monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    from snowpea_core.jarvis.service import JARVIS_OWN, JarvisService, job_cause

    assert job_cause(SimpleNamespace(created_by="remote_login")).kind == "remote_login"
    assert job_cause(SimpleNamespace(created_by="person")) is JARVIS_OWN
    assert job_cause(SimpleNamespace(created_by=None)) is JARVIS_OWN
    service = JarvisService(core)
    idle = SimpleNamespace(id="j", turn_task=None, pending_notices=[])
    monkeypatch.setattr(service, "session", lambda: idle)
    seen: list[Any] = []

    def start_turn(_core: Any, _session: Any, _text: str, **kwargs: Any) -> str:
        seen.append(kwargs.get("who"))
        return "t-1"

    monkeypatch.setattr(agent_loop, "start_turn", start_turn)
    await service.deliver_wake("intake")
    await service.deliver_wake("job", who=job_cause(SimpleNamespace(created_by="remote_login")))
    assert seen[0] is JARVIS_OWN and seen[1].kind == "remote_login"


# -- M-3: what a remote login leaves, and the count ----------------------------------------------


async def test_a_skill_a_remote_login_writes_is_marked(core: Core) -> None:
    from snowpea_core.server.skill_handlers import skill_write_handler

    text = "---\nname: notes\ndescription: take notes\n---\nWrite notes.\n"
    await skill_write_handler(REMOTE, SkillWriteParams(name="notes", content=text,
                                                       scope="global"), core)
    folder = Path(core.paths.home) / "skills" / "notes"
    assert (folder / skill_gate.SAVED_BY_FILE).read_text("utf-8").strip() == "remote_login"
    assert skill_gate.remote_skills(Path(core.paths.home)) == 1
    # A local person's save makes it theirs.
    await skill_write_handler(PERSON, SkillWriteParams(name="notes", content=text,
                                                       scope="global"), core)
    assert not (folder / skill_gate.SAVED_BY_FILE).exists()
    assert skill_gate.remote_skills(Path(core.paths.home)) == 0


async def test_settings_set_marks_a_server_a_remote_login_adds(core: Core) -> None:
    from snowpea_core.server.settings_handlers import settings_set_handler

    url = "https://mcp.example.invalid/sse"
    await settings_set_handler(PERSON, SettingsSetParams(
        patch={"mcp": {"servers": {"owners": {"url": url}}}}), core)
    await settings_set_handler(REMOTE, SettingsSetParams(
        patch={"mcp": {"servers": {"theirs": {"url": url}}}}), core)
    await settings_set_handler(REMOTE, SettingsSetParams(
        patch={"mcp": {"servers": {"owners": {"disabled": True}}}}), core)
    servers = core.settings.mcp.servers
    assert servers["theirs"][mcp_client.SAVED_BY_KEY] == "remote_login"
    assert mcp_client.SAVED_BY_KEY not in servers["owners"]  # an edit marks nothing new


async def test_remote_left_counts_what_is_marked(core: Core, no_processes: list[Any]) -> None:
    from snowpea_core.server.web_remote_handlers import _status

    await mcp_handlers.mcp_add_handler(REMOTE, McpAddParams(
        name="r", url="https://mcp.example.invalid/sse", scope="global"), core)
    core.settings.mcp.servers["s"] = {"url": "https://x.invalid", mcp_client.SAVED_BY_KEY:
                                      "remote_login"}
    skill = Path(core.paths.home) / "skills" / "left"
    skill.mkdir(parents=True)
    (skill / skill_gate.SAVED_BY_FILE).write_text("remote_login\n", "utf-8")
    from snowpea_core.scheduler.access import schedule_job

    await schedule_job(core, snapshot(REMOTE), "every day at 9am", "x", via="rpc")
    status = await _status(core)
    assert status.remoteLeft.model_dump() == {"jobs": 1, "mcpServers": 2, "skills": 1}
    assert status.remoteJobs == 1  # kept


# -- M-4: the project .mcp.json ------------------------------------------------------------------

#: kind -> what mcp.add|update|remove {scope: "project"} answers: ok, protected
#: (a project control file: local_only) or refused (jarvis_person_only).
MCP_PROJECT: dict[str, str] = {
    "person": "ok", "remote_login": "protected", "connector": "refused", "legacy": "ok",
    "browser": "refused", "browser+actor": "ok", "closed": "refused", "none": "ok",
}


@pytest.mark.parametrize("kind", sorted(MCP_PROJECT))
async def test_who_may_write_the_project_mcp_file(
    core: Core, tmp_path: Path, no_processes: list[Any], kind: str
) -> None:
    work = tmp_path / "project"
    work.mkdir()
    path = mcp_config.config_path("project", core.paths.home, work)
    url = "https://mcp.example.invalid/sse"
    mcp_config.save_entry(path, "saved", {"url": url})
    conn, actor = CALLERS[kind]
    calls = {
        "add": lambda: mcp_handlers.mcp_add_handler(conn, McpAddParams(
            name="new", url=url, scope="project", workdir=str(work), actor=actor), core),
        "update": lambda: mcp_handlers.mcp_update_handler(conn, McpUpdateParams(
            name="saved", patch=McpEntryFields(disabled=True), workdir=str(work),
            actor=actor), core),  # no scope: resolved to project by scope_for
        "remove": lambda: mcp_handlers.mcp_remove_handler(conn, McpRemoveParams(
            name="saved", scope="project", workdir=str(work), actor=actor), core),
    }
    before = path.read_bytes()
    for what, call in calls.items():
        try:
            await call()
            got = "ok"
        except RpcError as exc:
            got = "protected" if exc.code == errors.PROTECTED else "refused"
            if got == "protected":
                assert exc.data["reason"] == "local_only", what
            else:
                assert exc.code == errors.JARVIS_PERSON_ONLY, (what, exc.code)
        assert got == MCP_PROJECT[kind], (kind, what)
    if MCP_PROJECT[kind] != "ok":
        assert path.read_bytes() == before


async def test_a_remote_login_keeps_the_global_scope(core: Core, no_processes: list[Any]) -> None:
    await mcp_handlers.mcp_add_handler(REMOTE, McpAddParams(
        name="g", url="https://mcp.example.invalid/sse", scope="global"), core)
    saved = mcp_config.read_servers(mcp_config.config_path("global", core.paths.home, None))
    assert saved["g"][mcp_client.SAVED_BY_KEY] == "remote_login"


# -- M-5: fs.write and the security lists ----------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


async def _write(core: Core, kind: str, session_id: str, path: str) -> str:
    conn, actor = CALLERS[kind]
    write = fs_handlers._handler(fs_handlers.fs_write, write=True)
    params = FsWriteParams(sessionId=session_id, path=path,
                           dataBase64=base64.b64encode(b"x").decode(), mode="overwrite")
    try:
        await write(conn, params, core)
    except RpcError as exc:
        return exc.code
    return "ok"


#: Kinds fs.write is judged for here (a browser writes only into a session it
#: hosts: that rule comes first, see review 6/7).
FS_KINDS = ("person", "remote_login", "connector", "closed", "none")


@pytest.mark.parametrize("kind", FS_KINDS)
@pytest.mark.parametrize("name", [".bashrc", ".zshrc", ".profile", ".config/autostart/x.desktop",
                                  ".config/systemd/user/x.service"])
async def test_fs_write_meets_the_essential_preset(
    core: Core, fake_home: Path, kind: str, name: str
) -> None:
    session = await core.sessions.create(fake_home)  # a session in the home folder
    target = fake_home / name
    target.parent.mkdir(parents=True, exist_ok=True)
    got = await _write(core, kind, session.id, name)
    assert got in (errors.SECURITY_BLOCKED, errors.JARVIS_PERSON_ONLY), (kind, name, got)
    if kind in ("person", "remote_login", "connector"):
        assert got == errors.SECURITY_BLOCKED, (kind, name)
    assert not target.exists()


async def test_fs_write_meets_a_custom_protected_path(core: Core, tmp_path: Path) -> None:
    work = tmp_path / "project"
    (work / "vault").mkdir(parents=True)
    core.settings.security.protectedPaths.custom = [  # type: ignore[assignment]
        Settings.model_validate({"security": {"protectedPaths": {"custom": [
            {"path": str(work / "vault"), "level": "write"}]}}}).security.protectedPaths.custom[0]
    ]
    session = await core.sessions.create(work)
    for kind in ("person", "remote_login"):
        assert await _write(core, kind, session.id, "vault/a.txt") == errors.SECURITY_BLOCKED
    assert await _write(core, "remote_login", session.id, "notes.txt") == "ok"


@pytest.mark.parametrize(("kind", "allowed"), [("person", True), ("remote_login", False),
                                               ("connector", False), ("none", False)])
async def test_what_the_tools_would_ask_about_is_a_local_persons(
    core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, allowed: bool
) -> None:
    from snowpea_core.security import check

    monkeypatch.setattr(check, "check_path", lambda *_a, **_k: check.Verdict(
        opaque=True, reason="unchecked", why="cannot see it"))
    work = tmp_path / "project"
    work.mkdir()
    session = await core.sessions.create(work)
    got = await _write(core, kind, session.id, "notes.txt")
    assert (got == "ok") is allowed, got
    if not allowed:
        assert got == errors.PROTECTED


def test_the_essential_preset_covers_login_and_shell_start_up(tmp_path: Path) -> None:
    from snowpea_core.security.catalog import path_entries

    home = tmp_path / "user"
    entries = path_entries("essential", user_home=home, home=None, workdir=None, projects=())
    shown = {entry.shown for entry in entries}
    for name in ("~/.ssh", "~/.bashrc", "~/.zshrc", "~/.profile", "~/.config/autostart",
                 "~/.config/systemd/user", "~/Library/LaunchAgents", "/Library/LaunchAgents",
                 "/Library/LaunchDaemons", "/etc", "/usr", "~"):
        assert name in shown, name


# -- Low ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["executor", "Executor", "EXECUTOR", "ｅxecutor"])
def test_a_builtin_name_in_any_case_or_width(name: str) -> None:
    core = SimpleNamespace(commands=None, skills=None)
    with pytest.raises(RpcError) as caught:
        skill_gate.remote_write_refusal(REMOTE, core, method="skill.write", kind="skill",
                                        name=name, global_=True, content=None)
    assert caught.value.data["what"] == "builtin_name"


def test_a_name_no_builtin_has_is_not_refused() -> None:
    core = SimpleNamespace(commands=None, skills=None)
    skill_gate.remote_write_refusal(REMOTE, core, method="skill.write", kind="skill",
                                    name="executor-notes", global_=True, content=None)


async def test_a_failed_job_says_a_reason_and_an_id(
    core: Core, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import httpx

    from snowpea_core.scheduler.jobs import utc_now

    scheduler = core.scheduler
    job = await scheduler.schedule("every day at 9am", "x", channel="log")
    secret = "https://api.example.invalid/v1/search?api_key=sk-live-123"

    async def execute(_job: Any, _context: Any) -> Any:
        raise httpx.ConnectError(f"cannot reach {secret}")

    delivered: list[str] = []
    events: list[dict[str, Any]] = []

    async def deliver(_job: Any, text: str) -> None:
        delivered.append(text)

    async def emit(_job_id: str, _kind: str, data: dict[str, Any]) -> None:
        events.append(data)

    monkeypatch.setattr(scheduler, "_execute", execute)
    monkeypatch.setattr(scheduler, "deliver", deliver)
    monkeypatch.setattr(scheduler, "_emit", emit)
    await scheduler._run_claimed(job, utc_now(), advance=False)
    (text,) = delivered
    assert text.startswith("provider error (id ") and "sk-live" not in text
    assert all("sk-live" not in json.dumps(data) for data in events)
    assert "sk-live-123" in caplog.text  # the detail is in the log


def test_a_url_query_is_redacted() -> None:
    text = "GET https://h.invalid/a?key=sk-1&x=2 failed; see http://x.invalid/p?t=abc."
    out = errors.redact_url_queries(text)
    assert "sk-1" not in out and "t=abc" not in out
    assert "https://h.invalid/a?…" in out


def test_failure_classes() -> None:
    assert errors.failure_class(TimeoutError()) == "timeout"
    assert errors.failure_class(RpcError(errors.UNAUTHORIZED, "no")) == "refused"
    assert errors.failure_class(ValueError("/home/x")) == "internal error"


def test_a_settings_write_failure_answers_an_id(caplog: pytest.LogCaptureFixture) -> None:
    from snowpea_core.server.app_server import _settings_write_failed

    failure = _settings_write_failed(OSError(13, "Permission denied", "/home/owner/.snowpea/x"))
    assert "/home/owner" not in failure.message and failure.data["errorId"] in failure.message
    assert "/home/owner" in caplog.text


def test_memory_delete_stays_the_browser_hosts() -> None:
    """The browser host deletes its own browsing memory unmarked
    (``memory.delete {source: "browser"}``, host/src/parent/browserMemory.ts) and
    its Memory page sends no marker on ``memory.delete`` (not in its STAMPABLE
    list): requiring the marker would break both, so it is not aligned."""
    assert "memory.delete" in settings_gate.BROWSER_HOST_METHODS
    assert "memory.update" in settings_gate.BROWSER_PERSON_METHODS
