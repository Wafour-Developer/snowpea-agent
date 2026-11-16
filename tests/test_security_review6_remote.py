"""Security review 6, part B: the remote web listener (M-1, M-3, L-5).

M-1: a login that ends — by expiry, logout, a token rotation, a password set or
cleared — takes its WebSockets with it, and ending everyone closes every socket
of the listener, listed or not.  M-3: the per-address limit counts an IPv6 /64
as one address, at most two scrypt checks run at once, and a full
everyone-together bucket costs every attempt a growing wait instead of an
unchecked refusal.  L-5: the per-address limit refuses wrong credentials only,
so a right one gets in whoever failed "from its address".
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import web_remote, web_remote_handlers
from snowpea_core.server.protocol import Empty, WebRemoteSetPasswordParams
from snowpea_core.server.rpc import RpcDispatcher
from snowpea_core.server.web_remote import (
    MAX_FAILURES_GLOBAL,
    MAX_FAILURES_PER_IP,
    MAX_PARALLEL_CHECKS,
    RemoteAuth,
    create_remote_app,
    rate_key,
)

PASSWORD = "correct horse battery"


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    paths = Paths.create(tmp_path / "home")
    web_remote.set_password(paths, PASSWORD)
    return paths


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Waits:
    """Stands in for ``asyncio.sleep`` in the limits: records, does not wait."""

    def __init__(self) -> None:
        self.seen: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seen.append(seconds)


async def _remote(paths: Paths, *, trust_proxy: bool = False
                  ) -> tuple[TestClient, RemoteAuth, Clock, Waits]:
    auth = RemoteAuth("owner", 12, paths)
    auth.failure_delay = 0.0
    clock, waits = Clock(), Waits()
    auth.clock = clock
    auth.sleep = waits
    core = SimpleNamespace(token="daemon-token", hub=None)
    app = create_remote_app(RpcDispatcher(core), auth, trust_proxy=trust_proxy)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    return client, auth, clock, waits


async def _login(client: TestClient, password: str = PASSWORD, ip: str | None = None
                 ) -> aiohttp.ClientResponse:
    headers = {"X-Forwarded-For": ip} if ip else {}
    return await client.post(
        "/web/login", json={"username": "owner", "password": password}, headers=headers)


async def _socket(client: TestClient) -> aiohttp.ClientWebSocketResponse:
    own = f"http://{client.server.host}:{client.server.port}"
    return await client.ws_connect("/ws", headers={"Origin": own})


async def _closed(ws: aiohttp.ClientWebSocketResponse) -> None:
    message = await ws.receive(timeout=5)
    assert message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
    assert ws.close_code == 4401


# -- M-1 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("seen_by", ["request", "status", "sweep"])
async def test_an_expired_login_loses_its_socket(paths: Paths, seen_by: str) -> None:
    client, auth, clock, _ = await _remote(paths)
    try:
        assert (await _login(client)).status == 200
        ws = await _socket(client)
        clock.now += auth.session_sec + 1
        if seen_by == "request":  # the old leak: this popped the login and kept the socket
            assert (await client.get("/web/session")).status == 401
        elif seen_by == "status":
            assert auth.active() == 0
        else:
            await auth.sweep()
        await _closed(ws)
        assert auth.sessions == {} and auth.sockets == set()
    finally:
        await client.close()


def _core(paths: Paths, auth: RemoteAuth) -> Any:
    settings = Settings.model_validate({"web": {"remote": {"username": "owner"}}})
    listener = SimpleNamespace(auth=auth, listening=False, error=None, sessions=auth.active,
                               host="127.0.0.1", port=0, username="owner")
    return SimpleNamespace(paths=paths, settings=settings, web_remote=listener, scheduler=None,
                           sessions=SimpleNamespace(all=lambda: []))


@pytest.mark.parametrize("how", ["logout", "rotateToken", "setPassword", "clearPassword"])
async def test_ending_logins_closes_every_socket_of_the_listener(paths: Paths, how: str) -> None:
    client, auth, _, _ = await _remote(paths)
    try:
        assert (await _login(client)).status == 200
        unlisted = await _socket(client)
        # A socket whose login is no longer listed (what an expiry used to leave).
        auth.sessions.clear()
        assert (await _login(client)).status == 200
        listed = await _socket(client)
        assert len(auth.sockets) == 2
        core = _core(paths, auth)
        if how == "logout":
            assert (await client.post("/web/logout")).status == 200
            await _closed(listed)
            await web_remote_handlers._logout_all(core)  # what stop and the handlers call
        elif how == "rotateToken":
            await web_remote_handlers.rotate_token_handler(None, Empty(), core)
        elif how == "setPassword":
            await web_remote_handlers.set_password_handler(
                None, WebRemoteSetPasswordParams(password="another long password"), core)
        else:
            await web_remote_handlers.clear_password_handler(None, Empty(), core)
        await _closed(unlisted)
        if how != "logout":
            await _closed(listed)
        assert auth.sessions == {} and auth.sockets == set()
        assert (await client.get("/web/session")).status == 401
    finally:
        await client.close()


async def test_stopping_the_listener_closes_every_socket(paths: Paths) -> None:
    client, auth, _, _ = await _remote(paths)
    try:
        assert (await _login(client)).status == 200
        ws = await _socket(client)
        auth.sessions.clear()
        listener = web_remote.RemoteListener(RpcDispatcher(SimpleNamespace(hub=None)), paths,
                                             connections=set(), sockets=set())
        listener.auth = auth
        await listener.stop()
        await _closed(ws)
    finally:
        await client.close()


# -- M-3: the address key --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("203.0.113.7", "203.0.113.7", True),
        ("203.0.113.7", "203.0.113.8", False),
        ("2001:db8:1:2::1", "2001:db8:1:2:ffff:ffff:ffff:ffff", True),  # one /64
        ("2001:db8:1:2::1", "2001:db8:1:3::1", False),
        ("2001:DB8:1:2::1", "2001:db8:1:2:0:0:0:9", True),
        ("::ffff:203.0.113.7", "203.0.113.7", True),  # IPv4-mapped
        ("fe80::1%eth0", "fe80::2%eth1", True),
        ("unknown", "unknown", True),
        ("unknown", "other", False),
    ],
)
def test_rate_key(a: str, b: str, same: bool) -> None:
    assert (rate_key(a) == rate_key(b)) is same


async def test_an_ipv6_64_is_one_address(paths: Paths) -> None:
    client, auth, _, _ = await _remote(paths, trust_proxy=True)
    try:
        for index in range(MAX_FAILURES_PER_IP):
            bad = await _login(client, "nope", ip=f"2001:db8:1:2::{index + 1}")
            assert bad.status == 401
        # Another address of the same /64 is over the limit; another /64 is not.
        assert (await _login(client, "nope", ip="2001:db8:1:2:aaaa::1")).status == 429
        assert (await _login(client, "nope", ip="2001:db8:1:3::1")).status == 401
        assert auth.retry_after_ip("2001:db8:1:2:bbbb::9") > 0
    finally:
        await client.close()


# -- M-3: scrypt checks at once ----------------------------------------------------------------


async def test_at_most_two_password_checks_run_at_once(paths: Paths) -> None:
    client, auth, _, _ = await _remote(paths, trust_proxy=True)
    lock = threading.Lock()
    running = {"now": 0, "most": 0, "calls": 0}

    def check(_username: str, _password: str) -> bool:
        with lock:
            running["now"] += 1
            running["calls"] += 1
            running["most"] = max(running["most"], running["now"])
        time.sleep(0.05)
        with lock:
            running["now"] -= 1
        return False

    auth.check = check  # type: ignore[method-assign]
    try:
        answers = await asyncio.gather(
            *(_login(client, "nope", ip=f"198.51.100.{i}") for i in range(10)))
        assert {a.status for a in answers} == {401}
        assert running["calls"] == 10
        assert running["most"] == MAX_PARALLEL_CHECKS == 2
    finally:
        await client.close()


# -- M-3: the everyone-together bucket costs time ----------------------------------------------


async def _fill_global(client: TestClient) -> None:
    for index in range(MAX_FAILURES_GLOBAL):
        ip = f"10.0.{index // 4}.1"  # four each: under the per-address limit
        assert (await _login(client, "nope", ip=ip)).status == 401


async def test_a_full_global_bucket_delays_every_attempt_and_refuses_none_unchecked(
        paths: Paths) -> None:
    client, auth, _, waits = await _remote(paths, trust_proxy=True)
    try:
        await _fill_global(client)
        assert waits.seen == []  # under the limits nothing waits
        # Wrong ones: checked after a growing wait, answered 429.
        for index, expected in enumerate([1.0, 2.0, 4.0]):
            wrong = await _login(client, "nope", ip=f"10.9.{index}.1")
            assert wrong.status == 429 and int(wrong.headers["Retry-After"]) > 0
            assert waits.seen[-1] == expected
        # The right password, from an address never seen: it waits too, and gets in.
        good = await _login(client, ip="10.8.8.8")
        assert good.status == 200 and waits.seen[-1] == 8.0
        assert len(waits.seen) == 4
    finally:
        await client.close()


async def test_the_wait_is_capped(paths: Paths) -> None:
    auth = RemoteAuth("owner", 12, paths)
    for index in range(MAX_FAILURES_GLOBAL + 40):
        auth.record_failure(f"10.1.{index}.1")
    assert auth.penalty("10.2.2.2") == auth.penalty_max == web_remote.PENALTY_MAX_SEC
    assert RemoteAuth("owner", 12, paths).penalty("10.2.2.2") == 0.0


# -- L-5: the per-address limit refuses wrong credentials only ----------------------------------


async def test_the_right_password_gets_in_from_an_address_over_its_limit(paths: Paths) -> None:
    client, auth, _, waits = await _remote(paths)
    try:
        for _ in range(MAX_FAILURES_PER_IP):
            assert (await _login(client, "nope")).status == 401
        wrong = await _login(client, "nope")
        assert wrong.status == 429 and int(wrong.headers["Retry-After"]) == 300
        assert waits.seen == [1.0]  # checked, after the wait
        good = await _login(client)
        assert good.status == 200 and waits.seen == [1.0, 2.0]
        assert auth.active() == 1
    finally:
        await client.close()


async def test_a_claimed_address_cannot_lock_its_person_out(paths: Paths) -> None:
    """trustProxy with the port reachable directly: anyone may claim any address."""
    client, _, _, _ = await _remote(paths, trust_proxy=True)
    try:
        for _ in range(MAX_FAILURES_PER_IP + 3):
            await _login(client, "nope", ip="203.0.113.7")  # the attacker, as the victim
        assert (await _login(client, "nope", ip="203.0.113.7")).status == 429
        assert (await _login(client, ip="203.0.113.7")).status == 200  # the victim
    finally:
        await client.close()


async def test_the_access_link_follows_the_same_rule(paths: Paths) -> None:
    web_remote.clear_password(paths)
    token = web_remote.access_token(paths)
    client, _, _, waits = await _remote(paths)
    try:
        for _ in range(MAX_FAILURES_PER_IP):
            page = await client.get("/web/?t=wrong", allow_redirects=False)
            assert page.status == 401
        assert (await client.get("/web/?t=wrong", allow_redirects=False)).status == 429
        good = await client.get(f"/web/?t={token}", allow_redirects=False)
        assert good.status == 302 and waits.seen == [1.0, 2.0]
    finally:
        await client.close()


async def test_over_its_limit_an_address_is_checked_one_at_a_time(paths: Paths) -> None:
    client, auth, _, _ = await _remote(paths)
    auth.sleep = asyncio.sleep
    auth.penalty_base = 0.01
    auth.penalty_max = 0.05
    lock = threading.Lock()
    running = {"now": 0, "most": 0, "calls": 0}

    def check(_username: str, _password: str) -> bool:
        with lock:
            running["now"] += 1
            running["calls"] += 1
            running["most"] = max(running["most"], running["now"])
        time.sleep(0.02)
        with lock:
            running["now"] -= 1
        return False

    try:
        for _ in range(MAX_FAILURES_PER_IP):
            assert (await _login(client, "nope")).status == 401
        auth.check = check  # type: ignore[method-assign]
        answers = await asyncio.gather(*(_login(client, "nope") for _ in range(12)))
        assert {a.status for a in answers} == {429}
        # The queued ones go one at a time; the ones past the queue are checked
        # too, after the longest wait (security review 7, Low 2: none is answered
        # unchecked any more) — never more than the two check slots at once.
        assert running["most"] <= MAX_PARALLEL_CHECKS
        assert running["calls"] == 12
        assert auth._waiting == {} and auth._turns == {}
    finally:
        await client.close()


async def test_one_address_cannot_fill_the_global_bucket(paths: Paths) -> None:
    client, auth, _, _ = await _remote(paths)
    try:
        for _ in range(MAX_FAILURES_GLOBAL + 10):
            await _login(client, "nope")
        assert auth.retry_after_global() == 0
        assert len(auth._global_failures) == MAX_FAILURES_PER_IP
    finally:
        await client.close()
