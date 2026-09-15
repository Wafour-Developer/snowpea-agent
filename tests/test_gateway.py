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
    GatewayError,
    approval_callback,
    parse_approval_callback,
)
from snowpea_core.gateway.chat import MENU_COMMANDS
from snowpea_core.gateway.discord import (
    OPTION_DESCRIPTION,
    DiscordAdapter,
    action_row,
    menu_payload,
    parse_event,
)
from snowpea_core.gateway.fake import FakeAdapter
from snowpea_core.gateway.router import Binding, GatewayConnection
from snowpea_core.gateway.slack import SlackAdapter, parse_envelope, slack_manifest
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


async def test_only_the_answer_is_forwarded_to_the_chat() -> None:
    """``message.user`` is for a resumed transcript, never for the channel.

    The person in the chat wrote the prompt themselves; forwarding the event
    would post every message they sent straight back at them.
    """
    sent: list[str] = []

    class _Router:
        async def send(
            self, _binding: Any, _channel: str, text: str, buttons: Any = None
        ) -> str:
            sent.append(text)
            return "m1"

    binding = Binding(id="gw-1", platform="fake", credentials_ref="f", channel_id="c1")
    conn = GatewayConnection(_Router(), binding, "c1")  # type: ignore[arg-type]
    conn.session_id = "s-1"

    def event(kind: str, **payload: Any) -> dict[str, Any]:
        return {"sessionId": "s-1", "seq": 1, "kind": kind, "payload": payload}

    await conn.notify("session.event", event("message.user", text="무엇을 하고 있어?"))
    assert sent == []
    await conn.notify("session.event", event("message.delta", text="half"))
    assert sent == []
    await conn.notify("session.event", event("message.done", text="the answer"))
    assert sent == ["the answer"]


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


class _Frame:
    """One text frame off a fake socket (Discord's gateway, Slack's Socket Mode)."""

    type = aiohttp.WSMsgType.TEXT

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _Socket:
    """Just enough of an aiohttp websocket for either adapter's ``_consume``."""

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._frames = frames
        self.sent: list[dict[str, Any]] = []

    def __aiter__(self) -> Any:
        async def frames() -> Any:
            for frame in self._frames:
                yield _Frame(frame)

        return frames()

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


class _DeadSession:
    """A client session whose socket never opens, so ``start`` returns fast."""

    async def ws_connect(self, url: str) -> Any:
        raise RuntimeError("no socket in a test")

    async def close(self) -> None:
        return None


def _discord_routes(seen: list[httpx.Request]) -> Any:
    """MockTransport handler covering every Discord call the adapter makes."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/v10/oauth2/applications/@me":
            return httpx.Response(200, json={"id": "app-1"})
        if path == "/api/v10/applications/app-1/commands":
            return httpx.Response(200, json=[])
        if path.endswith("/callback"):
            return httpx.Response(204)
        if path == "/api/v10/webhooks/app-1/tok-7/messages/@original":
            return httpx.Response(200, json={"id": "m-original"})
        return httpx.Response(200, json={"id": "m-channel"})

    return handler


COMMAND_FRAME = {
    "op": 0,
    "t": "INTERACTION_CREATE",
    "d": {
        "id": "int-7",
        "token": "tok-7",
        "channel_id": "chan-1",
        "member": {"user": {"id": "user-9"}},
        "data": {"type": 2, "name": "new", "options": [{"name": "args", "value": "foo"}]},
    },
}


async def test_discord_start_registers_the_command_menu() -> None:
    seen: list[httpx.Request] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(_discord_routes(seen)))
    adapter = DiscordAdapter("bot-token", client=client, session=_DeadSession())

    async def on_message(message: Any) -> None:  # pragma: no cover - never called
        raise AssertionError("no inbound traffic in this test")

    await adapter.start(on_message)
    await adapter.stop()

    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/api/v10/oauth2/applications/@me"),
        ("GET", "/api/v10/applications/app-1/commands"),
        ("PUT", "/api/v10/applications/app-1/commands"),
    ]
    body = json.loads(seen[2].content)
    assert [item["name"] for item in body] == [name for name, _ in MENU_COMMANDS]
    for item in body:
        assert item["type"] == 1
        assert len(item["description"]) <= 100
        assert item["options"] == [
            {
                "type": 3,
                "name": "args",
                "description": OPTION_DESCRIPTION,
                "required": False,
            }
        ]


async def test_discord_skips_the_put_when_the_menu_is_already_right() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v10/oauth2/applications/@me":
            return httpx.Response(200, json={"id": "app-1"})
        return httpx.Response(200, json=menu_payload())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DiscordAdapter("bot-token", client=client)
    await adapter.register_commands()
    await adapter.stop()

    assert [r.method for r in seen] == ["GET", "GET"]


async def test_discord_command_registration_failure_only_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "nope"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DiscordAdapter("bot-token", client=client)
    with caplog.at_level("WARNING"):
        await adapter.register_commands()
    await adapter.stop()

    assert "could not register the discord command menu" in caplog.text
    assert "bot-token" not in caplog.text


def test_discord_parses_a_slash_command_interaction() -> None:
    message = parse_event(COMMAND_FRAME)
    assert message is not None
    assert (message.channel_id, message.user_id, message.text) == ("chan-1", "user-9", "/new foo")
    assert message.callback_id == "int-7:tok-7"
    assert message.callback_data is None

    bare = parse_event(
        {
            "op": 0,
            "t": "INTERACTION_CREATE",
            "d": {
                "id": "int-8",
                "token": "tok-8",
                "channel_id": "chan-1",
                "user": {"id": "user-9"},
                "data": {"type": 2, "name": "sessions"},
            },
        }
    )
    assert bare is not None
    assert bare.text == "/sessions"


async def test_discord_answers_a_command_in_the_interaction() -> None:
    seen: list[httpx.Request] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(_discord_routes(seen)))
    adapter = DiscordAdapter("bot-token", client=client)
    received: list[Any] = []

    async def on_message(message: Any) -> None:
        received.append(message)

    await adapter._consume(_Socket([COMMAND_FRAME]), on_message)
    first = await adapter.send("chan-1", "one", buttons=[Button("ok", "apr:ap-5:allow")])
    second = await adapter.send("chan-1", "two")
    await adapter.stop()

    assert [m.text for m in received] == ["/new foo"]
    # The deferred callback comes before the handler ever runs.
    assert (seen[0].method, seen[0].url.path) == ("GET", "/api/v10/oauth2/applications/@me")
    assert seen[1].url.path == "/api/v10/interactions/int-7/tok-7/callback"
    assert json.loads(seen[1].content) == {"type": 5}
    # The first answer edits the deferred reply; the second is a plain post.
    assert (seen[2].method, seen[2].url.path) == (
        "PATCH",
        "/api/v10/webhooks/app-1/tok-7/messages/@original",
    )
    patched = json.loads(seen[2].content)
    assert patched["content"] == "one"
    assert patched["components"] == action_row([Button("ok", "apr:ap-5:allow")])
    assert (seen[3].method, seen[3].url.path) == ("POST", "/api/v10/channels/chan-1/messages")
    assert (first, second) == ("m-original", "m-channel")


async def test_discord_button_presses_are_untouched_by_the_command_path() -> None:
    seen: list[httpx.Request] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(_discord_routes(seen)))
    adapter = DiscordAdapter("bot-token", client=client)
    received: list[Any] = []

    async def on_message(message: Any) -> None:
        received.append(message)

    press = {
        "op": 0,
        "t": "INTERACTION_CREATE",
        "d": {
            "id": "int-9",
            "token": "tok-9",
            "channel_id": "chan-1",
            "member": {"user": {"id": "user-9"}},
            "data": {"type": 3, "custom_id": "apr:ap-6:allow"},
        },
    }
    await adapter._consume(_Socket([press]), on_message)
    message_id = await adapter.send("chan-1", "posted")
    await adapter.stop()

    assert parse_approval_callback(received[0].callback_data) == ("ap-6", "allow")
    # No deferral, so the answer goes to the channel as it always did.
    assert [r.url.path for r in seen] == ["/api/v10/channels/chan-1/messages"]
    assert message_id == "m-channel"


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


SLASH_ENVELOPE: dict[str, Any] = {
    "type": "slash_commands",
    "envelope_id": "env-3",
    "payload": {
        "command": "/new",
        "text": "foo",
        "channel_id": "C1",
        "user_id": "U1",
        "response_url": "https://hooks.slack.test/commands/1",
        "trigger_id": "trig-1",
    },
}


def test_slack_parses_a_slash_command_envelope() -> None:
    message = parse_envelope(SLASH_ENVELOPE)
    assert message is not None
    assert (message.channel_id, message.user_id, message.text) == ("C1", "U1", "/new foo")
    assert message.callback_id == "env-3"
    assert message.callback_data is None

    bare = parse_envelope(
        {
            "type": "slash_commands",
            "envelope_id": "env-4",
            "payload": {"command": "/sessions", "text": "", "channel_id": "C1", "user_id": "U1"},
        }
    )
    assert bare is not None
    assert bare.text == "/sessions"


async def test_slack_answers_a_slash_command_on_its_response_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "hooks.slack.test":
            return httpx.Response(200, text="ok")
        return httpx.Response(200, json={"ok": True, "ts": "1700.9"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SlackAdapter("xoxb-1", app_token="xapp-1", client=client)
    socket = _Socket([SLASH_ENVELOPE])
    received: list[Any] = []

    async def on_message(message: Any) -> None:
        received.append(message)

    await adapter._consume(socket, on_message)
    first = await adapter.send("C1", "one", buttons=[Button("ok", "apr:ap-7:allow")])
    second = await adapter.send("C1", "two")
    await adapter.stop()

    assert [m.text for m in received] == ["/new foo"]
    # Socket Mode wants the envelope acked, and the ack carries no reply text.
    assert socket.sent == [{"envelope_id": "env-3"}]
    # The first answer goes to the response_url, the second to the channel.
    assert str(seen[0].url) == "https://hooks.slack.test/commands/1"
    body = json.loads(seen[0].content)
    assert body["response_type"] == "in_channel"
    assert body["text"] == "one"
    assert body["blocks"][1]["elements"][0]["value"] == "apr:ap-7:allow"
    assert seen[1].url.path == "/api/chat.postMessage"
    assert json.loads(seen[1].content)["text"] == "two"
    # A response_url post names no message, so there is nothing to edit later.
    assert (first, second) == ("", "1700.9")


async def test_slack_sends_to_the_channel_without_a_slash_command() -> None:
    """A plain message still goes through ``chat.postMessage`` unchanged."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "ts": "1701.0"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SlackAdapter("xoxb-1", client=client)
    ts = await adapter.send("C1", "hello")
    await adapter.stop()

    assert ts == "1701.0"
    assert [r.url.path for r in seen] == ["/api/chat.postMessage"]


def test_slack_manifest_declares_every_chat_command() -> None:
    manifest = slack_manifest()
    commands = manifest["features"]["slash_commands"]
    assert [entry["command"] for entry in commands] == [f"/{name}" for name, _ in MENU_COMMANDS]
    for entry in commands:
        assert entry["should_escape"] is False
        assert 0 < len(entry["description"]) <= 2000
    assert manifest["settings"]["socket_mode_enabled"] is True
    assert manifest["settings"]["interactivity"]["is_enabled"] is True
    assert "commands" in manifest["oauth_config"]["scopes"]["bot"]
    assert "message.im" in manifest["settings"]["event_subscriptions"]["bot_events"]
    assert manifest["features"]["bot_user"]["display_name"] == "snowpea"
    assert slack_manifest("beanbot")["features"]["bot_user"]["display_name"] == "beanbot"


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


async def test_cli_prints_a_slack_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``snowpea gateway slack-manifest`` needs no daemon: it is pure text."""
    from snowpea_core.cli import commands as cli_commands
    from snowpea_core.cli.main import build_parser

    args = build_parser().parse_args(["gateway", "slack-manifest"])
    assert await cli_commands.dispatch(args, tmp_path / "home") == 0
    manifest = json.loads(capsys.readouterr().out)
    commands = [entry["command"] for entry in manifest["features"]["slash_commands"]]
    assert "/sessions" in commands
    assert commands == [f"/{name}" for name, _ in MENU_COMMANDS]

    args = build_parser().parse_args(["gateway", "slack-manifest", "--name", "beanbot", "--json"])
    assert await cli_commands.dispatch(args, tmp_path / "home") == 0
    line = capsys.readouterr().out.strip()
    assert "\n" not in line
    assert json.loads(line)["features"]["bot_user"]["display_name"] == "beanbot"


async def test_start_and_unknown_commands_are_answered_in_the_chat(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """Telegram's ``/start`` is a hello, not a snowpea command; and a command
    snowpea does not know must say so in the chat rather than only in an
    ``error`` event nobody there can see."""
    from snowpea_core.gateway import router as gateway_router

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
                "userId": "u1",
            },
        )
        adapter = FakeAdapter.instances["tg_test"]
        await adapter.push("/start", channel_id="c1", user_id="u1")
        reply = await adapter.wait_for_send(TIMEOUT)
        assert reply.text == gateway_router.WELCOME_TEXT

        await adapter.push("/nosuchthing now", channel_id="c1", user_id="u1")
        reply = await adapter.wait_for_send(TIMEOUT)
        assert "/nosuchthing" in reply.text and "/help" in reply.text
        # Neither greeting nor typo opened a session.
        assert (await client.ok("session.list", {}))["sessions"] == []
        await client.stop()
    finally:
        await daemon.stop()


def test_telegram_errors_never_carry_the_bot_token() -> None:
    import asyncio

    transport = httpx.MockTransport(lambda request: httpx.Response(409, json={"ok": False}))
    adapter = TelegramAdapter(token="123:SECRET", client=httpx.AsyncClient(transport=transport))
    with pytest.raises(GatewayError) as caught:
        asyncio.run(adapter._api("getUpdates", {}))
    assert "SECRET" not in str(caught.value)
    assert "<token>" in str(caught.value)


def test_split_message_cuts_at_line_breaks_under_the_limit() -> None:
    from snowpea_core.gateway.base import split_message

    text = "\n".join(f"line {i:03d} " + "x" * 30 for i in range(200))
    pieces = split_message(text, 4096)
    assert len(pieces) > 1
    assert all(len(piece) <= 4096 for piece in pieces)
    assert "\n".join(pieces).replace("\n", "") == text.replace("\n", "")
    assert split_message("short", 4096) == ["short"]
    assert split_message("x" * 10, 4) == ["xxxx", "xxxx", "xx"]


async def test_a_long_answer_is_sent_in_pieces(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """The registry's /help alone is 8k chars; Telegram rejects anything over
    4096 with a 400, which is what silently ate an answer."""
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
        adapter = FakeAdapter.instances["tg_test"]
        adapter.max_message_chars = 50
        router = daemon.core.gateway
        binding = next(b for b in router.list() if b.id == bound["bindingId"])
        await router.send(binding, "c1", "\n".join(f"row {i} " + "y" * 20 for i in range(8)))
        sent = [item for item in adapter.sent if item.channel_id == "c1"]
        assert len(sent) >= 3
        assert all(len(item.text) <= 50 for item in sent)
        await client.stop()
    finally:
        await daemon.stop()
