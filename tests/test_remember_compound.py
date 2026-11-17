"""m21 C: compound command lines (``;`` ``&&`` ``||`` ``|``) and remembering.

(i) A chain every simple command of which would run unasked on its own (a
stored rule, plan mode's inspection, a session answer) is not asked merely
because it is a chain.  (ii) "Allow for this session" on a compound line that
does ask remembers that exact line (whitespace normalised, folder included) for
that session only, in memory, and only from a person's own surface.  A plain
command's answer is exactly what 0.2.89 stored.

Every command line below is DATA handed to pure functions or to an approval
queue whose answers are canned; nothing is ever run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
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
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.permissions.shell_remember import (
    EXACT_LINE_NOTE,
    exact_line,
    is_compound,
    segments,
)
from snowpea_core.server.protocol import ApprovalRequest
from snowpea_core.session.session import Session

SHELL = SimpleNamespace(name="shell", permission="exec", permission_for=None)


# -- pure helpers ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "parts"),
    [
        ("git status && npm test", ["git status", "npm test"]),
        ("git status;git diff", ["git status", "git diff"]),
        ("ls -la || true", ["ls -la", "true"]),
        ("git log --oneline | head -5", ["git log --oneline", "head -5"]),
        ("grep 'a;b' x && ls", None),  # a separator inside quotes: not split
        ('echo "x|y" | wc', None),
        ("ls > out && ls", None),  # a redirection
        ("ls $(id) && ls", None),  # a substitution
        ("ls & id", None),  # a background job
        ("ls && $X", None),  # a variable program
        ("ls &&", None),  # an empty part
        ("ls ;; ls", None),
        ("ls\nid", None),  # a second line
        ("FOO=1 ls && ls", None),  # an environment prefix
        ("git status", None),  # not a chain
    ],
)
def test_segments(line: str, parts: list[str] | None) -> None:
    assert segments(line) == parts


def test_exact_line_normalises_whitespace_outside_quotes_only() -> None:
    assert exact_line("  git status   &&\tnpm  test ") == "git status && npm test"
    assert exact_line("echo 'a   b' |  wc") == "echo 'a   b' | wc"
    assert exact_line('echo "a  \\"  b" | wc') == 'echo "a  \\"  b" | wc'
    assert exact_line("echo 'open | wc") is None
    assert exact_line("ls ​| wc") is None  # an invisible character


@pytest.mark.parametrize("line", ["a;b", "a&&b", "a|b", "a<b", "a>b", "a`b`", "a $(b)",
                                  "a\nb", "a &"])
def test_compound_marks(line: str) -> None:
    assert is_compound(line)


@pytest.mark.parametrize("line", ["git status", "ls $HOME", "ls *.py", "echo (x)"])
def test_plain_lines_are_not_compound(line: str) -> None:
    assert not is_compound(line)


# -- C(i): a chain of allowed parts is not asked --------------------------------------


@pytest.fixture
def rules(tmp_path: Path) -> tuple[PermissionPolicy, Any]:
    project = tmp_path / "project"
    project.mkdir()
    allowlist = Allowlist(Paths.create(tmp_path / "home"), Settings())
    allowlist.add("^git status( .*)?$", "global")
    allowlist.add("^npm test( .*)?$", "global")
    allowlist.add("^sh( .*)?$", "global")  # a (hand-typed) rule for a shell
    return PermissionPolicy(allowlist), SimpleNamespace(workdir=project, mode="accept")


@pytest.mark.parametrize(
    ("mode", "line", "verdict"),
    [
        ("accept", "git status && npm test", "allow"),
        ("accept", "git status ; npm test -- -u", "allow"),
        ("accept", "git status | npm test", "allow"),
        ("accept", "git status && ls", "ask"),  # ls has no rule in accept mode
        ("accept", "git status && git push", "ask"),
        ("accept", "git status | sh", "ask"),  # a shell reading a pipe is never covered
        ("accept", "git status > x && npm test", "ask"),
        ("accept", "git status && npm test $(id)", "ask"),
        ("plan", "ls -la && npm test", "allow"),  # an inspection plus a rule
        ("plan", "cat a | wc -l && git status", "allow"),
        ("plan", "ls && make", "ask"),
        ("auto", "anything && at-all", "allow"),  # the mode allows exec already
    ],
)
def test_a_chain_of_allowed_parts_is_not_asked(
    rules: tuple[PermissionPolicy, Any], mode: str, line: str, verdict: str
) -> None:
    policy, session = rules
    session.mode = mode
    assert policy.decide(mode, "exec", SHELL, {"command": line}, session) == verdict


def test_each_part_alone_is_what_decides(rules: tuple[PermissionPolicy, Any]) -> None:
    policy, session = rules
    for part in ("git status", "npm test"):
        assert policy.decide("accept", "exec", SHELL, {"command": part}, session) == "allow"


# -- the queue --------------------------------------------------------------------------


class _Conn:
    """An open connection answering every ``approval.request`` the same way."""

    authenticated = True
    closed = False

    def __init__(self, answer: dict[str, Any], *, kind: str = "tui",
                 remote: bool = False) -> None:
        self.answer = answer
        self.client_kind = kind
        self.remote_login = remote
        self.asked: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float = 0) -> Any:
        self.asked.append(params)
        return self.answer


@pytest.fixture
def setup(tmp_path: Path) -> tuple[ApprovalQueue, Allowlist, Path, Path]:
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    allowlist = Allowlist(Paths.create(tmp_path / "home"), Settings())
    return ApprovalQueue(allowlist=allowlist), allowlist, project, tmp_path


LINE = "git status && git log --oneline"


async def test_the_request_offers_the_exact_line(
    setup: tuple[ApprovalQueue, Allowlist, Path, Path],
) -> None:
    queue, _allowlist, project, _tmp = setup
    person = _Conn({"decision": "allow", "scope": "session"})
    session = Session(id="s-1", workdir=project)
    session.origin_conn = person
    await queue.request(session, "shell", {"command": "git status   &&  git log --oneline"})
    assert person.asked[0]["remember"] == {
        "pattern": r"^git\ status\ \&\&\ git\ log\ \-\-oneline$",
        "display": LINE,
        "scopes": [],
        "note": EXACT_LINE_NOTE,
        "exactLine": True,
    }


async def test_session_answer_remembers_the_exact_line_only(
    setup: tuple[ApprovalQueue, Allowlist, Path, Path],
) -> None:
    queue, allowlist, project, _tmp = setup
    person = _Conn({"decision": "allow", "scope": "session"})
    session = Session(id="s-1", workdir=project)
    session.origin_conn = person
    first = await queue.request(session, "shell", {"command": LINE})
    assert first.allowed and first.scope == "session"
    # The identical line, and the same line spaced differently: not asked.
    again = await queue.request(session, "shell", {"command": LINE})
    assert again.by == "cache"
    spaced = await queue.request(session, "shell",
                                 {"command": "git status &&   git log  --oneline"})
    assert spaced.by == "cache"
    assert len(person.asked) == 1
    # Changed text, another folder, the background, its first word alone: asked.
    for args in (
        {"command": "git status && git log --oneline -5"},
        {"command": LINE, "cwd": "sub"},
        {"command": LINE, "background": True},
        {"command": "git status"},
        {"command": "git status && id"},
    ):
        before = len(person.asked)
        await queue.request(session, "shell", args)
        assert len(person.asked) == before + 1, args
    # Nothing was stored anywhere.
    assert allowlist.list(workdir=project) == []


async def test_other_session_and_restart_forget_it(
    setup: tuple[ApprovalQueue, Allowlist, Path, Path],
) -> None:
    queue, allowlist, project, _tmp = setup
    person = _Conn({"decision": "allow", "scope": "session"})
    one = Session(id="s-1", workdir=project)
    one.origin_conn = person
    await queue.request(one, "shell", {"command": LINE})
    other = Session(id="s-2", workdir=project)
    other.origin_conn = person
    await queue.request(other, "shell", {"command": LINE})
    assert len(person.asked) == 2
    restarted = ApprovalQueue(allowlist=allowlist)
    await restarted.request(one, "shell", {"command": LINE})
    assert len(person.asked) == 3


async def test_project_and_always_on_a_compound_line_are_session_only(
    setup: tuple[ApprovalQueue, Allowlist, Path, Path],
) -> None:
    queue, allowlist, project, tmp = setup
    for scope in ("project", "always"):
        person = _Conn({"decision": "allow", "scope": scope})
        session = Session(id=f"s-{scope}", workdir=project)
        session.origin_conn = person
        decided = await queue.request(session, "shell", {"command": LINE})
        assert decided.scope == "session"
        assert (await queue.request(session, "shell", {"command": LINE})).by == "cache"
    assert allowlist.list(workdir=project) == []
    assert Settings.load(Paths.create(tmp / "home")).allowlist == []


async def test_a_remote_login_never_remembers_a_line(
    setup: tuple[ApprovalQueue, Allowlist, Path, Path],
) -> None:
    queue, _allowlist, project, _tmp = setup
    remote = _Conn({"decision": "allow", "scope": "session"}, kind="web", remote=True)
    session = Session(id="s-r", workdir=project)
    session.origin_conn = remote
    decided = await queue.request(session, "shell", {"command": LINE})
    assert decided.allowed and decided.scope == "once"
    await queue.request(session, "shell", {"command": LINE})
    assert len(remote.asked) == 2


async def test_a_browser_remembers_a_line_only_from_a_click(
    setup: tuple[ApprovalQueue, Allowlist, Path, Path],
) -> None:
    queue, _allowlist, project, _tmp = setup
    browser = _Conn({"decision": "allow", "scope": "session"}, kind="browser")
    browser.client_version = "0.3.0"
    session = Session(id="s-b", workdir=project)
    session.origin_conn = browser
    first = await queue.request(session, "shell", {"command": LINE})
    assert first.scope == "once"
    browser.answer = {"decision": "allow", "scope": "session", "actor": "person"}
    second = await queue.request(session, "shell", {"command": LINE})
    assert second.scope == "session"
    assert (await queue.request(session, "shell", {"command": LINE})).by == "cache"


async def _answer_unattended(queue: ApprovalQueue, session: Session, args: dict[str, Any],
                             **respond: Any) -> Decision:
    task = asyncio.ensure_future(queue.request(session, "shell", args, unattended=True))
    for _ in range(50):
        await asyncio.sleep(0)
        if queue.unattended(session.id):
            break
    request_id = queue.unattended(session.id)[0].requestId
    await queue.respond(request_id, "allow", **respond)
    return await task


async def test_jarvis_and_in_process_answers_never_remember_a_line(
    setup: tuple[ApprovalQueue, Allowlist, Path, Path],
) -> None:
    queue, _allowlist, project, _tmp = setup
    session = Session(id="s-j", workdir=project)
    for by in ("jarvis", "gateway:telegram:1", "client"):
        decided = await _answer_unattended(queue, session, {"command": LINE},
                                           scope="session", by=by)
        assert decided.allowed and decided.scope == "once", by
    assert not queue._lines


def test_an_uncheckable_line_offers_no_memory() -> None:
    # The queue passes cacheable=False for a person-only (uncheckable) call.
    assert remember_for("shell", {"command": LINE}, cacheable=False) is None
    assert remember_for("shell", {"command": LINE}, guard="agent-switch") is None
    assert remember_for("shell", {"command": LINE, "onceOnly": True}) is None
    assert remember_for("shell", {"command": "ls ​| wc"}) is None


async def test_a_person_only_compound_line_is_never_remembered(
    setup: tuple[ApprovalQueue, Allowlist, Path, Path],
) -> None:
    queue, _allowlist, project, _tmp = setup
    person = _Conn({"decision": "allow", "scope": "session"})
    session = Session(id="s-p", workdir=project)
    session.origin_conn = person
    decided = await queue.request(session, "shell", {"command": LINE}, person_only=True)
    assert decided.allowed and decided.scope == "once"
    assert person.asked[0]["remember"] is None
    await queue.request(session, "shell", {"command": LINE}, person_only=True)
    assert len(person.asked) == 2


def test_guard_scope_on_an_exact_line() -> None:
    args = {"command": LINE}
    request = ApprovalRequest(requestId="ap-1", sessionId="s", tool="shell", args=args,
                              remember=remember_for("shell", args))
    assert guard_scope(request, Decision("allow", "session")).scope == "once"
    assert guard_scope(request, Decision("allow", "session", line_ok=True)).scope == "session"
    assert guard_scope(request, Decision("allow", "always", line_ok=True)).scope == "session"


async def test_a_session_answer_covers_a_chain_of_answered_parts(
    setup: tuple[ApprovalQueue, Allowlist, Path, Path],
) -> None:
    queue, _allowlist, project, _tmp = setup
    person = _Conn({"decision": "allow", "scope": "session"})
    session = Session(id="s-c", workdir=project)
    session.origin_conn = person
    await queue.request(session, "shell", {"command": "git status"})
    await queue.request(session, "shell", {"command": "npm test"})
    assert (await queue.request(session, "shell", {"command": "git status && npm test"})
            ).by == "cache"
    assert len(person.asked) == 2


# -- plain commands: exactly what 0.2.89 stored --------------------------------------------


async def test_plain_command_answers_are_unchanged(
    setup: tuple[ApprovalQueue, Allowlist, Path, Path],
) -> None:
    queue, allowlist, project, _tmp = setup
    person = _Conn({"decision": "allow", "scope": "session"})
    session = Session(id="s-plain", workdir=project)
    session.origin_conn = person
    decided = await queue.request(session, "shell", {"command": "git status -s"})
    assert decided.scope == "session"
    # The session answer is the first-word key, as before ...
    assert queue._cache == {("s-plain", "shell", "git")}
    assert not queue._lines
    # The wire object is byte-for-byte 0.2.89's: no new key, not even a null.
    assert person.asked[0]["remember"] == {
        "pattern": "^git status( .*)?$",
        "display": "git status …",
        "scopes": ["project", "always"],
        "note": None,
    }
    assert (await queue.request(session, "shell", {"command": "git log"})).by == "cache"
    # ... and a project answer stores the proposed rule, as before.
    person.answer = {"decision": "allow", "scope": "project"}
    await queue.request(session, "shell", {"command": "npm test -- -u"})
    assert [(i.pattern, i.target) for i in allowlist.list("project", workdir=project)] == [
        ("^npm test( .*)?$", SHELL_TARGET)
    ]
    # A plain line that is not matchable ($VAR) keeps its old "once".
    decided = await queue.request(session, "shell", {"command": "ls $HOME"})
    assert decided.scope == "once"
