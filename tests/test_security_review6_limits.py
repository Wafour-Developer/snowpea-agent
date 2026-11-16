"""Security review 6, part B: what a remote web login (and the other kinds) may not do.

H-1 (no program started from a remote login's text), M-4 (no access left
behind), L-2 (``hostToolsFrom``), L-6 (``fs.*``), the generic error text and the
re-decided browser classifications.  Every rule is a table here, one row per
connection kind where the kind matters.

The command names below ("prog", "other") are DATA: nothing in this file starts
a process — the probe and the server start are replaced by recorders.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import (
    errors,
    fs_handlers,
    mcp_handlers,
    person_gate,
    settings_gate,
    settings_handlers,
    skill_handlers,
)
from snowpea_core.server.app_server import Core, provider_configure_handler
from snowpea_core.server.errors import DETAIL_REASONS, RpcError
from snowpea_core.server.protocol import (
    Empty,
    FsWriteParams,
    JobScheduleParams,
    McpAddParams,
    McpEntryFields,
    McpTestParams,
    McpUpdateParams,
    ProviderConfigureParams,
    SkillInstallParams,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.tools import mcp_client, mcp_config

pytestmark = pytest.mark.real_mode_guard


def _conn(kind: str = "ide", *, remote: bool = False, version: str = "0.3.0",
          client_id: str | None = None) -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind=kind,
                           remote_login=remote, client_version=version,
                           client_id=client_id or f"{kind}-1", surface_id=f"{kind}-s")


PERSON = _conn("ide")
REMOTE = _conn("web", remote=True)
BROWSER = _conn("browser", client_id="b-1")
LEGACY = _conn("browser", version="0.2.0", client_id="b-legacy")
CONNECTOR = _conn("connector")


def _local_only(caught: pytest.ExceptionInfo[RpcError], kind: str = "remote_login") -> None:
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert caught.value.data["reason"] == "local_only"
    assert caught.value.data["kind"] == kind


@pytest.fixture
def core(tmp_path: Path) -> Core:
    return Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")


def test_local_only_is_an_enumerated_reason() -> None:
    assert DETAIL_REASONS["local_only"][0] == errors.JARVIS_PERSON_ONLY


# -- H-1: MCP servers ---------------------------------------------------------------------

STDIO = {"command": "prog", "args": ["--serve"]}
URL = {"url": "https://mcp.example.com/sse"}


@pytest.mark.parametrize(
    ("entry", "saved", "accept", "refused"),
    [
        (URL, None, False, False),  # a url-only server: add
        (URL, URL, False, False),
        # Security review 7, H-3: a saved server's url/headers/oauth are a local
        # person's to change (the daemon fills ${VAR} from credentials.json there).
        ({**URL, "headers": {"X": "y"}}, URL, False, True),
        ({**URL, "headers": {"X": "y"}}, None, False, False),  # a new one, no variable
        (URL, None, True, True),  # force / acceptFindings is a local person's
        (STDIO, None, False, True),  # a new server that runs a command
        ({"command": "prog"}, None, False, True),
        (STDIO, STDIO, False, False),  # the saved one, launch unchanged (disable, timeouts)
        ({**STDIO, "disabled": True, "timeoutSec": 5}, STDIO, False, False),
        ({**STDIO, "args": ["--other"]}, STDIO, False, True),
        ({**STDIO, "env": {"A": "b"}}, STDIO, False, True),
        ({**STDIO, "cwd": "/somewhere"}, STDIO, False, True),
        ({"command": "other", "args": ["--serve"]}, STDIO, False, True),
        (STDIO, URL, False, True),  # a url server turned into a command
        # A command server turned into a url: a new target for a saved entry
        # (security review 7, H-3; it was allowed as "takes away" in review 6).
        (URL, STDIO, False, True),
    ],
)
def test_remote_mcp_rule(entry: dict[str, Any], saved: dict[str, Any] | None, accept: bool,
                         refused: bool) -> None:
    def judge(conn: Any) -> None:
        settings_gate.remote_mcp_refusal(conn, None, method="mcp.add", entry=entry, saved=saved,
                                         accept=accept)

    for other in (PERSON, BROWSER, LEGACY, CONNECTOR, None):
        judge(other)  # only a remote login is judged here (the tables judge the rest)
    if refused:
        with pytest.raises(RpcError) as caught:
            judge(REMOTE)
        _local_only(caught)
    else:
        judge(REMOTE)


@pytest_asyncio.fixture
async def no_processes(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[Any]]:
    """Replace the probe and the server start: nothing runs; what would is recorded."""
    probed: list[Any] = []

    async def probe(config: Any) -> tuple[bool, list[Any], str | None, int]:
        probed.append(config)
        return True, [], None, 1

    async def register(_core: Any, config: Any) -> None:
        probed.append(("start", config))

    monkeypatch.setattr(mcp_handlers, "probe", probe)
    monkeypatch.setattr(mcp_client, "register_config", register)
    mcp_client.MANAGER.listeners = []
    yield probed
    await mcp_client.MANAGER.close_all()
    mcp_client.MANAGER.listeners = []


async def test_remote_login_mcp_handlers(core: Core, tmp_path: Path,
                                         no_processes: list[Any]) -> None:
    work = tmp_path / "project"
    work.mkdir()
    where = {"scope": "project", "workdir": str(work)}
    path = mcp_config.config_path("project", core.paths.home, work)

    # A server that runs a command: never from a remote login, and nothing is written.
    with pytest.raises(RpcError) as caught:
        await mcp_handlers.mcp_add_handler(
            REMOTE, McpAddParams(name="run", command="prog", args=["x"], **where), core)
    _local_only(caught)
    with pytest.raises(RpcError) as caught:
        await mcp_handlers.mcp_test_handler(
            REMOTE, McpTestParams(name="draft", command="prog", workdir=str(work)), core)
    _local_only(caught)
    assert no_processes == [] and "run" not in mcp_config.read_servers(path)

    # force / acceptFindings: a local person's, even for a url.
    for extra in ({"force": True}, {"acceptFindings": True}):
        with pytest.raises(RpcError) as caught:
            await mcp_handlers.mcp_add_handler(
                REMOTE, McpAddParams(name="u", url="https://mcp.example.com/sse", **where,
                                     **extra), core)
        _local_only(caught)

    # A url-only server stays a remote login's: add, probe, change, disable.
    await mcp_handlers.mcp_add_handler(
        REMOTE, McpAddParams(name="u", url="https://mcp.example.com/sse", **where), core)
    assert mcp_config.read_servers(path)["u"]["url"] == "https://mcp.example.com/sse"
    await mcp_handlers.mcp_test_handler(REMOTE, McpTestParams(name="u", workdir=str(work)), core)
    await mcp_handlers.mcp_update_handler(REMOTE, McpUpdateParams(
        name="u", patch=McpEntryFields(disabled=True), **where), core)
    with pytest.raises(RpcError) as caught:  # ... but not into a command
        await mcp_handlers.mcp_update_handler(REMOTE, McpUpdateParams(
            name="u", patch=McpEntryFields(command="prog"), **where), core)
    _local_only(caught)
    assert "command" not in mcp_config.read_servers(path)["u"]

    # The person's own command server: a remote login may probe it as saved and
    # switch it off and on, never change what it runs.
    await mcp_handlers.mcp_add_handler(PERSON, McpAddParams(
        name="mine", command="prog", args=["--serve"], disabled=True, test=False, force=True,
        **where), core)
    no_processes.clear()
    await mcp_handlers.mcp_test_handler(
        REMOTE, McpTestParams(name="mine", workdir=str(work)), core)
    assert [getattr(c, "command", None) for c in no_processes] == ["prog"]  # the saved one
    await mcp_handlers.mcp_update_handler(REMOTE, McpUpdateParams(
        name="mine", patch=McpEntryFields(disabled=True), **where), core)
    for patch in (McpEntryFields(args=["--other"]), McpEntryFields(env={"A": "b"}),
                  McpEntryFields(command="other")):
        with pytest.raises(RpcError) as caught:
            await mcp_handlers.mcp_update_handler(
                REMOTE, McpUpdateParams(name="mine", patch=patch, **where), core)
        _local_only(caught)
    with pytest.raises(RpcError) as caught:
        await mcp_handlers.mcp_test_handler(
            REMOTE, McpTestParams(name="mine", args=["--other"], workdir=str(work)), core)
    _local_only(caught)
    assert mcp_config.read_servers(path)["mine"]["args"] == ["--serve"]

    # A local person is not narrowed.
    await mcp_handlers.mcp_update_handler(PERSON, McpUpdateParams(
        name="mine", patch=McpEntryFields(args=["--other"]), force=True, **where), core)
    assert mcp_config.read_servers(path)["mine"]["args"] == ["--other"]


# -- H-1: skill.install ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "link", "allowed"),
    [
        ("registry:pdf-tools", False, True),
        ("registry:team/pdf-tools@1.2.0", False, True),
        ("official/pdf-tools", False, True),  # <marketplace>/<plugin>
        ("official/pdf-tools", True, False),  # a link installs a local folder
        ("/srv/plugins/x", False, False),
        ("~/plugins/x", False, False),
        ("./x", False, False),
        ("../x", False, False),
        ("C:\\plugins\\x", False, False),
        ("https://example.com/x.git", False, False),
        ("git@example.com:x/y.git", False, False),
        ("github:owner/repo", False, False),
        ("clawhub:thing", False, False),
        ("file:///srv/x", False, False),
        ("superpowers", False, False),  # a shortcut resolves to a git URL
        ("a/b/c", False, False),
        ("registry:", False, False),
        ("", False, False),
    ],
)
def test_remote_skill_sources(source: str, link: bool, allowed: bool) -> None:
    assert (settings_gate.remote_skill_source_problem(source, link) is None) is allowed


def test_a_marketplace_name_that_is_a_local_folder_is_a_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "official" / "pdf-tools").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert settings_gate.remote_skill_source_problem("official/pdf-tools") == "a local path"


@pytest.mark.parametrize("conn", [PERSON, REMOTE], ids=["person", "remote"])
async def test_skill_install_by_kind(core: Core, tmp_path: Path, conn: Any,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[str] = []

    class Loader:
        async def install(self, source: str, *, link: bool = False) -> Path:
            installed.append(source)
            return tmp_path / "plugins" / "x"

    monkeypatch.setattr(skill_handlers, "_loader", lambda _core: Loader())
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    for source in (str(plugin), "https://example.com/x.git"):
        if conn is REMOTE:
            with pytest.raises(RpcError) as caught:
                await skill_handlers.skill_install_handler(
                    conn, SkillInstallParams(source=source), core)
            _local_only(caught)
        else:
            await skill_handlers.skill_install_handler(
                conn, SkillInstallParams(source=source), core)
    await skill_handlers.skill_install_handler(
        conn, SkillInstallParams(source="registry:pdf-tools"), core)
    expected = ["registry:pdf-tools"] if conn is REMOTE else [
        str(plugin), "https://example.com/x.git", "registry:pdf-tools"]
    assert installed == expected


# -- H-1 / M-4: settings keys -----------------------------------------------------------------

#: patch -> may a remote login make it (global scope)?
REMOTE_PATCHES: list[tuple[dict[str, Any], bool]] = [
    # programs to run
    ({"mcp": {"servers": {"x": {"command": "prog"}}}}, False),
    ({"mcp": {"servers": {"x": {"args": ["a"]}}}}, False),
    ({"mcp": {"servers": {"x": {"env": {"A": "b"}}}}}, False),
    ({"mcp": {"servers": {"x": {"cwd": "/tmp"}}}}, False),
    ({"mcp": {"servers": {"x": {"command": None}}}}, False),
    ({"mcp": {"servers": {"x": {"url": "https://mcp.example.com"}}}}, True),
    ({"mcp": {"servers": {"x": {"url": "https://mcp.example.com", "command": "prog"}}}}, False),
    ({"mcp": {"servers": {"x": None}}}, True),  # removing a server only takes away
    ({"lsp": {"servers": {"clangd": {"command": ["prog"]}}}}, False),
    ({"lsp": {"servers": {"clangd": None}}}, False),
    ({"lsp": {"enabled": False}}, True),
    ({"lsp": {"disabled": ["eslint"]}}, True),
    ({"audio": {"stt": {"command": "prog {path}"}}}, False),
    ({"audio": {"tts": {"command": "prog {text} {out}"}}}, False),
    ({"audio": {"player": "prog"}}, False),
    ({"audio": {"recorder": "prog"}}, False),
    ({"audio": {"tts": {"voice": "F2"}}}, True),
    ({"audio": {"stt": {"language": "ko"}}}, True),
    # access left behind
    ({"gateway": {"telegram": {"token": "t"}}}, False),
    ({"gateway": {"slack": {"app_token": "t"}}}, False),
    ({"gateway": {"telegram": {"allowed_user_id": "1"}}}, False),
    ({"gateway": {"telegram": {"user_id": "1"}}}, False),
    ({"gateway": {"telegram": {"channel_id": "1"}}}, False),
    ({"gateway": {"telegram": {"enabled": True}}}, False),
    ({"gateway": {"telegram": {"enabled": False}}}, False),
    ({"gateway": {"typing": False}}, True),  # the two switches
    ({"gateway": {"progress": False}}, True),
    ({"gateway": {"telegram": {"shareActive": False}}}, True),
    ({"providers": {"openai": {"base_url": "https://elsewhere.example/v1"}}}, False),
    ({"providers": {"openai": {"base_url": None}}}, False),
    ({"providers": {"openai": {"model": "m"}}}, True),
    ({"providers": {"openai": {"api_key": "k"}}}, True),
    ({"providers": {"default": "openai"}}, True),
    ({"skills": {"registry": {"url": "https://elsewhere.example"}}}, False),
    ({"skills": {"disabled": ["x"]}}, True),
    ({"scheduler": {"tickSec": 30}}, True),
    ({"approvals": {"timeoutSec": 60}}, True),
    ({"models": {"default": "x"}}, True),
]


@pytest.mark.parametrize(("patch", "allowed"), REMOTE_PATCHES,
                         ids=[json.dumps(p)[:60] for p, _ in REMOTE_PATCHES])
def test_settings_keys_of_a_remote_login(patch: dict[str, Any], allowed: bool) -> None:
    # A local person is never narrowed.
    settings_handlers.guard_patch(PERSON, patch, project=False)
    if allowed:
        settings_handlers.guard_patch(REMOTE, patch, project=False)
        return
    for project in (False, True):
        with pytest.raises(RpcError) as caught:
            settings_handlers.guard_patch(REMOTE, patch, project=project)
        _local_only(caught)


def test_every_never_key_is_a_key_the_remote_table_would_allow() -> None:
    """The never-keys narrow REMOTE_LOGIN_KEYS; one outside it would be dead text."""
    for pattern in settings_gate.REMOTE_LOGIN_NEVER_KEYS:
        assert any(settings_gate.touches(pattern, allowed)
                   for allowed in settings_gate.REMOTE_LOGIN_KEYS), pattern


# -- M-4: provider endpoint, gateway.bind, jobs ---------------------------------------------


@pytest.mark.parametrize(
    ("conn", "config", "refused"),
    [
        (REMOTE, {"base_url": "https://elsewhere.example/v1"}, True),
        (REMOTE, {"base_url": None}, True),
        (REMOTE, {"preset": "local", "base_url": "http://10.0.0.5:8000/v1"}, True),
        (REMOTE, {"model": "m"}, False),
        (REMOTE, {"api_key": "k"}, False),
        (PERSON, {"base_url": "https://elsewhere.example/v1"}, False),
        (_conn("browser", client_id="b-2"), {"base_url": "https://elsewhere.example/v1"}, True),
    ],
)
async def test_provider_base_url(core: Core, conn: Any, config: dict[str, Any], refused: bool,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.server import app_server

    saved: list[dict[str, Any]] = []

    async def persist(_core: Any, _vendor: str, cfg: dict[str, Any]) -> None:
        saved.append(cfg)

    monkeypatch.setattr(app_server, "_persist_provider", persist)
    call = provider_configure_handler(
        conn, ProviderConfigureParams(vendor="openai", config=config), core)
    if not refused:
        await call
        assert saved == [config]
        return
    with pytest.raises(RpcError) as caught:
        await call
    assert caught.value.code == errors.JARVIS_PERSON_ONLY and saved == []
    if conn is REMOTE:
        _local_only(caught)  # (an unmarked browser is refused by the marker rule)


def test_gateway_bind_is_a_local_persons() -> None:
    assert "gateway.bind" not in settings_gate.REMOTE_LOGIN_ALLOWED
    for conn in (REMOTE, CONNECTOR, BROWSER, LEGACY):
        with pytest.raises(RpcError):
            settings_gate.browser_method_refusal("gateway.bind", {"actor": "person"}, conn, None)
            settings_gate.kind_method_refusal("gateway.bind", {}, conn, None)
    settings_gate.kind_method_refusal("gateway.bind", {}, PERSON, None)


async def test_a_remote_logins_jobs_are_tagged_and_counted(core: Core, tmp_path: Path) -> None:
    from snowpea_core.scheduler.jobs import Job
    from snowpea_core.server import job_handlers, web_remote_handlers
    from snowpea_core.server.session_handlers import wire_core

    wire_core(core)
    params = JobScheduleParams(spec="every day at 9:00", task="summarise", workdir=str(tmp_path))
    mine = await job_handlers.job_schedule_handler(PERSON, params, core)
    theirs = await job_handlers.job_schedule_handler(REMOTE, params, core)
    rows = {row.jobId: row for row in (await job_handlers.job_list_handler(
        PERSON, Empty(), core)).jobs}
    assert rows[mine.jobId].createdBy is None
    assert rows[theirs.jobId].createdBy == "remote_login"
    # ... and it survives the store.
    stored = await core.scheduler.get(theirs.jobId)
    assert isinstance(stored, Job) and stored.created_by == "remote_login"
    assert stored.target is None  # a prompt job: the only kind job.schedule makes
    assert (await web_remote_handlers.status_handler(PERSON, Empty(), core)).remoteJobs == 1
    await job_handlers.job_delete_handler(
        PERSON, job_handlers.JobDeleteParams(jobId=theirs.jobId), core)
    assert (await web_remote_handlers.status_handler(PERSON, Empty(), core)).remoteJobs == 0


async def test_a_remote_login_schedules_no_run_on_a_hosts_tools(core: Core,
                                                                tmp_path: Path) -> None:
    from snowpea_core.server import job_handlers
    from snowpea_core.server.session_handlers import wire_core

    wire_core(core)
    params = JobScheduleParams(
        spec="every day at 9:00", task="x", workdir=str(tmp_path),
        sessionTemplate={"hostToolsFrom": "snowpea-browser-A"})
    with pytest.raises(RpcError) as caught:
        await job_handlers.job_schedule_handler(REMOTE, params, core)
    _local_only(caught)
    assert await core.scheduler.list() == []
    await job_handlers.job_schedule_handler(PERSON, params, core)


def test_narrowed_methods_are_ones_a_remote_login_may_call() -> None:
    assert settings_gate.REMOTE_LOGIN_NARROWED <= settings_gate.REMOTE_LOGIN_ALLOWED


# -- every RPC that starts a process, and how a remote login meets it ------------------------

#: method -> how a remote web login is kept from starting a program of its choosing:
#: ``refused``  the dispatcher refuses the method (not in REMOTE_LOGIN_ALLOWED);
#: ``narrowed`` the handler refuses the part that names a program (REMOTE_LOGIN_NARROWED);
#: ``fixed``    it runs only what snowpea ships or the person saved (a catalogue entry,
#:              a saved server, a registered CLI), never caller text;
#: ``turn``     it starts an agent turn: every command the model runs meets the
#:              permission system (approvals, the security lists).
STARTS_A_PROCESS: dict[str, str] = {
    "mcp.add": "narrowed", "mcp.update": "narrowed", "mcp.test": "narrowed",
    "settings.set": "narrowed", "skill.install": "narrowed", "provider.configure": "narrowed",
    "job.schedule": "narrowed",
    "mcp.reload": "fixed", "mcp.remove": "fixed", "session.create": "fixed",
    "session.resume": "fixed", "lsp.install": "fixed", "lsp.restart": "fixed",
    "audio.install": "fixed", "audio.speak": "fixed", "audio.transcribe": "fixed",
    "audio.record.start": "fixed", "audio.record.stop": "fixed",
    "provider.loginWeb": "fixed", "external.create": "fixed", "external.open": "fixed",
    "external.send": "fixed", "checkpoint.restore": "fixed", "checkpoint.diff": "fixed",
    "file.complete": "fixed", "gateway.sync": "fixed", "system.reloadSettings": "fixed",
    "session.prompt": "turn", "session.steer": "turn", "session.continue": "turn",
    "command.run": "turn", "job.runNow": "turn", "agent.spawn": "turn", "team.start": "turn",
    "skill.create": "turn", "skill.learn": "turn", "agent.create": "turn",
    "system.update": "refused", "system.restart": "refused", "system.shutdown": "refused",
    "extension.enable": "refused", "extension.configure": "refused",
    "extension.reload": "refused", "external.clis.confirmPath": "refused",
    "project.trust.set": "refused", "mcp.preset.apply": "refused", "gateway.bind": "refused",
    "web.remote.listen": "refused", "tool.register": "refused", "setup.applyDefaults": "refused",
}


@pytest.mark.parametrize(("method", "how"), sorted(STARTS_A_PROCESS.items()))
def test_how_a_remote_login_meets_each_process_starter(method: str, how: str) -> None:
    allowed = method in settings_gate.REMOTE_LOGIN_ALLOWED
    try:
        person_gate.method_refusal(method, REMOTE)
    except RpcError:
        allowed = False
    assert allowed is (how != "refused"), method
    assert (method in settings_gate.REMOTE_LOGIN_NARROWED) is (how == "narrowed"), method


def test_the_process_table_names_registered_methods(tmp_path: Path) -> None:
    from snowpea_core.server.app_server import build_dispatcher

    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    assert set(STARTS_A_PROCESS) <= set(build_dispatcher(core).method_names())


# -- L-2: session.create {hostToolsFrom} ---------------------------------------------------------


@pytest.mark.parametrize(
    ("conn", "host", "reason"),
    [
        (PERSON, "snowpea-browser-A", None),
        (BROWSER, "b-1", None),  # itself, by clientId
        (BROWSER, "browser-s", None),  # itself, by surface id
        (BROWSER, "browser", None),  # the alias its standing orders and Tasks store
        (LEGACY, "browser", None),
        (REMOTE, "browser", "local_only"),
        (CONNECTOR, "browser", "person_only"),
        (BROWSER, "b-other", "person_only"),
        (LEGACY, "b-other", "person_only"),
        (REMOTE, "b-1", "local_only"),
        (REMOTE, "web-1", "local_only"),  # not even "itself": it hosts no tools
        (CONNECTOR, "b-1", "person_only"),
        (REMOTE, None, None),
        (None, "b-1", None),  # in-process callers are not judged here
    ],
)
def test_who_may_choose_a_sessions_host(conn: Any, host: str | None,
                                        reason: str | None) -> None:
    if reason is None:
        settings_gate.host_choice_refusal(conn, host)
        return
    with pytest.raises(RpcError) as caught:
        settings_gate.host_choice_refusal(conn, host)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert caught.value.data["reason"] == reason


async def test_session_create_refuses_before_a_session_exists(core: Core,
                                                              tmp_path: Path) -> None:
    from snowpea_core.server.protocol import SessionCreateParams
    from snowpea_core.server.session_handlers import session_create_handler

    with pytest.raises(RpcError) as caught:
        await session_create_handler(REMOTE, SessionCreateParams(
            workdir=str(tmp_path), hostToolsFrom="snowpea-browser-A"), core)
    _local_only(caught)
    assert len(core.sessions) == 0


# -- L-6: fs.* ---------------------------------------------------------------------------------

CONTROL = (".git/hooks/pre-commit", ".git/config", ".snowpea/hooks.json",
           ".snowpea/commands/x.md", ".snowpea/skills/x/SKILL.md", ".claude/settings.local.json",
           ".claude/hooks/run.sh", ".mcp.json", ".codex/config.toml", ".cursor/rules")
PLAIN = ("notes.txt", "src/app.py", ".github/workflows/ci.yml", "git/hooks/x")


def test_the_control_file_classifier(tmp_path: Path) -> None:
    for name in CONTROL:
        assert fs_handlers.control_file(tmp_path / name), name
    for name in PLAIN:
        assert not fs_handlers.control_file(tmp_path / name), name
    assert fs_handlers.control_file(tmp_path / ".GIT" / "Hooks" / "pre-commit")  # folded


MARKED = _conn("browser", client_id="b-1")  # the marker changes nothing for fs.write
NOT_HOSTED = errors.JARVIS_PERSON_ONLY


@pytest.mark.parametrize(
    ("conn", "hosted_by", "opened_by", "control", "plain", "read"),
    [
        (PERSON, None, None, None, None, None),
        (REMOTE, None, None, errors.PROTECTED, None, None),
        # A browser writes only into a session it hosts, and never a control file.
        (BROWSER, "b-1", None, errors.PROTECTED, None, None),
        (BROWSER, "browser", None, errors.PROTECTED, None, None),  # the alias
        (BROWSER, None, "b-1", NOT_HOSTED, NOT_HOSTED, None),  # opened, not hosted: reads only
        (BROWSER, "b-other", None, NOT_HOSTED, NOT_HOSTED, errors.NOT_FOUND),
        (BROWSER, None, None, NOT_HOSTED, NOT_HOSTED, errors.NOT_FOUND),  # an IDE's session
        # A legacy browser: the same rule for writes; reads as before.
        (LEGACY, "b-legacy", None, errors.PROTECTED, None, None),
        (LEGACY, None, None, NOT_HOSTED, NOT_HOSTED, None),
    ],
)
async def test_fs_by_kind(core: Core, tmp_path: Path, conn: Any, hosted_by: str | None,
                          opened_by: str | None, control: str | None, plain: str | None,
                          read: str | None) -> None:
    from snowpea_core.server.protocol import FsPathParams
    from snowpea_core.server.rpc import CURRENT_ACTOR

    work = tmp_path / "project"
    (work / ".git" / "hooks").mkdir(parents=True)
    session = await core.sessions.create(work)
    session.host_tools_from, session.origin_client_id = hosted_by, opened_by
    write = fs_handlers._handler(fs_handlers.fs_write, write=True)
    stat = fs_handlers._handler(fs_handlers.fs_stat)

    async def attempt(path: str, expected: str | None, mode: str = "overwrite") -> None:
        params = FsWriteParams(sessionId=session.id, path=path, dataBase64="aGk=", mode=mode)
        if expected is None:
            await write(conn, params, core)
            assert (work / path).read_bytes() == b"hi"
            (work / path).unlink()
            return
        with pytest.raises(RpcError) as caught:
            await write(conn, params, core)
        assert caught.value.code == expected
        if expected == NOT_HOSTED:
            assert caught.value.data["reason"] == "person_only"
        if expected == errors.PROTECTED:
            assert caught.value.data["reason"] == "local_only"
        assert not (work / path).exists()

    for marked in (False, True):  # marker or not: the same answers
        token = CURRENT_ACTOR.set("person" if marked else None)
        try:
            await attempt(".git/hooks/pre-commit", control)
            await attempt(".git/config", control)
            await attempt(".mcp.json", control)
            await attempt(".snowpea/hooks.json", control)
            await attempt("notes.txt", plain)
            await attempt("artifacts/report.pdf", plain, mode="create")  # the host's saves
        finally:
            CURRENT_ACTOR.reset(token)
    # A path outside the session's folders is refused for everyone who gets that far.
    outside = FsWriteParams(sessionId=session.id, path=str(tmp_path / "elsewhere.txt"),
                            dataBase64="aGk=", mode="create")
    with pytest.raises(RpcError) as caught:
        await write(conn, outside, core)
    assert caught.value.code == (plain or errors.OUTSIDE_ROOT)
    assert not (tmp_path / "elsewhere.txt").exists()
    # Reading: a session it hosts, or an unhosted one it opened.
    reading = stat(conn, FsPathParams(sessionId=session.id, path="."), core)
    if read is None:
        await reading
    else:
        with pytest.raises(RpcError) as caught:
            await reading
        assert caught.value.code == read


# -- Info: an unhandled handler error ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "remote", "shown"),
    [("ide", False, True), ("web", True, False), ("browser", False, False),
     ("connector", False, False)],
)
async def test_exception_text_stays_with_a_local_person(
        core: Core, kind: str, remote: bool, shown: bool,
        caplog: pytest.LogCaptureFixture) -> None:
    sent: list[dict[str, Any]] = []

    async def send(frame: dict[str, Any]) -> None:
        sent.append(frame)

    async def boom(_conn: Any, _params: Any, _core: Any) -> None:
        raise ValueError("could not read /home/owner/private/notes.txt")

    dispatcher = RpcDispatcher(core)
    dispatcher.register("system.health", boom, Empty)
    conn = RpcConnection(send)
    conn.authenticated, conn.client_kind, conn.remote_login = True, kind, remote
    conn.client_version = "0.3.0"
    with caplog.at_level("ERROR", logger="snowpea.rpc"):
        await dispatcher._dispatch(conn, {"jsonrpc": "2.0", "id": 7, "method": "system.health"})
    error = sent[0]["error"]
    assert error["data"]["code"] == errors.INTERNAL
    if shown:
        assert "private/notes.txt" in error["message"]
        return
    assert "notes.txt" not in json.dumps(error) and "ValueError" not in json.dumps(error)
    error_id = error["data"]["details"]["errorId"]
    assert error_id in error["message"]
    # The detail is in the daemon log, under the id the caller was given.
    assert any(error_id in record.getMessage() for record in caplog.records)
    assert "private/notes.txt" in caplog.text


# -- the re-decided browser classifications ------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "browser"),
    [
        ("agent.create", False),  # no browser calls these (the browser team's source check)
        ("agent.delete", False),
        ("fs.write", True),  # ... the host's saves, into a session it hosts (test_fs_by_kind)
        ("checkpoint.restore", True),  # ... its own sessions only (below)
        ("session.deleteSaved", True),
        ("session.close", True),
    ],
)
def test_browser_classifications_at_the_dispatcher(method: str, browser: bool) -> None:
    def passes(conn: Any, params: dict[str, Any]) -> bool:
        try:
            settings_gate.browser_method_refusal(method, params, conn, None)
            settings_gate.kind_method_refusal(method, params, conn, None)
        except RpcError as exc:
            assert exc.code == errors.JARVIS_PERSON_ONLY
            return False
        return True

    for conn, params in ((BROWSER, {}), (BROWSER, {"actor": "person"}), (LEGACY, {})):
        assert passes(conn, params) is browser, (method, conn.client_version, params)
    assert passes(REMOTE, {}) and passes(PERSON, {})
    assert not passes(CONNECTOR, {})


#: (who hosts the session, the surface that opened it) -> a browser's own?
SESSIONS = {
    "hosted": ("b-1", "ide"), "alias": ("browser", "ide"), "browser-opened": (None, "browser"),
    "another-browser": ("b-other", "ide"), "ide": (None, "ide"),
}
OWN = {"hosted", "alias", "browser-opened"}


@pytest.mark.parametrize("which", sorted(SESSIONS))
@pytest.mark.parametrize(
    ("method", "marker_opens_others"),
    [("checkpoint.restore", False), ("session.deleteSaved", True), ("session.close", True)],
)
async def test_a_browser_acts_on_its_own_sessions(core: Core, tmp_path: Path, method: str,
                                                  marker_opens_others: bool, which: str) -> None:
    session = await core.sessions.create(tmp_path)
    session.host_tools_from, session.origin_surface = SESSIONS[which]

    async def passes(conn: Any, actor: str | None = None) -> bool:
        try:
            await settings_gate.browser_session_refusal(conn, core, session.id, method=method,
                                                        actor=actor)
        except RpcError as exc:
            assert exc.code == errors.JARVIS_PERSON_ONLY
            assert exc.data["reason"] == "person_only"
            return False
        return True

    own = which in OWN
    assert await passes(BROWSER) is own
    assert await passes(BROWSER, "person") is (own or marker_opens_others)
    # A legacy browser counts as marked; "b-1" is another client to it.
    legacy_own = which in OWN - {"hosted"}
    assert await passes(LEGACY) is (legacy_own or marker_opens_others)
    for other in (PERSON, REMOTE, CONNECTOR, None):
        assert await passes(other)  # only a browser is judged here


async def test_the_session_rule_reads_a_stored_session_too(core: Core) -> None:
    rows = {"s-own": {"host_tools_from": None, "origin_surface": "browser"},
            "s-ide": {"host_tools_from": None, "origin_surface": "ide"}}

    async def session(session_id: str) -> Any:
        return rows.get(session_id)

    fake = SimpleNamespace(sessions=SimpleNamespace(get=lambda _i: None),
                           store=SimpleNamespace(session=session))
    await settings_gate.browser_session_refusal(BROWSER, fake, "s-own",
                                                method="session.deleteSaved")
    with pytest.raises(RpcError):
        await settings_gate.browser_session_refusal(BROWSER, fake, "s-ide",
                                                    method="session.deleteSaved")
    # Unknown: the handler answers not_found.  A bulk delete (no id) needs the marker.
    await settings_gate.browser_session_refusal(BROWSER, fake, "s-none",
                                                method="session.deleteSaved")
    with pytest.raises(RpcError):
        await settings_gate.browser_session_refusal(BROWSER, fake, None,
                                                    method="session.deleteSaved")
    await settings_gate.browser_session_refusal(BROWSER, fake, None,
                                                method="session.deleteSaved", actor="person")


async def test_the_handlers_apply_the_session_rule(core: Core, tmp_path: Path) -> None:
    from snowpea_core.server import checkpoint_handlers, session_handlers
    from snowpea_core.server.protocol import (
        CheckpointRestoreParams,
        SessionDeleteParams,
        SessionIdParams,
    )

    session = await core.sessions.create(tmp_path)  # an IDE's: not this browser's
    session.origin_surface = "ide"
    calls = (
        checkpoint_handlers.checkpoint_restore_handler(
            BROWSER, CheckpointRestoreParams(sessionId=session.id, id="a" * 12), core),
        session_handlers.session_delete_saved_handler(
            BROWSER, SessionDeleteParams(sessionId=session.id), core),
        session_handlers.session_close_handler(
            BROWSER, SessionIdParams(sessionId=session.id), core),
    )
    for call in calls:
        with pytest.raises(RpcError) as caught:
            await call
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert core.sessions.get(session.id) is session
    session.host_tools_from = "b-1"
    await session_handlers.session_close_handler(
        BROWSER, SessionIdParams(sessionId=session.id), core)
    assert core.sessions.get(session.id) is None


# -- L-2 in session.attach and in a job's template -------------------------------------------------


async def test_session_attach_checks_the_host(core: Core, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.server import host_handlers
    from snowpea_core.server.host_handlers import session_attach_handler
    from snowpea_core.server.protocol import SessionAttachParams

    # "b-other" is an open browser connection (else the call is invalid_params).
    monkeypatch.setattr(host_handlers.HOST_TOOLS, "live_browser", lambda *_a: object())
    session = await core.sessions.create(tmp_path)
    for conn, reason in ((REMOTE, "local_only"), (CONNECTOR, "person_only"),
                         (BROWSER, "person_only")):
        with pytest.raises(RpcError) as caught:
            await session_attach_handler(conn, SessionAttachParams(
                sessionId=session.id, hostToolsFrom="b-other"), core)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
        assert caught.value.data["reason"] == reason
    assert session.origin_conn is None and session.host_tools_from is None


@pytest.mark.parametrize(
    ("conn", "host", "allowed", "creator"),
    [
        (PERSON, "snowpea-browser-A", True, "person"),
        (PERSON, "browser", True, "person"),
        (BROWSER, "browser", True, "browser"),  # standing orders and Tasks
        (BROWSER, "b-1", True, "browser"),
        (BROWSER, "b-other", False, None),
        (REMOTE, "browser", False, None),
        (REMOTE, "b-1", False, None),
        (CONNECTOR, "browser", False, None),
    ],
)
async def test_a_jobs_host_is_judged_for_its_creator(core: Core, tmp_path: Path, conn: Any,
                                                     host: str, allowed: bool,
                                                     creator: str | None) -> None:
    from snowpea_core.server import job_handlers
    from snowpea_core.server.session_handlers import wire_core

    wire_core(core)
    params = JobScheduleParams(
        spec="every day at 9:00", task="x", workdir=str(tmp_path),
        sessionTemplate={"agent": "browser", "hostToolsFrom": host})
    if not allowed:
        with pytest.raises(RpcError) as caught:
            await job_handlers.job_schedule_handler(conn, params, core)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
        assert await core.scheduler.list() == []
        return
    made = await job_handlers.job_schedule_handler(conn, params, core)
    job = await core.scheduler.get(made.jobId)
    # What the scheduler's in-process session will use: judged now, kept with its creator.
    assert job.host_tools_from == host and job.created_by == creator


@pytest.mark.parametrize(
    ("conn", "channel", "allowed"),
    [
        (REMOTE, None, True),  # in-app: the session that made it, else the log
        (REMOTE, "log", True),
        (REMOTE, "telegram:12345", False),  # leaves the machine
        (REMOTE, "slack:C0123", False),
        (REMOTE, "discord:99", False),
        (REMOTE, "anything-else", False),
        (PERSON, "telegram:12345", True),
        (BROWSER, "telegram:12345", True),
    ],
)
async def test_a_remote_logins_job_delivers_in_app_only(core: Core, tmp_path: Path, conn: Any,
                                                        channel: str | None,
                                                        allowed: bool) -> None:
    from snowpea_core.server import job_handlers
    from snowpea_core.server.session_handlers import wire_core

    wire_core(core)
    params = JobScheduleParams(spec="every day at 9:00", task="x", workdir=str(tmp_path),
                               channel=channel)
    if allowed:
        made = await job_handlers.job_schedule_handler(conn, params, core)
        assert (await core.scheduler.get(made.jobId)).channel == channel
        return
    with pytest.raises(RpcError) as caught:
        await job_handlers.job_schedule_handler(conn, params, core)
    _local_only(caught)
    assert await core.scheduler.list() == []


#: job action (``job_handlers._guard``) -> may a remote login take it on a job
#: made by: itself, a local person, a browser, nobody recorded (an older job).
REMOTE_JOB_ACTIONS: dict[str, str] = {
    "update": "ynnn", "cancel": "ynnn", "delete": "ynnn", "run": "yyyy",
}
JOB_MAKERS = ("remote_login", "person", "browser", None)


@pytest.mark.parametrize(("action", "expected"), sorted(REMOTE_JOB_ACTIONS.items()))
async def test_a_remote_login_changes_only_the_jobs_it_created(core: Core, tmp_path: Path,
                                                               action: str,
                                                               expected: str) -> None:
    from snowpea_core.server import job_handlers
    from snowpea_core.server.session_handlers import wire_core

    wire_core(core)
    got = ""
    for maker in JOB_MAKERS:
        job = await core.scheduler.schedule("every day at 9:00", "x", workdir=str(tmp_path),
                                            created_via="rpc", created_by=maker)
        try:
            await job_handlers._guard(REMOTE, core, job.id, action)
            got += "y"
        except RpcError as exc:
            assert exc.code == errors.JARVIS_PERSON_ONLY and exc.data["reason"] == "local_only"
            got += "n"
        # Nobody else is narrowed by who made the job.
        for other in (PERSON, BROWSER):
            await job_handlers._guard(other, core, job.id, action)
    assert got == expected


async def test_the_job_handlers_apply_the_ownership_rule(core: Core, tmp_path: Path) -> None:
    from snowpea_core.server import job_handlers
    from snowpea_core.server.protocol import JobIdParams, JobUpdateParams
    from snowpea_core.server.session_handlers import wire_core

    wire_core(core)
    params = JobScheduleParams(spec="every day at 9:00", task="the owner's task",
                               workdir=str(tmp_path), channel="telegram:12345")
    owners = (await job_handlers.job_schedule_handler(PERSON, params, core)).jobId
    calls = (
        job_handlers.job_update_handler(REMOTE, JobUpdateParams(jobId=owners, task="mine"), core),
        job_handlers.job_update_handler(
            REMOTE, JobUpdateParams(jobId=owners, enabled=False), core),
        job_handlers.job_cancel_handler(REMOTE, JobIdParams(jobId=owners), core),
        job_handlers.job_delete_handler(
            REMOTE, job_handlers.JobDeleteParams(jobId=owners), core),
    )
    for call in calls:
        with pytest.raises(RpcError) as caught:
            await call
        _local_only(caught)
    kept = await core.scheduler.get(owners)
    assert kept.task == "the owner's task" and kept.enabled and kept.channel == "telegram:12345"
    # It reads it, and its own job is its to change, disable and delete.
    listed = (await job_handlers.job_list_handler(REMOTE, Empty(), core)).jobs
    assert [row.jobId for row in listed] == [owners]
    mine = (await job_handlers.job_schedule_handler(REMOTE, JobScheduleParams(
        spec="every day at 9:00", task="x", workdir=str(tmp_path)), core)).jobId
    await job_handlers.job_update_handler(REMOTE, JobUpdateParams(jobId=mine, task="y"), core)
    await job_handlers.job_update_handler(REMOTE, JobUpdateParams(jobId=mine, enabled=False), core)
    await job_handlers.job_delete_handler(REMOTE, job_handlers.JobDeleteParams(jobId=mine), core)
    assert await core.scheduler.get(mine) is None


def test_job_update_cannot_change_a_channel() -> None:
    """So a channel a local person saved stays as saved, whoever updates the job."""
    from snowpea_core.server.protocol import JobUpdateParams, JobUpdateTemplate

    assert "channel" not in JobUpdateParams.model_fields
    assert "channel" not in JobUpdateTemplate.model_fields


def test_a_stored_job_without_a_creator_loads_as_before() -> None:
    from snowpea_core.scheduler.jobs import _options_of

    class Row(dict):  # type: ignore[type-arg]
        def keys(self) -> Any:  # sqlite3.Row has keys() and item access
            return super().keys()

    old = _options_of(Row(options=json.dumps(  # type: ignore[arg-type]
        {"hostToolsFrom": "browser", "hostWaitSec": 5, "createdVia": "rpc"})))
    assert old["host_tools_from"] == "browser" and "created_by" not in old
    new = _options_of(Row(options=json.dumps(  # type: ignore[arg-type]
        {"createdBy": "remote_login"})))
    assert new["created_by"] == "remote_login"
    assert "created_by" not in _options_of(Row(options=json.dumps(  # type: ignore[arg-type]
        {"createdBy": "somebody-else"})))
