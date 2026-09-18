"""M2 §9: what plan mode may write, and which commands it runs without asking.

Plan mode denies ``write`` and asks before ``exec``.  Two carve-outs make it
usable: the plan is itself a file, and inspection is the whole job.  The tables
below are the contract — a silent edit to either classifier fails here first —
and the end-to-end cases drive a real daemon so the assertions are about the
events a surface sees, not about the helpers in isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, make_daemon
from test_session_loop import prompt

from snowpea_core.permissions.plan_paths import REFUSAL_NOTE, is_plan_writable, writable_globs
from snowpea_core.permissions.policy import MODE_MATRIX, PermissionPolicy
from snowpea_core.permissions.safe_commands import is_read_only
from snowpea_core.server.app_server import Daemon
from snowpea_core.tools.config_guard import is_config_path

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "permissions.json"


# ---------------------------------------------------------------------------
# the command classifier
# ---------------------------------------------------------------------------


READ_ONLY: tuple[str, ...] = (
    "ls",
    "ls -la src",
    "cat README.md",
    "head -n 20 setup.py",
    "tail -n 5 log.txt",
    "wc -l core/*.py",
    "grep -rn TODO core",
    "rg --json pattern",
    "find . -name '*.py'",
    "pwd",
    "echo hello",
    "which python",
    "env",
    "stat setup.py",
    "file setup.py",
    "du -sh .",
    "df -h",
    "tree -L 2",
    "git status",
    "git diff HEAD~1",
    "git log --oneline -n 5",
    "git show HEAD",
    "git branch",
    "git ls-files",
    "git blame core/x.py",
    "npm test",
    "npm run test",
    "npm ls",
    "pnpm test",
    "yarn test",
    "pytest -q",
    "uv run pytest tests/test_x.py",
    "ls && git status",
    "grep -rn x core | wc -l",
)

WRITES_OR_UNKNOWN: tuple[str, ...] = (
    "",
    "   ",
    "rm -rf build",
    "mv a b",
    "cp a b",
    "chmod +x run.sh",
    "chown me file",
    "tee out.txt",
    "sed -i s/a/b/ file",
    "touch new.txt",
    "mkdir dist",
    "git commit -m x",
    "git push",
    "git reset --hard",
    "git checkout main",
    "git clean -fd",
    "git branch -D old",
    "git branch --delete old",
    # Arbitrary code is arbitrary code, whatever it happens to print.
    'python -c "print(1)"',
    "python3 -c 'print(1)'",
    'node -e "console.log(1)"',
    "bash script.sh",
    "sh -c ls",
    # A redirection writes even when the program does not.
    "ls > out.txt",
    "echo hi >> log",
    "cat < in.txt",
    # A substitution runs something this classifier never sees.
    "echo `rm -rf .`",
    "echo $(rm -rf .)",
    "ls ${HOME}",
    # One unsafe segment poisons the chain.
    "ls && rm -rf build",
    "git status; npm publish",
    "cat x | tee y",
    "ls || curl http://x",
    # Backgrounding leaves something running that nothing is watching.
    "pytest &",
    # An assignment changes the environment the next command runs in.
    "FOO=1 ls",
    "env FOO=1 rm x",
    # find can execute and delete.
    "find . -name '*.pyc' -delete",
    "find . -exec rm {} ;",
    # Unknown programs are unknown.
    "make build",
    "docker run x",
    "unknown-tool --help",
)


@pytest.mark.parametrize("command", READ_ONLY)
def test_read_only_commands_are_recognised(command: str) -> None:
    assert is_read_only(command) is True, command


@pytest.mark.parametrize("command", WRITES_OR_UNKNOWN)
def test_everything_else_still_asks(command: str) -> None:
    assert is_read_only(command) is False, command


# ---------------------------------------------------------------------------
# which paths are documents
# ---------------------------------------------------------------------------


WRITABLE: tuple[str, ...] = (
    "PLAN.md",
    "notes.markdown",
    "scratch.txt",
    "docs/design/x.md",
    "docs/anything.png",
    ".snowpea/plans/today.md",
    ".snowpea/plans/nested/deep.json",
    "src/README.md",
)

REFUSED: tuple[str, ...] = (
    "",
    "src/a.py",
    "core/snowpea_core/agent/loop.py",
    "pyproject.toml",
    "data.json",
    "Makefile",
    # Resolved, not spelled: an escape lands outside the workdir.
    "../outside.md",
    "../../etc/passwd",
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "docs" / "design").mkdir(parents=True)
    (root / ".snowpea" / "plans").mkdir(parents=True)
    (root / "src").mkdir()
    return root


@pytest.mark.parametrize("path", WRITABLE)
def test_documents_are_writable_in_plan_mode(project: Path, path: str) -> None:
    assert is_plan_writable(path, workdir=project) is True, path


@pytest.mark.parametrize("path", REFUSED)
def test_everything_else_stays_denied(project: Path, path: str) -> None:
    assert is_plan_writable(path, workdir=project) is False, path


def test_an_absolute_path_inside_the_workdir_is_judged_the_same(project: Path) -> None:
    assert is_plan_writable(str(project / "docs" / "x.md"), workdir=project) is True
    assert is_plan_writable(str(project / "src" / "a.py"), workdir=project) is False


def test_the_home_plans_directory_is_writable_and_is_not_configuration(
    tmp_path: Path, project: Path
) -> None:
    """A plan kept across projects is still a plan, not settings."""
    home = tmp_path / "home"
    (home / "plans").mkdir(parents=True)
    plan = home / "plans" / "cross-project.md"
    assert is_plan_writable(str(plan), workdir=project, home=home) is True
    assert is_config_path(str(plan), workdir=project, home=home) is False
    # The rest of the home is still configuration, and configuration is never
    # writable in plan mode whatever the path looks like.
    assert is_config_path(str(home / "settings.json"), workdir=project, home=home) is True
    assert is_plan_writable(str(home / "settings.json"), workdir=project, home=home) is False


def test_writable_globs_come_from_settings_when_set(project: Path) -> None:
    class _Plan:
        writableGlobs = ["notes/**"]

    class _Modes:
        plan = _Plan()

    class _Settings:
        modes = _Modes()

    settings = _Settings()
    assert writable_globs(settings) == ("notes/**",)
    assert is_plan_writable("notes/a.py", workdir=project, settings=settings) is True
    assert is_plan_writable("PLAN.md", workdir=project, settings=settings) is False
    # An empty list is a misconfiguration, not an instruction to deny
    # everything; the built-in list stands.
    _Plan.writableGlobs = []
    assert writable_globs(settings) == tuple(writable_globs(None))


# ---------------------------------------------------------------------------
# the policy decision
# ---------------------------------------------------------------------------


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Session:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir


def _decide(policy: PermissionPolicy, mode: str, tag: str, tool: str, args: dict[str, Any],
            project: Path) -> str:
    return policy.decide(mode, tag, _Tool(tool), args, _Session(project))  # type: ignore[arg-type]


def test_the_matrix_rows_themselves_are_unchanged() -> None:
    """The exception is a carve-out, not a new row: plan still denies write."""
    assert MODE_MATRIX["plan"]["write"] == "deny"
    assert MODE_MATRIX["plan"]["exec"] == "ask"
    assert MODE_MATRIX["plan"]["config"] == "deny"


def test_the_policy_allows_a_plan_write_and_denies_a_source_write(project: Path) -> None:
    policy = PermissionPolicy()
    assert _decide(policy, "plan", "write", "write_file",
                   {"path": ".snowpea/plans/x.md"}, project) == "allow"
    assert _decide(policy, "plan", "write", "patch",
                   {"path": "docs/x.md"}, project) == "allow"
    assert _decide(policy, "plan", "write", "write_file",
                   {"path": "src/a.py"}, project) == "deny"


def test_the_exception_never_reaches_config_or_another_tool(project: Path) -> None:
    policy = PermissionPolicy()
    # A settings write arrives already re-tagged `config`, which has no carve-out.
    assert _decide(policy, "plan", "config", "write_file",
                   {"path": ".snowpea/settings.json"}, project) == "deny"
    # A write-tagged tool that is not one of the two named ones is untouched.
    assert _decide(policy, "plan", "write", "some_other_writer",
                   {"path": "x.md"}, project) == "deny"


def test_the_policy_runs_a_read_only_command_and_asks_for_the_rest(project: Path) -> None:
    policy = PermissionPolicy()
    assert _decide(policy, "plan", "exec", "shell", {"command": "git status"}, project) == "allow"
    assert _decide(policy, "plan", "exec", "shell", {"command": "rm -rf ."}, project) == "ask"
    # Only `shell` is loosened; another exec-tagged tool keeps the matrix answer.
    assert _decide(policy, "plan", "exec", "process_kill", {"command": "ls"}, project) == "ask"


@pytest.mark.parametrize("mode", ["accept", "auto"])
def test_the_other_modes_are_untouched(project: Path, mode: str) -> None:
    policy = PermissionPolicy()
    assert _decide(policy, mode, "write", "write_file", {"path": "src/a.py"}, project) == "allow"
    expected = "ask" if mode == "accept" else "allow"
    assert _decide(policy, mode, "exec", "shell", {"command": "rm -rf ."}, project) == expected


# ---------------------------------------------------------------------------
# end to end, through a real daemon
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> Any:
    from _support import fake_provider

    with fake_provider(FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            await instance.stop()


@pytest_asyncio.fixture
async def http() -> Any:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".snowpea" / "plans").mkdir(parents=True)
    (root / "note.txt").write_text("x\n", encoding="utf-8")
    return root


async def _open(client: RpcClient, workdir: Path) -> str:
    result = await client.ok(
        "session.create", {"workdir": str(workdir), "mode": "plan", "provider": "fake"}
    )
    return str(result["sessionId"])


async def test_a_plan_turn_writes_its_plan_and_runs_git_status_unasked(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    client.approval_mode = "deny"
    session_id = await _open(client, workdir)

    write_turn = await prompt(client, session_id, "write the plan")
    assert await client.wait_turn(write_turn) == "complete"
    assert client.approval_requests == []
    assert client.of_kind("error") == []
    assert (workdir / ".snowpea" / "plans" / "x.md").read_text(encoding="utf-8") == "# plan\n"

    shell_turn = await prompt(client, session_id, "use shell")
    assert await client.wait_turn(shell_turn) == "complete"
    assert client.approval_requests == [], "ls is read-only; plan mode must not ask"
    assert client.of_kind("error") == []

    await client.stop()


async def test_a_plan_turn_is_still_refused_a_source_write(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    session_id = await _open(client, workdir)

    turn_id = await prompt(client, session_id, "use write_file")
    assert await client.wait_turn(turn_id) == "complete"
    assert [event["payload"]["code"] for event in client.of_kind("error")] == ["mode_denied"]
    assert client.approval_requests == []
    assert not (workdir / "out.py").exists()

    # The refusal has to say a different path would have worked, or the model
    # retries the same write and reads the same message.
    refused = client.of_kind("tool.result")
    assert refused and refused[0]["payload"]["ok"] is False
    assert REFUSAL_NOTE in refused[0]["payload"]["error"]

    await client.stop()
