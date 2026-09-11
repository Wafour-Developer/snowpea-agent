"""Shapes shared by every browser provider (M2 contract §4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

Tier = Literal["free", "paid", "subscription"]
KeyKind = Literal["no key", "key optional", "key required", "self-hosted"]


class BrowserProviderUnavailable(RuntimeError):
    """Raised when a provider is catalog-only or has no credentials."""


class BrowserNotInstalled(RuntimeError):
    """Playwright is importable but its browser binaries are missing."""

    hint = "uv run playwright install chromium"


@dataclass(frozen=True)
class BrowserProviderMeta:
    """Catalog entry for the setup screen and ``browser_*`` tool errors."""

    id: str
    label: str
    tier: Tier
    key: KeyKind
    default: bool = False
    env: tuple[str, ...] = ()
    endpoint: str = ""


@dataclass(frozen=True)
class PageState:
    """What every ``browser_*`` tool reports back after it acts."""

    url: str = ""
    title: str = ""
    text: str = ""

    def render(self) -> str:
        head = f"{self.title}\n{self.url}".strip()
        return f"{head}\n\n{self.text}".strip() if self.text else head


@runtime_checkable
class BrowserProvider(Protocol):
    """One browser per session; the session id keys the context."""

    meta: BrowserProviderMeta

    def available(self, settings: Any = None) -> bool: ...

    async def navigate(self, session_id: str, url: str) -> PageState: ...

    async def click(self, session_id: str, selector: str) -> PageState: ...

    async def type_text(
        self, session_id: str, selector: str, text: str, *, submit: bool = False
    ) -> PageState: ...

    async def scroll(self, session_id: str, delta_y: int) -> PageState: ...

    async def snapshot(self, session_id: str) -> PageState: ...

    async def close_session(self, session_id: str) -> None: ...

    async def close(self) -> None: ...


__all__ = [
    "BrowserNotInstalled",
    "BrowserProvider",
    "BrowserProviderMeta",
    "BrowserProviderUnavailable",
    "KeyKind",
    "PageState",
    "Tier",
]
