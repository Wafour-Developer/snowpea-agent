"""Plan-then-auto (1.32.0): "Allow Everything" pressed while the session plans.

The session stays in PLAN mode, every ``ask`` becomes ``allow`` and every
``deny`` stays a deny; when the model asks to leave plan, the picker shows the
plan for review with auto first, and a revision keeps the session planning.
Any switch out of plan clears the flag, and it is kept with the session row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from _support import make_daemon
from test_set_mode import _Core, _ctx, _Origin, _session

from snowpea_core.agent import agent as agent_mod
from snowpea_core.agent.subagent import get_manager
from snowpea_core.exec.local import LocalBackend
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import (
    CAPABILITIES,
    PROTOCOL_VERSION,
    SessionSetModeParams,
)
from snowpea_core.server.session_handlers import session_set_mode_handler
from snowpea_core.session.session import Session
from snowpea_core.tools import set_mode as set_mode_mod
from snowpea_core.tools.registry import ToolRegistry
from snowpea_core.tools.set_mode import REVIEW_TEXT, set_mode

KO = REVIEW_TEXT["ko"]["labels"]


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()


def _registry() -> ToolRegistry:
    from snowpea_core.tools.registry import register_builtin_tools

    return register_builtin_tools(ToolRegistry())


def _flagged(tmp_path: Path, **extra: Any) -> Session:
    session = Session(id="s-p", workdir=tmp_path, mode="plan")
    session.auto_after_plan = True
    for key, value in extra.items():
        setattr(session, key, value)
    return session


# -- the handler sets it; any way out of plan clears it --------------------


async def test_the_handler_sets_the_flag_and_reports_it(daemon: Daemon, tmp_path: Path) -> None:
    core = daemon.core
    assert tuple(map(int, PROTOCOL_VERSION.split("."))) >= (1, 32, 0)
    assert "planAutoApprove" in CAPABILITIES
    session = await core.sessions.create(tmp_path, mode="plan")
    events: list[Any] = []
    real = core.hub.emit_event

    async def record(session_id: str, event: Any) -> None:
        events.append(event)
        await real(session_id, event)

    core.hub.emit_event = record  # type: ignore[method-assign]
    conn = SimpleNamespace()
    result = await session_set_mode_handler(
        conn,  # type: ignore[arg-type]
        SessionSetModeParams(sessionId=session.id, mode="plan", autoAfterPlan=True),
        core,
    )
    assert result.mode == "plan" and result.autoAfterPlan is True
    assert session.auto_after_plan is True
    kind, payload = events[-1]
    assert kind == "mode.changed" and payload["autoAfterPlan"] is True
    assert session.summary().autoAfterPlan is True

    # Staying in plan without the argument keeps it.
    await core.sessions.set_mode(session, "plan")
    assert session.auto_after_plan is True
    # Shift+Tab / the picker / /mode accept: out of plan clears it.
    result = await session_set_mode_handler(
        conn,  # type: ignore[arg-type]
        SessionSetModeParams(sessionId=session.id, mode="accept"),
        core,
    )
    assert result.autoAfterPlan is False and session.auto_after_plan is False
    # Coming back to plan is a fresh pass: no flag unless asked again.
    await core.sessions.set_mode(session, "plan")
    assert session.auto_after_plan is False
    # A non-plan mode never takes it.
    await core.sessions.set_mode(session, "auto", True)
    assert session.auto_after_plan is False


async def test_the_flag_survives_a_restart(tmp_path: Path) -> None:
    home = tmp_path / "home"
    work = tmp_path / "w"
    work.mkdir()
    first = await make_daemon(home)
    try:
        session = await first.core.sessions.create(work, mode="plan")
        await first.core.sessions.set_mode(session, "plan", True)
        sid = session.id
        rows = [row for row in first.core.sessions.list() if row.sessionId == sid]
        assert rows and rows[0].autoAfterPlan is True
    finally:
        await first.stop()
    second = await make_daemon(home)
    try:
        restored = await second.core.sessions.restore(sid)
        assert restored is not None
        assert restored.mode == "plan" and restored.auto_after_plan is True
    finally:
        await second.stop()


# -- the policy: ask becomes allow, deny stays deny ------------------------


def test_inspection_runs_and_changes_still_ask(tmp_path: Path) -> None:
    tools = _registry()
    policy = PermissionPolicy()
    shell = tools.get("shell")
    write = tools.get("write_file")
    plain = Session(id="s-q", workdir=tmp_path, mode="plan")
    flagged = _flagged(tmp_path)

    def run(session: Session, command: str, tool: Any = shell) -> str:
        return policy.decide("plan", "exec", tool, {"command": command}, session)

    assert run(plain, "jq . package.json") == "ask"
    for command in (
        "jq . package.json",
        "npx vitest run",
        "uv run pytest -q",
        "python -m pytest tests -x",
        "npx tsc --noEmit",
        "ps aux | grep node",
        "pytest 2>&1 | tail -5",
        "echo draft > PLAN.md",
    ):
        assert run(flagged, command) == "allow", command
    for command in (
        "sed -i s/a/b/ src/x.py",
        "cat <<EOF > src/x.py\nprint(1)\nEOF",
        "echo x > src/x.py",
        "git commit -m wip",
        "git push",
        "npm install left-pad",
        "curl -X POST https://example.com",
        "rm -rf build",
        "python -c 'import os'",
        "echo '{}' > ~/.snowpea/settings.json",
        "./deploy.sh",
    ):
        assert run(flagged, command) == "ask", command
    # Only shell is widened: arbitrary code and process control still ask.
    execute_code = tools.get("execute_code")
    assert policy.decide("plan", "exec", execute_code, {"code": "1"}, flagged) == "ask"
    assert run(flagged, "npm test", tools.get("process")) == "ask"
    assert policy.decide("plan", "secret", None, {}, flagged) == "allow"
    # Plan still writes only its documents, sends nothing, configures nothing,
    # and never goes outside the project.
    source = {"path": "src/main.py", "content": "x"}
    assert policy.decide("plan", "write", write, source, flagged) == "deny"
    assert policy.decide("plan", "write", write, {"path": "PLAN.md"}, flagged) == "allow"
    assert policy.decide("plan", "send", None, {}, flagged) == "deny"
    assert policy.decide("plan", "config", write, source, flagged) == "deny"
    assert policy.decide("plan", "outside", write, source, flagged) == "deny"
    # The flag means nothing outside plan mode: accept still asks for exec.
    assert policy.decide("accept", "exec", shell, {"command": "jq ."}, flagged) == "ask"


def test_a_read_only_child_still_asks_nobody(tmp_path: Path) -> None:
    tools = _registry()
    flagged = _flagged(tmp_path, read_only=True)
    shell = tools.get("shell")
    # ``ask`` is left alone, so the loop's read-only rule refuses it.
    assert PermissionPolicy().decide("plan", "exec", shell, {"command": "jq ."}, flagged) == "ask"


async def test_children_follow_the_parent_flag_live(daemon: Daemon, tmp_path: Path) -> None:
    core = daemon.core
    manager = get_manager(core)
    shell = core.tools.get("shell")
    parent = await core.sessions.create(tmp_path, mode="plan")
    child = await manager._child_session(parent, manager.new_record(parent, "look", None), None)
    assert child.mode == "plan" and child.auto_after_plan is False

    def verdict() -> str:
        return core.policy.decide("plan", "exec", shell, {"command": "jq ."}, child)

    assert verdict() == "ask"
    # "Allow Everything" pressed on the parent reaches the running child.
    await core.sessions.set_mode(parent, "plan", True)
    assert verdict() == "allow"
    # A child spawned now carries its own (stored) copy as well.
    later = await manager._child_session(parent, manager.new_record(parent, "x", None), None)
    assert later.auto_after_plan is True
    row = await core.store.session(later.id)
    assert row is not None and row["auto_after_plan"] == 1
    record = manager.new_record(parent, "read", None)
    record.read_only = True
    reader = await manager._child_session(parent, record, None)
    assert reader.auto_after_plan is False
    assert core.policy.decide("plan", "exec", shell, {"command": "jq ."}, reader) == "ask"
    # The parent leaving plan stops every child, whatever its own copy says.
    await core.sessions.set_mode(parent, "accept")
    assert verdict() == "ask"
    assert core.policy.decide("plan", "exec", shell, {"command": "jq ."}, later) == "ask"


# -- the model is told, on the turn's message or the next tool result ------


def test_the_turn_note_appears_only_for_a_flagged_plan(tmp_path: Path) -> None:
    plain = Session(id="s-n", workdir=tmp_path, mode="plan")
    assert agent_mod.PLAN_AUTO_NOTE not in agent_mod.turn_context(plain)
    flagged = _flagged(tmp_path)
    note = agent_mod.turn_context(flagged)
    assert agent_mod.PLAN_AUTO_NOTE in note
    assert 'set_mode("auto")' in note and "readOnly" in note
    assert "still asks" in note


async def test_a_mid_turn_flag_rides_on_the_next_tool_result_once(
    daemon: Daemon, tmp_path: Path
) -> None:
    from snowpea_core.agent.loop import _with_plan_auto_note

    core = daemon.core
    session = await core.sessions.create(tmp_path, mode="plan")
    assert session.plan_auto_note_pending is False
    await core.sessions.set_mode(session, "plan", True)
    assert session.plan_auto_note_pending is True
    first = _with_plan_auto_note(session, "ok")
    assert first == f"ok\n\n{agent_mod.PLAN_AUTO_NOTE}"
    assert _with_plan_auto_note(session, "ok") == "ok"
    # Setting it again while on is no news; turning it off cancels a pending note.
    await core.sessions.set_mode(session, "plan", True)
    assert session.plan_auto_note_pending is False
    await core.sessions.set_mode(session, "plan", False)
    await core.sessions.set_mode(session, "plan", True)
    await core.sessions.set_mode(session, "accept")
    assert session.plan_auto_note_pending is False
    assert _with_plan_auto_note(session, "ok") == "ok"


# -- leaving plan: the plan for review, auto first -------------------------


def _flagged_tool_session(tmp_path: Path, origin: Any, plan: str = "") -> Session:
    session = _session(origin)
    session.workdir = tmp_path
    session.backend = LocalBackend(tmp_path)
    session.auto_after_plan = True
    (tmp_path / "notes.md").write_text("# Notes\n\nnot the plan\n", encoding="utf-8")
    (tmp_path / "PLAN.md").write_text(plan or "# Plan\n\n1. mesher\n2. camera\n", encoding="utf-8")
    # The notes were written last, but a PLAN*.md is the plan.
    session.plan_files.extend(["PLAN.md", "notes.md"])
    return session


def _plan_events(core: Any) -> list[dict[str, Any]]:
    return [
        payload
        for kind, payload in core.hub.events
        if kind == "message.done" and payload.get("kind") == "plan"
    ]


async def test_the_plan_goes_to_the_transcript_and_auto_comes_first(tmp_path: Path) -> None:
    core = _Core()
    origin = _Origin({"selected": [KO["auto"]], "text": None})
    session = _flagged_tool_session(tmp_path, origin)

    result = await set_mode(_ctx(core, session), {"mode": "accept"})

    [shown] = _plan_events(core)
    assert "1. mesher" in shown["text"] and "나머지" not in shown["text"]
    [asked] = origin.asked
    assert [option["label"] for option in asked["options"]] == [
        KO["auto"],
        KO["accept"],
        KO["plan"],
    ]
    assert KO["auto"].endswith("(추천)") and "(" not in KO["accept"]
    assert asked["allowOther"] is True
    # The question stays short: the plan's path, not its text.
    assert "PLAN.md" in asked["question"] and "1. mesher" not in asked["question"]
    assert asked["question"].count("\n") < 6
    assert core.sessions.calls == ["auto"]
    assert session.mode == "auto"
    assert result.meta["changed"] is True and result.meta["review"] is True


async def test_a_long_plan_is_cut_at_a_line_with_a_note(tmp_path: Path) -> None:
    core = _Core()
    origin = _Origin({"selected": [KO["accept"]], "text": None})
    lines = "\n".join(f"{n}. step {n} " + "x" * 60 for n in range(1, 400))
    session = _flagged_tool_session(tmp_path, origin, f"# Plan\n\n{lines}\n")

    await set_mode(_ctx(core, session), {"mode": "auto"})

    [shown] = _plan_events(core)
    body, note = shown["text"].rsplit("\n\n", 1)
    assert len(body) <= set_mode_mod.REVIEW_MAX_CHARS
    assert body.splitlines()[-1].endswith("x" * 60)  # a whole line, not half of one
    assert "PLAN.md" in note and "나머지" in note
    assert core.sessions.calls == ["accept"]


async def test_typed_changes_keep_planning_and_go_back_to_the_model(tmp_path: Path) -> None:
    core = _Core()
    origin = _Origin({"selected": [], "text": "테스트 단계를 먼저 넣어줘"})
    session = _flagged_tool_session(tmp_path, origin)

    result = await set_mode(_ctx(core, session), {"mode": "auto"})

    assert core.sessions.calls == []
    assert session.mode == "plan" and session.auto_after_plan is True
    assert "테스트 단계를 먼저 넣어줘" in result.output
    assert 'set_mode("auto")' in result.output
    assert result.meta["revision"] == "테스트 단계를 먼저 넣어줘"


@pytest.mark.parametrize(
    ("typed", "mode"),
    [("auto", "auto"), ("1", "auto"), ("자동", "auto"), ("2", "accept"), ("Accept", "accept")],
)
async def test_short_typed_answers_name_a_row(tmp_path: Path, typed: str, mode: str) -> None:
    core = _Core()
    session = _flagged_tool_session(tmp_path, _Origin({"selected": [], "text": typed}))
    result = await set_mode(_ctx(core, session), {"mode": "auto"})
    assert core.sessions.calls == [mode] and "instruction" not in result.meta


async def test_typed_3_means_keep_revising(tmp_path: Path) -> None:
    core = _Core()
    session = _flagged_tool_session(tmp_path, _Origin({"selected": [], "text": "3"}))
    result = await set_mode(_ctx(core, session), {"mode": "auto"})
    assert core.sessions.calls == [] and result.output == REVIEW_TEXT["ko"]["revise_plain"]


async def test_a_row_and_words_start_with_the_words_as_an_instruction(tmp_path: Path) -> None:
    core = _Core()
    origin = _Origin({"selected": [KO["auto"]], "text": "테스트부터 작성해줘"})
    session = _flagged_tool_session(tmp_path, origin)
    result = await set_mode(_ctx(core, session), {"mode": "auto"})
    assert core.sessions.calls == ["auto"]
    assert "테스트부터 작성해줘" in result.output and result.meta["instruction"]


async def test_keep_revising_and_a_decline_both_stay_in_plan(tmp_path: Path) -> None:
    core = _Core()
    origin = _Origin({"selected": [KO["plan"]], "text": None})
    session = _flagged_tool_session(tmp_path, origin)
    result = await set_mode(_ctx(core, session), {"mode": "auto"})
    assert session.mode == "plan" and session.auto_after_plan is True
    assert result.output == REVIEW_TEXT["ko"]["revise_plain"]

    declined = _Origin({"selected": [], "text": None})
    session = _flagged_tool_session(tmp_path, declined)
    result = await set_mode(_ctx(core, session), {"mode": "auto"})
    assert session.mode == "plan" and core.sessions.calls == []
    assert result.output == set_mode_mod.TEXT["ko"]["declined"]


async def test_an_unflagged_plan_keeps_the_old_picker(tmp_path: Path) -> None:
    core = _Core()
    origin = _Origin({"selected": [], "text": None})
    session = _session(origin)
    await set_mode(_ctx(core, session), {"mode": "accept"})
    [asked] = origin.asked
    assert asked["header"] == set_mode_mod.TEXT["ko"]["header"]
    assert asked["allowOther"] is False
    assert _plan_events(core) == []


async def test_a_browser_session_keeps_the_old_picker(tmp_path: Path) -> None:
    core = _Core()
    origin = _Origin({"selected": [], "text": None})
    session = _flagged_tool_session(tmp_path, origin)
    session.origin_surface = "browser"
    await set_mode(_ctx(core, session), {"mode": "accept"})
    [asked] = origin.asked
    assert asked["header"] == set_mode_mod.BROWSER_TEXT["ko"]["header"]
    assert _plan_events(core) == []


async def test_mode_plan_auto_sets_the_flag(daemon: Daemon, tmp_path: Path) -> None:
    from snowpea_core.commands.mode_cmd import cmd_mode

    core = daemon.core
    session = await core.sessions.create(tmp_path, mode="accept")
    emitted: list[Any] = []
    said: list[str] = []

    async def emit(event: Any) -> None:
        emitted.append(event)

    async def say(text: str) -> None:
        said.append(text)

    ctx = SimpleNamespace(core=core, session=session, emit=emit, say=say)
    await cmd_mode(ctx, "plan auto")  # type: ignore[arg-type]
    assert session.mode == "plan" and session.auto_after_plan is True
    assert emitted[-1][1]["autoAfterPlan"] is True
    await cmd_mode(ctx, "plan")  # type: ignore[arg-type]
    assert session.auto_after_plan is True  # staying in plan keeps it
    await cmd_mode(ctx, "accept")  # type: ignore[arg-type]
    assert session.auto_after_plan is False


INSPECTING = [
    "ls -la",
    "cat README.md",
    "rg -n TODO src",
    "grep -r foo .",
    "git status",
    "git log --oneline -5",
    "git diff HEAD~1",
    "git show HEAD",
    "git branch -a",
    "pytest tests/",
    "uv run pytest -q",
    "python -m pytest tests -x",
    "npm test",
    "pnpm lint",
    "npm run typecheck",
    "npx vitest run",
    "npx tsc --noEmit",
    "tsc --noEmit",
    "cargo test",
    "go vet ./...",
    "curl https://example.com",
    "curl -s https://example.com",
    "ruff check .",
    "jq . package.json",
    "ps aux | grep node",
    "echo hi > /dev/null",
    "pytest 2>&1 | tail -5",
    "echo x > notes.md",
    "find . -name '*.py'",
    "/usr/bin/grep x a",
    "time pytest -x",
    "timeout 30 pytest -x",
    "node --version",
]
CHANGING = [
    # a dashed word after `--` is still read by some parsers
    "find -- . -delete",
    "find . -- -delete",
    "go test -v -- -exec sh",
    "pytest -x -- --junitxml=src/x",
    # the obvious ones
    "sed -i s/a/b/ src/x.py",
    "echo x > src/x.py",
    "git commit -m x",
    "git push",
    "git checkout main",
    "curl -X POST https://x",
    "curl -d a=1 https://x",
    "curl -o src/x https://x",
    "rm -rf build",
    "mv a b",
    "npm install left-pad",
    "pip install x",
    "python -c 'print(1)'",
    "node -e 'x'",
    "ssh host ls",
    "docker ps",
    "sqlite3 db 'delete from t'",
    "echo x >> ~/.snowpea/settings.json",
    "tee src/a.py",
    "./deploy.sh",
    "make test",
    "npm run build",
    "git branch new",
    "git stash",
    "ruff check --fix .",
    "npx prettier --write .",
    "wget https://x",
    "echo $(rm x)",
    "sleep 100 &",
    "vitest",
    "git -C sub commit -m x",
    "xargs rm",
    "python script.py",
    "git config user.name x",
    "cat <<EOF > a.py",
    "FOO=1 rm x",
    "command rm x",
    "env rm x",
    "/bin/rm x",
    "python -m pip install x",
    "find . -delete",
    "'rm' -rf src",
    "r\\m -rf src",
    "ls; rm x",
    "ls && rm x",
    "ls | xargs rm",
    "dd if=a of=b",
    "git apply x.diff",
    # the round-2 bypasses
    "nice -n 19 rm -rf src",
    "timeout -s KILL 5 rm -rf src",
    "env -S 'rm -rf src'",
    "X=rm; $X -rf src",
    "${X} -rf src",
    "/usr/bin/find . -delete",
    "find . -fprintf out.py x",
    "awk 'BEGIN{system (\"rm -rf src\")}'",
    "gawk -iinplace '{print}' src/a.py",
    "echo x > /tmp/../home/u/src/main.py",
    "echo x > $HOME/notes.md",
    "echo x > ~/notes.md",
    "/usr/../tmp/x.sh",
    "/bin/../tmp/x.sh",
    "dash -c 'rm -rf src'",
    "busybox rm -rf src",
    "php -r 'unlink(\"a\");'",
    "lua -e 'os.remove(\"a\")'",
    "git -c diff.external='rm -rf src' diff",
    "git log --output=src/a.py",
    "git ls-remote --upload-pack='rm -rf src' .",
    "git grep --open-files-in-pager='rm -rf src' x",
    "git diff --ext-diff",
    "rg --pre 'rm -rf src' x .",
    "npm run lint -- --fix",
    "npm test -- -u",
    "pnpm lint --fix",
    "python -m pytest /tmp/test_x.py",
    "pytest docs/test_x.py",
    "npx vitest run --config /tmp/x.config.ts",
    "uniq a src/main.py",
    "split -b1 a src/x",
    "cp a b",
    "ln -s a b",
    "parallel rm ::: a",
    "watch rm a",
    "setsid rm a",
    "doas rm a",
    "sudo rm a",
    "ionice rm a",
    "stdbuf -o0 rm a",
    "flock /tmp/l rm a",
    "tclsh x.tcl",
    "Rscript -e 'file.remove(\"a\")'",
    "java Evil.java",
    "gcc -o src/a a.c",
    "xz -d a.xz",
    "mvn deploy",
    "gradle publish",
    "cmake --build .",
    "ssh-keygen -f k",
    "chattr +i a",
    "git-lfs push",
    "hg commit",
    "svn commit",
    "http POST x.com a=1",
    "nc x.com 80",
    "rclone sync . r:",
    "redis-cli flushall",
    "vim -es -c 'wq' src/a",
    "ed src/a",
    "sponge src/a",
    "tar xf a",
    "jq . a > src/a.json",
    "cargo run",
    "go generate",
    "base64 -d a",
    "node_modules/.bin/foo",
    "sort -o src/a a",
    "tree -o src/a",
]


@pytest.mark.parametrize("command", INSPECTING)
def test_inspection_is_not_mutating(command: str, tmp_path: Path) -> None:
    from snowpea_core.permissions.safe_commands import is_mutating

    assert is_mutating(command, workdir=tmp_path) is False


@pytest.mark.parametrize("command", CHANGING)
def test_changes_are_mutating(command: str, tmp_path: Path) -> None:
    from snowpea_core.permissions.safe_commands import is_mutating

    assert is_mutating(command, workdir=tmp_path) is True


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [("sub", "allow"), ("/home/whitevil", "ask"), ("../x", "ask"), ("~", "ask")],
)
def test_a_shell_cwd_outside_the_project_asks(tmp_path: Path, cwd: str, expected: str) -> None:
    shell = _registry().get("shell")
    args = {"command": "echo x > notes.md", "cwd": cwd}
    assert PermissionPolicy().decide("plan", "exec", shell, args, _flagged(tmp_path)) == expected


#: Round-3 probe: every flag that writes or runs code, on a program that is otherwise read-only.
FLAG_BYPASSES = [
    "tree -o src/x",
    "tree -osrc/x",
    "tree --output src/x",
    "find . -fprint src/x",
    "find . -fprint=src/x",
    "less src/a",
    "more src/a",
    "man ls",
    "xxd -r a src/b",
    "diff --output=src/x a b",
    "diff -o src/x a b",
    "tar cf src/a.tar .",
    "git format-patch -o src HEAD~1",
    "git archive -o src/x.tar HEAD",
    "git archive --output=src/x.tar HEAD",
    "git log --output src/a",
    "git show -Osrc/a",
    "git diff --output-indicator-new=x",
    "git -c core.pager=sh log",
    "git --git-dir=.snowpea/plans/evil status",
    "git --work-tree=/ status",
    "git -C .snowpea/plans/evil status",
    "git log --exec=x",
    "git grep -Osh x",
    "git grep --open-files-in-pager=sh x",
    "git diff --no-index a /etc/shadow",
    "git blame --contents=x a",
    "git rev-parse --git-path x",
    "git log -g",
    "git shortlog --output=src/a",
    "pytest --basetemp=src",
    "pytest --junitxml=src/x.xml",
    "pytest --junit-xml=src/x.xml",
    "pytest --cov-report=html:src/x",
    "pytest --cov=. --cov-report=xml:src/x.xml",
    "pytest --cache-clear",
    "pytest -o cache_dir=src",
    "pytest --override-ini=cache_dir=src",
    "pytest --resultlog=src/x",
    "pytest --log-file=src/x",
    "pytest --report-log=src/x",
    "pytest --html=src/x.html",
    "pytest --pastebin=all",
    "pytest --debug=src/x",
    "python -m pytest --junitxml=src/x",
    "python -m mypy --cache-dir=src",
    "mypy --html-report src",
    "mypy --junit-xml src/x",
    "mypy --install-types",
    "python -m unittest discover -s /tmp",
    "vitest run --outputFile=src/x",
    "vitest run --reporter=junit --outputFile src/x",
    "vitest run --coverage.reportsDirectory=src",
    "vitest run --globalSetup=x",
    "vitest run --environment=x",
    "vitest run --dir /tmp",
    "vitest run -r /tmp",
    "vitest run --root /tmp",
    "jest --outputFile=src/x",
    "jest --json --outputFile=src/x",
    "jest --rootDir /tmp",
    "jest --testRunner=x",
    "jest --cacheDirectory=src",
    "jest --coverageDirectory=src",
    "eslint -o src/x .",
    "eslint --output-file src/x .",
    "eslint --cache --cache-location src/x .",
    "eslint --format=/tmp/f.js .",
    "eslint -f /tmp/f.js .",
    "eslint --ext .js --parser x .",
    "tsc --noEmit --generateTrace src",
    "tsc --noEmit --tsBuildInfoFile src/x",
    "tsc --noEmit --incremental",
    "tsc --noEmit --listFilesOnly",
    "tsc --noEmit -b",
    "tsc --noEmit --build",
    "tsc --noEmit --plugins x",
    "prettier --check --plugin=/tmp/x.js .",
    "prettier --check --cache-location=src/x .",
    "mocha --reporter /tmp/x.js",
    "mocha --file /tmp/x.js",
    "mocha /tmp/x.js",
    "mocha --reporter-option output=src/x",
    "cargo test --target-dir src",
    "cargo check --target-dir=/tmp",
    "cargo clippy --fix",
    "cargo test --manifest-path /tmp/Cargo.toml",
    "cargo test --config x",
    "cargo test -Z x",
    "go test -o src/x",
    "go test -c",
    "go test -exec 'rm -rf src'",
    "go test -toolexec sh",
    "go test -coverprofile=src/x",
    "go test -json -cpuprofile src/x",
    "go vet -vettool=/tmp/x",
    "go test -modfile=/tmp/go.mod",
    "curl file:///etc/passwd",
    "curl --upload-file=a x",
    "curl -Ta x",
    "curl --variable x",
    "curl -w '%output{src/x}' x",
    "curl -s -oa x",
    "curl --output-dir src -O x",
    "curl --create-dirs x",
    "curl -q x",
    "jq -n --rawfile a /etc/shadow",
    "rg --pre=sh x",
    "rg --pre-glob x",
    "rg -z x",
    "rg --search-zip x",
    "sort -osrc/x a",
    "sort --compress-program=sh a",
    "sort -T src a",
    "date --set=x",
    "date -s x",
    "hostname newname",
    "sleep 1000000",
    "npm test -- --watch",
    "npm run test -- --outputFile=src/x",
    "npm test --prefix /tmp",
    "npm test --ignore-scripts=false",
    "pnpm test --dir /tmp",
    "yarn test --cwd /tmp",
    "npm list -g",
    "npm --prefix=/tmp test",
    "bun test --preload /tmp/x.ts",
    "npx -p evil vitest run",
    "npx --package=evil vitest run",
    "ruff check --output-file src/x",
    "ruff check -o src/x",
    "ruff check --cache-dir src",
    "ruff check --config x",
    "ruff check --extension x",
    "ruff check --watch",
    "echo a > src/x.py",
    "ls 1>src/x",
    "ls >| src/x",
    "ls &>src/x",
    "ls >& src/x",
    "ls -la | tee",
    "xargs",
    "find . -delete",
    "find . -D exec",
    "find . -execdir x ;",
]

MORE_INSPECTING = [
    "git status --porcelain",
    "git describe --dirty",
    "pytest --lf",
    "jq --arg a b . a",
    "stat -c x a",
    "od a",
    "strings a",
    "timeout 10 sleep 5",
    "command ls",
    "bun test",
    "bunx vitest run",
    "pnpm exec vitest run",
    "git status; npm test && ls | wc -l",
    "echo a 2>&1",
    "echo a >&2",
    "echo a > /tmp/x",
    "echo a >/tmp/./x",
    "echo a > docs/notes.md",
    "cat < src/a",
    "find . -newer x",
    "find -L / -name x",
    "rg x --files",
    "diff -r a b",
    "cmp a b",
    "printf %s x",
    "npm view x",
]


@pytest.mark.parametrize("command", FLAG_BYPASSES)
def test_flags_that_write_or_run_code_ask(command: str, tmp_path: Path) -> None:
    from snowpea_core.permissions.safe_commands import is_mutating

    assert is_mutating(command, workdir=tmp_path) is True


@pytest.mark.parametrize("command", MORE_INSPECTING)
def test_known_read_flags_still_run(command: str, tmp_path: Path) -> None:
    from snowpea_core.permissions.safe_commands import is_mutating

    assert is_mutating(command, workdir=tmp_path) is False


async def test_the_plain_picker_shows_the_plan_too(tmp_path: Path) -> None:
    # Without Allow Everything the picker is the plain one, but leaving plan
    # is still approving the plan: it goes to the transcript before the ask.
    core = _Core()
    origin = _Origin({"selected": [], "text": None})
    session = _flagged_tool_session(tmp_path, origin)
    session.auto_after_plan = False

    await set_mode(_ctx(core, session), {"mode": "accept"})

    [shown] = _plan_events(core)
    assert "1. mesher" in shown["text"]
    [asked] = origin.asked
    assert "PLAN.md" in asked["question"]
    assert KO["auto"] not in [option["label"] for option in asked["options"]]


async def test_the_plain_picker_without_a_plan_shows_nothing(tmp_path: Path) -> None:
    core = _Core()
    origin = _Origin({"selected": [], "text": None})
    session = _flagged_tool_session(tmp_path, origin)
    session.auto_after_plan = False
    session.plan_files.clear()

    await set_mode(_ctx(core, session), {"mode": "accept"})

    assert _plan_events(core) == []
    [asked] = origin.asked
    assert "PLAN.md" not in asked["question"]
