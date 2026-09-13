"""ChatGPT/Codex browser login for the ``openai`` vendor (CORE-codex-login).

The device-code flow in :mod:`snowpea_core.providers.auth_web` is the headless
fallback: it needs no local port, but OpenAI answers ``HTTP 403`` to it from
many machines and the token it mints is a *ChatGPT subscription* token, which
``api.openai.com`` rejects.  On a real desktop the supported path is the one
the Codex CLI uses — OAuth 2.0 **authorization code + PKCE (S256)** against
``https://auth.openai.com/oauth/authorize`` with a ``localhost:1455`` redirect:

1. :func:`browser_login_start` builds the authorize URL, binds the callback
   server, opens a browser and returns a
   :class:`~snowpea_core.providers.auth_web.LoginStart` straight away, so the
   RPC layer can show the URL while the wait continues in the background;
2. ``finish()`` waits for ``?code=``, exchanges it at ``/oauth/token``, reads
   the ``https://api.openai.com/auth`` claim out of the ``id_token`` and
   returns credentials shaped for ``settings.providers.openai``.

The credentials carry ``auth_method: "chatgpt"``, which is what routes the
vendor to :mod:`snowpea_core.providers.codex_transport` instead of the
API-key adapter; API-key users are untouched.

The JWT is only *decoded*, never verified: it arrives over TLS straight from
the token endpoint, and the claims are used to address the account, not to
authorise anything.  Structure is still validated so a malformed token fails
at login instead of as an opaque 401 on the first prompt.

Flow parameters are OpenAI's, as published by the Codex CLI; the Codex
device-code and Responses-API knowledge is adapted from hermes-agent (MIT),
``hermes_cli/auth_codex.py`` and ``agent/codex_headers.py``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import errno
import json
import logging
import secrets
import time
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from aiohttp import web

from snowpea_core.providers.auth_web import (
    LoginResult,
    LoginStart,
    ProgressHook,
    Prompt,
    _error_detail,
    _post_with_retry,
    _prompt_default,
    _report,
    new_pkce_pair,
)
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

log = logging.getLogger("snowpea.providers.openai_oauth")

#: The Codex CLI's public client id — the only one the ChatGPT backend trusts.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"

#: OpenAI registered exactly this redirect for :data:`CLIENT_ID`; any other
#: host, port or path is rejected with ``invalid_redirect_uri``.
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

SCOPES = "openid profile email offline_access"
#: Identity the *auth* server knows; the transport sends its own originator.
AUTH_ORIGINATOR = "codex_cli_rs"
#: Namespaced claim carrying ``chatgpt_account_id`` / ``chatgpt_plan_type``.
AUTH_CLAIM = "https://api.openai.com/auth"

#: ``method`` reported on :class:`LoginStart`/:class:`LoginResult` and in every
#: ``provider.loginProgress`` notification of this flow.
METHOD = "browser_pkce"
#: ``settings.providers.openai.auth_method`` written by a successful login.
AUTH_METHOD = "chatgpt"

#: How long the user has to finish in the browser.
DEFAULT_TIMEOUT_SEC = 300.0
#: Refresh this long before the access token actually expires.
REFRESH_SKEW_SEC = 120.0

_SUCCESS_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Snowpea</title>"
    "<body style='font:16px system-ui;padding:3rem'>"
    "<h1>Signed in</h1><p>You can close this tab and return to Snowpea.</p>"
)
_FAILURE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Snowpea</title>"
    "<body style='font:16px system-ui;padding:3rem'>"
    "<h1>Login failed</h1><p>{detail}</p>"
)

#: Credential fields never shown to a UI or written to a log.
SECRET_FIELDS = frozenset({"access_token", "refresh_token", "id_token", "token", "api_key"})
_MASK = "***"


# ---------------------------------------------------------------------------
# JWT claims
# ---------------------------------------------------------------------------


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode the payload of a JWS compact token without verifying it.

    Raises :class:`ValueError` when ``token`` is not three dot-separated
    segments whose middle one is base64url-encoded JSON — i.e. the caller gets
    a failure at login rather than a header that silently goes missing.
    """
    if not isinstance(token, str) or token.count(".") < 2:
        raise ValueError("not a JWT (expected three dot-separated segments)")
    payload = token.split(".")[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"JWT payload is not base64url: {exc}") from exc
    try:
        claims = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"JWT payload is not JSON: {exc}") from exc
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not a JSON object")
    return claims


def account_claims(id_token: str) -> tuple[str | None, str | None]:
    """``(chatgpt_account_id, chatgpt_plan_type)`` from an ``id_token``.

    A token that parses but carries no ``https://api.openai.com/auth`` claim
    yields ``(None, None)``: the login still succeeded, and the transport will
    simply not send the ``chatgpt-account-id`` header.
    """
    auth = decode_jwt_claims(id_token).get(AUTH_CLAIM)
    if not isinstance(auth, dict):
        return None, None
    account = auth.get("chatgpt_account_id")
    plan = auth.get("chatgpt_plan_type")
    return (
        account if isinstance(account, str) and account else None,
        plan if isinstance(plan, str) and plan else None,
    )


def account_id_of(access_token: str) -> str | None:
    """``chatgpt_account_id`` from an access token, or ``None`` if unreadable.

    Unlike :func:`account_claims` this never raises: it runs on every request
    the transport builds, where a malformed token should surface as a 401 from
    the server, not as a crash while assembling headers.
    """
    try:
        return account_claims(access_token)[0]
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def credentials_from_tokens(
    payload: dict[str, Any], *, now: Callable[[], float] = time.time
) -> dict[str, Any]:
    """Token-endpoint JSON to the ``settings.providers.openai`` shape.

    ``expires_at`` is absolute wall-clock seconds because it outlives the
    process that minted it; everything else is passed through verbatim.
    """
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise RpcError(errors.INTERNAL, "openai: token exchange returned no access_token")
    id_token = str(payload.get("id_token") or "")
    account_id: str | None = None
    plan_type: str | None = None
    if id_token:
        try:
            account_id, plan_type = account_claims(id_token)
        except ValueError as exc:
            raise RpcError(errors.INTERNAL, f"openai: id_token was malformed ({exc})") from exc
    if account_id is None:
        # Some responses carry the claim only on the access token.
        account_id = account_id_of(access_token)
    credentials: dict[str, Any] = {
        "auth_method": AUTH_METHOD,
        "access_token": access_token,
    }
    if payload.get("refresh_token"):
        credentials["refresh_token"] = str(payload["refresh_token"])
    if id_token:
        credentials["id_token"] = id_token
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, int | float) and expires_in > 0:
        credentials["expires_at"] = float(now()) + float(expires_in)
    if account_id:
        credentials["account_id"] = account_id
    if plan_type:
        credentials["plan_type"] = plan_type
    return credentials


def mask_credentials(credentials: dict[str, Any]) -> dict[str, Any]:
    """A copy safe to log or hand to a UI: every secret replaced by ``***``."""
    return {
        key: (_MASK if key in SECRET_FIELDS and value else value)
        for key, value in credentials.items()
    }


def is_chatgpt_auth(config: dict[str, Any] | None) -> bool:
    """True when ``settings.providers.openai`` holds a ChatGPT OAuth session."""
    if not isinstance(config, dict):
        return False
    return str(config.get("auth_method") or "") == AUTH_METHOD and bool(config.get("access_token"))


def is_expired(
    credentials: dict[str, Any],
    *,
    skew_sec: float = REFRESH_SKEW_SEC,
    now: Callable[[], float] = time.time,
) -> bool:
    """True when the access token is gone, expired, or about to be.

    An unknown ``expires_at`` counts as *not* expired: the transport refreshes
    reactively on a 401, so guessing here would burn a refresh every turn.
    """
    if not credentials.get("access_token"):
        return True
    expires_at = credentials.get("expires_at")
    if not isinstance(expires_at, int | float):
        return False
    return float(expires_at) <= now() + max(0.0, skew_sec)


async def refresh_credentials(
    credentials: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Exchange the refresh token for a new access token.

    Returns the *merged* credentials — OpenAI may rotate the refresh token or
    omit it, and dropping the old one would end the session.
    """
    refresh_token = str(credentials.get("refresh_token") or "")
    if not refresh_token:
        raise RpcError(
            errors.INTERNAL,
            "openai: the stored ChatGPT session has no refresh token; run `snowpea setup "
            "--login openai` again",
        )
    owned = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await _post_with_retry(
            http,
            TOKEN_URL,
            vendor="openai",
            action="token refresh",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
                "scope": "openid profile email",
            },
        )
        if response.status_code >= 400:
            detail = _error_detail(response)
            raise RpcError(
                errors.INTERNAL,
                f"openai: token refresh failed (HTTP {response.status_code})"
                + (f": {detail}" if detail else ""),
                data={"vendor": "openai", "status": response.status_code},
            )
        payload = response.json() if response.content else {}
    finally:
        if owned:
            await http.aclose()
    if not isinstance(payload, dict):
        raise RpcError(errors.INTERNAL, "openai: token refresh returned no JSON object")
    fresh = credentials_from_tokens(payload, now=now)
    merged = {**credentials, **fresh}
    merged.setdefault("refresh_token", refresh_token)
    return merged


# ---------------------------------------------------------------------------
# PKCE browser flow
# ---------------------------------------------------------------------------


def build_authorize_url(
    challenge: str,
    state: str,
    *,
    redirect_uri: str = REDIRECT_URI,
    originator: str = AUTH_ORIGINATOR,
) -> str:
    """The ``/oauth/authorize`` URL the user's browser is sent to."""
    return AUTHORIZE_URL + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": originator,
        }
    )


class CodexCallbackServer:
    """One-shot ``localhost:1455/auth/callback`` listener with state checking.

    Distinct from :class:`~snowpea_core.providers.auth_web.CallbackServer`: the
    port is fixed (OpenAI registered it), the ``state`` parameter is verified,
    and the browser gets a real HTML page instead of a line of text.
    """

    def __init__(self, state: str, *, host: str = CALLBACK_HOST, path: str = CALLBACK_PATH) -> None:
        self.host = host
        self.path = path
        self.port = CALLBACK_PORT
        self._state = state
        self._future: Any = None
        self._runner: web.AppRunner | None = None

    async def start(self, port: int = CALLBACK_PORT) -> int:
        self._future = asyncio.get_running_loop().create_future()
        app = web.Application()
        app.router.add_get(self.path, self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, port)
        try:
            await site.start()
        except OSError as exc:
            await self.close()
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                raise RpcError(
                    errors.INTERNAL,
                    f"openai: port {port} is already in use, and OpenAI only accepts "
                    f"{REDIRECT_URI} as a redirect. Close whatever is listening on it "
                    f"(often another Codex or Snowpea login) and try again, or use the "
                    f"headless device-code login instead.",
                    data={"vendor": "openai", "port": port},
                ) from exc
            raise RpcError(
                errors.INTERNAL, f"openai: could not open the login callback port ({exc})"
            ) from exc
        self.port = port
        return port

    async def _handle(self, request: web.Request) -> web.Response:
        code = request.query.get("code", "")
        state = request.query.get("state", "")
        error = request.query.get("error", "") or request.query.get("error_description", "")
        detail = ""
        if error:
            detail = f"OpenAI refused the login: {error}"
        elif not code:
            detail = "the callback carried no authorization code"
        elif state != self._state:
            # A mismatched state means this callback is not the one we started.
            detail = "the callback state did not match; the login was not completed"
        if self._future is not None and not self._future.done():
            if detail:
                self._future.set_exception(RpcError(errors.INTERNAL, f"openai: {detail}"))
            else:
                self._future.set_result(code)
        if detail:
            return web.Response(text=_FAILURE_PAGE.format(detail=detail), content_type="text/html")
        return web.Response(text=_SUCCESS_PAGE, content_type="text/html")

    async def wait(self, timeout: float) -> str:
        if self._future is None:  # pragma: no cover - start() always runs first
            raise RpcError(errors.INTERNAL, "openai: callback server was not started")
        return await asyncio.wait_for(asyncio.shield(self._future), timeout=timeout)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @property
    def callback_url(self) -> str:
        return f"http://localhost:{self.port}{self.path}"


async def browser_login_start(
    vendor: str = "openai",
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
    timeout_sec: float | None = None,
    on_progress: ProgressHook | None = None,
    now: Callable[[], float] = time.time,
) -> LoginStart:
    """Open the ChatGPT consent page and return it as a :class:`LoginStart`.

    ``finish()`` waits for the ``localhost`` callback, exchanges the code and
    returns credentials for ``settings.providers.openai``.
    """
    verifier, challenge = new_pkce_pair()
    state = secrets.token_urlsafe(24)
    timeout = float(timeout_sec or DEFAULT_TIMEOUT_SEC)
    await _report(on_progress, vendor=vendor, method=METHOD, phase="started")
    server = CodexCallbackServer(state)
    owned = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        await server.start()
        url = build_authorize_url(challenge, state, redirect_uri=REDIRECT_URI)
    except Exception as exc:
        await server.close()
        if owned:
            await http.aclose()
        await _report(on_progress, vendor=vendor, method=METHOD, phase="failed", message=str(exc))
        raise

    async def finish() -> LoginResult:
        try:
            on_prompt(f"Open {url} to sign in to ChatGPT")
            if open_browser:
                webbrowser.open(url)
            await _report(
                on_progress,
                vendor=vendor,
                method=METHOD,
                phase="await_user",
                verificationUri=url,
                expiresInSec=timeout,
            )
            await _report(on_progress, vendor=vendor, method=METHOD, phase="polling")
            code = await server.wait(timeout)
            exchanged = await _post_with_retry(
                http,
                TOKEN_URL,
                vendor=vendor,
                action="token exchange",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": CLIENT_ID,
                    "code_verifier": verifier,
                },
            )
            if exchanged.status_code >= 400:
                detail = _error_detail(exchanged)
                message = f"{vendor}: token exchange failed (HTTP {exchanged.status_code})"
                if detail:
                    message += f": {detail}"
                raise RpcError(
                    errors.INTERNAL,
                    message,
                    data={"vendor": vendor, "status": exchanged.status_code, "body": detail},
                )
            payload = exchanged.json() if exchanged.content else {}
            if not isinstance(payload, dict):
                raise RpcError(errors.INTERNAL, f"{vendor}: token exchange returned no JSON object")
            credentials = credentials_from_tokens(payload, now=now)
            plan = credentials.get("plan_type")
            message = f"{vendor}: signed in with ChatGPT" + (f" ({plan})" if plan else "")
            log.info("openai browser login complete: %s", mask_credentials(credentials))
            await _report(
                on_progress, vendor=vendor, method=METHOD, phase="done", message=message
            )
            return LoginResult(
                vendor=vendor, method=METHOD, credentials=credentials, message=message
            )
        except TimeoutError as exc:
            wrapped = RpcError(
                errors.INTERNAL,
                f"{vendor}: browser login timed out after {int(timeout)}s",
            )
            await _report(
                on_progress, vendor=vendor, method=METHOD, phase="failed", message=str(wrapped)
            )
            raise wrapped from exc
        except Exception as exc:
            await _report(
                on_progress, vendor=vendor, method=METHOD, phase="failed", message=str(exc)
            )
            raise
        finally:
            await server.close()
            if owned:
                await http.aclose()

    return LoginStart(
        vendor=vendor,
        method=METHOD,
        verification_uri=url,
        expires_in_sec=timeout,
        finish=finish,
    )


async def browser_login(
    vendor: str = "openai",
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
    timeout_sec: float | None = None,
    on_progress: ProgressHook | None = None,
) -> LoginResult:
    """Run the browser PKCE flow start-to-finish."""
    started = await browser_login_start(
        vendor,
        client=client,
        on_prompt=on_prompt,
        open_browser=open_browser,
        timeout_sec=timeout_sec,
        on_progress=on_progress,
    )
    return await started.finish()


__all__ = [
    "AUTHORIZE_URL",
    "AUTH_CLAIM",
    "AUTH_METHOD",
    "CALLBACK_PATH",
    "CALLBACK_PORT",
    "CLIENT_ID",
    "DEFAULT_TIMEOUT_SEC",
    "METHOD",
    "REDIRECT_URI",
    "REFRESH_SKEW_SEC",
    "SCOPES",
    "SECRET_FIELDS",
    "TOKEN_URL",
    "CodexCallbackServer",
    "account_claims",
    "account_id_of",
    "browser_login",
    "browser_login_start",
    "build_authorize_url",
    "credentials_from_tokens",
    "decode_jwt_claims",
    "is_chatgpt_auth",
    "is_expired",
    "mask_credentials",
    "refresh_credentials",
]
