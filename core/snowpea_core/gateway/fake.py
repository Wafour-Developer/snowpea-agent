"""In-memory adapter for tests (M5 contract §3).

``push`` feeds an inbound message to whatever the router registered; ``sent``
keeps every outbound message so a test can assert on text and buttons without
a socket anywhere.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any

from snowpea_core.gateway.base import Button, InboundMessage, OnMessage


@dataclass
class SentMessage:
    """One outbound message captured by :class:`FakeAdapter`."""

    channel_id: str
    text: str
    buttons: list[Button] = field(default_factory=list)
    message_id: str = ""

    def button_data(self) -> list[str]:
        return [button.data for button in self.buttons]


class FakeAdapter:
    """A platform that exists only inside the test process."""

    #: Every adapter built in this process, keyed by credential ref, so a test
    #: can reach the instance the router created for a binding.
    instances: dict[str, FakeAdapter] = {}

    def __init__(self, platform: str = "fake", credentials_ref: str = "fake") -> None:
        self.platform = platform
        self.credentials_ref = credentials_ref
        self.sent: list[SentMessage] = []
        self.started = False
        self.stopped = False
        self._on_message: OnMessage | None = None
        self._ids = itertools.count(1)
        self._delivered = asyncio.Event()
        FakeAdapter.instances[credentials_ref] = self

    async def start(self, on_message: OnMessage) -> None:
        self._on_message = on_message
        self.started = True

    async def send(self, channel_id: str, text: str, *, buttons: list[Button] | None = None) -> str:
        message_id = f"fake-{next(self._ids)}"
        self.sent.append(
            SentMessage(
                channel_id=channel_id,
                text=text,
                buttons=list(buttons or []),
                message_id=message_id,
            )
        )
        self._delivered.set()
        return message_id

    async def stop(self) -> None:
        self.stopped = True
        self._on_message = None

    # -- test helpers --------------------------------------------------
    async def push(
        self,
        text: str,
        *,
        channel_id: str = "c1",
        user_id: str = "u1",
        callback_data: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Deliver one inbound message to the router."""
        if self._on_message is None:
            raise RuntimeError("adapter was never started")
        await self._on_message(
            InboundMessage(
                platform=self.platform,
                channel_id=channel_id,
                user_id=user_id,
                text=text,
                attachments=list(attachments or []),
                message_id=f"in-{next(self._ids)}",
                callback_data=callback_data,
                callback_id="cb-1" if callback_data else None,
            )
        )

    async def press(self, data: str, *, channel_id: str = "c1", user_id: str = "u1") -> None:
        """Press an inline button carrying ``data``."""
        await self.push("", channel_id=channel_id, user_id=user_id, callback_data=data)

    async def wait_for_send(self, timeout: float = 5.0, count: int = 1) -> SentMessage:
        """Wait until at least ``count`` messages have gone out; return the last."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.sent) < count:
            if loop.time() > deadline:
                raise AssertionError(f"no outbound message; saw {[m.text for m in self.sent]}")
            await asyncio.sleep(0.02)
        return self.sent[-1]

    def texts(self) -> list[str]:
        return [message.text for message in self.sent]

    def with_buttons(self) -> list[SentMessage]:
        return [message for message in self.sent if message.buttons]


__all__ = ["FakeAdapter", "SentMessage"]
