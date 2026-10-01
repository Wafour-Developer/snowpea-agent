"""The ``browser_*`` tools, delegated to the configured browser provider.

Each session gets its own browser context, keyed by session id, and
``SessionManager`` closes it when the session ends.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import Any

from snowpea_core.tools import browser_providers, http_util
from snowpea_core.tools.browser_providers import url_policy
from snowpea_core.tools.browser_providers.base import (
    BrowserNotInstalled,
    BrowserProviderUnavailable,
    PageState,
)
from snowpea_core.tools.browser_providers.local_chromium import MAX_HOLD_MS
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.tools.web import max_output_chars

BROWSER_NOT_INSTALLED = "browser_not_installed"
BROWSER_UNAVAILABLE = "browser_provider_unavailable"

DEFAULT_SCROLL = 600


def _policed(provider: Any) -> bool:
    """Core's own Chromium applies the url policy; a connected browser has its own."""
    return getattr(getattr(provider, "meta", None), "id", None) == "local_chromium"


async def _url_refusal(ctx: ToolContext, url: str) -> str | None:
    return await asyncio.to_thread(
        url_policy.refusal, url, getattr(ctx, "session", None), getattr(ctx, "core", None)
    )


def _can_see_images(ctx: ToolContext) -> bool:
    from snowpea_core.tools.view_image import session_can_see

    try:
        return session_can_see(ctx)
    except Exception:  # noqa: BLE001 - a core without a provider registry: let it through
        return True


def _result(ctx: ToolContext, state: PageState) -> ToolResult:
    output = http_util.truncate(state.render(), max_output_chars(ctx))
    if state.image is None:
        return ToolResult(ok=True, output=output, path=state.url or None)
    from snowpea_core.tools.view_image import image_dimensions

    width, height = image_dimensions(state.image, state.image_mime)
    size = f"{width}×{height}" if width and height else "unknown size"
    extension = state.image_mime.rsplit("/", 1)[-1]
    head = f"screenshot attached: {size} {state.image_mime} ({max(1, len(state.image) // 1024)} KB)"
    return ToolResult(
        ok=True,
        output=f"{head}\n{output}".strip(),
        path=state.url or None,
        meta={
            "image": {
                "bytes_b64": base64.b64encode(state.image).decode("ascii"),
                "mime": state.image_mime,
                "name": f"browser-screenshot.{extension}",
                "width": width,
                "height": height,
            }
        },
    )


async def _act(
    ctx: ToolContext,
    action: Callable[[Any], Awaitable[PageState]],
    *,
    url: str | None = None,
    render: Callable[[PageState], ToolResult] | None = None,
) -> ToolResult:
    """Run one provider action and turn its failures into tool errors.

    On core's own Chromium the url policy (``url_policy``) is checked before a
    navigation to ``url`` and again on the page every action lands on, so a
    redirect or a clicked link into a refused address returns no page content.
    """
    settings = getattr(ctx.core, "settings", None)
    session = getattr(ctx, "session", None)
    if browser_providers.browser_locked(session) and not browser_providers.host_browser_attached(
        session
    ):
        # Never fall back to core's own headless browser: the user would see
        # nothing while the agent says it opened the page (addendum 9).
        return ToolResult(
            ok=False,
            error=f"{browser_providers.HOST_UNAVAILABLE}: "
            f"{browser_providers.HOST_UNAVAILABLE_MESSAGE}",
            meta={"code": browser_providers.HOST_UNAVAILABLE},
        )
    provider = browser_providers.resolve_for_session(settings, session)
    if not provider.available(settings):
        return ToolResult(
            ok=False,
            error=f"{BROWSER_UNAVAILABLE}: {provider.meta.label} is not usable in this install",
        )
    policed = _policed(provider)
    if policed and url:
        refused = await _url_refusal(ctx, url)
        if refused:
            return ToolResult(ok=False, error=refused, meta={"code": url_policy.URL_BLOCKED})
    try:
        state = await action(provider)
    except BrowserNotInstalled as exc:
        return ToolResult(
            ok=False,
            error=(
                f"{BROWSER_NOT_INSTALLED}: chromium is not downloaded. "
                f"Run `{BrowserNotInstalled.hint}`. ({exc})"
            ),
        )
    except BrowserProviderUnavailable as exc:
        return ToolResult(ok=False, error=f"{BROWSER_UNAVAILABLE}: {exc}")
    except Exception as exc:  # noqa: BLE001 - playwright raises its own Error type
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    if policed and state.url and state.url != url:
        refused = await _url_refusal(ctx, state.url)
        if refused:
            return ToolResult(
                ok=False,
                error=f"the page moved to {state.url}, which is refused — {refused}",
                meta={"code": url_policy.URL_BLOCKED},
            )
    return (render or (lambda s: _result(ctx, s)))(state)


async def browser_navigate(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    url = str(args.get("url", "")).strip()
    if not url:
        return ToolResult(ok=False, error="url is required")
    if not url.lower().startswith(("http://", "https://", "file://", "about:")):
        url = f"https://{url}"
    return await _act(ctx, lambda p: p.navigate(ctx.session.id, url), url=url)


async def browser_click(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    selector = str(args.get("selector", "")).strip()
    if not selector:
        return ToolResult(ok=False, error="selector is required")
    return await _act(ctx, lambda p: p.click(ctx.session.id, selector))


async def browser_type(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    selector = str(args.get("selector", "")).strip()
    if not selector:
        return ToolResult(ok=False, error="selector is required")
    text = str(args.get("text", ""))
    submit = bool(args.get("submit", False))
    return await _act(ctx, lambda p: p.type_text(ctx.session.id, selector, text, submit=submit))


async def browser_scroll(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        delta = int(args.get("deltaY", DEFAULT_SCROLL))
    except (TypeError, ValueError):
        delta = DEFAULT_SCROLL
    return await _act(ctx, lambda p: p.scroll(ctx.session.id, delta))


async def browser_snapshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _act(ctx, lambda p: p.snapshot(ctx.session.id))


async def browser_screenshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if not _can_see_images(ctx):
        return ToolResult(
            ok=False,
            error=(
                "this model cannot see images; use browser_snapshot for the page text and "
                "browser_console for its errors, or switch to a vision-capable model with /model"
            ),
        )
    full_page = bool(args.get("full_page", False))
    selector = str(args.get("selector") or "").strip() or None
    return await _act(
        ctx,
        lambda p: p.screenshot(ctx.session.id, full_page=full_page, selector=selector),
    )


def _render_console(ctx: ToolContext, state: PageState) -> ToolResult:
    head = f"{state.title}\n{state.url}".strip()
    if state.console:
        body = "\n".join(f"- {line}" for line in state.console)
        text = f"{head}\n\nConsole errors and warnings since the last report:\n{body}"
    else:
        text = f"{head}\n\nNo console errors or warnings since the last report."
    return ToolResult(
        ok=True, output=http_util.truncate(text.strip(), max_output_chars(ctx)), path=state.url
    )


async def browser_console(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _act(
        ctx, lambda p: p.console(ctx.session.id), render=lambda s: _render_console(ctx, s)
    )


async def browser_press(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raw = str(args.get("key", ""))
    key = "Space" if raw == " " or raw.strip().lower() == "space" else raw.strip()
    if not key:
        return ToolResult(ok=False, error="key is required")
    try:
        hold_ms = int(args.get("hold_ms") or 0)
    except (TypeError, ValueError):
        return ToolResult(ok=False, error="hold_ms must be a whole number of milliseconds")
    hold_ms = max(0, min(hold_ms, MAX_HOLD_MS))
    return await _act(ctx, lambda p: p.press(ctx.session.id, key, hold_ms=hold_ms))


_SELECTOR = {"type": "string", "description": "CSS selector for the target element."}


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="browser_navigate",
        category="browser",
        description="Open a url in this session's browser and return the page text.",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Page to open."}},
            "required": ["url"],
        },
        permission="network",
        run=browser_navigate,
    ),
    Tool(
        name="browser_click",
        category="browser",
        description="Click the first element matching a CSS selector.",
        input_schema={
            "type": "object",
            "properties": {"selector": _SELECTOR},
            "required": ["selector"],
        },
        permission="network",
        run=browser_click,
    ),
    Tool(
        name="browser_type",
        category="browser",
        description="Fill a form field, optionally pressing Enter afterwards.",
        input_schema={
            "type": "object",
            "properties": {
                "selector": _SELECTOR,
                "text": {"type": "string", "description": "Text to type."},
                "submit": {"type": "boolean", "description": "Press Enter after typing."},
            },
            "required": ["selector", "text"],
        },
        permission="network",
        run=browser_type,
    ),
    Tool(
        name="browser_scroll",
        category="browser",
        description="Scroll the page vertically; a negative deltaY scrolls up.",
        input_schema={
            "type": "object",
            "properties": {
                "deltaY": {
                    "type": "integer",
                    "description": f"Pixels to scroll (default {DEFAULT_SCROLL}).",
                }
            },
        },
        permission="network",
        run=browser_scroll,
    ),
    Tool(
        name="browser_snapshot",
        category="browser",
        description="Return the current page's url, title and visible text.",
        input_schema={"type": "object", "properties": {}},
        permission="network",
        run=browser_snapshot,
    ),
    Tool(
        name="browser_screenshot",
        category="browser",
        description=(
            "Capture the current page (the 1280x800 viewport, the full page, or one "
            "element) and attach it as an image you can see. Use it to check a UI, "
            "canvas or WebGL game you built; console errors are listed with it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the whole scrollable page, not just the viewport.",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of one element to capture instead.",
                },
            },
        },
        permission="network",
        run=browser_screenshot,
    ),
    Tool(
        name="browser_console",
        category="browser",
        description=(
            "Return the page's console errors, warnings and uncaught exceptions "
            "since the last report (each is reported once)."
        ),
        input_schema={"type": "object", "properties": {}},
        permission="network",
        run=browser_console,
    ),
    Tool(
        name="browser_press",
        category="browser",
        description=(
            'Press a key or key combo on the page, no selector needed ("w", '
            '"ArrowUp", "Space", "Enter", "Control+a"). hold_ms keeps it held '
            "down, e.g. to move a game character."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Playwright key name or combo, e.g. w, ArrowUp, Control+a.",
                },
                "hold_ms": {
                    "type": "integer",
                    "description": f"Hold the key down this long (0-{MAX_HOLD_MS} ms).",
                },
            },
            "required": ["key"],
        },
        permission="network",
        run=browser_press,
    ),
)


__all__ = [
    "BROWSER_NOT_INSTALLED",
    "BROWSER_UNAVAILABLE",
    "DEFAULT_SCROLL",
    "TOOLS",
    "browser_click",
    "browser_console",
    "browser_navigate",
    "browser_press",
    "browser_screenshot",
    "browser_scroll",
    "browser_snapshot",
    "browser_type",
]
