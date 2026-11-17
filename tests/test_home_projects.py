"""0.2.90: a project a PERSON registered inside snowpea's home is an ordinary place.

Field report (Windows, core 0.2.89): ``SNOWPEA_HOME`` was the person's own
folder ``snowpea.ai`` and their project lived inside it (``godgrade blog``);
since review 8's default-deny home (H8-2) every write there was refused.

Every path below is DATA handed to the pure checker or made inside pytest's
``tmp_path``; nothing runs.  Also the "(origin)" denial of an approval nobody
saw (a host reply without a decision).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.project import home_problem
from snowpea_core.security import control_plane as cp
from snowpea_core.security.check import Context, check_call, check_command
from snowpea_core.security.lists import from_raw
from snowpea_core.server.errors import RpcError
from snowpea_core.server.project_gate import (
    OWN_FOLDER,
    admit_home_project,
    forget_home_project,
    require_project,
)
from snowpea_core.tools import config_guard

DEFAULT = from_raw({})
PROJECT = "godgrade blog"
FILE = "posts/20261119_검토.html"


@pytest.fixture(autouse=True)
def _clean() -> Any:
    cp.clear_person_projects()
    yield
    cp.clear_person_projects()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    found = tmp_path / "snowpea.ai"
    (found / "security").mkdir(parents=True)
    (found / PROJECT / "posts").mkdir(parents=True)
    (found / "token").write_text("not a real token", encoding="utf-8")
    return found


def _ctx(home: Path, workdir: Path, locale: str | None = None) -> Context:
    return Context(workdir=workdir, user_home=home.parent / "user", home=home,
                   projects=(str(workdir),), mounts=(), env_venv=False, locale=locale)


def _core(home: Path) -> Any:
    return SimpleNamespace(paths=SimpleNamespace(home=home))


def _conn(kind: str | None = "ide") -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind=kind,
                           remote_login=False, client_version="0.3.0",
                           client_id=f"{kind}-1", surface_id=f"{kind}-s")


def _write(home: Path, rel: str, workdir: Path | None = None, locale: str | None = None) -> Any:
    target = home / rel
    return check_call("write_file", {"path": str(target), "content": "x"}, DEFAULT,
                      _ctx(home, workdir or home / PROJECT, locale), tags=("write",))


# -- the field report ---------------------------------------------------------------


def test_the_field_report_is_refused_with_what_to_do_until_a_person_registers(
        home: Path) -> None:
    before = _write(home, f"{PROJECT}/{FILE}")
    assert before.blocked
    assert "inside snowpea's data folder ($SNOWPEA_HOME)" in before.why
    assert "Register the folder as a project (open it in snowpea)" in before.why
    ko = _write(home, f"{PROJECT}/{FILE}", locale="ko")
    assert "데이터 폴더" in ko.why and "프로젝트로 등록" in ko.why

    assert admit_home_project(_core(home), _conn("ide"), str(home / PROJECT)) is True
    after = _write(home, f"{PROJECT}/{FILE}")
    assert not after.blocked and not after.opaque, after.why
    assert cp.control_plane_effect(home / PROJECT / FILE, "write", home=home) is None
    assert cp.control_plane_effect(home / PROJECT / FILE, "read", home=home) is None
    shell = check_command(f"echo hi > '{home / PROJECT / FILE}'", DEFAULT,
                          _ctx(home, home / PROJECT))
    assert not shell.blocked, shell.why
    # The file tools no longer re-tag it "config" (asked in auto mode) ...
    assert not config_guard.is_config_path(str(home / PROJECT / FILE), home=home)
    # ... but the project's own settings stay configuration, as in any project.
    assert config_guard.is_config_path(str(home / PROJECT / ".snowpea" / "settings.json"),
                                       home=home)
    # A session may work there now, and the project has its own .snowpea.
    assert home_problem(home / PROJECT, home) is None
    require_project(_core(home), str(home / PROJECT), dot=False)
    require_project(_core(home), str(home / PROJECT), dot=True)
    # Kept across a restart.
    saved = json.loads((home / "security" / "home-projects.json").read_text(encoding="utf-8"))
    assert saved["projects"] == [[PROJECT]]
    cp.clear_person_projects()
    assert cp.load_person_projects(home) == ((PROJECT,),)
    assert not _write(home, f"{PROJECT}/{FILE}").blocked


def test_snowpeas_own_files_stay_out_of_reach(home: Path) -> None:
    cp.add_person_project(home, home / PROJECT)
    for rel in ("token", "credentials.json", "daemon.json", "settings.json", "state.db",
                "skills/x/SKILL.md", "hooks.json", "security/home-projects.json",
                "some-new-file.json", f"{PROJECT}/../token"):
        verdict = _write(home, rel)
        assert verdict.blocked or verdict.opaque or config_guard.is_config_path(
            str(home / rel), home=home), rel
    for rel in ("token", "credentials.json", "daemon.json", f"{PROJECT}/../token"):
        assert cp.control_plane_effect(home / rel, "read", home=home) is not None, rel
    # A link inside the project to a secret is judged where it points.
    (home / PROJECT / "t").symlink_to(home / "token")
    assert cp.control_plane_effect(home / PROJECT / "t", "read", home=home) is not None
    assert _write(home, f"{PROJECT}/t").blocked
    # Another folder under the home is still not a project.
    (home / "other").mkdir()
    assert _write(home, "other/a.txt").blocked


# -- who may register ------------------------------------------------------------------


@pytest.mark.parametrize("conn", [None, _conn("browser"), _conn("connector-x"),
                                  SimpleNamespace(authenticated=True, closed=True,
                                                  client_kind="ide", remote_login=False)])
def test_only_a_person_registers(home: Path, conn: Any) -> None:
    (home / "agent-made").mkdir()
    assert admit_home_project(_core(home), conn, str(home / "agent-made")) is False
    assert cp.person_projects(home) == ()
    assert _write(home, "agent-made/x.txt", workdir=home / "agent-made").blocked
    with pytest.raises(RpcError):
        require_project(_core(home), str(home / "agent-made"), dot=False)


def test_an_agent_cannot_make_the_folder_either(home: Path) -> None:
    line = f"mkdir -p '{home}/new folder' && echo x > '{home}/new folder/a.txt'"
    assert check_command(line, DEFAULT, _ctx(home, home / PROJECT)).blocked
    # Nor write the record of registered projects.
    record = home / "security" / "home-projects.json"
    assert check_command(f"echo '{{}}' > '{record}'", DEFAULT, _ctx(home, home)).blocked
    assert _write(home, "security/home-projects.json").blocked


def test_a_registered_project_reached_by_a_non_person_stays_usable(home: Path) -> None:
    admit_home_project(_core(home), _conn("tui"), str(home / PROJECT))
    assert admit_home_project(_core(home), _conn("browser"), str(home / PROJECT)) is True
    forget_home_project(_core(home), str(home / PROJECT))
    assert _write(home, f"{PROJECT}/{FILE}").blocked


@pytest.mark.parametrize("name", ["skills", "hooks", "sessions", "jarvis", "plugins", "agents",
                                  "extensions", "extension-data", "trust", "security", "logs",
                                  "Skills", "SKILLS.", "skills ", ".claude", ".snowpea", "token2",
                                  "credentials-old", "state.db-wal", "mcp-servers", "lsp",
                                  "SKILLS~1", "skills::$DATA", "settings.json.d"])
def test_snowpeas_own_names_are_never_projects(home: Path, name: str) -> None:
    assert cp.own_name(name), name
    folder = home / name
    assert cp.home_project_problem(folder, home) == "own"
    with pytest.raises(ValueError):
        cp.add_person_project(home, folder)
    with pytest.raises(RpcError) as caught:
        admit_home_project(_core(home), _conn("ide"), str(folder))
    assert caught.value.message == OWN_FOLDER and caught.value.data["what"] == "own"
    assert cp.person_projects(home) == ()


def test_the_home_itself_and_a_link_to_an_own_folder_are_never_projects(home: Path) -> None:
    assert cp.home_project_problem(home, home) == "home"
    (home / "skills").mkdir()
    (home / "my skills").symlink_to(home / "skills")
    assert cp.home_project_problem(home / "my skills", home) == "own"
    (home / "home again").symlink_to(home)
    assert cp.home_project_problem(home / "home again", home) == "home"
    assert cp.home_project_problem(home.parent / "elsewhere", home) == "outside"


def test_a_tampered_record_never_names_an_own_folder(home: Path) -> None:
    record = home / "security" / "home-projects.json"
    record.write_text(json.dumps({"projects": [["skills"], [".."], ["a/b"], [PROJECT], "x"]}),
                      encoding="utf-8")
    assert cp.load_person_projects(home) == ((PROJECT,),)
    assert config_guard.is_config_path(str(home / "skills/x/SKILL.md"), home=home)
    assert check_command(f"echo x > {home}/skills/a", DEFAULT, _ctx(home, home / PROJECT)).opaque


def test_ordinary_names_are_projects() -> None:
    for name in ("godgrade blog", "projects", "work", "블로그", "my-site", "notes"):
        assert not cp.own_name(name), name


# -- Windows -----------------------------------------------------------------------------


def test_windows_paths_fold_case_and_take_both_separators() -> None:
    home = PureWindowsPath(r"C:\Users\limkt\snowpea.ai")
    cp._PERSON_PROJECTS[cp._home_key(str(home))] = ((PROJECT,),)
    for raw in (r"C:\Users\limkt\snowpea.ai\godgrade blog\posts\20261119_검토.html",
                "C:/Users/limkt/snowpea.ai/GODGRADE BLOG/posts/x.html"):
        parts = PureWindowsPath(raw).parts[len(home.parts):]
        assert cp.person_project(parts, str(home), fold=True) == (PROJECT,), raw
    upper = PureWindowsPath("C:/Users/limkt/snowpea.ai/GODGRADE BLOG/x").parts[len(home.parts):]
    assert cp.person_project(upper, str(home), fold=False) is None
    for raw in (r"C:\Users\limkt\snowpea.ai\Skills\x", r"C:\Users\limkt\snowpea.ai\TOKEN"):
        parts = PureWindowsPath(raw).parts[len(home.parts):]
        assert cp.person_project(parts, str(home), fold=True) is None
        assert cp.judge_parts(parts, "write", fold=True, home=str(home)) is not None


# -- the "(origin)" denial nobody saw ---------------------------------------------------


@pytest.mark.parametrize("reply", [{"result": None}, {}, {"decision": "maybe"}])
def test_an_origin_reply_without_a_decision_says_so(reply: dict[str, Any]) -> None:
    from snowpea_core.permissions.approval_queue import ApprovalQueue, _Pending
    from snowpea_core.server.protocol import ApprovalRequest

    async def run() -> Any:
        class Origin:
            async def call(self, method: str, params: dict[str, Any], timeout: Any = None
                           ) -> dict[str, Any]:
                return reply

        request = ApprovalRequest(requestId="r", sessionId="s", tool="write_file", args={})
        entry = _Pending(request=request, future=asyncio.get_running_loop().create_future(),
                         unattended=False, origin_conn=Origin())
        await ApprovalQueue()._ask_origin(entry)
        return entry.future.result()

    decision = asyncio.run(run())
    assert not decision.allowed and decision.by == "origin-no-answer"
    assert "could not show the approval" in decision.reason


def test_an_origin_error_is_named() -> None:
    from snowpea_core.permissions.approval_queue import ApprovalQueue, _Pending
    from snowpea_core.server.protocol import ApprovalRequest

    async def run() -> Any:
        class Origin:
            async def call(self, method: str, params: dict[str, Any], timeout: Any = None
                           ) -> dict[str, Any]:
                raise RpcError("internal", "method not found: approval.request")

        request = ApprovalRequest(requestId="r", sessionId="s", tool="write_file", args={})
        entry = _Pending(request=request, future=asyncio.get_running_loop().create_future(),
                         unattended=False, origin_conn=Origin())
        await ApprovalQueue()._ask_origin(entry)
        return entry.future.result()

    decision = asyncio.run(run())
    assert decision.by == "origin-unreachable"
    assert "method not found" in decision.reason


def test_a_real_deny_stays_the_origins() -> None:
    from snowpea_core.permissions.approval_queue import ApprovalQueue, _Pending
    from snowpea_core.server.protocol import ApprovalRequest

    async def run() -> Any:
        class Origin:
            async def call(self, method: str, params: dict[str, Any], timeout: Any = None
                           ) -> dict[str, Any]:
                return {"decision": "deny", "reason": "not now"}

        request = ApprovalRequest(requestId="r", sessionId="s", tool="write_file", args={})
        entry = _Pending(request=request, future=asyncio.get_running_loop().create_future(),
                         unattended=False, origin_conn=Origin())
        await ApprovalQueue()._ask_origin(entry)
        return entry.future.result()

    decision = asyncio.run(run())
    assert decision.by == "origin" and decision.reason == "not now"


# -- over the wire ------------------------------------------------------------------------


async def test_session_create_and_workspace_add_over_the_wire(tmp_path: Path) -> None:
    import aiohttp
    from _support import connect, make_daemon

    home = tmp_path / "snowpea.ai"
    (home / PROJECT).mkdir(parents=True)
    (home / "skills").mkdir()
    daemon = await make_daemon(home)
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            for method, key, folder in (("session.create", "workdir", "skills"),
                                        ("workspace.add", "path", "Hooks"),
                                        ("session.create", "workdir", "jarvis")):
                params: dict[str, Any] = {key: str(home / folder)}
                if method == "session.create":
                    params["mode"] = "auto"
                frame = await client.call(method, params)
                assert frame.get("error"), (method, folder)
                assert frame["error"]["data"]["details"]["what"] == "own", frame["error"]
            created = await client.ok("session.create",
                                      {"workdir": str(home / PROJECT), "mode": "auto"})
            assert created["sessionId"]
            assert cp.person_projects(home) == ((PROJECT,),)
            assert not _write(home, f"{PROJECT}/{FILE}").blocked
            other = home / "second project"
            other.mkdir()
            await client.ok("workspace.add", {"path": str(other)})
            assert ("second project",) in cp.person_projects(home)
    finally:
        await daemon.stop()
