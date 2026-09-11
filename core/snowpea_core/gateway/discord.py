"""Discord adapter (M5 contract §3): REST to send, the gateway socket to receive.

Minimal but real.  Outbound messages are ``POST /channels/{id}/messages`` with
an optional action row of buttons; inbound traffic is the v10 gateway
WebSocket, where ``MESSAGE_CREATE`` carries chat and ``INTERACTION_CREATE``
carries a button press (``data.custom_id`` is our approval callback).

Only Telegram has to be fully exercised in v0.1, so the socket loop here keeps
to the minimum that actually works: HELLO, IDENTIFY, heartbeat, dispatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import aiohttp
import httpx

from snowpea_core.gateway.base import Button, GatewayError, InboundMessage, OnMessage

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
        return InboundMessage(
            platform="discord",
            channel_id=str(channel_id),
            user_id=str(user.get("id", "")),
            text="",
            callback_data=str(inner.get("custom_id", "")) or None,
            callback_id=f"{data.get('id', '')}:{data.get('token', '')}",
        )
    return None


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

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self._token}", "Content-Type": "application/json"}

    # -- PlatformAdapter -----------------------------------------------
    async def start(self, on_message: OnMessage) -> None:
        self._stopping = False
        self._task = asyncio.ensure_future(self._run(on_message))

    async def send(self, channel_id: str, text: str, *, buttons: list[Button] | None = None) -> str:
        payload: dict[str, Any] = {"content": text}
        if buttons:
            payload["components"] = action_row(buttons)
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
                            "properties": {"os": "linux", "browser": "snowpea", "device": "snowpea"},
                        },
                    }
                )
                continue
            if op in (OP_HEARTBEAT_ACK, OP_HEARTBEAT):
                continue
            inbound = parse_event(frame)
            if inbound is None:
                continue
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
    "GATEWAY_URL",
    "INTENTS",
    "DiscordAdapter",
    "action_row",
    "parse_event",
]
