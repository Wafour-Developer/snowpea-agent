"""Slack adapter (M5 contract §3): Socket Mode to receive, Web API to send.

Socket Mode is used rather than the Events API because it needs no public URL,
which matters for a daemon that normally runs on a laptop.  It wants two
credentials: a bot token (``xoxb-``) for ``chat.postMessage`` and an app-level
token (``xapp-``) for ``apps.connections.open``.  With only a bot token the
adapter still sends — useful for scheduler delivery — and logs that it cannot
listen.

Approval buttons are a Block Kit ``actions`` block; the press arrives as an
``interactive`` envelope whose ``actions[0].value`` is our callback string.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import aiohttp
import httpx

from snowpea_core.gateway.base import Button, GatewayError, InboundMessage, OnMessage

log = logging.getLogger("snowpea.gateway.slack")

API_BASE = "https://slack.com/api"


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
    return None


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
        payload: dict[str, Any] = {"channel": channel_id, "text": text}
        if buttons:
            payload["blocks"] = action_blocks(text, buttons)
        body = await self._api("chat.postMessage", payload, self._token)
        return str(body.get("ts", ""))

    async def stop(self) -> None:
        self._stopping = True
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
            try:
                await on_message(inbound)
            except Exception:  # noqa: BLE001
                log.warning("slack message handler failed", exc_info=True)


__all__ = ["API_BASE", "SlackAdapter", "action_blocks", "parse_envelope"]
