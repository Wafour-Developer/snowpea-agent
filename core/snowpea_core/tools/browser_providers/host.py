"""``host`` browser provider: the page actions of a connected browser (1.6.0).

A client that registers host tools named ``browser_*`` (snowpea-browser) does
the browsing itself.  Two things make the built-in browser tools use it:

1. **Shadowing** (the primary path).  A host tool with a built-in's name
   replaces it in every session that sees that host (``tools/host_tools``),
   schema and all, so the model calls the host's richer version directly.
2. **This provider**, for ``browser.provider: "host"``: a built-in
   ``browser_*`` tool the host did not shadow still routes to the host's tool of
   the same name, with the built-in's arguments.  With no host attached it
   refuses clearly instead of falling back to a different browser.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.tools.browser_providers.base import (
    BrowserProviderMeta,
    BrowserProviderUnavailable,
    PageState,
)

META = BrowserProviderMeta(
    id="host",
    label="Connected browser (snowpea-browser)",
    tier="free",
    key="no key",
)


class HostBrowserProvider:
    """Forwards browser actions to the session's host tools."""

    meta = META

    def __init__(self) -> None:
        self._core: Any = None

    def bind(self, core: Any) -> HostBrowserProvider:
        """Late wiring from the daemon, which owns the sessions."""
        self._core = core
        return self

    def available(self, settings: Any = None) -> bool:
        return self._core is not None

    async def _call(self, session_id: str, name: str, args: dict[str, Any]) -> PageState:
        from snowpea_core.tools.host_tools import HOST_TOOLS
        from snowpea_core.tools.registry import ToolContext

        core = self._core
        session = core.sessions.get(session_id) if core is not None else None
        tool = HOST_TOOLS.for_session(session).get(name) if session is not None else None
        if tool is None:
            raise BrowserProviderUnavailable(
                f"no connected browser provides {name} for this session"
            )
        ctx = ToolContext(session=session, core=core, backend=getattr(session, "backend", None))
        result = await tool.run(ctx, args)
        if not result.ok:
            raise BrowserProviderUnavailable(result.error or f"{name} failed in the browser")
        return PageState(text=result.output)

    async def navigate(self, session_id: str, url: str) -> PageState:
        return await self._call(session_id, "browser_navigate", {"url": url})

    async def click(self, session_id: str, selector: str) -> PageState:
        return await self._call(session_id, "browser_click", {"selector": selector})

    async def type_text(
        self, session_id: str, selector: str, text: str, *, submit: bool = False
    ) -> PageState:
        return await self._call(
            session_id, "browser_type", {"selector": selector, "text": text, "submit": submit}
        )

    async def scroll(self, session_id: str, delta_y: int) -> PageState:
        return await self._call(session_id, "browser_scroll", {"deltaY": delta_y})

    async def snapshot(self, session_id: str) -> PageState:
        return await self._call(session_id, "browser_snapshot", {})

    async def close_session(self, session_id: str) -> None:
        return None

    async def close(self) -> None:
        return None


HOST_BROWSER = HostBrowserProvider()

__all__ = ["HOST_BROWSER", "META", "HostBrowserProvider"]
