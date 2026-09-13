"""Interactive token login for providers that offer one (M3 contract §3).

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
import shutil
import subprocess
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
    "gemini": {
        "method": "google_adc",
        "verification_url": "https://accounts.google.com/",
        "timeout_sec": 900.0,
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


@dataclass
class LoginStart:
    """What is known the moment a browser login has something to show the user.

    ``finish`` resumes the flow (polling the token endpoint, or waiting on the
    PKCE callback) and resolves to the same :class:`LoginResult` the one-shot
    ``login()`` returns.  Splitting start/finish lets a caller such as the RPC
    handler show ``userCode``/``verificationUri`` immediately while the wait
    continues in the background.
    """

    vendor: str
    method: str
    #: Short code the user types in, when the flow has one (device code).
    user_code: str | None = None
    #: URL to open; for PKCE this is the only thing the user needs.
    verification_uri: str | None = None
    #: Same URL with the code already embedded, when the vendor provides one.
    verification_uri_complete: str | None = None
    #: Seconds until the code/session expires, when known.
    expires_in_sec: float | None = None
    #: Resumes the flow to completion; always set by the functions that build one.
    finish: Callable[[], Any] = field(default=None, repr=False)  # type: ignore[assignment]


Prompt = Callable[[str], None]
#: Called at each phase of a browser login: started, await_user, polling, done, failed.
ProgressHook = Callable[[dict[str, Any]], Any]


async def _report(on_progress: ProgressHook | None, **fields: Any) -> None:
    if on_progress is None:
        return
    result = on_progress({k: v for k, v in fields.items() if v is not None})
    if result is not None and hasattr(result, "__await__"):
        await result


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


async def device_code_start(
    vendor: str = "openai",
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Any] = asyncio.sleep,
    on_progress: ProgressHook | None = None,
) -> LoginStart:
    """Ask the vendor for a device code and return it as a :class:`LoginStart`.

    ``finish()`` on the result polls the token endpoint until the user
    approves (or the flow times out/fails) and returns the final
    :class:`LoginResult`.
    """
    config = ENDPOINTS[vendor]
    owned = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    await _report(on_progress, vendor=vendor, method="device_code", phase="started")
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
        verification_uri = str(payload.get("verification_uri") or "")
        verification_uri_complete = str(payload.get("verification_uri_complete") or "")
        verification = verification_uri_complete or verification_uri
        if not device_code or not verification:
            raise RpcError(errors.INTERNAL, f"{vendor}: device authorization response was empty")
        interval = float(payload.get("interval") or config["poll_interval_sec"])
        expires_in = float(payload.get("expires_in") or 0) or None
        deadline = now() + min(expires_in or 1e9, config["timeout_sec"])
    except Exception:
        if owned:
            await http.aclose()
        raise

    async def finish() -> LoginResult:
        try:
            on_prompt(f"Open {verification} and enter the code {user_code}")
            if open_browser:
                webbrowser.open(verification)
            await _report(
                on_progress,
                vendor=vendor,
                method="device_code",
                phase="await_user",
                userCode=user_code,
                verificationUri=verification_uri or None,
                verificationUriComplete=verification_uri_complete or None,
                expiresInSec=expires_in,
            )
            await _report(on_progress, vendor=vendor, method="device_code", phase="polling")
            poll_interval = interval
            while True:
                if now() >= deadline:
                    raise RpcError(errors.INTERNAL, f"{vendor}: device login timed out")
                await sleep(poll_interval)
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
                    await _report(
                        on_progress,
                        vendor=vendor,
                        method="device_code",
                        phase="done",
                        message=f"{vendor}: signed in",
                    )
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
                    poll_interval += 5.0
                    continue
                raise RpcError(
                    errors.INTERNAL,
                    f"{vendor}: device login failed ({error or polled.status_code})",
                )
        except Exception as exc:
            await _report(
                on_progress,
                vendor=vendor,
                method="device_code",
                phase="failed",
                message=str(exc),
            )
            raise
        finally:
            if owned:
                await http.aclose()

    return LoginStart(
        vendor=vendor,
        method="device_code",
        user_code=user_code,
        verification_uri=verification_uri or None,
        verification_uri_complete=verification_uri_complete or None,
        expires_in_sec=expires_in,
        finish=finish,
    )


async def device_code_login(
    vendor: str = "openai",
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> LoginResult:
    """Run the device-code flow start-to-finish and return the token it granted."""
    started = await device_code_start(
        vendor,
        client=client,
        on_prompt=on_prompt,
        open_browser=open_browser,
        now=now,
        sleep=sleep,
    )
    return await started.finish()


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


async def oauth_pkce_start(
    vendor: str = "openrouter",
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
    timeout_sec: float | None = None,
    on_progress: ProgressHook | None = None,
) -> LoginStart:
    """Open the PKCE consent page and return it as a :class:`LoginStart`.

    ``finish()`` on the result waits for the ``localhost`` callback, exchanges
    the code for an API key, and returns the final :class:`LoginResult`.
    """
    config = ENDPOINTS[vendor]
    verifier, challenge = new_pkce_pair()
    await _report(on_progress, vendor=vendor, method="oauth_pkce", phase="started")
    server = CallbackServer(str(config["callback_host"]), str(config["callback_path"]))
    owned = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        await server.start(int(config["callback_port"]))
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
    except Exception:
        await server.close()
        if owned:
            await http.aclose()
        raise

    async def finish() -> LoginResult:
        try:
            on_prompt(f"Open {url} to authorise Snowpea")
            if open_browser:
                webbrowser.open(url)
            await _report(
                on_progress,
                vendor=vendor,
                method="oauth_pkce",
                phase="await_user",
                verificationUri=url,
                expiresInSec=float(timeout_sec or config["timeout_sec"]),
            )
            await _report(on_progress, vendor=vendor, method="oauth_pkce", phase="polling")
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
            await _report(
                on_progress,
                vendor=vendor,
                method="oauth_pkce",
                phase="done",
                message=f"{vendor}: API key stored",
            )
            return LoginResult(
                vendor=vendor,
                method="oauth_pkce",
                credentials={"api_key": key},
                message=f"{vendor}: API key stored",
            )
        except TimeoutError as exc:
            wrapped = RpcError(errors.INTERNAL, f"{vendor}: login timed out")
            await _report(
                on_progress,
                vendor=vendor,
                method="oauth_pkce",
                phase="failed",
                message=str(wrapped),
            )
            raise wrapped from exc
        except Exception as exc:
            await _report(
                on_progress, vendor=vendor, method="oauth_pkce", phase="failed", message=str(exc)
            )
            raise
        finally:
            await server.close()
            if owned:
                await http.aclose()

    return LoginStart(
        vendor=vendor,
        method="oauth_pkce",
        verification_uri=url,
        expires_in_sec=float(timeout_sec or config["timeout_sec"]),
        finish=finish,
    )


async def oauth_pkce_login(
    vendor: str = "openrouter",
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
    timeout_sec: float | None = None,
) -> LoginResult:
    """Run the PKCE flow start-to-finish and return the API key it minted."""
    started = await oauth_pkce_start(
        vendor,
        client=client,
        on_prompt=on_prompt,
        open_browser=open_browser,
        timeout_sec=timeout_sec,
    )
    return await started.finish()


# ---------------------------------------------------------------------------
# Google Application Default Credentials (Gemini)
# ---------------------------------------------------------------------------


async def google_adc_start(
    vendor: str = "gemini", *, on_progress: ProgressHook | None = None
) -> LoginStart:
    """Start Google's supported desktop OAuth flow through the Cloud CLI.

    Google owns browser consent, refresh-token storage, and refresh. Snowpea
    persists only the selected auth method, never Google's refresh token.
    """
    executable = shutil.which("gcloud")
    if not executable:
        raise RpcError(
            errors.LOGIN_UNSUPPORTED,
            "gemini OAuth login needs the Google Cloud CLI (`gcloud`); "
            "install it or configure a Gemini API key",
        )
    await _report(on_progress, vendor=vendor, method="google_adc", phase="started")
    process = await asyncio.create_subprocess_exec(
        executable,
        "auth",
        "application-default",
        "login",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    verification = str(ENDPOINTS[vendor]["verification_url"])

    async def finish() -> LoginResult:
        await _report(
            on_progress,
            vendor=vendor,
            method="google_adc",
            phase="await_user",
            verificationUri=verification,
        )
        await _report(on_progress, vendor=vendor, method="google_adc", phase="polling")
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), float(ENDPOINTS[vendor]["timeout_sec"])
            )
        except TimeoutError as exc:
            process.kill()
            raise RpcError(errors.INTERNAL, "gemini: Google OAuth login timed out") from exc
        if process.returncode:
            detail = (stderr or stdout).decode("utf-8", "replace").strip()[-400:]
            raise RpcError(errors.INTERNAL, f"gemini: Google OAuth login failed: {detail}")
        await _report(
            on_progress,
            vendor=vendor,
            method="google_adc",
            phase="done",
            message="gemini: signed in with Google ADC",
        )
        return LoginResult(
            vendor=vendor,
            method="google_adc",
            credentials={"auth_method": "google_adc"},
            message="gemini: signed in with Google ADC",
        )

    return LoginStart(
        vendor=vendor,
        method="google_adc",
        verification_uri=verification,
        expires_in_sec=float(ENDPOINTS[vendor]["timeout_sec"]),
        finish=finish,
    )


async def google_adc_login(
    vendor: str = "gemini", *, on_progress: ProgressHook | None = None
) -> LoginResult:
    started = await google_adc_start(vendor, on_progress=on_progress)
    return await started.finish()


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
    if resolved == "google_adc":
        return await google_adc_login(vendor)
    return await oauth_pkce_login(
        vendor, client=client, on_prompt=on_prompt, open_browser=open_browser
    )


async def login_started(
    vendor: str,
    method: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
    on_progress: ProgressHook | None = None,
) -> LoginStart:
    """Like :func:`login`, but return as soon as there is something to show the
    user; ``result.finish()`` resumes the flow and finishes it.  Used by
    ``provider.loginWeb`` so it can answer with ``userCode``/``verificationUri``
    immediately and keep polling in a background task.
    """
    resolved = method_for(vendor, method)
    if resolved == "device_code":
        return await device_code_start(
            vendor,
            client=client,
            on_prompt=on_prompt,
            open_browser=open_browser,
            on_progress=on_progress,
        )
    if resolved == "google_adc":
        return await google_adc_start(vendor, on_progress=on_progress)
    return await oauth_pkce_start(
        vendor,
        client=client,
        on_prompt=on_prompt,
        open_browser=open_browser,
        on_progress=on_progress,
    )


__all__ = [
    "API_KEY_HINT",
    "ENDPOINTS",
    "CallbackServer",
    "LoginResult",
    "LoginStart",
    "device_code_login",
    "device_code_start",
    "google_adc_login",
    "google_adc_start",
    "login",
    "login_started",
    "method_for",
    "new_pkce_pair",
    "oauth_pkce_login",
    "oauth_pkce_start",
    "unsupported",
]
