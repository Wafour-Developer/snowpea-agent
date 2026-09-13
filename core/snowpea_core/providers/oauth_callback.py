"""The ``localhost`` redirect target of a browser OAuth login (CORE-codex-login).

Both real browser logins Snowpea offers — ChatGPT/Codex
(:mod:`snowpea_core.providers.openai_oauth`) and Google/Gemini
(:mod:`snowpea_core.providers.google_oauth`) — end the same way: the vendor
redirects the user's browser to a one-shot HTTP server on this machine with
``?code=`` in the query string.  The two differ only in port policy, so the
server itself lives here.

Beyond what :class:`snowpea_core.providers.auth_web.CallbackServer` (the
OpenRouter one) does, this one verifies the ``state`` parameter — without it a
page the user happens to have open could feed us someone else's code — and
answers with a real HTML page rather than a line of text, because a human is
looking at it.
"""

from __future__ import annotations

import asyncio
import errno
import logging
from typing import Any

from aiohttp import web

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

log = logging.getLogger("snowpea.providers.oauth")

#: Loopback literal to bind.  ``localhost`` can resolve to ``::1`` first, which
#: is not where a vendor's registered ``http://localhost:<port>`` redirect goes
#: on every machine; binding the v4 literal is what the CLIs do.
LOOPBACK = "127.0.0.1"

_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Snowpea</title>"
    "<body style='font:16px/1.5 system-ui;padding:3rem;max-width:32rem'>"
    "<h1>{heading}</h1><p>{body}</p>"
)


def success_page() -> str:
    return _PAGE.format(
        heading="Signed in", body="You can close this tab and return to Snowpea."
    )


def failure_page(detail: str) -> str:
    return _PAGE.format(heading="Login failed", body=detail)


class OAuthCallbackServer:
    """One-shot ``http://127.0.0.1:<port><path>`` listener for ``?code=``.

    ``port=0`` asks the OS for a free one (Google's client accepts any
    loopback port); a fixed port is for vendors — OpenAI — that registered
    exactly one redirect URI and reject every other.
    """

    def __init__(
        self,
        state: str,
        *,
        path: str,
        port: int = 0,
        host: str = LOOPBACK,
        redirect_host: str = "localhost",
        vendor: str = "provider",
    ) -> None:
        self.state = state
        self.path = path
        self.host = host
        self.port = port
        self.redirect_host = redirect_host
        self.vendor = vendor
        self._future: asyncio.Future[str] | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> int:
        """Bind the port and return it.  Raises when it cannot be had."""
        self._future = asyncio.get_running_loop().create_future()
        app = web.Application()
        app.router.add_get(self.path, self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await site.start()
        except OSError as exc:
            await self.close()
            raise self._bind_error(exc) from exc
        sockets = getattr(site._server, "sockets", None) if site._server else None  # noqa: SLF001
        if sockets:
            self.port = sockets[0].getsockname()[1]
        return self.port

    def _bind_error(self, exc: OSError) -> RpcError:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES) and self.port:
            return RpcError(
                errors.INTERNAL,
                f"{self.vendor}: port {self.port} is already in use, and {self.vendor} only "
                f"accepts {self.redirect_uri} as a login redirect. Close whatever is "
                f"listening on it (often another sign-in that is still open) and try again, "
                f"or use the headless login instead.",
                data={"vendor": self.vendor, "port": self.port},
            )
        return RpcError(
            errors.INTERNAL, f"{self.vendor}: could not open the login callback port ({exc})"
        )

    def _verdict(self, query: Any) -> tuple[str, str]:
        """``(code, failure detail)`` — exactly one of the two is non-empty."""
        error = query.get("error", "") or query.get("error_description", "")
        code = query.get("code", "")
        if error:
            return "", f"the provider refused the login: {error}"
        if not code:
            return "", "the callback carried no authorization code"
        if query.get("state", "") != self.state:
            # Not the login we started: never exchange this code.
            return "", "the callback state did not match; the login was not completed"
        return code, ""

    async def _handle(self, request: web.Request) -> web.Response:
        code, detail = self._verdict(request.query)
        if self._future is not None and not self._future.done():
            if detail:
                self._future.set_exception(RpcError(errors.INTERNAL, f"{self.vendor}: {detail}"))
            else:
                self._future.set_result(code)
        if detail:
            return web.Response(text=failure_page(detail), content_type="text/html")
        return web.Response(text=success_page(), content_type="text/html")

    async def wait(self, timeout: float) -> str:
        """The authorization code, or the failure the callback reported."""
        if self._future is None:  # pragma: no cover - start() always runs first
            raise RpcError(errors.INTERNAL, f"{self.vendor}: callback server was not started")
        return await asyncio.wait_for(asyncio.shield(self._future), timeout=timeout)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @property
    def redirect_uri(self) -> str:
        """What goes in the authorize URL — the *name* the vendor registered."""
        return f"http://{self.redirect_host}:{self.port}{self.path}"


__all__ = ["LOOPBACK", "OAuthCallbackServer", "failure_page", "success_page"]
