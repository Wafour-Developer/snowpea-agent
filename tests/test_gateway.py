"""US-016: the chat gateway — bindings, lazy sessions, adapters (M5 contract §3).

The daemon tests run a real in-process daemon with the scripted provider and
``SNOWPEA_GATEWAY_FAKE=1``, so a "platform" is an in-memory queue.  The adapter
tests drive the real Telegram/Discord/Slack code over a mocked httpx transport:
request building and response parsing are exercised, only the socket is fake.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import aiohttp
import httpx
import pytest
from _support import connect

from snowpea_core.gateway.base import (
    Button,
    approval_callback,
    parse_approval_callback,
)
from snowpea_core.gateway.discord import DiscordAdapter, action_row, parse_event
from snowpea_core.gateway.fake import FakeAdapter
from snowpea_core.gateway.slack import SlackAdapter, parse_envelope
from snowpea_core.gateway.telegram import TelegramAdapter, inline_keyboard, parse_update
from snowpea_core.server.app_server import Daemon

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "gateway.json"
TIMEOUT = 10.0




# ---------------------------------------------------------------------------
# (a) bind -> inbound message -> session -> reply
# ---------------------------------------------------------------------------


async def test_inbound_message_creates_a_gateway_session_and_replies(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        bound = await client.ok(
            "gateway.bind",
            {
                "platform": "telegram",
                "credentialsRef": "tg_test",
                "target": {"new_session": {"workdir": str(workdir), "mode": "accept"}},
                "channelId": "c1",
                "userId": "u1",
            },
        )
        assert bound["bindingId"].startswith("gw-")

        listed = await client.ok("gateway.list", {})
        assert [b["platform"] for b in listed["bindings"]] == ["telegram"]
        assert listed["bindings"][0]["credentialsRef"] == "tg_test"
        assert listed["bindings"][0]["target"] == "new_session"

        adapter = FakeAdapter.instances["tg_test"]
        await adapter.push("지금 작업 중인 레포 이름", channel_id="c1", user_id="u1")

        reply = await adapter.wait_for_send(TIMEOUT)
        assert "snowpea" in reply.text
        assert reply.channel_id == "c1"

        sessions = (await client.ok("session.list", {}))["sessions"]
        assert [s["originSurface"] for s in sessions] == ["gateway:telegram:c1"]
        assert sessions[0]["workdir"] == str(workdir)

        # The lifecycle keepalive counts the binding (plan §2.6).
        assert (await client.ok("system.info", {}))["counters"]["gateway_bindings"] == 1

        await client.ok("gateway.unbind", {"bindingId": bound["bindingId"]})
        assert (await client.ok("gateway.list", {}))["bindings"] == []
        assert adapter.stopped is True
        await client.stop()
    finally:
        await daemon.stop()


async def test_a_second_message_reuses_the_same_session(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        await client.ok(
            "gateway.bind",
            {
                "platform": "telegram",
                "credentialsRef": "tg_test",
                "target": {"new_session": {"workdir": str(workdir), "mode": "accept"}},
            },
        )
        adapter = FakeAdapter.instances["tg_test"]
        await adapter.push("지금 작업 중인 레포 이름", channel_id="c1")
        await adapter.wait_for_send(TIMEOUT)
        await adapter.push("한 번 더", channel_id="c1")
        await adapter.wait_for_send(TIMEOUT, count=2)

        sessions = (await client.ok("session.list", {}))["sessions"]
        assert len(sessions) == 1

        # A different chat gets its own session.
        await adapter.push("다른 방", channel_id="c2")
        await adapter.wait_for_send(TIMEOUT, count=3)
        surfaces = {s["originSurface"] for s in (await client.ok("session.list", {}))["sessions"]}
        assert surfaces == {"gateway:telegram:c1", "gateway:telegram:c2"}
        await client.stop()
    finally:
        await daemon.stop()


async def test_a_binding_from_an_unbound_channel_is_ignored(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        await client.ok(
            "gateway.bind",
            {
                "platform": "telegram",
                "credentialsRef": "tg_test",
                "target": {"new_session": {"workdir": str(workdir), "mode": "accept"}},
                "channelId": "c1",
            },
        )
        adapter = FakeAdapter.instances["tg_test"]
        await adapter.push("from somewhere else", channel_id="c9")
        assert (await client.ok("session.list", {}))["sessions"] == []
        assert adapter.sent == []
        await client.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (b) AC-17: bindings survive a restart
# ---------------------------------------------------------------------------


async def test_bindings_are_restored_after_a_restart(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    home = tmp_path / "home"

    first = Daemon(port=0, home=home)
    await first.start()
    try:
        client = await connect(http, first)
        created = await client.ok(
            "gateway.bind",
            {
                "platform": "telegram",
                "credentialsRef": "tg_test",
                "target": {"new_session": {"workdir": str(workdir), "mode": "accept"}},
                "channelId": "c1",
                "userId": "u1",
            },
        )
        await client.stop()
    finally:
        await first.stop()

    second = Daemon(port=0, home=home)
    await second.start()
    try:
        client = await connect(http, second)
        listed = (await client.ok("gateway.list", {}))["bindings"]
        assert [b["bindingId"] for b in listed] == [created["bindingId"]]
        assert listed[0]["channelId"] == "c1"
        assert listed[0]["userId"] == "u1"
        assert listed[0]["state"] == "active"

        # The restored binding is live, not just a row.
        adapter = FakeAdapter.instances["tg_test"]
        await adapter.push("지금 작업 중인 레포 이름", channel_id="c1", user_id="u1")
        assert "snowpea" in (await adapter.wait_for_send(TIMEOUT)).text
        await client.stop()
    finally:
        await second.stop()


# ---------------------------------------------------------------------------
# (c) credentials never leave the home directory
# ---------------------------------------------------------------------------


async def test_credentials_file_is_private_and_resolves_refs(tmp_path: Path) -> None:
    from snowpea_core.config.credentials import CredentialError, CredentialStore
    from snowpea_core.config.paths import Paths

    paths = Paths.create(tmp_path / "home")
    store = CredentialStore(paths)
    store.set("tg_main", "123:ABC")
    store.set("slack_work", {"token": "xoxb-1", "app_token": "xapp-1"})

    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.resolve("tg_main") == "123:ABC"
    assert store.resolve_tokens("slack_work")["app_token"] == "xapp-1"
    assert store.names() == ["slack_work", "tg_main"]

    os.environ["SNOWPEA_TEST_TOKEN"] = "from-env"
    try:
        assert store.resolve_tokens("SNOWPEA_TEST_TOKEN") == {"token": "from-env"}
    finally:
        os.environ.pop("SNOWPEA_TEST_TOKEN", None)

    with pytest.raises(CredentialError) as caught:
        store.resolve("nope")
    assert "123:ABC" not in str(caught.value)


# ---------------------------------------------------------------------------
# (d) callback encoding
# ---------------------------------------------------------------------------


def test_approval_callback_round_trips() -> None:
    assert approval_callback("ap-1", "allow") == "apr:ap-1:allow"
    assert parse_approval_callback("apr:ap-1:deny") == ("ap-1", "deny")
    assert parse_approval_callback("apr:ap-1:maybe") is None
    assert parse_approval_callback("something:else") is None
    assert parse_approval_callback(None) is None


# ---------------------------------------------------------------------------
# (e) Telegram adapter over a mocked transport
# ---------------------------------------------------------------------------


async def test_telegram_send_builds_a_bot_api_call() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TelegramAdapter("123:ABC", client=client)
    message_id = await adapter.send(
        "555", "please decide", buttons=[Button("ok", "apr:ap-1:allow")]
    )
    await adapter.stop()

    assert message_id == "77"
    assert seen[0].url.path == "/bot123:ABC/sendMessage"
    body = json.loads(seen[0].content)
    assert body["chat_id"] == "555"
    assert body["reply_markup"] == inline_keyboard([Button("ok", "apr:ap-1:allow")])
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "apr:ap-1:allow"


async def test_telegram_poll_parses_a_message_and_a_button_press() -> None:
    updates = [
        {
            "update_id": 10,
            "message": {
                "message_id": 1,
                "chat": {"id": -100},
                "from": {"id": 42},
                "text": "안녕",
            },
        },
        {
            "update_id": 11,
            "callback_query": {
                "id": "cbq-1",
                "from": {"id": 42},
                "data": "apr:ap-9:deny",
                "message": {"message_id": 2, "chat": {"id": -100}},
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": updates})
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TelegramAdapter("123:ABC", client=client)
    received: list[Any] = []

    async def on_message(message: Any) -> None:
        received.append(message)

    assert await adapter.poll_once(on_message) == 2
    await adapter.stop()

    assert received[0].platform == "telegram"
    assert received[0].channel_id == "-100"
    assert received[0].user_id == "42"
    assert received[0].text == "안녕"
    assert parse_approval_callback(received[1].callback_data) == ("ap-9", "deny")
    assert received[1].callback_id == "cbq-1"
    # An edit with no text and no attachment is not something to answer.
    empty = {"update_id": 12, "message": {"chat": {"id": 1}, "from": {"id": 2}}}
    assert parse_update(empty) is None


# ---------------------------------------------------------------------------
# (f) Discord adapter
# ---------------------------------------------------------------------------


async def test_discord_send_posts_a_message_with_an_action_row() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "9001"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DiscordAdapter("bot-token", client=client)
    message_id = await adapter.send("chan-1", "decide", buttons=[Button("ok", "apr:ap-2:allow")])
    await adapter.stop()

    assert message_id == "9001"
    assert seen[0].url.path == "/api/v10/channels/chan-1/messages"
    assert seen[0].headers["authorization"] == "Bot bot-token"
    body = json.loads(seen[0].content)
    assert body["components"] == action_row([Button("ok", "apr:ap-2:allow")])
    assert body["components"][0]["components"][0]["custom_id"] == "apr:ap-2:allow"


def test_discord_parses_messages_and_interactions() -> None:
    message = parse_event(
        {
            "op": 0,
            "t": "MESSAGE_CREATE",
            "d": {
                "id": "5",
                "channel_id": "chan-1",
                "content": "hello there",
                "author": {"id": "user-1"},
            },
        }
    )
    assert message is not None
    assert (message.channel_id, message.user_id, message.text) == (
        "chan-1",
        "user-1",
        "hello there",
    )

    press = parse_event(
        {
            "op": 0,
            "t": "INTERACTION_CREATE",
            "d": {
                "id": "int-1",
                "token": "tok",
                "channel_id": "chan-1",
                "member": {"user": {"id": "user-1"}},
                "data": {"custom_id": "apr:ap-3:allow"},
            },
        }
    )
    assert press is not None
    assert parse_approval_callback(press.callback_data) == ("ap-3", "allow")
    # The bot's own messages are not prompts.
    assert (
        parse_event(
            {
                "op": 0,
                "t": "MESSAGE_CREATE",
                "d": {"channel_id": "c", "author": {"id": "me", "bot": True}, "content": "hi"},
            }
        )
        is None
    )


# ---------------------------------------------------------------------------
# (g) Slack adapter
# ---------------------------------------------------------------------------


async def test_slack_send_posts_blocks_with_button_values() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "ts": "1700.5"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SlackAdapter("xoxb-1", app_token="xapp-1", client=client)
    ts = await adapter.send("C1", "decide", buttons=[Button("ok", "apr:ap-4:allow")])
    await adapter.stop()

    assert ts == "1700.5"
    assert seen[0].url.path == "/api/chat.postMessage"
    assert seen[0].headers["authorization"] == "Bearer xoxb-1"
    body = json.loads(seen[0].content)
    assert body["blocks"][1]["elements"][0]["value"] == "apr:ap-4:allow"


async def test_slack_rejects_a_not_ok_response() -> None:
    from snowpea_core.gateway.base import GatewayError

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        )
    )
    adapter = SlackAdapter("xoxb-1", client=client)
    with pytest.raises(GatewayError) as caught:
        await adapter.send("C1", "hi")
    await adapter.stop()
    assert "invalid_auth" in str(caught.value)
    assert "xoxb-1" not in str(caught.value)


def test_slack_parses_events_and_interactive_presses() -> None:
    message = parse_envelope(
        {
            "type": "events_api",
            "envelope_id": "env-1",
            "payload": {"event": {"type": "message", "channel": "C1", "user": "U1", "text": "hi"}},
        }
    )
    assert message is not None
    assert (message.channel_id, message.user_id, message.text) == ("C1", "U1", "hi")

    press = parse_envelope(
        {
            "type": "interactive",
            "envelope_id": "env-2",
            "payload": {
                "channel": {"id": "C1"},
                "user": {"id": "U1"},
                "actions": [{"value": "apr:ap-5:deny"}],
            },
        }
    )
    assert press is not None
    assert parse_approval_callback(press.callback_data) == ("ap-5", "deny")
    # The bot's own posts are not prompts.
    assert (
        parse_envelope(
            {
                "type": "events_api",
                "payload": {"event": {"type": "message", "channel": "C1", "bot_id": "B1"}},
            }
        )
        is None
    )


# ---------------------------------------------------------------------------
# (h) CLI target parsing
# ---------------------------------------------------------------------------


def test_cli_parses_every_target_spelling(tmp_path: Path) -> None:
    from snowpea_core.cli.commands import parse_target

    assert parse_target("agent:ops") == {"agent": "ops"}
    assert parse_target("session:s-1") == {"session": "s-1"}
    assert parse_target(f"new:{tmp_path}") == {"new_session": {"workdir": str(tmp_path)}}
    assert parse_target('{"agent": "ops"}') == {"agent": "ops"}
    with pytest.raises(ValueError):
        parse_target("nonsense")
