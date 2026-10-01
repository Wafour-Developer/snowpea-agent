"""Headless Chromium through Playwright — the default browser provider.

One browser process per daemon, one browser context and page per session, so
cookies and logins from one session never leak into another.  The context is
torn down by ``SessionManager`` when the session closes.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from snowpea_core.tools.browser_providers.base import (
    CONSOLE_BUFFER,
    CONSOLE_LINE_CHARS,
    BrowserNotInstalled,
    BrowserProviderMeta,
    BrowserProviderUnavailable,
    PageState,
)

#: Characters of page text a snapshot returns before the tool truncates it.
SNAPSHOT_CHARS = 20_000

NAV_TIMEOUT_MS = 30_000

#: The page size a session's context opens with, and so a screenshot's size.
VIEWPORT = {"width": 1280, "height": 800}

#: A screenshot past this many bytes is retaken as a JPEG.
SCREENSHOT_MAX_BYTES = 3 * 1024 * 1024

#: JPEG quality of that retake.
SCREENSHOT_JPEG_QUALITY = 70

#: Longest a key may be held down for ``press``.
MAX_HOLD_MS = 10_000

#: Console message types worth reporting (``log``/``info`` are noise).
CONSOLE_TYPES = frozenset({"error", "warning"})


def _console_line(kind: str, text: str) -> str:
    line = " ".join(str(text or "").split())
    if len(line) > CONSOLE_LINE_CHARS:
        line = line[:CONSOLE_LINE_CHARS] + "…"
    return f"[{kind}] {line}"


class LocalChromiumProvider:
    """Navigate, click, type, press, scroll, snapshot and screenshot a real headless Chromium."""

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
        #: session id -> console errors/warnings not yet reported
        self._console: dict[str, deque[str]] = {}

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
            context = await browser.new_context(viewport=dict(VIEWPORT))
            page = await context.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)
            self._watch_console(session_id, page)
            self._pages[session_id] = (context, page)
            return page

    def _watch_console(self, session_id: str, page: Any) -> None:
        """Buffer the page's console errors/warnings and uncaught exceptions."""
        buffer: deque[str] = deque(maxlen=CONSOLE_BUFFER)
        self._console[session_id] = buffer

        def on_console(message: Any) -> None:
            kind = str(getattr(message, "type", "") or "")
            if kind in CONSOLE_TYPES:
                buffer.append(_console_line(kind, getattr(message, "text", "")))

        def on_pageerror(error: Any) -> None:
            buffer.append(_console_line("pageerror", str(error)))

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)

    def _drain_console(self, session_id: str) -> tuple[str, ...]:
        """The buffered console lines, cleared so each is reported once."""
        buffer = self._console.get(session_id)
        if not buffer:
            return ()
        lines = tuple(buffer)
        buffer.clear()
        return lines

    async def close_session(self, session_id: str) -> None:
        """Drop this session's context; the browser stays up for the others."""
        async with self._lock:
            entry = self._pages.pop(session_id, None)
            self._console.pop(session_id, None)
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
    async def _state(
        self, page: Any, session_id: str = "", *, with_text: bool = True
    ) -> PageState:
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
            console=self._drain_console(session_id),
        )

    async def navigate(self, session_id: str, url: str) -> PageState:
        page = await self._page(session_id)
        await page.goto(url, wait_until="domcontentloaded")
        return await self._state(page, session_id)

    async def click(self, session_id: str, selector: str) -> PageState:
        page = await self._page(session_id)
        await page.click(selector)
        return await self._state(page, session_id)

    async def type_text(
        self, session_id: str, selector: str, text: str, *, submit: bool = False
    ) -> PageState:
        page = await self._page(session_id)
        await page.fill(selector, text)
        if submit:
            await page.press(selector, "Enter")
        return await self._state(page, session_id)

    async def scroll(self, session_id: str, delta_y: int) -> PageState:
        page = await self._page(session_id)
        await page.mouse.wheel(0, delta_y)
        return await self._state(page, session_id)

    async def snapshot(self, session_id: str) -> PageState:
        page = await self._page(session_id)
        return await self._state(page, session_id)

    async def press(self, session_id: str, key: str, *, hold_ms: int = 0) -> PageState:
        """Press ``key`` (``"w"``, ``"ArrowUp"``, ``"Control+a"``) on the focused page.

        With ``hold_ms`` the key is held down that long, so a game loop that
        polls key state (WASD movement) sees it pressed across frames.
        """
        page = await self._page(session_id)
        hold = max(0, min(int(hold_ms or 0), MAX_HOLD_MS))
        if hold:
            parts = key.split("+")
            modifiers, main = parts[:-1], parts[-1] or "+"
            for modifier in modifiers:
                await page.keyboard.down(modifier)
            await page.keyboard.down(main)
            await asyncio.sleep(hold / 1000)
            await page.keyboard.up(main)
            for modifier in reversed(modifiers):
                await page.keyboard.up(modifier)
        else:
            await page.keyboard.press(key)
        return await self._state(page, session_id)

    async def screenshot(
        self, session_id: str, *, full_page: bool = False, selector: str | None = None
    ) -> PageState:
        """A PNG of the viewport, the whole page, or one element.

        A screenshot too large for a model is downscaled (when Pillow is
        installed) and, failing that, retaken as a JPEG.
        """
        from snowpea_core.attachments.model import downscale_image

        page = await self._page(session_id)

        async def take(**options: Any) -> bytes:
            if selector:
                return await page.locator(selector).first.screenshot(**options)
            return await page.screenshot(full_page=full_page, **options)

        data, mime = downscale_image(await take(type="png"), "image/png")
        if len(data) > SCREENSHOT_MAX_BYTES:
            data = await take(type="jpeg", quality=SCREENSHOT_JPEG_QUALITY)
            data, mime = downscale_image(data, "image/jpeg")
        state = await self._state(page, session_id, with_text=False)
        return PageState(
            url=state.url, title=state.title, console=state.console, image=data, image_mime=mime
        )

    async def console(self, session_id: str) -> PageState:
        """The console errors/warnings buffered since the last report."""
        page = await self._page(session_id)
        return await self._state(page, session_id, with_text=False)


__all__ = [
    "MAX_HOLD_MS",
    "NAV_TIMEOUT_MS",
    "SCREENSHOT_MAX_BYTES",
    "SNAPSHOT_CHARS",
    "VIEWPORT",
    "LocalChromiumProvider",
]
