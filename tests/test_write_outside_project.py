"""snowpea-browser addendum 29: writes outside the project, verify-gate opt-outs, C5."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent import verify_gate
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.lsp import servers
from snowpea_core.permissions.allowlist import DIR_TARGET, Allowlist, pattern_for_dir
from snowpea_core.permissions.approval_queue import OUTSIDE_REASON, ApprovalQueue
from snowpea_core.permissions.policy import MODE_MATRIX, PermissionPolicy
from snowpea_core.server.protocol import CAPABILITIES
from snowpea_core.session.session import Session
from snowpea_core.tools.config_guard import outside_paths, permission_for_write
from snowpea_core.tools.registry import ToolResult


@pytest.fixture
def layout(tmp_path: Path) -> SimpleNamespace:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    session = Session(id="s-out", workdir=project)
    core = SimpleNamespace(paths=SimpleNamespace(home=home))
    return SimpleNamespace(
        project=project, home=home, elsewhere=elsewhere, session=session, core=core
    )


# ---------------------------------------------------------------------------
# C1: the outside tag
# ---------------------------------------------------------------------------


def test_a_write_outside_the_project_is_tagged_outside(layout: SimpleNamespace) -> None:
    def tag(path: str) -> str:
        return permission_for_write({"path": path}, layout.session, layout.core)

    assert tag("src/a.py") == "write"
    assert tag(str(layout.project / ".snowpea" / "worktrees" / "w" / "a.py")) == "write"
    assert tag("../outside-note.txt") == "outside"
    assert tag(str(layout.elsewhere / "x.txt")) == "outside"
    # snowpea's own working areas are not outside; its settings stay config.
    assert tag(str(layout.home / "plans" / "p.md")) == "write"
    assert tag(str(layout.home / "sessions" / "2026_x" / "tmp" / "s.py")) == "write"
    assert tag(str(layout.home / "settings.json")) == "config"
    # A batched patch is outside when any of its files is.
    batched = {"edits": [{"path": "a.py"}, {"path": str(layout.elsewhere / "b.py")}]}
    assert permission_for_write(batched, layout.session, layout.core) == "outside"
    assert outside_paths(batched, layout.session, layout.home) == [
        str((layout.elsewhere / "b.py").resolve())
    ]


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_symlink_out_of_the_project_counts_as_outside(layout: SimpleNamespace) -> None:
    (layout.project / "link").symlink_to(layout.elsewhere)
    tag = permission_for_write({"path": "link/x.txt"}, layout.session, layout.core)
    assert tag == "outside"


def test_the_matrix_denies_asks_and_allows() -> None:
    assert [MODE_MATRIX[mode]["outside"] for mode in ("plan", "accept", "auto")] == [
        "deny",
        "ask",
        "allow",
    ]
    assert "writeOutsideAsks" in CAPABILITIES


def _policy(tmp_path: Path) -> tuple[PermissionPolicy, Allowlist]:
    allowlist = Allowlist(Paths.create(tmp_path / "home"), Settings())
    return PermissionPolicy(allowlist), allowlist


def test_only_a_folder_rule_promotes_an_outside_write(
    tmp_path: Path, layout: SimpleNamespace
) -> None:
    policy, allowlist = _policy(tmp_path)
    tool = SimpleNamespace(name="write_file", permission="write")
    args = {"path": str(layout.elsewhere / "notes" / "a.txt")}
    decide = lambda: policy.decide("accept", "outside", tool, args, layout.session)  # noqa: E731
    assert decide() == "ask"
    # "always write_file" must not open every folder.
    allowlist.add("^write_file$", "global", "tool:write_file")
    assert decide() == "ask"
    allowlist.add(pattern_for_dir(str(layout.elsewhere.resolve())), "global", DIR_TARGET)
    assert decide() == "allow"
    other = {"path": str(tmp_path / "third" / "b.txt")}
    assert policy.decide("accept", "outside", tool, other, layout.session) == "ask"
    assert policy.decide("plan", "outside", tool, args, layout.session) == "deny"


async def test_the_approval_carries_the_reason_and_a_folder_answer_is_kept(
    tmp_path: Path, layout: SimpleNamespace
) -> None:
    allowlist = Allowlist(Paths.create(tmp_path / "home"), Settings())
    queue = ApprovalQueue(allowlist=allowlist)
    asked: list[dict[str, Any]] = []
    answer = {"decision": "allow", "scope": "session"}

    class _Origin:
        async def call(self, method: str, params: dict[str, Any], timeout: float = 0) -> Any:
            asked.append(params)
            return answer

    layout.session.origin_conn = _Origin()
    target = str((layout.elsewhere / "a.txt").resolve())
    decision = await queue.request(
        layout.session,
        "write_file",
        {"path": target},
        reason_code=OUTSIDE_REASON,
        paths=[target],
    )
    assert decision.allowed
    assert asked[0]["reasonCode"] == "outside_project" and asked[0]["paths"] == [target]
    # "For this session" covers that folder, not another one.
    again = await queue.request(
        layout.session, "write_file", {"path": target}, reason_code=OUTSIDE_REASON, paths=[target]
    )
    assert again.by == "cache" and len(asked) == 1
    other = str(tmp_path / "third" / "b.txt")
    await queue.request(
        layout.session, "write_file", {"path": other}, reason_code=OUTSIDE_REASON, paths=[other]
    )
    assert len(asked) == 2
    # "Always" stores a folder rule, never a tool rule.
    answer["scope"] = "always"
    third = str(tmp_path / "fourth" / "c.txt")
    await queue.request(
        layout.session, "write_file", {"path": third}, reason_code=OUTSIDE_REASON, paths=[third]
    )
    rules = [(i.target, i.pattern, i.reason_code) for i in allowlist.list("global")]
    assert rules == [(DIR_TARGET, pattern_for_dir(str(tmp_path / "fourth")), OUTSIDE_REASON)]
    assert allowlist.covers_paths([str(tmp_path / "fourth" / "deep" / "d.txt")])
    assert not allowlist.covers_paths([str(tmp_path / "fourthx" / "d.txt")])


# ---------------------------------------------------------------------------
# C3: verify-on-stop respects a declined check and the user's words
# ---------------------------------------------------------------------------


def _gate_core() -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(agent=SimpleNamespace(verifyOnStop="on")))


def _edited(tmp_path: Path, text: str = "고쳐줘", path: str = "src/a.py") -> Session:
    session = Session(id="s-gate", workdir=tmp_path)
    verify_gate.begin_turn(session, text)
    verify_gate.observe(session, "patch", {"path": path}, ToolResult(ok=True, path=path))
    return session


@pytest.mark.parametrize(
    ("text", "declines"),
    [
        ("검사 명령은 돌리지 말고 고쳐줘", True),
        ("테스트는 실행하지 마", True),
        ("테스트는 생략하고 수정만", True),
        ("테스트 안 돌려도 돼", True),
        ("don't run the tests", True),
        ("skip the tests.", True),
        ("테스트 돌려줘", False),
        ("don't break the tests", False),
        ("run the checks", False),
        ("빌드하지 못하는 문제 고쳐줘", False),
        ("테스트하지 않은 코드가 있어서 테스트 추가해줘", False),
        ("테스트를 돌리지 않으면 실패를 알 수 없어. 테스트 돌려서 확인해", False),
        ("테스트 없이 배포했더니 깨졌어, 테스트 추가해줘", False),
        ("체크박스 하지 말고 라디오로 바꿔줘", False),
        ("테스트케이스 생략된 부분 추가", False),
        ("this module has no tests, add some", False),
        ("don't skip the tests", False),
        ("skip tests that are flaky and fix the rest", False),
    ],
)
def test_the_users_own_no_checks(text: str, declines: bool) -> None:
    assert verify_gate.user_declines_checks(text) is declines


def test_a_user_who_said_no_checks_is_not_nudged(tmp_path: Path) -> None:
    session = _edited(tmp_path, "검사 명령은 돌리지 말고 고쳐줘")
    assert verify_gate.nudge(_gate_core(), session) is None


def test_a_declined_check_ends_with_one_closing_line(tmp_path: Path) -> None:
    session = _edited(tmp_path)
    verify_gate.note_declined(session, "shell", {"command": "npx tsc"}, "exec")
    closing = verify_gate.nudge(_gate_core(), session) or ""
    assert "not approved" in closing and "unverified" in closing
    assert verify_gate.nudge(_gate_core(), session) is None


def test_the_nudge_puts_the_users_opt_out_first(tmp_path: Path) -> None:
    session = _edited(tmp_path)
    assert "Unless the user asked you not to run checks" in (
        verify_gate.nudge(_gate_core(), session) or ""
    )


def test_a_ui_look_is_still_asked_for_after_no_checks(tmp_path: Path) -> None:
    session = _edited(tmp_path, "don't run the tests", "src/App.vue")
    nudge = verify_gate.nudge(_gate_core(), session) or ""
    assert "browser" in nudge and "Do not run tests" in nudge


# ---------------------------------------------------------------------------
# C5: a command-only override of a default-off builtin still serves its files
# ---------------------------------------------------------------------------


def test_a_command_only_ruff_override_keeps_ruffs_extensions() -> None:
    ruff = servers.catalog(overrides={"ruff": {"command": ["ruff", "server"]}})["ruff"]
    assert ruff.serves(".py") and ruff.extensions
    assert ruff.candidates == (("ruff", ("server",)),)


def test_a_malformed_paths_argument_cannot_hide_an_outside_write(
    layout: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.tools import config_guard
    from snowpea_core.tools.registry import effective_permission

    for bad in ({"paths": 5}, {"edits": 5}):
        args = {"path": "../o.txt", "content": "x", **bad}
        assert permission_for_write(args, layout.session, layout.core) == "outside"
    # Even a guard that raises does not let the write through as a plain write.
    from snowpea_core.tools import call_paths as call_paths_mod

    def explode(_args: Any) -> list[str]:
        raise TypeError("boom")

    monkeypatch.setattr(call_paths_mod, "call_paths", explode)
    guarded = SimpleNamespace(permission="write", permission_for=config_guard.permission_for_write)
    assert effective_permission(guarded, {"path": "../o.txt"}, layout.session) == "config"


def test_a_folder_answer_never_covers_home_root_or_above_the_project(
    tmp_path: Path, layout: SimpleNamespace
) -> None:
    from snowpea_core.permissions.allowlist import outside_folder

    def folder(*paths: str) -> str | None:
        return outside_folder(list(paths), workdir=layout.project, home=layout.home)

    assert folder(str(layout.elsewhere / "a.txt")) == str(layout.elsewhere)
    assert folder(str(Path.home() / ".bashrc")) is None
    assert folder("/x") is None  # the filesystem root
    assert folder(str(tmp_path / "x.txt")) is None  # the project's parent
    assert folder(str(layout.home / "x.json")) is None


def test_a_projects_own_folder_rule_is_ignored(tmp_path: Path, layout: SimpleNamespace) -> None:
    allowlist = Allowlist(Paths.create(tmp_path / "home"), Settings())
    allowlist.add(".*", "project", DIR_TARGET, workdir=layout.project)
    assert not allowlist.covers_paths(
        [str(layout.elsewhere / "a.txt")], workdir=layout.project
    )


def test_a_remote_session_is_judged_against_its_own_cwd(layout: SimpleNamespace) -> None:
    from pathlib import PurePosixPath

    backend = SimpleNamespace(kind="ssh", cwd=PurePosixPath("/workspace"))
    session = SimpleNamespace(workdir=layout.project, backend=backend)
    tag = lambda path: permission_for_write({"path": path}, session, layout.core)  # noqa: E731
    assert tag("src/a.py") == "write"
    assert tag("/workspace/src/a.py") == "write"
    assert tag("../etc/x") == "outside"
    assert tag("~/x") == "outside"


async def test_lsp_rename_refuses_to_rewrite_files_outside_the_project(
    layout: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.exec.local import LocalBackend
    from snowpea_core.lsp import tools as lsp_tools
    from snowpea_core.lsp.client import path_to_uri
    from snowpea_core.tools.registry import ToolContext

    inside = layout.project / "a.py"
    inside.write_text("old = 1\n")
    shared = layout.elsewhere / "shared.py"
    shared.write_text("old = 2\n")
    span = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}
    edit = {
        "changes": {
            path_to_uri(inside): [{"range": span, "newText": "new"}],
            path_to_uri(shared): [{"range": span, "newText": "new"}],
        }
    }

    class _Manager:
        enabled = True

        async def rename(self, *_args: Any) -> Any:
            return edit

        async def touch_file(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return []

    monkeypatch.setattr(lsp_tools, "_manager", lambda _ctx: _Manager())
    layout.session.mode = "accept"
    ctx = ToolContext(
        session=layout.session, core=layout.core, backend=LocalBackend(layout.project)
    )
    args = {"path": "a.py", "line": 0, "character": 0, "newName": "new"}
    result = await lsp_tools.lsp_rename(ctx, args)
    assert not result.ok and "outside the project" in (result.error or "")
    assert inside.read_text() == "old = 1\n" and shared.read_text() == "old = 2\n"
    layout.session.mode = "auto"
    assert (await lsp_tools.lsp_rename(ctx, args)).ok
    assert shared.read_text() == "new = 2\n"


def test_declining_npm_install_is_not_declining_a_check(tmp_path: Path) -> None:
    session = _edited(tmp_path)
    verify_gate.note_declined(session, "shell", {"command": "npm install left-pad"}, "exec")
    assert "Unless the user asked" in (verify_gate.nudge(_gate_core(), session) or "")
