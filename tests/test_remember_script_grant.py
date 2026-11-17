"""m21 D: script grants — a person's allow remembered for exact script content.

A call that asks ONLY because of what the content of a project script it runs
does (``Verdict.script_grant``) may be remembered by a person for that exact
command and those files as they are now: "session" by default, "project" when
chosen (kept in the owner's settings, listed and removable through
``permission.allowlist.*``).  An edited script asks again, saying so.  Never for
control-plane reasons, a script outside the project, a script the line writes
first, a remote login, Jarvis or a messenger.

Every command line and script body below is DATA handed to the pure reader or to
an approval queue whose answers are canned; nothing is ever run.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.permissions.allowlist import SCRIPT_TARGET, Allowlist
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.security import code as code_mod
from snowpea_core.security import written
from snowpea_core.security.check import Context, check_command
from snowpea_core.security.lists import from_raw
from snowpea_core.server.app_server import allowlist_list_handler, allowlist_remove_handler
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import AllowlistListParams, AllowlistRemoveParams
from snowpea_core.session.session import Session

#: A script that computes a file name: the reader cannot tell what it writes.
SCRIPT = "#!/bin/sh\nOUT=$(date +%s).png\nscreencap > \"$OUT\"\n"
EDITED = "#!/bin/sh\nOUT=$(date +%s).jpg\nscreencap > \"$OUT\"\n"
RUN = {"command": "bash bin/shot.sh"}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    written.reset()
    project = tmp_path / "project"
    (project / "bin").mkdir(parents=True)
    (project / "bin" / "shot.sh").write_text(SCRIPT, encoding="utf-8")
    monkeypatch.setattr(code_mod, "RECENT_SEC", 10.0 ** 9)  # every script is "recent"
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    paths = Paths.create(tmp_path / "home")
    settings = Settings()
    allowlist = Allowlist(paths, settings)
    queue = ApprovalQueue(settings=settings, paths=paths, allowlist=allowlist)
    return SimpleNamespace(tmp=tmp_path, project=project, paths=paths, settings=settings,
                           allowlist=allowlist, queue=queue)


class _Conn:
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


def _session(world: SimpleNamespace, conn: Any, *, sid: str = "s-1",
             mode: str = "accept") -> Session:
    session = Session(id=sid, workdir=world.project)
    session.mode = mode
    session.origin_conn = conn
    return session


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# -- the verdict ----------------------------------------------------------------------


def _verdict(world: SimpleNamespace, line: str) -> Any:
    ctx = Context(workdir=world.project, user_home=Path("/home/u"),
                  home=Path("/home/u/.snowpea"), projects=(str(world.project),), mounts=(),
                  env_venv=False, unknown_code="ask")
    return check_command(line, from_raw({}), ctx, cwd=world.project)


def test_the_verdict_names_a_script_only_ask(world: SimpleNamespace) -> None:
    verdict = _verdict(world, "bash bin/shot.sh")
    assert verdict.opaque and verdict.script_grant
    assert [Path(p).name for p, _ in verdict.scripts] == ["shot.sh"]


@pytest.mark.parametrize(
    "line",
    [
        "bash -c 'OUT=$(date); screencap > $OUT'",  # the line's own code
        "python3 -c 'import os; os.system(input())'",
        "printf x > bin/new.sh; bash bin/new.sh",  # a script the line writes first
    ],
)
def test_the_line_itself_is_never_a_script_grant(world: SimpleNamespace, line: str) -> None:
    verdict = _verdict(world, line)
    assert verdict.opaque and not verdict.script_grant


@pytest.mark.parametrize(
    "body",
    [
        'eval "$(cat cmds.txt)"\n',  # code from a file the grant does not hash
        "X=$(cat prog.txt)\n$X --go\n",  # a computed program
        "cat cmds.txt | sh\n",  # a pipe into a shell
        "export PYTHONPATH=$(pwd)/lib\npython3 tool.py\n",  # a variable read as code
    ],
)
def test_a_script_that_runs_unhashed_code_is_never_a_script_grant(
    world: SimpleNamespace, body: str
) -> None:
    (world.project / "bin" / "run.sh").write_text("#!/bin/sh\n" + body, encoding="utf-8")
    verdict = _verdict(world, "bash bin/run.sh")
    assert verdict.opaque and not verdict.script_grant


def test_a_script_on_snowpea_controls_is_never_a_script_grant(world: SimpleNamespace) -> None:
    (world.project / "bin" / "ctl.sh").write_text(
        "#!/bin/sh\nX=$(date)\nsnowpea config set security.unknownCode allow\n",
        encoding="utf-8")
    verdict = _verdict(world, "bash bin/ctl.sh")
    assert verdict.opaque and not verdict.script_grant


# -- the offer and the session grant --------------------------------------------------


async def test_the_request_offers_the_grant(world: SimpleNamespace) -> None:
    person = _Conn({"decision": "allow", "scope": "session"})
    await world.queue.request(_session(world, person), "shell", dict(RUN))
    remember = person.asked[0]["remember"]
    assert person.asked[0]["personOnly"] is True
    script = str(world.project / "bin" / "shot.sh")
    assert remember == {
        "pattern": "bash bin/shot.sh",
        "display": "bash bin/shot.sh (while bin/shot.sh is unchanged)",
        "note": "remembered only for this exact command while bin/shot.sh is unchanged; "
                "asked again when it changes",
        "scopes": ["project"],
        "kind": "script",
        "script": {"path": script, "sha256": _sha(SCRIPT)},
        "scripts": [{"path": script, "sha256": _sha(SCRIPT)}],
    }


@pytest.mark.parametrize("mode", ["plan", "accept"])
async def test_a_session_grant_holds_while_the_script_is_unchanged(
    world: SimpleNamespace, mode: str
) -> None:
    person = _Conn({"decision": "allow", "scope": "session"})
    session = _session(world, person, mode=mode)
    first = await world.queue.request(session, "shell", dict(RUN))
    assert first.allowed and first.scope == "session"
    again = await world.queue.request(session, "shell", dict(RUN))
    assert again.allowed and again.by == "script-grant"
    assert len(person.asked) == 1
    # Another command running the same script, another session: asked.
    await world.queue.request(session, "shell", {"command": "bash bin/shot.sh x"})
    await world.queue.request(_session(world, person, sid="s-2", mode=mode), "shell",
                              dict(RUN))
    assert len(person.asked) == 3
    # Nothing was stored, and no first word was cached.
    assert world.allowlist.list(workdir=world.project) == []
    assert not world.queue._cache


async def test_unknown_code_ask_in_auto_mode_is_covered_too(world: SimpleNamespace) -> None:
    world.settings.security.unknownCode = "ask"
    person = _Conn({"decision": "allow", "scope": "session"})
    session = _session(world, person, mode="auto")
    await world.queue.request(session, "shell", dict(RUN))
    assert (await world.queue.request(session, "shell", dict(RUN))).by == "script-grant"
    assert len(person.asked) == 1


async def test_an_edited_script_asks_again_saying_so(world: SimpleNamespace) -> None:
    person = _Conn({"decision": "allow", "scope": "session"})
    session = _session(world, person)
    await world.queue.request(session, "shell", dict(RUN))
    (world.project / "bin" / "shot.sh").write_text(EDITED, encoding="utf-8")
    decided = await world.queue.request(session, "shell", dict(RUN))
    assert len(person.asked) == 2
    assert person.asked[1]["note"].startswith(
        "the script changed since you allowed it: bin/shot.sh.")
    assert person.asked[1]["remember"]["script"]["sha256"] == _sha(EDITED)
    # The new allow holds for the new content.
    assert decided.scope == "session"
    assert (await world.queue.request(session, "shell", dict(RUN))).by == "script-grant"


# -- the project grant -------------------------------------------------------------------


async def test_a_project_grant_is_kept_listed_and_removable(world: SimpleNamespace) -> None:
    person = _Conn({"decision": "allow", "scope": "always"})  # kept for the project
    session = _session(world, person)
    decided = await world.queue.request(session, "shell", dict(RUN))
    assert decided.scope == "project"
    # Never in the project's own settings file.
    assert not (world.project / ".snowpea" / "settings.json").exists()
    stored = Settings.load(world.paths).allowlist
    assert [e.target for e in stored] == [SCRIPT_TARGET]
    # A restart keeps it.
    restarted = ApprovalQueue(settings=world.settings, paths=world.paths,
                              allowlist=world.allowlist)
    assert (await restarted.request(session, "shell", dict(RUN))).by == "script-grant"
    # permission.allowlist.list shows it like any row, plus the new fields.
    core = SimpleNamespace(allowlist=world.allowlist)
    listed = await allowlist_list_handler(
        None, AllowlistListParams(workdir=str(world.project)), core)  # type: ignore[arg-type]
    rows = [row.model_dump(mode="json") for row in listed.patterns]
    script = str(world.project / "bin" / "shot.sh")
    assert rows == [{
        "patternId": stored[0].id,
        "pattern": "bash bin/shot.sh",
        "scope": "project",
        "tool": "shell",
        "origin": None,
        "createdAt": stored[0].created_at,
        "hostToolsFrom": None,
        "reasonCode": None,
        "kind": "script",
        "display": "bash bin/shot.sh (while bin/shot.sh is unchanged)",
        "script": {"path": script, "sha256": _sha(SCRIPT)},
        "scripts": [{"path": script, "sha256": _sha(SCRIPT)}],
    }]
    # Global listings and the shell matcher ignore it.
    assert world.allowlist.list("global") == []
    assert not world.allowlist.matches("shell", dict(RUN), workdir=world.project)
    # Removed with the existing call: asked again.
    await allowlist_remove_handler(
        None, AllowlistRemoveParams(patternId=stored[0].id, workdir=str(world.project)),
        core)  # type: ignore[arg-type]
    await restarted.request(session, "shell", dict(RUN))
    assert len(person.asked) == 2


async def test_a_project_grant_is_for_that_project_only(world: SimpleNamespace) -> None:
    person = _Conn({"decision": "allow", "scope": "project"})
    await world.queue.request(_session(world, person), "shell", dict(RUN))
    other = world.tmp / "other"
    (other / "bin").mkdir(parents=True)
    (other / "bin" / "shot.sh").write_text(SCRIPT, encoding="utf-8")
    session = Session(id="s-o", workdir=other)
    session.mode = "accept"
    session.origin_conn = person
    await world.queue.request(session, "shell", dict(RUN))
    assert len(person.asked) == 2


# -- never ----------------------------------------------------------------------------


async def test_a_script_outside_the_project_is_never_offered(world: SimpleNamespace) -> None:
    outside = world.tmp / "elsewhere"
    outside.mkdir()
    (outside / "x.sh").write_text(SCRIPT, encoding="utf-8")
    person = _Conn({"decision": "allow", "scope": "session"})
    session = _session(world, person)
    decided = await world.queue.request(session, "shell", {"command": f"bash {outside}/x.sh"})
    assert decided.scope == "once" and person.asked[0]["remember"] is None
    await world.queue.request(session, "shell", {"command": f"bash {outside}/x.sh"})
    assert len(person.asked) == 2


async def test_a_line_of_its_own_is_never_offered(world: SimpleNamespace) -> None:
    person = _Conn({"decision": "allow", "scope": "session"})
    session = _session(world, person)
    for command in ("bash bin/shot.sh $(date)", "bash bin/shot.sh && true",
                    "OUT=1 bash bin/shot.sh"):
        await world.queue.request(session, "shell", {"command": command})
        assert person.asked[-1]["remember"] is None or (
            person.asked[-1]["remember"].get("kind") != "script"), command
    assert not world.queue._script_grants


async def test_a_remote_login_never_keeps_a_grant(world: SimpleNamespace) -> None:
    remote = _Conn({"decision": "allow", "scope": "project"}, kind="web", remote=True)
    session = _session(world, remote)
    # Whether or not a remote login may answer a person-only request at all, its
    # allow never keeps a grant (a refused allow leaves the request to time out).
    decided = await world.queue.request(session, "shell", dict(RUN), timeout_sec=1)
    if decided.allowed:
        assert decided.scope == "once"
    assert not world.queue._script_grants
    assert Settings.load(world.paths).allowlist == []


async def test_jarvis_and_a_messenger_never_keep_a_grant(world: SimpleNamespace) -> None:
    session = _session(world, None)
    task = asyncio.ensure_future(
        world.queue.request(session, "shell", dict(RUN), unattended=True))
    for _ in range(50):
        await asyncio.sleep(0)
        if world.queue.unattended(session.id):
            break
    request_id = world.queue.unattended(session.id)[0].requestId
    with pytest.raises(RpcError):
        await world.queue.respond(request_id, "allow", "session", by="jarvis")
    await world.queue.respond(request_id, "deny", by="jarvis")
    assert not (await task).allowed
    assert not world.queue._script_grants


async def test_a_caller_person_only_request_is_never_offered(world: SimpleNamespace) -> None:
    person = _Conn({"decision": "allow", "scope": "session"})
    session = _session(world, person)
    await world.queue.request(session, "shell", dict(RUN), person_only=True)
    assert person.asked[0]["remember"] is None
    await world.queue.request(session, "shell", dict(RUN), person_only=True)
    assert len(person.asked) == 2
