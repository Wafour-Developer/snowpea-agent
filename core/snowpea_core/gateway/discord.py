"""Discord adapter (M5 contract §3): REST to send, the gateway socket to receive.

Minimal but real.  Outbound messages are ``POST /channels/{id}/messages`` with
an optional action row of buttons; inbound traffic is the v10 gateway
WebSocket, where ``MESSAGE_CREATE`` carries chat and ``INTERACTION_CREATE``
carries either a button press (``data.custom_id`` is our approval callback) or
a slash command (``data.type == 2``).

The chat commands are registered as global application commands on start, so
they appear in Discord's ``/`` picker exactly as they appear in Telegram's
menu.  A command arrives as an interaction that must be answered within three
seconds: the adapter posts a *deferred* callback immediately and remembers the
interaction token for that channel, and the next :meth:`DiscordAdapter.send`
for the channel edits that deferred reply instead of posting a new message.
Everything else — typed ``/sessions`` text, plain prompts, buttons — is
unchanged, so a bot invited without the ``applications.commands`` scope keeps
working through the message-content path.

Only Telegram has to be fully exercised in v0.1, so the socket loop here keeps
to the minimum that actually works: HELLO, IDENTIFY, heartbeat, dispatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import aiohttp
import httpx

from snowpea_core.gateway.base import Button, GatewayError, InboundMessage, OnMessage
from snowpea_core.gateway.chat import MENU_COMMANDS

log = logging.getLogger("snowpea.gateway.discord")

API_BASE = "https://discord.com/api/v10"
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
#: GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT.
INTENTS = (1 << 9) | (1 << 12) | (1 << 15)

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

#: ``interaction.data.type``: 2 is a slash command, 3 a message component.
INTERACTION_APPLICATION_COMMAND = 2
#: ``interaction callback type``: 5 is "deferred channel message with source",
#: which buys the sixty-second window the answer actually needs.
CALLBACK_DEFERRED = 5
#: ``application command type``: 1 is a chat-input (slash) command.
COMMAND_CHAT_INPUT = 1
#: ``application command option type``: 3 is a string.
OPTION_STRING = 3
#: The single free-text option every registered command takes.
OPTION_NAME = "args"
OPTION_DESCRIPTION = "Anything the command takes after its name"
#: Discord's cap on one application command description.
MENU_DESCRIPTION_CHARS = 100
#: How long a deferred interaction stays answerable; Discord allows fifteen
#: minutes, but a reply that late belongs in the channel, not in the command.
INTERACTION_TTL_SEC = 60.0

#: Button styles: 1 primary, 4 danger.
BUTTON_STYLES = (1, 4)


def parse_event(frame: dict[str, Any]) -> InboundMessage | None:
    """Turn one gateway dispatch frame into an :class:`InboundMessage`."""
    if frame.get("op") != OP_DISPATCH:
        return None
    name = frame.get("t")
    data = frame.get("d") or {}
    if not isinstance(data, dict):
        return None
    if name == "MESSAGE_CREATE":
        author = data.get("author") or {}
        if author.get("bot"):
            return None
        channel_id = data.get("channel_id")
        if channel_id is None:
            return None
        return InboundMessage(
            platform="discord",
            channel_id=str(channel_id),
            user_id=str(author.get("id", "")),
            text=str(data.get("content") or ""),
            attachments=list(data.get("attachments") or []),
            message_id=str(data.get("id", "")) or None,
        )
    if name == "INTERACTION_CREATE":
        inner = data.get("data") or {}
        member = data.get("member") or {}
        user = member.get("user") or data.get("user") or {}
        channel_id = data.get("channel_id")
        if channel_id is None:
            return None
        callback_id = f"{data.get('id', '')}:{data.get('token', '')}"
        if inner.get("type") == INTERACTION_APPLICATION_COMMAND:
            # A slash command is a prompt the router already knows how to read:
            # rebuild the line the person would have typed.
            return InboundMessage(
                platform="discord",
                channel_id=str(channel_id),
                user_id=str(user.get("id", "")),
                text=command_text(inner),
                callback_id=callback_id,
            )
        return InboundMessage(
            platform="discord",
            channel_id=str(channel_id),
            user_id=str(user.get("id", "")),
            text="",
            callback_data=str(inner.get("custom_id", "")) or None,
            callback_id=callback_id,
        )
    return None


def command_text(inner: dict[str, Any]) -> str:
    """``{"name": "new", "options": [{"name": "args", ...}]}`` -> ``"/new foo"``."""
    name = str(inner.get("name") or "").strip()
    if not name:
        return ""
    args = ""
    for option in inner.get("options") or []:
        if isinstance(option, dict) and option.get("name") == OPTION_NAME:
            args = str(option.get("value") or "").strip()
            break
    return f"/{name} {args}".rstrip()


def command_payload(name: str, description: str) -> dict[str, Any]:
    """One global chat-input command, with a free-text argument."""
    return {
        "name": name,
        "description": description[:MENU_DESCRIPTION_CHARS],
        "type": COMMAND_CHAT_INPUT,
        "options": [
            {
                "type": OPTION_STRING,
                "name": OPTION_NAME,
                "description": OPTION_DESCRIPTION,
                "required": False,
            }
        ],
    }


def menu_payload() -> list[dict[str, Any]]:
    """The whole command menu, in the order ``/help`` lists it."""
    return [command_payload(name, description) for name, description in MENU_COMMANDS]


def _registered(commands: Any) -> set[tuple[str, str]]:
    """Name/description pairs of what Discord already has, for comparison."""
    if not isinstance(commands, list):
        return set()
    return {
        (str(item.get("name", "")), str(item.get("description", "")))
        for item in commands
        if isinstance(item, dict)
    }


def is_command_interaction(frame: dict[str, Any]) -> bool:
    """True for a dispatch frame carrying a slash command (not a button)."""
    if frame.get("op") != OP_DISPATCH or frame.get("t") != "INTERACTION_CREATE":
        return False
    data = frame.get("d") or {}
    if not isinstance(data, dict):
        return False
    inner = data.get("data") or {}
    return bool(isinstance(inner, dict) and inner.get("type") == INTERACTION_APPLICATION_COMMAND)


def action_row(buttons: list[Button]) -> list[dict[str, Any]]:
    """One action row; Discord allows five buttons per row."""
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": BUTTON_STYLES[min(index, len(BUTTON_STYLES) - 1)],
                    "label": button.text,
                    "custom_id": button.data,
                }
                for index, button in enumerate(buttons[:5])
            ],
        }
    ]


class DiscordAdapter:
    """Bot-token client for one Discord application."""

    #: The platform's hard cap on one message; the router splits above it.
    max_message_chars = 2000

    platform = "discord"

    def __init__(
        self,
        token: str,
        *,
        api_base: str = API_BASE,
        gateway_url: str = GATEWAY_URL,
        client: httpx.AsyncClient | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not token:
            raise GatewayError("discord needs a bot token")
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._gateway_url = gateway_url
        self._client = client
        self._owns_client = client is None
        self._session = session
        self._owns_session = session is None
        self._task: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._stopping = False
        self._app_id: str | None = None
        #: channel id -> (application id, interaction token, deadline).
        self._pending: dict[str, tuple[str, str, float]] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self._token}", "Content-Type": "application/json"}

    def _redact(self, exc: Exception) -> str:
        """Whatever went wrong, said without the bot token in it."""
        return str(exc).replace(self._token, "<token>") or type(exc).__name__

    async def _api(self, method: str, path: str, payload: Any | None = None) -> Any:
        """One REST call against the bot token; raises on anything but 2xx."""
        response = await self._http().request(
            method, f"{self._api_base}{path}", json=payload, headers=self._headers
        )
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    # -- application commands -------------------------------------------
    async def application_id(self) -> str:
        """The bot's application id, fetched once and remembered."""
        if self._app_id:
            return self._app_id
        body = await self._api("GET", "/oauth2/applications/@me")
        app_id = str(body.get("id", "")) if isinstance(body, dict) else ""
        if not app_id:
            raise GatewayError("discord did not name the application")
        self._app_id = app_id
        return app_id

    async def register_commands(self) -> None:
        """Publish the chat command menu as global application commands.

        Never fatal.  A bot invited without the ``applications.commands`` scope
        cannot have a ``/`` picker, and the typed-text path is unaffected, so a
        failure here is a warning and nothing more.
        """
        desired = menu_payload()
        try:
            app_id = await self.application_id()
            current = await self._api("GET", f"/applications/{app_id}/commands")
            if _registered(current) == {
                (item["name"], item["description"]) for item in desired
            }:
                return
            await self._api("PUT", f"/applications/{app_id}/commands", desired)
        except (httpx.HTTPError, ValueError, GatewayError) as exc:
            log.warning("could not register the discord command menu: %s", self._redact(exc))

    async def defer(self, message: InboundMessage) -> None:
        """Answer a slash command inside Discord's three seconds.

        Posts a deferred reply and remembers the token, so the router's first
        :meth:`send` for that channel lands *in* the command instead of beside
        it.  Without an application id there is nothing to edit later, so the
        interaction is left alone and the answer simply arrives in the channel.
        """
        interaction_id, _, token = (message.callback_id or "").partition(":")
        if not interaction_id or not token:
            return
        try:
            app_id = await self.application_id()
            await self._api(
                "POST",
                f"/interactions/{interaction_id}/{token}/callback",
                {"type": CALLBACK_DEFERRED},
            )
        except (httpx.HTTPError, ValueError, GatewayError) as exc:
            log.warning("could not defer the discord interaction: %s", self._redact(exc))
            return
        self._pending[message.channel_id] = (app_id, token, time.monotonic() + INTERACTION_TTL_SEC)

    def _take_pending(self, channel_id: str) -> tuple[str, str] | None:
        """Claim this channel's open interaction, if it has not gone stale."""
        now = time.monotonic()
        for key, (_app, _token, deadline) in list(self._pending.items()):
            if deadline <= now:
                del self._pending[key]
        entry = self._pending.pop(channel_id, None)
        if entry is None:
            return None
        app_id, token, _deadline = entry
        return app_id, token

    # -- PlatformAdapter -----------------------------------------------
    async def start(self, on_message: OnMessage) -> None:
        self._stopping = False
        await self.register_commands()
        self._task = asyncio.ensure_future(self._run(on_message))

    async def send(self, channel_id: str, text: str, *, buttons: list[Button] | None = None) -> str:
        payload: dict[str, Any] = {"content": text}
        if buttons:
            payload["components"] = action_row(buttons)
        pending = self._take_pending(channel_id)
        if pending is not None:
            app_id, token = pending
            try:
                body = await self._api(
                    "PATCH",
                    f"/webhooks/{app_id}/{token}/messages/@original",
                    {"content": text, "components": action_row(buttons) if buttons else []},
                )
            except (httpx.HTTPError, ValueError) as exc:
                # The window closed, or Discord said no: fall back to the channel.
                log.warning("discord interaction reply failed: %s", self._redact(exc))
            else:
                return str(body.get("id", "")) if isinstance(body, dict) else ""
        try:
            response = await self._http().post(
                f"{self._api_base}/channels/{channel_id}/messages",
                json=payload,
                headers=self._headers,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GatewayError(f"discord send failed: {exc}") from exc
        return str(body.get("id", "")) if isinstance(body, dict) else ""

    async def stop(self) -> None:
        self._stopping = True
        self._pending.clear()
        for attr in ("_heartbeat", "_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def typing(self, channel_id: str) -> None:
        """Show the typing hint; Discord expires it after about ten seconds."""
        with contextlib.suppress(httpx.HTTPError):
            await self._http().post(
                f"{self._api_base}/channels/{channel_id}/typing",
                headers=self._headers,
            )

    async def edit(self, channel_id: str, message_id: str, text: str) -> None:
        """Rewrite one of our own messages (the progress line)."""
        with contextlib.suppress(httpx.HTTPError):
            await self._http().patch(
                f"{self._api_base}/channels/{channel_id}/messages/{message_id}",
                json={"content": text},
                headers=self._headers,
            )

    async def acknowledge(self, callback_id: str, text: str = "") -> None:
        """Answer an interaction so the client stops showing "thinking"."""
        interaction_id, _, token = callback_id.partition(":")
        if not interaction_id or not token:
            return
        payload = {"type": 4, "data": {"content": text or "ok", "flags": 64}}
        with contextlib.suppress(httpx.HTTPError):
            await self._http().post(
                f"{self._api_base}/interactions/{interaction_id}/{token}/callback",
                json=payload,
                headers=self._headers,
            )

    # -- gateway socket -------------------------------------------------
    async def _run(self, on_message: OnMessage) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.ws_connect(self._gateway_url) as ws:
                await self._consume(ws, on_message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dead socket must not kill the daemon
            log.warning("discord gateway loop ended", exc_info=True)

    async def _consume(self, ws: Any, on_message: OnMessage) -> None:
        """Drive one connected socket: HELLO, IDENTIFY, then dispatches."""
        async for message in ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            frame = message.json()
            op = frame.get("op")
            if op == OP_HELLO:
                interval = float((frame.get("d") or {}).get("heartbeat_interval", 41250)) / 1000.0
                self._heartbeat = asyncio.ensure_future(self._beat(ws, interval))
                await ws.send_json(
                    {
                        "op": OP_IDENTIFY,
                        "d": {
                            "token": self._token,
                            "intents": INTENTS,
                            "properties": {
                                "os": "linux",
                                "browser": "snowpea",
                                "device": "snowpea",
                            },
                        },
                    }
                )
                continue
            if op in (OP_HEARTBEAT_ACK, OP_HEARTBEAT):
                continue
            inbound = parse_event(frame)
            if inbound is None:
                continue
            if is_command_interaction(frame):
                # Three seconds, or Discord tells the person it failed.
                await self.defer(inbound)
            try:
                await on_message(inbound)
            except Exception:  # noqa: BLE001
                log.warning("discord message handler failed", exc_info=True)

    async def _beat(self, ws: Any, interval: float) -> None:
        while not self._stopping:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await ws.send_json({"op": OP_HEARTBEAT, "d": None})


__all__ = [
    "API_BASE",
    "CALLBACK_DEFERRED",
    "GATEWAY_URL",
    "INTENTS",
    "INTERACTION_APPLICATION_COMMAND",
    "INTERACTION_TTL_SEC",
    "MENU_DESCRIPTION_CHARS",
    "OPTION_DESCRIPTION",
    "OPTION_NAME",
    "DiscordAdapter",
    "action_row",
    "command_payload",
    "command_text",
    "is_command_interaction",
    "menu_payload",
    "parse_event",
]
