"""The five ``browser_*`` tools, delegated to the configured browser provider.

Each session gets its own browser context, keyed by session id, and
``SessionManager`` closes it when the session ends.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from snowpea_core.tools import browser_providers, http_util
from snowpea_core.tools.browser_providers.base import (
    BrowserNotInstalled,
    BrowserProviderUnavailable,
    PageState,
)
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.tools.web import max_output_chars

BROWSER_NOT_INSTALLED = "browser_not_installed"
BROWSER_UNAVAILABLE = "browser_provider_unavailable"

DEFAULT_SCROLL = 600


async def _act(
    ctx: ToolContext, action: Callable[[Any], Awaitable[PageState]]
) -> ToolResult:
    """Run one provider action and turn its failures into tool errors."""
    settings = getattr(ctx.core, "settings", None)
    provider = browser_providers.resolve(settings)
    if not provider.available(settings):
        return ToolResult(
            ok=False,
            error=f"{BROWSER_UNAVAILABLE}: {provider.meta.label} is not usable in this install",
        )
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
    return ToolResult(
        ok=True,
        output=http_util.truncate(state.render(), max_output_chars(ctx)),
        path=state.url or None,
    )


async def browser_navigate(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    url = str(args.get("url", "")).strip()
    if not url:
        return ToolResult(ok=False, error="url is required")
    if not url.lower().startswith(("http://", "https://", "file://", "about:")):
        url = f"https://{url}"
    return await _act(ctx, lambda p: p.navigate(ctx.session.id, url))


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
)


__all__ = [
    "BROWSER_NOT_INSTALLED",
    "BROWSER_UNAVAILABLE",
    "DEFAULT_SCROLL",
    "TOOLS",
    "browser_click",
    "browser_navigate",
    "browser_scroll",
    "browser_snapshot",
    "browser_type",
]
