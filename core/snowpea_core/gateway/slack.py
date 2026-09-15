"""Slack adapter (M5 contract §3): Socket Mode to receive, Web API to send.

Socket Mode is used rather than the Events API because it needs no public URL,
which matters for a daemon that normally runs on a laptop.  It wants two
credentials: a bot token (``xoxb-``) for ``chat.postMessage`` and an app-level
token (``xapp-``) for ``apps.connections.open``.  With only a bot token the
adapter still sends — useful for scheduler delivery — and logs that it cannot
listen.

Approval buttons are a Block Kit ``actions`` block; the press arrives as an
``interactive`` envelope whose ``actions[0].value`` is our callback string.

Slash commands are a third envelope type.  Slack swallows any message that
starts with ``/`` before the Events API ever sees it, so ``/sessions`` typed in
a channel reaches us only as a ``slash_commands`` envelope — and only when the
app *declares* that command, which a Slack app can do from its manifest alone.
:func:`slack_manifest` prints one, and ``snowpea gateway slack-manifest`` hands
it to the user for api.slack.com.  The envelope carries a ``response_url``
valid for thirty minutes: the adapter remembers it per channel for a minute and
sends the first answer there, so the reply lands under the command instead of
beside it, exactly as the Discord adapter reuses a deferred interaction.
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

log = logging.getLogger("snowpea.gateway.slack")

API_BASE = "https://slack.com/api"

#: How long a slash command's ``response_url`` is kept for its channel.  Slack
#: allows thirty minutes, but an answer that late belongs in the channel.
RESPONSE_URL_TTL_SEC = 60.0

#: Slack's cap on one slash-command description in a manifest.
COMMAND_DESCRIPTION_CHARS = 2000
#: Slack's cap on ``display_information.description``.
APP_DESCRIPTION_CHARS = 140

#: The bot scopes the gateway needs: post anywhere it is asked to, own slash
#: commands, and read the message events it subscribes to.
BOT_SCOPES: tuple[str, ...] = (
    "chat:write",
    "chat:write.public",
    "commands",
    "channels:history",
    "groups:history",
    "im:history",
    "mpim:history",
    "files:read",
)
#: The message events Socket Mode delivers as ``events_api`` envelopes.
BOT_EVENTS: tuple[str, ...] = (
    "message.channels",
    "message.groups",
    "message.im",
    "message.mpim",
)


def parse_envelope(envelope: dict[str, Any]) -> InboundMessage | None:
    """Turn one Socket Mode envelope into an :class:`InboundMessage`."""
    kind = envelope.get("type")
    payload = envelope.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    if kind == "events_api":
        event = payload.get("event") or {}
        if event.get("type") != "message" or event.get("bot_id") or event.get("subtype"):
            return None
        channel = event.get("channel")
        if channel is None:
            return None
        return InboundMessage(
            platform="slack",
            channel_id=str(channel),
            user_id=str(event.get("user", "")),
            text=str(event.get("text") or ""),
            attachments=list(event.get("files") or []),
            message_id=str(event.get("ts", "")) or None,
            reply_to=str(event.get("thread_ts")) if event.get("thread_ts") else None,
        )
    if kind == "interactive":
        actions = payload.get("actions") or []
        action = actions[0] if actions else {}
        channel = (payload.get("channel") or {}).get("id")
        user = (payload.get("user") or {}).get("id")
        if channel is None:
            return None
        return InboundMessage(
            platform="slack",
            channel_id=str(channel),
            user_id=str(user or ""),
            text="",
            callback_data=str(action.get("value", "")) or None,
            callback_id=str(envelope.get("envelope_id", "")) or None,
        )
    if kind == "slash_commands":
        # Slack intercepts anything starting with "/", so this *is* the typed
        # line: rebuild it and let the router read it as it reads any prompt.
        channel = payload.get("channel_id")
        command = str(payload.get("command") or "").strip()
        if channel is None or not command:
            return None
        args = str(payload.get("text") or "").strip()
        return InboundMessage(
            platform="slack",
            channel_id=str(channel),
            user_id=str(payload.get("user_id") or ""),
            text=f"{command} {args}".rstrip(),
            callback_id=str(envelope.get("envelope_id", "")) or None,
        )
    return None


def slack_manifest(bot_name: str = "snowpea") -> dict[str, Any]:
    """An app manifest declaring the bot, its scopes and the command menu.

    Slack apps cannot be given slash commands over the API: they come from the
    manifest, pasted into api.slack.com/apps (Create from manifest, or App
    Manifest on an existing app).  Without the declaration a typed ``/sessions``
    never reaches the daemon at all, because Slack answers it itself.
    """
    return {
        "display_information": {
            "name": bot_name,
            "description": "Your coding agent, in chat."[:APP_DESCRIPTION_CHARS],
        },
        "features": {
            "bot_user": {"display_name": bot_name, "always_online": True},
            "slash_commands": [
                {
                    "command": f"/{name}",
                    "description": description[:COMMAND_DESCRIPTION_CHARS],
                    "usage_hint": "[args]",
                    "should_escape": False,
                }
                for name, description in MENU_COMMANDS
            ],
        },
        "oauth_config": {"scopes": {"bot": list(BOT_SCOPES)}},
        "settings": {
            "event_subscriptions": {"bot_events": list(BOT_EVENTS)},
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def action_blocks(text: str, buttons: list[Button]) -> list[dict[str, Any]]:
    """A section with the message plus one actions block of buttons."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": button.text},
                    "value": button.data,
                    "action_id": button.data,
                }
                for button in buttons
            ],
        },
    ]


class SlackAdapter:
    """One Slack app: ``chat.postMessage`` out, Socket Mode in."""

    #: The platform's hard cap on one message; the router splits above it.
    max_message_chars = 4000

    platform = "slack"

    def __init__(
        self,
        token: str,
        *,
        app_token: str | None = None,
        api_base: str = API_BASE,
        client: httpx.AsyncClient | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not token:
            raise GatewayError("slack needs a bot token")
        self._token = token
        self._app_token = app_token
        self._api_base = api_base.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._session = session
        self._owns_session = session is None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        #: channel id -> (slash command response_url, deadline).
        self._pending: dict[str, tuple[str, float]] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _api(self, method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        try:
            response = await self._http().post(
                f"{self._api_base}/{method}",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GatewayError(f"slack {method} failed: {exc}") from exc
        if not isinstance(body, dict) or not body.get("ok"):
            detail = body.get("error") if isinstance(body, dict) else "malformed response"
            raise GatewayError(f"slack {method} rejected: {detail}")
        return body

    # -- PlatformAdapter -----------------------------------------------
    async def start(self, on_message: OnMessage) -> None:
        self._stopping = False
        if not self._app_token:
            log.warning("slack adapter is send-only: no app-level token for Socket Mode")
            return
        self._task = asyncio.ensure_future(self._run(on_message))

    async def send(self, channel_id: str, text: str, *, buttons: list[Button] | None = None) -> str:
        response_url = self._take_pending(channel_id)
        if response_url is not None and await self._respond(response_url, text, buttons):
            # A response_url post names no message: the caller must treat the
            # empty id as "nothing to edit", which the progress line does.
            return ""
        payload: dict[str, Any] = {"channel": channel_id, "text": text}
        if buttons:
            payload["blocks"] = action_blocks(text, buttons)
        body = await self._api("chat.postMessage", payload, self._token)
        return str(body.get("ts", ""))

    def stash_response_url(self, channel_id: str, response_url: str) -> None:
        """Remember where a slash command's first answer should go."""
        if not channel_id or not response_url:
            return
        self._pending[channel_id] = (response_url, time.monotonic() + RESPONSE_URL_TTL_SEC)

    def _take_pending(self, channel_id: str) -> str | None:
        """Claim this channel's open slash command, if it has not gone stale."""
        now = time.monotonic()
        for key, (_url, deadline) in list(self._pending.items()):
            if deadline <= now:
                del self._pending[key]
        entry = self._pending.pop(channel_id, None)
        return None if entry is None else entry[0]

    async def _respond(
        self, response_url: str, text: str, buttons: list[Button] | None
    ) -> bool:
        """Answer the slash command itself; False when it has to go elsewhere."""
        payload: dict[str, Any] = {"response_type": "in_channel", "text": text}
        if buttons:
            payload["blocks"] = action_blocks(text, buttons)
        try:
            response = await self._http().post(response_url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # The window closed, or Slack said no: fall back to the channel.
            log.warning("slack slash-command reply failed: %s", exc)
            return False
        return True

    async def stop(self) -> None:
        self._stopping = True
        self._pending.clear()
        task, self._task = self._task, None
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

    async def edit(self, channel_id: str, message_id: str, text: str) -> None:
        """``chat.update`` — Slack's way to rewrite a message we posted.

        There is deliberately no ``typing``: Slack's typing indicator is a
        Real Time Messaging feature that bot tokens cannot use at all, so the
        router feature-detects the method away rather than pretending.
        """
        with contextlib.suppress(GatewayError):
            await self._api(
                "chat.update",
                {"channel": channel_id, "ts": message_id, "text": text},
                self._token,
            )

    async def acknowledge(self, callback_id: str, text: str = "") -> None:
        """Socket Mode acks are sent on the socket; nothing to do over HTTP."""
        return None

    # -- socket mode ----------------------------------------------------
    async def open_socket_url(self) -> str:
        body = await self._api("apps.connections.open", {}, self._app_token or "")
        url = str(body.get("url", ""))
        if not url:
            raise GatewayError("slack apps.connections.open returned no url")
        return url

    async def _run(self, on_message: OnMessage) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            url = await self.open_socket_url()
            async with self._session.ws_connect(url) as ws:
                await self._consume(ws, on_message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dead socket must not kill the daemon
            log.warning("slack socket loop ended", exc_info=True)

    async def _consume(self, ws: Any, on_message: OnMessage) -> None:
        async for message in ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            envelope = message.json()
            envelope_id = envelope.get("envelope_id")
            if envelope_id:
                with contextlib.suppress(Exception):
                    await ws.send_json({"envelope_id": envelope_id})
            inbound = parse_envelope(envelope)
            if inbound is None:
                continue
            if envelope.get("type") == "slash_commands":
                # The ack above carries no text on purpose: the router answers
                # asynchronously, through the response_url remembered here.
                self.stash_response_url(
                    inbound.channel_id, str((envelope.get("payload") or {}).get("response_url", ""))
                )
            try:
                await on_message(inbound)
            except Exception:  # noqa: BLE001
                log.warning("slack message handler failed", exc_info=True)


__all__ = [
    "API_BASE",
    "APP_DESCRIPTION_CHARS",
    "BOT_EVENTS",
    "BOT_SCOPES",
    "COMMAND_DESCRIPTION_CHARS",
    "RESPONSE_URL_TTL_SEC",
    "SlackAdapter",
    "action_blocks",
    "parse_envelope",
    "slack_manifest",
]
