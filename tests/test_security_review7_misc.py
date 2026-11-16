"""Security review 7, part B — M-1, M-3, the Low findings and the Info items.

M-1: a remote web login writes only a global skill that widens nothing; never a
project's skill, agent or command file, a global agent or command, a built-in's
name, or front matter with ``allowed-tools`` / ``autoInject`` / ``permission`` /
``hooks`` / ``model`` / ``tools``.  ``agent.create`` and ``agent.bindChannel``
are a local person's; ``team.guide.delete`` is the class of ``team.guide.set``.

M-3: a remote login's allow never stores an allowlist rule.

Low 2 (sign-in behind a shared address), Low 3 (an expired login's socket),
Low 5 (exception text), Low 6 (a browser's own session, the origin surface),
Low 9 (``fs.write`` resolves once); Info: ``audio.transcribe {path}``.

Tables are per connection kind.  Command strings are DATA; nothing runs.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.permissions.approval_queue import (
    NO_STORED_RULE_KINDS,
    ApprovalQueue,
    clamp_scope,
)
from snowpea_core.server import (
    errors,
    fs_handlers,
    person_gate,
    session_handlers,
    settings_gate,
    skill_gate,
    skill_handlers,
    web_remote,
)
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    AudioTranscribeParams,
    FsWriteParams,
    SkillCreateParams,
    SkillWriteParams,
)
from snowpea_core.server.rpc import RpcDispatcher
from snowpea_core.server.session_handlers import wire_core
from snowpea_core.server.web_remote import (
    MAX_FAILURES_PER_IP,
    MAX_WAITING_PER_IP,
    RemoteAuth,
    create_remote_app,
)

pytestmark = pytest.mark.real_mode_guard


def _conn(kind: str | None = "ide", *, remote: bool = False, version: str = "0.3.0",
          client_id: str | None = None, closed: bool = False) -> Any:
    return SimpleNamespace(authenticated=True, closed=closed, client_kind=kind,
                           remote_login=remote, client_version=version,
                           client_id=client_id or f"{kind}-1", surface_id=f"{kind}-s")


PERSON = _conn("ide")
REMOTE = _conn("web", remote=True)
BROWSER = _conn("browser", client_id="b-1")
LEGACY = _conn("browser", version="0.2.0", client_id="b-legacy")
CONNECTOR = _conn("connector")
CLOSED = _conn("ide", closed=True)
#: name -> (connection, actor): every kind the gates tell apart.
CALLERS: dict[str, tuple[Any, str | None]] = {
    "person": (PERSON, None), "remote_login": (REMOTE, None), "connector": (CONNECTOR, None),
    "legacy": (LEGACY, None), "browser": (BROWSER, None), "browser+actor": (BROWSER, "person"),
    "closed": (CLOSED, None), "none": (None, None),
}


@pytest.fixture
def core(tmp_path: Path) -> Core:
    made = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    return wire_core(made)


def _local_only(caught: pytest.ExceptionInfo[RpcError], what: str | None = None) -> None:
    assert caught.value.data["reason"] == "local_only"
    assert caught.value.data["kind"] == "remote_login"
    if what is not None:
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
        assert caught.value.data["what"] == what


# -- M-1: what a remote login may write as a skill ---------------------------------------------

PLAIN_SKILL = "---\nname: notes\ndescription: how we take notes\n---\nWrite it down.\n"


def _skill(extra: str) -> str:
    return f"---\nname: notes\ndescription: how we do it\n{extra}\n---\nbody\n"


@pytest.mark.parametrize(
    ("document", "keys"),
    [
        (PLAIN_SKILL, []),
        (_skill("version: 2\nlicense: MIT"), []),
        (_skill("allowed-tools: [shell]"), ["allowedtools"]),
        (_skill("allowed_tools: shell"), ["allowedtools"]),
        (_skill("allowedTools:\n  - shell"), ["allowedtools"]),
        (_skill('"allowed-tools": [shell]'), ["allowedtools"]),  # a quoted key
        (_skill("autoInject: {keywords: [mail]}"), ["autoinject"]),
        (_skill("auto-inject:\n  keywords:\n    - mail"), ["autoinject"]),
        (_skill("permission: auto"), ["permission"]),
        (_skill("permissions: {exec: allow}"), ["permissions"]),
        (_skill("hooks:\n  PreToolUse: x"), ["hooks"]),
        (_skill("model: some:model"), ["model"]),
        (_skill("tools: [shell, write_file]"), ["tools"]),
        (_skill("Model: x\nTOOLS: y"), ["model", "tools"]),  # case does not hide it
        ("﻿" + _skill("hooks: x"), ["hooks"]),  # nor a byte-order mark
        ("\n\n" + _skill("permission: auto"), ["permission"]),  # nor blank lines first
    ],
)
def test_front_matter_that_widens_what_runs(core: Core, document: str, keys: list[str]) -> None:
    found = sorted(skill_gate.front_matter_keys(document) & skill_gate.WIDENING_KEYS)
    assert found == sorted(keys), document
    judge = lambda conn: skill_gate.content_refusal(  # noqa: E731
        conn, core, method="skill.write", content=document)
    for name in ("person", "connector", "legacy", "browser", "browser+actor", "none"):
        judge(CALLERS[name][0])  # only a remote login is judged here
    if not keys:
        judge(REMOTE)
        return
    with pytest.raises(RpcError) as caught:
        judge(REMOTE)
    _local_only(caught, "front_matter")
    assert caught.value.data["keys"] == sorted(keys)


@pytest.mark.parametrize(
    ("kind", "scope", "name", "content", "code", "what"),
    [
        ("skill", "global", "notes", PLAIN_SKILL, None, None),  # the one thing it may write
        ("skill", "global", "notes", _skill("allowed-tools: [shell]"),
         errors.JARVIS_PERSON_ONLY, "front_matter"),
        ("skill", "project", "notes", PLAIN_SKILL, errors.PROTECTED, None),  # a control file
        ("agent", "project", "helper", "---\nname: helper\n---\nbody", errors.PROTECTED, None),
        ("command", "project", "ship", "do it", errors.PROTECTED, None),
        ("agent", "global", "helper", "---\nname: helper\n---\nbody",
         errors.JARVIS_PERSON_ONLY, "agent"),
        ("command", "global", "ship", "do it", errors.JARVIS_PERSON_ONLY, "command"),
        # A name snowpea's own commands and agents use.
        ("skill", "global", "ralph", PLAIN_SKILL.replace("notes", "ralph"),
         errors.JARVIS_PERSON_ONLY, "builtin_name"),
        ("skill", "global", "executor", PLAIN_SKILL.replace("notes", "executor"),
         errors.JARVIS_PERSON_ONLY, "builtin_name"),
    ],
)
async def test_skill_write_from_a_remote_login(
    core: Core, tmp_path: Path, kind: str, scope: str, name: str, content: str,
    code: str | None, what: str | None
) -> None:
    work = tmp_path / "project"
    work.mkdir()
    params = SkillWriteParams(name=name, kind=kind, scope=scope, workdir=str(work),
                              content=content)
    home = Path(core.paths.home)
    before = sorted(str(p) for p in [*home.rglob("*.md"), *work.rglob("*.md")])
    if code is None:
        result = await skill_handlers.skill_write_handler(REMOTE, params, core)
        assert Path(result.path).read_text("utf-8").strip() == content.strip()
        return
    with pytest.raises(RpcError) as caught:
        await skill_handlers.skill_write_handler(REMOTE, params, core)
    assert caught.value.code == code
    _local_only(caught, what)
    assert sorted(str(p) for p in [*home.rglob("*.md"), *work.rglob("*.md")]) == before
    # A local person writes each of them, as before.
    await skill_handlers.skill_write_handler(PERSON, params, core)


async def test_skill_create_with_content_from_a_remote_login(core: Core, tmp_path: Path) -> None:
    work = tmp_path / "project"
    work.mkdir()
    for scope, content, code in (
        ("project", PLAIN_SKILL, errors.PROTECTED),
        ("global", _skill("autoInject: {keywords: [x]}"), errors.JARVIS_PERSON_ONLY),
        ("global", _skill("hooks: x"), errors.JARVIS_PERSON_ONLY),
    ):
        with pytest.raises(RpcError) as caught:
            await skill_handlers.skill_create_handler(
                REMOTE, SkillCreateParams(name="notes", workdir=str(work), scope=scope,
                                          content=content), core)
        assert caught.value.code == code
        _local_only(caught)
    assert not (work / ".snowpea").exists()
    assert not (Path(core.paths.home) / "skills" / "notes").exists()
    made = await skill_handlers.skill_create_handler(
        REMOTE, SkillCreateParams(name="notes", workdir=str(work), scope="global",
                                  content=PLAIN_SKILL), core)
    assert Path(str(made.path)).is_file()


async def test_a_skill_a_model_writes_for_a_remote_login_is_judged_too(
    core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``skill.create {description}`` and ``/skill create``: the same judgement."""
    from snowpea_core.commands import skill_cmd
    from snowpea_core.commands.registry import CommandContext

    async def generate(_provider: Any, name: str, _description: str) -> str:
        return f"---\nname: {name}\ndescription: how we do it\nallowed-tools: [shell]\n---\nbody\n"

    monkeypatch.setattr(skill_cmd, "generate_skill_document", generate)
    monkeypatch.setattr(core.providers, "get", lambda *_a, **_k: object())
    work = tmp_path / "project"
    work.mkdir()
    session = await core.sessions.create(work)
    said: list[str] = []

    async def run(conn: Any, args: str) -> str:
        ctx = CommandContext(core=core, session=session, turn_id="t", conn=conn)

        async def say(text: str) -> None:
            said.append(text)

        ctx.say = say  # type: ignore[method-assign]
        await skill_cmd.create_skill(ctx, args)
        return said[-1]

    assert "local connection" in await run(REMOTE, 'notes "how we take notes"')  # project
    assert "front matter" in await run(REMOTE, 'notes "how we take notes" --global')
    assert not (work / ".snowpea").exists()
    assert not (Path(core.paths.home) / "skills" / "notes").exists()
    assert "Created skill" in await run(PERSON, 'notes "how we take notes" --global')


async def test_skill_learn_from_a_remote_login(core: Core, tmp_path: Path) -> None:
    from snowpea_core.server.protocol import SkillLearnParams

    work = tmp_path / "project"
    work.mkdir()
    session = await core.sessions.create(work)
    with pytest.raises(RpcError) as caught:  # the project scope: before any model call
        await skill_handlers.skill_learn_handler(
            REMOTE, SkillLearnParams(sessionId=session.id, name="notes", scope="project"), core)
    assert caught.value.code == errors.PROTECTED
    _local_only(caught)
    with pytest.raises(RpcError) as caught:
        await skill_handlers.skill_learn_handler(
            REMOTE, SkillLearnParams(sessionId=session.id, name="ralph", scope="global"), core)
    _local_only(caught, "builtin_name")


@pytest.mark.parametrize(
    ("method", "verdicts"),
    [
        # person, remote login, connector, legacy browser, browser unmarked, browser marked
        ("agent.create", "ynnnnn"),
        ("agent.bindChannel", "ynnnnn"),
        ("team.guide.delete", "ynnyny"),
        ("team.guide.set", "ynnyny"),  # ... the class it joins
        ("gateway.bind", "ynnnnn"),
        ("agent.delete", "yynnnn"),  # only takes away
        ("skill.write", "yynyny"),  # narrowed by its handler for a remote login
    ],
)
def test_the_methods_m1_and_low8_moved(method: str, verdicts: str) -> None:
    def passes(name: str) -> str:
        conn, actor = CALLERS[name]
        params = {"actor": actor} if actor else {}
        try:
            person_gate.method_refusal(method, conn)
            settings_gate.browser_method_refusal(method, params, conn, None)
            settings_gate.kind_method_refusal(method, params, conn, None)
        except RpcError as exc:
            assert exc.code == errors.JARVIS_PERSON_ONLY
            return "n"
        return "y"

    order = ("person", "remote_login", "connector", "legacy", "browser", "browser+actor")
    assert "".join(passes(name) for name in order) == verdicts
    assert passes("closed") == "n"  # nobody calls anything


def test_the_agent_create_command_follows_its_rpc() -> None:
    from snowpea_core.commands.persists import twins
    from snowpea_core.commands.registry import gate_command

    assert twins("agent", 'create "a reviewer"') == ("agent.create",)
    assert twins("agent", "list") == twins("agent", "use x") == ()
    session = SimpleNamespace(mode="accept", auto_after_plan=False, workdir=".")
    for conn in (REMOTE, BROWSER, LEGACY, CONNECTOR):
        with pytest.raises(RpcError):
            gate_command(None, session, "agent", 'create "a reviewer"', conn=conn, actor="person")
    gate_command(None, session, "agent", 'create "a reviewer"', conn=PERSON)
    gate_command(None, session, "agent", "list", conn=REMOTE)


# -- M-3: a remote login's allow stores no rule ------------------------------------------------


@pytest.mark.parametrize("scope", ["once", "session", "project", "always", "site"])
@pytest.mark.parametrize("name", sorted(CALLERS))
def test_the_scope_an_allow_may_apply(name: str, scope: str) -> None:
    conn, _actor = CALLERS[name]
    kind = person_gate.connection_kind(conn)
    stores = scope in ("project", "always", "site")
    want = "session" if stores and kind in NO_STORED_RULE_KINDS else scope
    assert clamp_scope(conn, scope) == want
    assert NO_STORED_RULE_KINDS == {"remote_login", "connector", "closed"}


class _Hub:
    def __init__(self) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.sessions = SimpleNamespace(get=lambda _i: None)

    async def notify(self, method: str, params: dict[str, Any], **_kw: Any) -> None:
        self.notes.append((method, params))


@pytest.mark.parametrize("scope", ["project", "always"])
@pytest.mark.parametrize(("name", "stored"), [("person", True), ("remote_login", False)])
async def test_approval_respond_from_a_remote_login_stores_no_rule(
    tmp_path: Path, name: str, stored: bool, scope: str
) -> None:
    hub = _Hub()
    queue = ApprovalQueue(Settings(), Paths.create(tmp_path / "home"), hub)  # type: ignore[arg-type]
    rules: list[tuple[Any, ...]] = []
    queue.allowlist = SimpleNamespace(  # type: ignore[assignment]
        add=lambda *args, **kwargs: rules.append((args, kwargs)),
        match=lambda *_a, **_k: None, list=lambda *_a, **_k: [])
    session = SimpleNamespace(id="s-1", workdir=str(tmp_path), origin_conn=None,
                              host_tools_from=None, owned_by=None)
    asking = asyncio.ensure_future(queue.request(
        session, "write_file", {"path": "notes.txt"}, tags=("write",), unattended=True,
        timeout_sec=30))
    for _ in range(200):
        if queue.list():
            break
        await asyncio.sleep(0.01)
    conn, _actor = CALLERS[name]
    applied = await queue.respond(queue.list()[0].requestId, "allow", scope, conn=conn)
    decision = await asyncio.wait_for(asking, 5)
    assert decision.allowed
    want = scope if stored else "session"
    # approval.respond's result and approval.resolved both say what applied.
    assert applied is not None and applied.scope == want and decision.scope == want
    resolved = [params for method, params in hub.notes if method == "approval.resolved"][-1]
    assert resolved["scope"] == want
    assert bool(rules) is stored, rules


async def test_a_remote_origins_reply_stores_no_rule_either(tmp_path: Path) -> None:
    """The reply to ``approval.request`` (the session's own surface) is clamped too."""
    hub = _Hub()
    queue = ApprovalQueue(Settings(), Paths.create(tmp_path / "home"), hub)  # type: ignore[arg-type]
    rules: list[Any] = []
    queue.allowlist = SimpleNamespace(  # type: ignore[assignment]
        add=lambda *args, **kwargs: rules.append((args, kwargs)),
        match=lambda *_a, **_k: None, list=lambda *_a, **_k: [])
    origin = _conn("web", remote=True)

    async def call(_method: str, _params: dict[str, Any], timeout: Any = None) -> dict[str, Any]:
        return {"decision": "allow", "scope": "always"}

    origin.call = call
    session = SimpleNamespace(id="s-1", workdir=str(tmp_path), origin_conn=origin,
                              host_tools_from=None, owned_by=None)
    decision = await queue.request(session, "write_file", {"path": "notes.txt"}, tags=("write",))
    assert decision.allowed and decision.scope == "session" and rules == []


# -- Low 2 and Low 3: the sign-in ----------------------------------------------------------------

PASSWORD = "correct horse battery"


async def _remote(paths: Paths) -> tuple[TestClient, RemoteAuth]:
    web_remote.set_password(paths, PASSWORD)
    auth = RemoteAuth("owner", 12, paths)
    auth.failure_delay = 0.0
    core = SimpleNamespace(token="daemon-token", hub=None)
    client = TestClient(TestServer(create_remote_app(RpcDispatcher(core), auth),
                                   host="127.0.0.1"))
    await client.start_server()
    return client, auth


async def test_the_owner_behind_the_same_nat_still_gets_in(tmp_path: Path) -> None:
    """Four wrong attempts queued from one address used to make every further
    attempt from it 429 unchecked — the owner's right password included."""
    client, auth = await _remote(Paths.create(tmp_path / "home"))
    waited: list[float] = []
    release = asyncio.Event()

    async def sleep(seconds: float) -> None:
        waited.append(seconds)
        if seconds < auth.penalty_max:
            await release.wait()  # the queued wrong attempts sit in the queue

    auth.sleep = sleep  # type: ignore[assignment]
    login = lambda password: client.post(  # noqa: E731
        "/web/login", json={"username": "owner", "password": password})
    try:
        for _ in range(MAX_FAILURES_PER_IP):
            assert (await login("nope")).status == 401
        queued = [asyncio.ensure_future(login("nope")) for _ in range(MAX_WAITING_PER_IP)]
        for _ in range(200):
            if auth._waiting and sum(auth._waiting.values()) >= MAX_WAITING_PER_IP:
                break
            await asyncio.sleep(0.01)
        assert sum(auth._waiting.values()) == MAX_WAITING_PER_IP
        # The queue is full.  The owner, behind the same address, signs in.
        answer = await asyncio.wait_for(login(PASSWORD), 10)
        assert answer.status == 200, "a right password was refused unchecked"
        assert waited[-1] == auth.penalty_max  # after the longest wait, not for free
        # ... and a wrong one past the queue is checked and refused, not let in.
        assert (await asyncio.wait_for(login("still wrong"), 10)).status == 429
        release.set()
        assert {(await attempt).status for attempt in queued} == {429}
    finally:
        release.set()
        await client.close()


async def test_an_expired_login_takes_no_further_call(tmp_path: Path) -> None:
    """Its socket used to work until the next sweep (up to 60 s)."""
    import aiohttp

    client, auth = await _remote(Paths.create(tmp_path / "home"))
    now = {"t": 1000.0}
    auth.clock = lambda: now["t"]
    try:
        assert (await client.post("/web/login", json={"username": "owner",
                                                      "password": PASSWORD})).status == 200
        own = f"http://{client.server.host}:{client.server.port}"
        ws = await client.ws_connect("/ws", headers={"Origin": own})
        await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "system.hello", "params": {}})
        first = await ws.receive(timeout=5)
        assert first.type == aiohttp.WSMsgType.TEXT  # the login stands: the frame is answered
        now["t"] += auth.session_sec + 1  # it expires; no sweep runs, nothing else asks
        await ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "session.list", "params": {}})
        message = await ws.receive(timeout=5)
        assert message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
        assert ws.close_code == 4401
    finally:
        await client.close()


# -- Low 5: exception text ---------------------------------------------------------------------


async def test_a_failed_command_tells_every_subscriber_an_id_only(
    core: Core, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from snowpea_core.commands.registry import Command

    async def broken(_ctx: Any, _args: str) -> None:
        raise ValueError("could not read /home/owner/.ssh/id_ed25519: token=abc123")

    core.commands.register(Command(name="broken", summary="x", run=broken))
    session = await core.sessions.create(tmp_path)
    seen: list[tuple[str, dict[str, Any]]] = []

    class Subscriber:
        closed = False

        async def notify(self, method: str, params: dict[str, Any]) -> None:
            if method == "session.event":
                event = params.get("event", params)
                seen.append((str(event.get("kind")), dict(event.get("payload") or {})))

    core.hub.subscribe(Subscriber(), session.id)
    await core.commands.run(core, session, "broken", "")
    await core.commands.run_beside(core, session, "broken", "", None, "c-1")
    everything = str(seen) + " ".join(str(m.content) for m in session.history.snapshot())
    assert "id_ed25519" not in everything and "abc123" not in everything
    assert "ValueError" not in everything
    (error,) = [payload for kind, payload in seen if kind == "error"]
    assert error["code"] == errors.INTERNAL and error["details"]["errorId"] in error["message"]
    (result,) = [payload for kind, payload in seen if kind == "command.result"]
    assert result["ok"] is False and "internal error (id " in result["text"]
    # The detail is in the daemon log, under the id each was given.
    assert "id_ed25519" in caplog.text and error["details"]["errorId"] in caplog.text


@pytest.mark.parametrize("name", sorted(CALLERS))
def test_a_settings_validation_error_quotes_values_to_a_local_person_only(name: str) -> None:
    from pydantic import ValidationError

    from snowpea_core.server.settings_handlers import _invalid

    conn, _actor = CALLERS[name]
    try:
        Settings.model_validate({"providers": {"x": {"api_key": "sk-secret-value"}},
                                 "scheduler": {"tickSec": "sk-secret-value"}})
    except ValidationError as exc:
        error = _invalid(conn, exc)
    assert error.code == errors.INVALID_PARAMS
    if name == "person":
        assert "tickSec" in error.message
        return
    assert "sk-secret-value" not in error.message + str(error.data)
    assert error.data["problems"][0]["field"] == "scheduler.tickSec"


# -- Low 6: a browser's own session, and who names a session's origin ---------------------------


@pytest.mark.parametrize(
    ("host", "surface", "creator", "others", "own"),
    [
        ("b-1", None, None, 0, True),  # hosted by this browser (clientId)
        ("browser-s", None, None, 0, True),  # ... by its surface id
        ("b-other", "browser", None, 0, False),
        ("b-other", "browser", "b-1", 0, True),  # it created it
        (None, "browser", "b-1", 1, True),
        (None, "browser", "b-other", 0, False),  # another browser created it
        (None, "browser", "b-other", 1, False),
        # The alias "browser": the single connected browser host, else the creator.
        ("browser", None, None, 0, True),
        ("browser", None, None, 1, False),
        ("browser", None, "b-1", 1, True),
        ("browser", None, "b-other", 1, False),
        ("browser", None, "b-other", 0, True),  # the only one connected now
        # Opened by "a browser" with no recorded creator (stored before 1.55.0).
        (None, "browser", None, 0, True),
        (None, "browser", None, 1, False),
        (None, "ide", None, 0, False),  # an IDE's session
        (None, None, None, 0, False),
    ],
)
def test_a_browsers_own_session(monkeypatch: pytest.MonkeyPatch, host: str | None,
                                surface: str | None, creator: str | None, others: int,
                                own: bool) -> None:
    from snowpea_core.tools import host_tools

    me = _conn("browser", client_id="b-1")
    connected = [me] + [_conn("browser", client_id=f"b-x{i}") for i in range(others)]
    monkeypatch.setattr(host_tools.HOST_TOOLS, "browser_hosts", lambda: connected)
    assert settings_gate.owns_session(me, host, surface, creator) is own


@pytest.mark.parametrize(
    ("name", "asked", "got"),
    [
        ("person", "tui", "tui"), ("person", "ide", "ide"), ("person", None, "ide-s"),
        ("person", "my-tool", "my-tool"),  # a local person's script: as before
        ("remote_login", "ide", "ide"),  # the web page is the IDE's
        ("remote_login", None, "web-s"),
        ("remote_login", "browser", "web-s"),  # not another kind's
        ("remote_login", "my-tool", "web-s"),
        ("browser", None, "browser"), ("browser", "browser", "browser"),
        ("browser", "ide", "browser"),  # a browser may not claim another origin
        ("browser+actor", "tui", "browser"), ("legacy", "cli", "browser"),
    ],
)
def test_the_server_names_a_sessions_origin(name: str, asked: str | None, got: str) -> None:
    conn, _actor = CALLERS[name]
    assert session_handlers.origin_surface_for(conn, asked) == got


@pytest.mark.parametrize("asked", ["jarvis-task", "jarvis", "scheduler", "skill", "connector",
                                   "gateway:telegram:1", "agent:helper"])
@pytest.mark.parametrize("name", ["person", "remote_login"])
def test_nobody_claims_one_of_the_cores_own_origins(name: str, asked: str) -> None:
    conn, _actor = CALLERS[name]
    with pytest.raises(RpcError) as caught:
        session_handlers.origin_surface_for(conn, asked)
    assert caught.value.code == errors.INVALID_PARAMS
    # A browser's is "browser" whatever it sends, so it cannot either.
    assert session_handlers.origin_surface_for(BROWSER, asked) == "browser"


async def test_the_creator_is_recorded_with_the_session(core: Core, tmp_path: Path) -> None:
    from snowpea_core.server.protocol import SessionCreateParams

    work = tmp_path / "project"
    work.mkdir()
    made = await session_handlers.session_create_handler(
        _session_conn("browser", "b-1"),
        SessionCreateParams(workdir=str(work), originSurface="jarvis-task"), core)
    session = core.sessions.get(made.sessionId)
    assert session.origin_surface == "browser" and session.origin_client_id == "b-1"
    assert not await person_gate.is_task_worker(core, session.id)  # the label was not taken
    row = await core.store.session(session.id)
    assert row["origin_client_id"] == "b-1" and row["origin_surface"] == "browser"
    await core.sessions.close(session.id)
    # ... so "own" holds for a stored session too, and only for that browser.
    for client_id, own in (("b-1", True), ("b-2", False)):
        conn = _session_conn("browser", client_id)
        if own:
            await settings_gate.browser_session_refusal(
                conn, core, session.id, method="checkpoint.restore")
        else:
            with pytest.raises(RpcError):
                await settings_gate.browser_session_refusal(
                    conn, core, session.id, method="checkpoint.restore")


def _session_conn(kind: str, client_id: str) -> Any:
    conn = _conn(kind, client_id=client_id)
    conn.surface_id = f"{client_id}-s"
    return conn


# -- Low 9: fs.write resolves once ---------------------------------------------------------------


async def test_fs_write_writes_the_path_that_was_judged(
    core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control-file rule and the write used to resolve the path separately:
    a directory swapped for a link in between sent the write somewhere the rule
    had not seen."""
    work = tmp_path / "project"
    (work / "notes").mkdir(parents=True)
    (work / ".git" / "hooks").mkdir(parents=True)
    session = await core.sessions.create(work)
    resolved: list[str] = []
    original = fs_handlers.Roots.no_follow

    def no_follow(self: Any, text: str, *, secrets: bool = False) -> Path:
        target = original(self, text, secrets=secrets)
        resolved.append(text)
        if len(resolved) == 1:
            # After the first (and only) resolution: the folder becomes a link
            # to the hooks folder.  A second resolution would follow it.
            (work / "notes").rmdir()
            os.symlink(work / ".git" / "hooks", work / "notes")
        return target

    monkeypatch.setattr(fs_handlers.Roots, "no_follow", no_follow)
    write = fs_handlers._handler(fs_handlers.fs_write, write=True)
    params = FsWriteParams(sessionId=session.id, path="notes/pre-commit", dataBase64="aGk=",
                           mode="create")
    result = await write(REMOTE, params, core)
    assert resolved == ["notes/pre-commit"]  # once
    assert result.path == str(work / "notes" / "pre-commit")  # the judged path, as written


# -- Info ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CALLERS))
def test_audio_transcribe_takes_a_path_only_of_a_recording_or_an_attachment(
    core: Core, tmp_path: Path, name: str
) -> None:
    from snowpea_core.server.audio_handlers import _refuse_foreign_path

    conn, _actor = CALLERS[name]
    recording = Path(core.paths.audio_dir) / "s-1" / "clip.wav"
    attachment = Path(core.paths.attachments_dir) / "s-1" / "voice.ogg"
    elsewhere = tmp_path / "private.key"
    link = Path(core.paths.audio_dir) / "s-1" / "link.wav"
    for path in (recording, attachment, elsewhere):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    link.symlink_to(elsewhere)
    local = name in ("person", "none")
    for path, inside in ((recording, True), (attachment, True), (elsewhere, False),
                         (link, False), (Path(core.paths.home) / "credentials.json", False)):
        if local or inside:
            _refuse_foreign_path(conn, core, str(path))
            continue
        with pytest.raises(RpcError) as caught:
            _refuse_foreign_path(conn, core, str(path))
        assert caught.value.code == errors.OUTSIDE_ROOT, (name, path)
    _refuse_foreign_path(conn, core, None)  # inline data: not a path at all
    assert AudioTranscribeParams(data="aGk=", mime="audio/wav").path is None


def test_the_unregistered_media_configure_handler_is_gone(core: Core) -> None:
    from snowpea_core.server import app_server
    from snowpea_core.server.app_server import build_dispatcher

    assert not hasattr(session_handlers, "provider_configure_handler")
    handler = build_dispatcher(core)._handlers["provider.configure"]
    assert handler is app_server.provider_configure_handler
