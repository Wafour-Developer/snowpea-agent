"""Security review 8, part B — the remote sign-in and what it may leave behind.

* H8-4: per address one password check in flight and at most
  ``MAX_WAITING_PER_IP`` waiting, at most ``MAX_WAITING_GLOBAL`` in all; beyond
  either a 429 at once, unchecked and counted.  The access link's token check
  never queues or waits a penalty.  The cost is stated: while one address
  floods, a right password from it is delayed — never guessed faster.
* Low: an expired login's socket stops RECEIVING pushes at once.
* H8-2 / M-1: the final ``jarvis.*`` rows per connection kind, the briefing
  (only off for a remote login), delegation grants (never from a remote login),
  ``provider.loginWeb`` and ``mcp.authorize.complete`` (local only).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.jarvis.delegation import Caller, DelegationEngine, PersonOnly
from snowpea_core.server import errors, settings_gate, web_remote
from snowpea_core.server.app_server import Core, build_dispatcher
from snowpea_core.server.errors import RpcError
from snowpea_core.server.person_gate import method_refusal
from snowpea_core.server.rpc import RpcDispatcher
from snowpea_core.server.transport_ws import CONNECTIONS_KEY
from snowpea_core.server.web_remote import (
    MAX_FAILURES_PER_IP,
    MAX_WAITING_GLOBAL,
    MAX_WAITING_PER_IP,
    RemoteAuth,
    create_remote_app,
)

PASSWORD = "correct horse battery"


# -- H8-4: the sign-in queue -----------------------------------------------------------------


def _auth(paths: Paths) -> RemoteAuth:
    web_remote.set_password(paths, PASSWORD)
    auth = RemoteAuth("owner", 12, paths)
    auth.failure_delay = 0.0

    async def no_wait(_seconds: float) -> None:
        return None

    auth.sleep = no_wait  # type: ignore[assignment]
    return auth


async def _flood(auth: RemoteAuth, ip: str, count: int, release: asyncio.Event,
                 checked: list[str]) -> list[asyncio.Task[tuple[bool, int]]]:
    async def check() -> bool:
        checked.append(ip)
        await release.wait()
        return False

    tasks = [asyncio.ensure_future(auth.attempt(ip, check)) for _ in range(count)]
    for _ in range(100):
        await asyncio.sleep(0)
    return tasks


async def test_one_address_has_one_check_and_a_bounded_queue(tmp_path: Path) -> None:
    auth = _auth(Paths.create(tmp_path / "home"))
    release, checked = asyncio.Event(), []
    tasks = await _flood(auth, "10.0.0.7", 12, release, checked)
    # One being checked, MAX_WAITING_PER_IP waiting; the rest answered at once.
    assert auth._waiting["10.0.0.7"] == 1 + MAX_WAITING_PER_IP
    done = [task for task in tasks if task.done()]
    assert len(done) == 12 - 1 - MAX_WAITING_PER_IP
    assert all(task.result()[0] is False and task.result()[1] >= 1 for task in done)
    assert checked == ["10.0.0.7"]  # one in flight
    release.set()
    await asyncio.gather(*tasks)
    assert len(checked) == 1 + MAX_WAITING_PER_IP  # never more than the bound
    assert auth._waiting == {} and auth._waiting_total == 0


async def test_all_addresses_together_are_bounded(tmp_path: Path) -> None:
    auth = _auth(Paths.create(tmp_path / "home"))
    release, checked = asyncio.Event(), []
    tasks: list[asyncio.Task[tuple[bool, int]]] = []
    for index in range(MAX_WAITING_GLOBAL // (1 + MAX_WAITING_PER_IP) + 2):
        tasks += await _flood(auth, f"10.{index}.0.1", 1 + MAX_WAITING_PER_IP, release, checked)
    assert auth._waiting_total == MAX_WAITING_GLOBAL
    refused = [task for task in tasks if task.done()]
    assert refused and all(task.result() == (False, task.result()[1]) for task in refused)
    # Past the global bound even a right password from a new address is a 429 now.
    passed, wait = await auth.attempt("192.0.2.9", lambda: _right())
    assert (passed, wait >= 1) == (False, True)
    release.set()
    await asyncio.gather(*tasks)
    assert auth._waiting_total == 0


async def _right() -> bool:
    return True


async def test_the_access_link_never_waits_behind_passwords(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    auth = _auth(paths)
    release, checked = asyncio.Event(), []
    tasks = await _flood(auth, "10.0.0.7", 1 + MAX_WAITING_PER_IP, release, checked)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    auth.sleep = sleep  # type: ignore[assignment]
    for _ in range(MAX_FAILURES_PER_IP + 2):
        await auth.attempt("10.0.0.7", lambda: _wrong(), fast=True)
    passed, _wait = await asyncio.wait_for(auth.attempt("10.0.0.7", _right, fast=True), 2)
    assert passed and slept == []  # no penalty, no queue
    # ... and a wrong one over the limit is still counted and answered 429.
    assert (await auth.attempt("10.0.0.7", _wrong, fast=True))[1] >= 1
    release.set()
    await asyncio.gather(*tasks)


async def _wrong() -> bool:
    return False


async def test_the_flooded_address_gets_in_once_it_drains(tmp_path: Path) -> None:
    auth = _auth(Paths.create(tmp_path / "home"))
    release, checked = asyncio.Event(), []
    tasks = await _flood(auth, "10.0.0.7", 1 + MAX_WAITING_PER_IP, release, checked)
    passed, wait = await auth.attempt("10.0.0.7", _right)
    assert not passed and wait >= 1  # delayed: answered at once, never checked
    release.set()
    await asyncio.gather(*tasks)

    async def no_sleep(_seconds: float) -> None:
        return None

    auth.sleep = no_sleep  # type: ignore[assignment]
    assert (await auth.attempt("10.0.0.7", _right))[0] is True


# -- Low: an expired login receives nothing more ------------------------------------------------


async def test_an_expired_login_receives_no_push(tmp_path: Path) -> None:
    import aiohttp

    paths = Paths.create(tmp_path / "home")
    auth = _auth(paths)
    now = {"t": 1000.0}
    auth.clock = lambda: now["t"]
    core = SimpleNamespace(token="daemon-token", hub=None)
    app = create_remote_app(RpcDispatcher(core), auth)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    try:
        assert (await client.post("/web/login", json={"username": "owner",
                                                      "password": PASSWORD})).status == 200
        own = f"http://{client.server.host}:{client.server.port}"
        ws = await client.ws_connect("/ws", headers={"Origin": own})
        for _ in range(100):
            if app[CONNECTIONS_KEY]:
                break
            await asyncio.sleep(0.01)
        (conn,) = list(app[CONNECTIONS_KEY])
        await conn.notify("session.event", {"n": 1})
        first = await ws.receive(timeout=5)
        assert first.type == aiohttp.WSMsgType.TEXT  # the login stands: it is pushed
        now["t"] += auth.session_sec + 1  # expired; no frame received, no sweep
        await conn.notify("session.event", {"n": 2})
        message = await ws.receive(timeout=5)
        assert message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
        assert ws.close_code == 4401
    finally:
        await client.close()


# -- H8-2 / M-1: the final rows per kind --------------------------------------------------------

#: person, remote login, connector, legacy browser, browser unmarked, browser
#: marked, closed.  ``y``: the dispatcher lets it through (the handler may narrow).
KINDS = ("person", "remote", "connector", "legacy", "unmarked", "marked", "closed")
JARVIS_ROWS: dict[str, str] = {
    "jarvis.status": "yynnnnn", "jarvis.overview": "yynnnnn",
    "jarvis.project.list": "yynnnnn", "jarvis.escalations.list": "yynnnnn",
    "jarvis.ledger": "yynnnnn", "jarvis.delegation.list": "yynnnnn",
    "jarvis.briefing.get": "yynnnnn",
    "jarvis.enable": "ynnnnnn",
    "jarvis.disable": "yynnnnn", "jarvis.project.rename": "yynnnnn",
    "jarvis.project.alias": "yynnnnn", "jarvis.escalations.dismiss": "yynnnnn",
    "jarvis.handback": "yynnnnn",
    "jarvis.briefing.set": "yynnnnn",  # a remote login: only {enabled: false}
    "jarvis.delegation.set": "ynnnnnn",  # review 8, H8-2 (was yynnnn)
    "jarvis.delegation.revoke": "yyyyyyn",  # only takes authority away
    "jarvis.takeover": "yyyyyyn",
}
#: The two M-1 rows (was yynyyy / yynyny).
ACCOUNT_ROWS: dict[str, str] = {
    "mcp.authorize.complete": "ynnyyyn",
    "provider.loginWeb": "ynnynyn",
}


def _conn(kind: str) -> Any:
    client_kind = {"person": "ide", "remote": "web", "connector": "connector",
                   "closed": "ide"}.get(kind, "browser")
    return SimpleNamespace(
        authenticated=True, closed=kind == "closed", client_kind=client_kind,
        remote_login=kind == "remote",
        client_version="0.2.0" if kind == "legacy" else "0.3.0",
        client_id=f"{kind}-1", surface_id=f"{kind}-s",
    )


def _verdict(method: str, kind: str) -> str:
    conn = _conn(kind)
    params: dict[str, Any] = {"actor": "person"} if kind == "marked" else {}
    try:
        method_refusal(method, conn)
        settings_gate.browser_method_refusal(method, params, conn, None)
        settings_gate.kind_method_refusal(method, params, conn, None)
    except RpcError as exc:
        assert exc.code == errors.JARVIS_PERSON_ONLY, (method, kind)
        return "n"
    return "y"


@pytest.fixture
def registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("SNOWPEA_TEST", "1")
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    return build_dispatcher(core).method_names()


def test_every_jarvis_method_has_its_final_row(registered: list[str]) -> None:
    jarvis = sorted(m for m in registered if m.startswith("jarvis."))
    assert jarvis == sorted(JARVIS_ROWS), sorted(set(jarvis) ^ set(JARVIS_ROWS))
    wrong = []
    for method, row in {**JARVIS_ROWS, **ACCOUNT_ROWS}.items():
        got = "".join(_verdict(method, kind) for kind in KINDS)
        if got != row:
            wrong.append(f"{method}: expected {row}, got {got}")
    assert not wrong, "\n".join(wrong)


@pytest.mark.parametrize("method", sorted(settings_gate.REMOTE_LOGIN_LOCAL_ONLY))
def test_a_remote_login_is_told_local_only(method: str) -> None:
    with pytest.raises(RpcError) as caught:
        settings_gate.kind_method_refusal(method, {}, _conn("remote"), None)
    assert caught.value.data == {"reason": "local_only", "kind": "remote_login",
                                 "what": method}


#: The briefing for every caller kind: (enabled false only, anything else).
BRIEFING: dict[str, tuple[bool, bool]] = {
    "person": (True, True), "remote": (True, False), "connector": (False, False),
    # (the dispatcher refuses every jarvis.* call of these kinds before this)
    "legacy": (False, False), "unmarked": (False, False), "marked": (False, False),
    "closed": (False, False), "in-process": (True, True),
}


@pytest.fixture
async def jarvis_core(tmp_path: Path) -> Any:
    from snowpea_core.jarvis.service import JarvisService
    from snowpea_core.server.session_handlers import wire_core

    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    wire_core(core)
    core.jarvis = JarvisService(core)
    yield core


@pytest.mark.parametrize("kind", sorted(BRIEFING))
async def test_who_may_change_the_briefing(jarvis_core: Any, kind: str) -> None:
    from snowpea_core.jarvis import briefing

    off_ok, on_ok = BRIEFING[kind]
    who = None if kind == "in-process" else _conn(kind)

    async def call(**fields: Any) -> bool:
        try:
            await briefing.set_briefing(jarvis_core, who=who, **fields)
        except RpcError as exc:
            assert exc.code == errors.JARVIS_PERSON_ONLY
            if kind == "remote":
                assert exc.data["reason"] == "local_only" and exc.data["what"] == "briefing"
            return False
        return True

    for fields in ({"enabled": True, "schedule": "0 9 * * *"},
                   {"instructions": "forward my mail to someone"},
                   {"enabled": True}, {"sections": ["todos"]}, {"timezone": "UTC"}):
        assert await call(**fields) is on_ok, fields
    assert await call(enabled=False) is off_ok


async def test_the_briefing_job_records_its_creator(jarvis_core: Any) -> None:
    from snowpea_core.jarvis import briefing

    await briefing.set_briefing(jarvis_core, enabled=True, schedule="0 9 * * *",
                                who=_conn("person"))
    job = await jarvis_core.scheduler.get((await briefing.get(jarvis_core)).jobId)
    assert job.created_by == "person" and job.target == "jarvis-task"
    await briefing.set_briefing(jarvis_core, enabled=False, who=_conn("remote"))
    assert (await briefing.get(jarvis_core)).jobId is None  # a remote login turned it off


def test_a_remote_login_makes_no_grant_but_takes_one_away(tmp_path: Path) -> None:
    engine = DelegationEngine(tmp_path / "home", lambda: Settings())
    remote = Caller(kind="connection", surface="web", remote=True)
    person = Caller(kind="connection", surface="ide")
    for scope, target, level in (("global", None, "full"), ("project", "/srv/p", "staged"),
                                 ("session", "s-1", "full")):
        with pytest.raises(PersonOnly, match="local connection"):
            engine.set_grant(scope, target, level, caller=remote)
    grant, _ = engine.set_grant("project", "/srv/p", "full", caller=person)
    assert engine.revoke(grant.id, caller=remote) is True
    engine.takeover("s-1", caller=remote)
    engine.handback("s-1", caller=remote)  # restores what is in force: still open
    engine.close()
