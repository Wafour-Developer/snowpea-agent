"""Browser token login for the two vendors that offer one (M3 contract §3).

* ``openai`` — OAuth 2.0 **device code**: ask for a code, show the user a URL
  and a short code, poll the token endpoint until they approve it.
* ``openrouter`` — OAuth 2.0 **PKCE**: generate a verifier, open the consent
  page pointed at a throw-away ``localhost`` callback, exchange the returned
  code for a permanent API key.

Every other vendor raises ``login_unsupported`` with the API-key instructions.

All endpoints live in :data:`ENDPOINTS` so a vendor changing a URL is a data
edit, not a code change.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
from aiohttp import web

from snowpea_core.providers.presets import PRESETS
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

log = logging.getLogger("snowpea.providers.auth")

#: Vendor login endpoints and flow parameters — data, not code.
ENDPOINTS: dict[str, dict[str, Any]] = {
    "openai": {
        "method": "device_code",
        "device_authorization_url": "https://auth.openai.com/oauth/device/code",
        "token_url": "https://auth.openai.com/oauth/token",
        "client_id": "snowpea-cli",
        "scope": "openid profile email offline_access api.read api.write",
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "poll_interval_sec": 5.0,
        "timeout_sec": 900.0,
    },
    "openrouter": {
        "method": "oauth_pkce",
        "auth_url": "https://openrouter.ai/auth",
        "keys_url": "https://openrouter.ai/api/v1/auth/keys",
        "callback_host": "127.0.0.1",
        "callback_path": "/callback",
        "callback_port": 0,
        "timeout_sec": 300.0,
    },
}

API_KEY_HINT = "run `snowpea setup --vendor {vendor} --key <API key>` instead"


@dataclass
class LoginResult:
    """What a finished login produced."""

    vendor: str
    method: str
    #: Merged into ``settings.providers[vendor]`` by the caller.
    credentials: dict[str, Any] = field(default_factory=dict)
    message: str = ""


Prompt = Callable[[str], None]


def _prompt_default(message: str) -> None:
    log.info("%s", message)


def unsupported(vendor: str) -> RpcError:
    """The error every API-key-only vendor gets."""
    known = vendor in PRESETS
    detail = (
        f"{vendor} has no browser login; " + API_KEY_HINT.format(vendor=vendor)
        if known
        else f"unknown provider vendor: {vendor}"
    )
    return RpcError(errors.LOGIN_UNSUPPORTED, detail)


def method_for(vendor: str, method: str | None = None) -> str:
    """Validate ``vendor``/``method``, or raise ``login_unsupported``."""
    config = ENDPOINTS.get(vendor)
    if config is None:
        raise unsupported(vendor)
    supported = str(config["method"])
    if method and method not in (supported, "web", "browser"):
        raise RpcError(
            errors.LOGIN_UNSUPPORTED,
            f"{vendor} supports '{supported}', not '{method}'",
        )
    return supported


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def new_pkce_pair() -> tuple[str, str]:
    """``(code_verifier, code_challenge)`` using S256."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ---------------------------------------------------------------------------
# device code (OpenAI)
# ---------------------------------------------------------------------------


async def device_code_login(
    vendor: str = "openai",
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> LoginResult:
    """Run the device-code flow and return the token it granted."""
    config = ENDPOINTS[vendor]
    owned = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        start = await http.post(
            str(config["device_authorization_url"]),
            data={"client_id": config["client_id"], "scope": config["scope"]},
        )
        if start.status_code >= 400:
            raise RpcError(
                errors.INTERNAL,
                f"{vendor}: device authorization failed (HTTP {start.status_code})",
            )
        payload = start.json()
        device_code = str(payload.get("device_code") or "")
        user_code = str(payload.get("user_code") or "")
        verification = str(
            payload.get("verification_uri_complete") or payload.get("verification_uri") or ""
        )
        if not device_code or not verification:
            raise RpcError(errors.INTERNAL, f"{vendor}: device authorization response was empty")
        interval = float(payload.get("interval") or config["poll_interval_sec"])
        deadline = now() + min(float(payload.get("expires_in") or 0) or 1e9, config["timeout_sec"])

        on_prompt(f"Open {verification} and enter the code {user_code}")
        if open_browser:
            webbrowser.open(verification)

        while True:
            if now() >= deadline:
                raise RpcError(errors.INTERNAL, f"{vendor}: device login timed out")
            await sleep(interval)
            polled = await http.post(
                str(config["token_url"]),
                data={
                    "client_id": config["client_id"],
                    "device_code": device_code,
                    "grant_type": config["grant_type"],
                },
            )
            body = polled.json() if polled.content else {}
            if polled.status_code < 400 and body.get("access_token"):
                credentials: dict[str, Any] = {"token": str(body["access_token"])}
                if body.get("refresh_token"):
                    credentials["refresh_token"] = str(body["refresh_token"])
                if body.get("expires_in"):
                    credentials["expires_in"] = int(body["expires_in"])
                return LoginResult(
                    vendor=vendor,
                    method="device_code",
                    credentials=credentials,
                    message=f"{vendor}: signed in",
                )
            error = str(body.get("error") or "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5.0
                continue
            raise RpcError(
                errors.INTERNAL, f"{vendor}: device login failed ({error or polled.status_code})"
            )
    finally:
        if owned:
            await http.aclose()


# ---------------------------------------------------------------------------
# OAuth PKCE (OpenRouter)
# ---------------------------------------------------------------------------


class CallbackServer:
    """A one-shot ``localhost`` HTTP server that catches ``?code=``."""

    def __init__(self, host: str = "127.0.0.1", path: str = "/callback") -> None:
        self.host = host
        self.path = path
        self.port = 0
        self._future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._runner: web.AppRunner | None = None

    async def start(self, port: int = 0) -> int:
        app = web.Application()
        app.router.add_get(self.path, self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, port)
        await site.start()
        sockets = getattr(site._server, "sockets", None) if site._server else None  # noqa: SLF001
        self.port = sockets[0].getsockname()[1] if sockets else port
        return self.port

    async def _handle(self, request: web.Request) -> web.Response:
        code = request.query.get("code", "")
        error = request.query.get("error", "")
        if not self._future.done():
            if code:
                self._future.set_result(code)
            else:
                self._future.set_exception(
                    RpcError(errors.INTERNAL, f"login was refused: {error or 'no code'}")
                )
        body = "Snowpea: you can close this tab." if code else f"Snowpea: login failed ({error})."
        return web.Response(text=body, content_type="text/plain")

    async def wait(self, timeout: float) -> str:
        return await asyncio.wait_for(asyncio.shield(self._future), timeout=timeout)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @property
    def callback_url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"


async def oauth_pkce_login(
    vendor: str = "openrouter",
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
    timeout_sec: float | None = None,
) -> LoginResult:
    """Run the PKCE flow and return the API key it minted."""
    config = ENDPOINTS[vendor]
    verifier, challenge = new_pkce_pair()
    server = CallbackServer(str(config["callback_host"]), str(config["callback_path"]))
    await server.start(int(config["callback_port"]))
    owned = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        url = (
            str(config["auth_url"])
            + "?"
            + urlencode(
                {
                    "callback_url": server.callback_url,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        on_prompt(f"Open {url} to authorise Snowpea")
        if open_browser:
            webbrowser.open(url)
        code = await server.wait(float(timeout_sec or config["timeout_sec"]))
        exchanged = await http.post(
            str(config["keys_url"]),
            json={
                "code": code,
                "code_verifier": verifier,
                "code_challenge_method": "S256",
            },
        )
        if exchanged.status_code >= 400:
            raise RpcError(
                errors.INTERNAL,
                f"{vendor}: key exchange failed (HTTP {exchanged.status_code})",
            )
        key = str((exchanged.json() or {}).get("key") or "")
        if not key:
            raise RpcError(errors.INTERNAL, f"{vendor}: key exchange returned no key")
        return LoginResult(
            vendor=vendor,
            method="oauth_pkce",
            credentials={"api_key": key},
            message=f"{vendor}: API key stored",
        )
    except TimeoutError as exc:
        raise RpcError(errors.INTERNAL, f"{vendor}: login timed out") from exc
    finally:
        await server.close()
        if owned:
            await http.aclose()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


async def login(
    vendor: str,
    method: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
) -> LoginResult:
    """Start the browser login ``vendor`` supports, or raise ``login_unsupported``."""
    resolved = method_for(vendor, method)
    if resolved == "device_code":
        return await device_code_login(
            vendor, client=client, on_prompt=on_prompt, open_browser=open_browser
        )
    return await oauth_pkce_login(
        vendor, client=client, on_prompt=on_prompt, open_browser=open_browser
    )


__all__ = [
    "API_KEY_HINT",
    "ENDPOINTS",
    "CallbackServer",
    "LoginResult",
    "device_code_login",
    "login",
    "method_for",
    "new_pkce_pair",
    "oauth_pkce_login",
    "unsupported",
]
