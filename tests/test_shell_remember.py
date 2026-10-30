"""1.34.0 ``shellRemember``: what remembering a shell approval would allow.

Before 1.34.0 a "project"/"always" answer to ``git status`` stored
``^git( .*)?$``, which also allowed ``git push --force``. Now the request
offers the rule (``remember``), the answer stores exactly that rule, and a
command with no safe rule is only ever allowed once.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.permissions.allowlist import SHELL_TARGET, Allowlist
from snowpea_core.permissions.approval_queue import (
    ApprovalQueue,
    Decision,
    guard_scope,
    remember_for,
)
from snowpea_core.permissions.shell_remember import propose
from snowpea_core.server.protocol import CAPABILITIES, ApprovalRequest
from snowpea_core.session.session import Session

BOTH = ("project", "always")
PROJECT = ("project",)

#: command -> (pattern, display, scopes), or None when nothing may be remembered.
PROPOSALS: list[tuple[str, tuple[str, str, tuple[str, ...]] | None]] = [
    ("git status", (r"^git status( .*)?$", "git status …", BOTH)),
    ("git status --short", (r"^git status( .*)?$", "git status …", BOTH)),
    ("git ls-files src", (r"^git ls\-files( .*)?$", "git ls-files …", BOTH)),
    ("git log --oneline -5", (r"^git log( .*)?$", "git log …", PROJECT)),
    ("git commit -m 'fix it'", (r"^git commit( .*)?$", "git commit …", PROJECT)),
    ("ls -la src", (r"^ls( .*)?$", "ls …", BOTH)),
    ("grep -n x note.txt", (r"^grep( .*)?$", "grep …", BOTH)),
    ("npm test", (r"^npm test( .*)?$", "npm test …", PROJECT)),
    ("npm run build", (r"^npm run build( .*)?$", "npm run build …", PROJECT)),
    ("pnpm install", (r"^pnpm install( .*)?$", "pnpm install …", PROJECT)),
    ("cargo build --release", (r"^cargo build( .*)?$", "cargo build …", PROJECT)),
    ("go test ./...", (r"^go test( .*)?$", "go test …", PROJECT)),
    ("uv run pytest -q", (r"^uv run pytest( .*)?$", "uv run pytest …", PROJECT)),
    ("docker compose up -d", (r"^docker compose up( .*)?$", "docker compose up …", PROJECT)),
    ("make test", (r"^make test( .*)?$", "make test …", PROJECT)),
    ("./gradlew build", (r"^\./gradlew build( .*)?$", "./gradlew build …", PROJECT)),
    ("pytest -q tests", (r"^pytest( .*)?$", "pytest …", PROJECT)),
    # compound and complex lines
    ("ls | head", None),
    ("git status && git push", None),
    ("ls; rm -rf ~", None),
    ("ls > out.txt", None),
    ("cat < in.txt", None),
    ("sleep 5 &", None),
    ("echo $(whoami)", None),
    ("echo `whoami`", None),
    ("echo $HOME", None),
    ("(cd src && ls)", None),
    ("cat <<EOF\nx\nEOF", None),
    ("FOO=1 npm test", None),
    ("git status 'unclosed", None),
    ("", None),
    # never remembered programs
    ("sudo apt install x", None),
    ("su -c ls", None),
    ("doas ls", None),
    ("rm -rf build", None),
    ("/bin/rm -rf build", None),
    ("dd if=/dev/zero of=x", None),
    ("mkfs.ext4 /dev/sda1", None),
    ("chmod +x run.sh", None),
    ("kill -9 1", None),
    ("bash run.sh", None),
    ("python -c 'print(1)'", None),
    ("python3.11 script.py", None),
    ("node -e 'x'", None),
    ("curl https://example.test", None),
    ("ssh host ls", None),
    ("xargs rm", None),
    ("find . -name x", None),
    ("npx foo", None),
    ("uvx ruff", None),
    ("bunx foo", None),
    # dangerous subcommands, and subcommand tools without one
    ("git push origin main", None),
    ("git push --force", None),
    ("git reset --hard", None),
    ("git clean -fdx", None),
    ("git rm x", None),
    ("git config core.hooksPath x", None),
    ("git -C other status", None),
    ("git", None),
    ("npm publish", None),
    ("npm exec foo", None),
    ("npm run", None),
    ("pip install requests", None),
    ("pip3 install requests", None),
    ("uv pip install x", None),
    ("uv run python -c 'x'", None),
    ("cargo install ripgrep", None),
    ("kubectl delete pod x", None),
    ("bun index.ts", None),
]


@pytest.mark.parametrize(("command", "expected"), PROPOSALS)
def test_the_proposed_rule(command: str, expected: Any) -> None:
    proposal = propose(command)
    if expected is None:
        assert proposal is None
        return
    assert proposal is not None
    assert (proposal.pattern, proposal.display, proposal.scopes) == expected


def test_a_proposed_rule_covers_more_arguments_but_not_another_subcommand() -> None:
    proposal = propose("git status")
    assert proposal is not None
    assert re.search(proposal.pattern, "git status")
    assert re.search(proposal.pattern, "git status --porcelain")
    assert not re.search(proposal.pattern, "git push --force")
    assert not re.search(proposal.pattern, "git statusx")


def _request(command: str) -> ApprovalRequest:
    args: dict[str, Any] = {"command": command}
    return ApprovalRequest(
        requestId="ap-1",
        sessionId="s",
        tool="shell",
        args=args,
        remember=remember_for("shell", args),
    )


def test_request_level_cases_offer_nothing() -> None:
    assert remember_for("shell", {"command": "git status"}) is not None
    assert remember_for("shell", {"command": "git status", "onceOnly": True}) is None
    assert remember_for("shell", {"command": "git status"}, guard="agent-switch") is None
    assert remember_for("shell", {"command": "git status"}, reason_code="config") is None
    assert remember_for("shell", {"command": "git status"}, cacheable=False) is None
    assert remember_for("read_file", {"path": "a"}) is None


def test_guard_keeps_a_command_with_no_rule_to_once() -> None:
    request = _request("rm -rf build")
    assert request.remember is None
    for scope in ("project", "always"):
        assert guard_scope(request, Decision("allow", scope)).scope == "once"
    # "session" and "once" are untouched.
    assert guard_scope(request, Decision("allow", "session")).scope == "session"


def test_guard_keeps_always_to_project_when_only_project_is_offered() -> None:
    request = _request("npm test")
    assert request.remember is not None and request.remember.scopes == ["project"]
    assert guard_scope(request, Decision("allow", "always")).scope == "project"
    assert guard_scope(request, Decision("allow", "project")).scope == "project"
    read_only = _request("git status")
    assert guard_scope(read_only, Decision("allow", "always")).scope == "always"


def test_guard_leaves_non_shell_calls_alone() -> None:
    request = ApprovalRequest(requestId="ap-2", sessionId="s", tool="net_fetch", args={})
    assert request.remember is None
    assert guard_scope(request, Decision("allow", "always")).scope == "always"


def test_a_request_built_without_remember_is_judged_by_its_command() -> None:
    """Code that builds an ``ApprovalRequest`` itself (no ``remember``)."""
    narrow = ApprovalRequest(
        requestId="ap-3", sessionId="s", tool="shell", args={"command": "git status"}
    )
    assert guard_scope(narrow, Decision("allow", "always")).scope == "always"
    risky = ApprovalRequest(
        requestId="ap-4", sessionId="s", tool="shell", args={"command": "git push"}
    )
    assert guard_scope(risky, Decision("allow", "project")).scope == "once"


class _Origin:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.asked: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float = 0) -> Any:
        self.asked.append(params)
        return self.answer


@pytest.fixture
def queue_setup(tmp_path: Path) -> tuple[ApprovalQueue, Allowlist, Session, Path]:
    project = tmp_path / "project"
    project.mkdir()
    allowlist = Allowlist(Paths.create(tmp_path / "home"), Settings())
    queue = ApprovalQueue(allowlist=allowlist)
    return queue, allowlist, Session(id="s-rem", workdir=project), project


async def test_the_request_carries_the_rule_and_the_answer_stores_it(
    queue_setup: tuple[ApprovalQueue, Allowlist, Session, Path],
) -> None:
    queue, allowlist, session, project = queue_setup
    origin = _Origin({"decision": "allow", "scope": "project"})
    session.origin_conn = origin
    decision = await queue.request(session, "shell", {"command": "git status -s"})
    assert decision.allowed and decision.scope == "project"
    assert origin.asked[0]["remember"] == {
        "pattern": "^git status( .*)?$",
        "display": "git status …",
        "scopes": ["project", "always"],
    }
    assert origin.asked[0]["scopeHint"] == "once"
    rules = [(i.pattern, i.target) for i in allowlist.list("project", workdir=project)]
    assert rules == [("^git status( .*)?$", SHELL_TARGET)]
    assert not allowlist.matches("shell", {"command": "git push --force"}, workdir=project)
    assert allowlist.matches("shell", {"command": "git status"}, workdir=project)


async def test_always_stores_a_global_rule_only_when_offered(
    queue_setup: tuple[ApprovalQueue, Allowlist, Session, Path],
) -> None:
    queue, allowlist, session, project = queue_setup
    session.origin_conn = _Origin({"decision": "allow", "scope": "always"})
    await queue.request(session, "shell", {"command": "ls -la"})
    await queue.request(session, "shell", {"command": "npm test"})
    assert [i.pattern for i in allowlist.list("global")] == ["^ls( .*)?$"]
    assert [i.pattern for i in allowlist.list("project", workdir=project)] == [
        "^npm test( .*)?$"
    ]


async def test_a_command_with_no_rule_is_allowed_once_and_stores_nothing(
    queue_setup: tuple[ApprovalQueue, Allowlist, Session, Path],
) -> None:
    queue, allowlist, session, project = queue_setup
    origin = _Origin({"decision": "allow", "scope": "always"})
    session.origin_conn = origin
    decision = await queue.request(session, "shell", {"command": "git push --force"})
    assert decision.allowed and decision.scope == "once"
    assert origin.asked[0]["remember"] is None
    assert allowlist.list(workdir=project) == []
    # Nor was it cached for the session: the next push asks again.
    await queue.request(session, "shell", {"command": "git push --force"})
    assert len(origin.asked) == 2


def test_the_capability_is_advertised() -> None:
    assert "shellRemember" in CAPABILITIES
