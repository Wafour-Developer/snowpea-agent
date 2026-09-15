"""Telegram Bot API adapter (M5 contract §3).

Long polling over ``httpx`` — ``getUpdates`` with a server-side timeout, so the
daemon holds one idle request instead of a busy loop, and no webhook (and no
public URL) is needed.  ``python-telegram-bot`` is deliberately not a
dependency: the three calls we make are plain JSON POSTs.

Approvals ride on an inline keyboard; the press comes back as a
``callback_query`` whose ``data`` is ``apr:<requestId>:allow|deny``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx

from snowpea_core.gateway.base import Button, GatewayError, InboundMessage, OnMessage

log = logging.getLogger("snowpea.gateway.telegram")

API_BASE = "https://api.telegram.org"
#: Seconds ``getUpdates`` is allowed to hold the connection open.
POLL_TIMEOUT_SEC = 25
#: Seconds to wait before retrying after a transport failure.
RETRY_DELAY_SEC = 3.0


def parse_update(update: dict[str, Any]) -> InboundMessage | None:
    """Turn one ``getUpdates`` entry into an :class:`InboundMessage`.

    Returns ``None`` for updates we do not act on (edits, joins, channel posts
    without a sender), so the caller can simply skip them.
    """
    query = update.get("callback_query")
    if isinstance(query, dict):
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        sender = query.get("from") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None
        return InboundMessage(
            platform="telegram",
            channel_id=str(chat_id),
            user_id=str(sender.get("id", "")),
            text="",
            message_id=str(message.get("message_id", "")) or None,
            callback_data=str(query.get("data", "")) or None,
            callback_id=str(query.get("id", "")) or None,
        )
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    text = str(message.get("text") or message.get("caption") or "")
    attachments: list[dict[str, Any]] = []
    for key in ("photo", "document", "voice", "audio", "video"):
        value = message.get(key)
        if value:
            attachments.append({"kind": key, "payload": value})
    if not text and not attachments:
        return None
    reply = message.get("reply_to_message") or {}
    return InboundMessage(
        platform="telegram",
        channel_id=str(chat_id),
        user_id=str(sender.get("id", "")),
        text=text,
        attachments=attachments,
        message_id=str(message.get("message_id", "")) or None,
        reply_to=str(reply.get("message_id")) if reply.get("message_id") else None,
    )


def inline_keyboard(buttons: list[Button]) -> dict[str, Any]:
    """One row per button, which reads best on a phone."""
    return {"inline_keyboard": [[{"text": b.text, "callback_data": b.data}] for b in buttons]}


class TelegramAdapter:
    """Bot API client: long-poll for updates, POST to reply."""

    platform = "telegram"

    def __init__(
        self,
        token: str,
        *,
        api_base: str = API_BASE,
        client: httpx.AsyncClient | None = None,
        poll_timeout_sec: int = POLL_TIMEOUT_SEC,
    ) -> None:
        if not token:
            raise GatewayError("telegram needs a bot token")
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._poll_timeout = poll_timeout_sec
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._offset: int | None = None

    # -- transport -----------------------------------------------------
    @property
    def _url_prefix(self) -> str:
        return f"{self._api_base}/bot{self._token}"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._poll_timeout + 10)
        return self._client

    async def _api(self, method: str, payload: dict[str, Any]) -> Any:
        """One Bot API call; raises :class:`GatewayError` on a non-ok answer."""
        try:
            response = await self._http().post(f"{self._url_prefix}/{method}", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # httpx names the URL in its message, and the URL carries the
            # bot token; a log line must never.
            detail = str(exc).replace(self._token, "<token>") or type(exc).__name__
            raise GatewayError(f"telegram {method} failed: {detail}") from exc
        if not body.get("ok"):
            raise GatewayError(f"telegram {method} rejected: {body.get('description')}")
        return body.get("result")

    # -- PlatformAdapter -----------------------------------------------
    async def start(self, on_message: OnMessage) -> None:
        self._stopping = False
        self._task = asyncio.ensure_future(self._poll(on_message))

    async def send(self, channel_id: str, text: str, *, buttons: list[Button] | None = None) -> str:
        payload: dict[str, Any] = {"chat_id": channel_id, "text": text}
        if buttons:
            payload["reply_markup"] = inline_keyboard(buttons)
        result = await self._api("sendMessage", payload)
        return str((result or {}).get("message_id", ""))

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

    # -- extras the router uses ----------------------------------------
    async def acknowledge(self, callback_id: str, text: str = "") -> None:
        """Clear the spinner on a pressed inline button."""
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        with contextlib.suppress(GatewayError):
            await self._api("answerCallbackQuery", payload)

    # -- polling -------------------------------------------------------
    async def poll_once(self, on_message: OnMessage) -> int:
        """Fetch one batch of updates and dispatch it; returns how many."""
        payload: dict[str, Any] = {"timeout": self._poll_timeout}
        if self._offset is not None:
            payload["offset"] = self._offset
        updates = await self._api("getUpdates", payload) or []
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1
            message = parse_update(update)
            if message is None:
                continue
            try:
                await on_message(message)
            except Exception:  # noqa: BLE001 - one bad message must not end the poll
                log.warning("telegram message handler failed", exc_info=True)
        return len(updates)

    async def _poll(self, on_message: OnMessage) -> None:
        while not self._stopping:
            try:
                await self.poll_once(on_message)
            except asyncio.CancelledError:
                raise
            except GatewayError as exc:
                log.warning("telegram polling paused: %s", exc)
                await asyncio.sleep(RETRY_DELAY_SEC)


__all__ = [
    "API_BASE",
    "POLL_TIMEOUT_SEC",
    "RETRY_DELAY_SEC",
    "TelegramAdapter",
    "inline_keyboard",
    "parse_update",
]
