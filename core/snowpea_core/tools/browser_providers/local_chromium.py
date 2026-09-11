"""Headless Chromium through Playwright — the default browser provider.

One browser process per daemon, one browser context and page per session, so
cookies and logins from one session never leak into another.  The context is
torn down by ``SessionManager`` when the session closes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from snowpea_core.tools.browser_providers.base import (
    BrowserNotInstalled,
    BrowserProviderMeta,
    BrowserProviderUnavailable,
    PageState,
)

#: Characters of page text a snapshot returns before the tool truncates it.
SNAPSHOT_CHARS = 20_000

NAV_TIMEOUT_MS = 30_000


class LocalChromiumProvider:
    """Navigate, click, type, scroll and snapshot a real headless Chromium."""

    meta = BrowserProviderMeta(
        id="local_chromium",
        label="Local Chromium (Playwright)",
        tier="free",
        key="no key",
        default=True,
    )

    def __init__(self, headless: bool = True) -> None:
        self._headless = headless
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        #: session id -> (context, page)
        self._pages: dict[str, tuple[Any, Any]] = {}

    # -- availability --------------------------------------------------
    def available(self, settings: Any = None) -> bool:
        if settings is not None:
            self._headless = bool(getattr(getattr(settings, "browser", None), "headless", True))
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False
        return True

    # -- lifecycle -----------------------------------------------------
    async def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserProviderUnavailable(
                "the playwright package is not installed"
            ) from exc
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(headless=self._headless)
        except Exception as exc:  # noqa: BLE001 - playwright raises its own Error type
            await self._shutdown_playwright()
            if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                raise BrowserNotInstalled(str(exc)) from exc
            raise BrowserProviderUnavailable(f"could not start chromium: {exc}") from exc
        return self._browser

    async def _page(self, session_id: str) -> Any:
        async with self._lock:
            existing = self._pages.get(session_id)
            if existing is not None:
                return existing[1]
            browser = await self._ensure_browser()
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)
            self._pages[session_id] = (context, page)
            return page

    async def close_session(self, session_id: str) -> None:
        """Drop this session's context; the browser stays up for the others."""
        async with self._lock:
            entry = self._pages.pop(session_id, None)
        if entry is None:
            return
        context, _page = entry
        try:
            await context.close()
        except Exception:  # noqa: BLE001 - a dead context is already closed
            pass

    async def close(self) -> None:
        """Close every context and the browser itself."""
        for session_id in list(self._pages):
            await self.close_session(session_id)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        await self._shutdown_playwright()

    async def _shutdown_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None

    # -- actions -------------------------------------------------------
    async def _state(self, page: Any, *, with_text: bool = True) -> PageState:
        text = ""
        if with_text:
            try:
                text = await page.inner_text("body")
            except Exception:  # noqa: BLE001 - a page with no body still has a url
                text = ""
        return PageState(
            url=str(page.url or ""),
            title=str(await page.title() or ""),
            text=text[:SNAPSHOT_CHARS],
        )

    async def navigate(self, session_id: str, url: str) -> PageState:
        page = await self._page(session_id)
        await page.goto(url, wait_until="domcontentloaded")
        return await self._state(page)

    async def click(self, session_id: str, selector: str) -> PageState:
        page = await self._page(session_id)
        await page.click(selector)
        return await self._state(page)

    async def type_text(
        self, session_id: str, selector: str, text: str, *, submit: bool = False
    ) -> PageState:
        page = await self._page(session_id)
        await page.fill(selector, text)
        if submit:
            await page.press(selector, "Enter")
        return await self._state(page)

    async def scroll(self, session_id: str, delta_y: int) -> PageState:
        page = await self._page(session_id)
        await page.mouse.wheel(0, delta_y)
        return await self._state(page)

    async def snapshot(self, session_id: str) -> PageState:
        page = await self._page(session_id)
        return await self._state(page)


__all__ = ["NAV_TIMEOUT_MS", "SNAPSHOT_CHARS", "LocalChromiumProvider"]
