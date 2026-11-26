"""1.72.0 (capability sessionPaths): results go to the project, or to the session's own
folder without one; downloads beside them; only throwaway files in scratch.

Feedback 308ac33d: downloads went to ~/Downloads and the agent could not find them.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from _support import env_vars
from test_host_tools import Daemon as SocketDaemon  # noqa: F401
from test_host_tools import daemon, new_session, open_client, run_turn, spec  # noqa: F401

from snowpea_core.agent import verify_gate
from snowpea_core.agent.subagent import inherit_host
from snowpea_core.prompts import environment as prompt_env
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION
from snowpea_core.session import paths

WS = "/h/sessions/2026-11-23_s1"


def _session(**extra: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": "s1",
        "workdir": "/w/proj",
        "workspace_dir": WS,
        "parent_session_id": None,
        "kind": "chat",
    }
    base.update(extra)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# resolver
# ---------------------------------------------------------------------------


def test_a_project_keeps_results_in_the_workdir() -> None:
    got = paths.resolve(_session())
    assert got.has_project is True
    assert got.results_dir == str(Path("/w/proj").resolve())
    assert got.downloads_dir == str(Path("/w/proj").resolve() / ".snowpea-downloads")
    assert got.scratch_dir == f"{WS}/tmp"
    assert got.wire()["workspaceDir"] == WS


def test_no_project_puts_results_in_the_workspace_artifacts() -> None:
    got = paths.resolve(_session(no_project=True))
    assert got.has_project is False
    assert got.results_dir == f"{WS}/artifacts"
    assert got.downloads_dir == f"{WS}/artifacts/.snowpea-downloads"
    assert got.scratch_dir == f"{WS}/tmp"
    assert got.wire() == {
        "workdir": str(Path("/w/proj").resolve()),
        "workspaceDir": WS,
        "resultsDir": f"{WS}/artifacts",
        "downloadsDir": f"{WS}/artifacts/.snowpea-downloads",
        "scratchDir": f"{WS}/tmp",
        "hasProject": False,
    }


def test_an_old_session_without_the_flag_keeps_the_workdir() -> None:
    old = SimpleNamespace(id="s0", workdir="/w/old", workspace_dir=WS)  # no no_project at all
    assert paths.resolve(old).results_dir == str(Path("/w/old").resolve())


def test_resolving_creates_nothing(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    got = paths.resolve(_session(workdir=str(project), workspace_dir=None), home=tmp_path / "h")
    assert got.scratch_dir and got.scratch_dir.startswith(str(tmp_path / "h" / "sessions"))
    assert list(project.iterdir()) == [] and not (tmp_path / "h").exists()


def test_a_child_follows_its_root() -> None:
    root = _session(no_project=True)
    child = _session(id="c1", parent_session_id="s1", kind="subagent")
    core = SimpleNamespace(sessions={"s1": root})
    assert paths.no_project(child, core) is True
    assert paths.resolve(child, core).results_dir == f"{WS}/artifacts"
    # A scheduled run is its own root.
    scheduled = _session(id="j1", parent_session_id="s1", kind="scheduled")
    assert paths.no_project(scheduled, core) is False


def test_inherit_host_copies_the_flag() -> None:
    parent = _session(no_project=True, host_tools_from=None, origin_client_id=None)
    child = SimpleNamespace(agent_guard=False)
    inherit_host(child, parent)
    assert child.no_project is True and child.workspace_dir == WS


def test_core_seals_a_downloads_folder_it_creates(tmp_path: Path) -> None:
    got = paths.resolve(_session(workdir=str(tmp_path), workspace_dir=str(tmp_path / "ws")))
    folder = paths.ensure_downloads(got)
    assert folder == tmp_path / ".snowpea-downloads"
    assert (folder / ".gitignore").read_text() == "*\n"


def test_an_existing_gitignore_is_never_overwritten(tmp_path: Path) -> None:
    folder = tmp_path / ".snowpea-downloads"
    folder.mkdir()
    (folder / ".gitignore").write_text("keep-me\n")
    paths.seal_downloads(folder)
    assert (folder / ".gitignore").read_text() == "keep-me\n"


@pytest.mark.asyncio
async def test_a_core_write_into_downloads_seals_it(tmp_path: Path) -> None:
    from snowpea_core.exec.local import LocalBackend

    backend = LocalBackend(tmp_path)
    await backend.write_bytes(str(tmp_path / ".snowpea-downloads" / "a" / "x.bin"), b"1")
    await backend.write_file(str(tmp_path / "src" / "m.py"), "x = 1\n")
    assert (tmp_path / ".snowpea-downloads" / ".gitignore").read_text() == "*\n"
    assert not (tmp_path / "src" / ".gitignore").exists()


def test_snowpea_downloads_is_not_the_snowpea_control_folder(tmp_path: Path) -> None:
    """No prefix match either way: a download is no control file, and .snowpea stays one."""
    from snowpea_core.security import check
    from snowpea_core.tools.config_guard import is_config_path, is_snowpea_config_path
    from snowpea_core.trust.projects import control_path

    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    for name in ("settings.json", "hooks.json", "hooks/x.sh", "skills/s/SKILL.md"):
        planted = proj / ".snowpea-downloads" / name
        assert not control_path(planted), name
        assert not is_config_path(str(planted), workdir=proj, home=home), name
        assert not is_snowpea_config_path(str(planted), workdir=proj, home=home), name
        real = proj / ".snowpea" / name
        assert control_path(real), name
        assert is_config_path(str(real), workdir=proj, home=home), name
    assert not control_path(proj / ".snowpea-downloads")
    assert control_path(proj / ".snowpea")
    # A shell line naming the downloads folder does not name snowpea; .snowpea still does.
    assert not check._SNOWPEA_WORD.search("curl -o .snowpea-downloads/a.pdf https://x")
    assert check._SNOWPEA_WORD.search("cat .snowpea/settings.json")


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------


def test_the_prompt_names_the_resolved_folders() -> None:
    env = prompt_env.Environment(
        workdir="/w/proj",
        workspace=WS,
        results_dir=f"{WS}/artifacts",
        downloads_dir=f"{WS}/artifacts/.snowpea-downloads",
    )
    text = prompt_env.environment_lines(env)
    assert f"Reports, documents, exports and other results go in {WS}/artifacts" in text
    assert (
        "Downloaded files go in .snowpea-downloads/ in the results folder: "
        f"{WS}/artifacts/.snowpea-downloads; use glob or grep with path set to that folder"
    ) in text
    assert f"Only throwaway files go in {WS}/tmp" in text
    assert "Code for a coding project goes in the project tree where it belongs." in text
    assert "If the person names a place, use it." in text


def test_the_prompt_defaults_to_the_workdir() -> None:
    text = prompt_env.environment_lines(prompt_env.Environment(workdir="/w", workspace=WS))
    assert "other results go in /w\n" in text
    assert (
        "Downloaded files go in .snowpea-downloads/ in the project: /w/.snowpea-downloads; "
        "use glob or grep with path set to that folder"
    ) in text


# ---------------------------------------------------------------------------
# verify gate
# ---------------------------------------------------------------------------


def _gate_session(**extra: Any) -> SimpleNamespace:
    return _session(origin_surface="tui", verify_turn=verify_gate.TurnEvidence(), **extra)


def _core() -> Any:
    return SimpleNamespace(settings=SimpleNamespace(agent=SimpleNamespace(verifyOnStop="on")))


def test_a_deliverable_in_os_downloads_is_sent_back_once() -> None:
    session = _gate_session(no_project=True)
    home = str(Path("~").expanduser())
    text = f"Saved the PDF to {home}/Downloads/report.pdf."
    first = verify_gate.nudge(_core(), session, text)
    assert first and f"{home}/Downloads/report.pdf" in first
    assert "Downloads folder" in first and f"{WS}/artifacts" in first
    assert verify_gate.nudge(_core(), session, text) is None  # once only


def test_the_tilde_form_is_caught_and_the_budget_is_shared() -> None:
    session = _gate_session()
    assert verify_gate.os_downloads_deliverable(session, "see ~/Downloads/a.csv") == (
        "~/Downloads/a.csv"
    )
    session.verify_turn.nudges = verify_gate.MAX_NUDGES
    assert verify_gate.nudge(_core(), session, "see ~/Downloads/a.csv") is None


def test_a_project_inside_downloads_is_fine() -> None:
    home = str(Path("~").expanduser())
    project = f"{home}/Downloads/myproj"
    session = _gate_session(workdir=project)
    assert verify_gate.nudge(_core(), session, f"wrote {project}/out.md") is None


def test_the_scratch_nudge_names_the_results_folder() -> None:
    session = _gate_session(no_project=True)
    reply = verify_gate.nudge(_core(), session, f"see {WS}/tmp/post.html")
    assert reply and "scratch space" in reply and f"{WS}/artifacts" in reply


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------

def test_capability_and_version() -> None:
    assert "sessionPaths" in CAPABILITIES and PROTOCOL_VERSION == "1.80.0"


@pytest.mark.asyncio
async def test_session_create_and_session_paths(
    http: aiohttp.ClientSession,
    daemon: SocketDaemon,  # noqa: F811
    tmp_path: Path,
) -> None:
    client = await open_client(http, daemon)
    try:
        (tmp_path / "p").mkdir()
        (tmp_path / "d").mkdir()
        project = await client.ok("session.create", {"workdir": str(tmp_path / "p")})
        loose = await client.ok(
            "session.create", {"workdir": str(tmp_path / "d"), "noProject": True}
        )
        ws = loose["workspaceDir"]
        assert project["hasProject"] is True
        assert project["resultsDir"] == str((tmp_path / "p").resolve())
        assert loose["hasProject"] is False and loose["resultsDir"] == f"{ws}/artifacts"
        assert loose["downloadsDir"] == f"{ws}/artifacts/.snowpea-downloads"
        assert loose["scratchDir"] == f"{ws}/tmp"
        got = await client.ok("session.paths", {"sessionId": loose["sessionId"]})
        assert got == {
            "workdir": str((tmp_path / "d").resolve()),
            "workspaceDir": ws,
            "resultsDir": f"{ws}/artifacts",
            "downloadsDir": f"{ws}/artifacts/.snowpea-downloads",
            "scratchDir": f"{ws}/tmp",
            "hasProject": False,
        }
        # Nothing was made inside the project.
        assert list((tmp_path / "p").iterdir()) == []
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_session_artifacts_lists_a_projects_downloads(
    http: aiohttp.ClientSession,
    daemon: SocketDaemon,  # noqa: F811
    tmp_path: Path,
) -> None:
    client = await open_client(http, daemon)
    try:
        (tmp_path / "p").mkdir()
        created = await client.ok("session.create", {"workdir": str(tmp_path / "p")})
        sid = created["sessionId"]
        assert (await client.ok("session.artifacts", {"sessionId": sid}))["artifacts"] == []
        (tmp_path / "p" / ".snowpea-downloads" / "sub").mkdir(parents=True)
        (tmp_path / "p" / ".snowpea-downloads" / "sub" / "a.pdf").write_bytes(b"%PDF")
        (tmp_path / "p" / ".snowpea-downloads" / ".gitignore").write_text("*\n")
        (Path(created["workspaceDir"]) / "artifacts" / "r.md").write_text("x", encoding="utf-8")
        listed = await client.ok("session.artifacts", {"sessionId": sid})
        names = sorted(a["name"] for a in listed["artifacts"])
        assert names == [".snowpea-downloads/sub/a.pdf", "r.md"]  # not its .gitignore
        row = next(
            a for a in listed["artifacts"] if a["name"] == ".snowpea-downloads/sub/a.pdf"
        )
        assert row["path"] == str(tmp_path / "p" / ".snowpea-downloads" / "sub" / "a.pdf")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_tool_invoke_carries_the_folders(
    http: aiohttp.ClientSession,
    daemon: SocketDaemon,  # noqa: F811
    tmp_path: Path,
) -> None:
    host = await open_client(http, daemon)
    try:
        await host.ok("tool.register", {"tools": [spec("host_echo")]})
        host.handlers["host_echo"] = lambda p: {"ok": True, "output": "ok"}
        sid = await new_session(host, tmp_path / "w", noProject=True)
        assert await run_turn(host, sid, "use host echo") == "complete"
        call = host.invocations[-1]
        ws = call["workspaceDir"]
        assert call["resultsDir"] == f"{ws}/artifacts"
        assert call["downloadsDir"] == f"{ws}/artifacts/.snowpea-downloads"
        assert call["scratchDir"] == f"{ws}/tmp"
        assert call["hasProject"] is False
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_no_project_survives_a_restart(tmp_path: Path) -> None:
    from snowpea_core.server.protocol import SessionCreateParams
    from snowpea_core.server.session_handlers import session_create_handler

    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "w"
    work.mkdir()
    conn = SimpleNamespace(
        client_id=None, client_kind=None, surface_id="t", closed=False, on_close=[]
    )
    with env_vars(SNOWPEA_PROVIDER=None):
        first = Daemon(port=0, home=home)
        await first.start()
        try:
            loose = await session_create_handler(
                conn,  # type: ignore[arg-type]
                SessionCreateParams(workdir=str(work), noProject=True),
                first.core,
            )
            plain = await session_create_handler(
                conn,  # type: ignore[arg-type]
                SessionCreateParams(workdir=str(work)),
                first.core,
            )
        finally:
            await first.stop()
        second = Daemon(port=0, home=home)
        await second.start()
        try:
            core = second.core
            a = await core.sessions.restore(loose.sessionId)
            b = await core.sessions.restore(plain.sessionId)
            assert a is not None and b is not None
            assert a.no_project is True and b.no_project is False
            assert paths.resolve(a, core).results_dir.endswith("/artifacts")
            assert paths.resolve(b, core).results_dir == str(work.resolve())
        finally:
            await second.stop()
