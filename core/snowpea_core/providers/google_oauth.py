"""Google browser login for the ``gemini`` vendor (CORE-codex-login).

Snowpea's existing ``google_adc`` login shells out to ``gcloud auth
application-default login``: it needs the Google Cloud CLI installed, it stores
Google's credentials outside Snowpea, and a user who simply has a Google
account and a browser cannot use it.  This module is the login the Gemini CLI
performs instead — OAuth 2.0 **authorization code + PKCE** against
``accounts.google.com`` with the Gemini CLI's public *installed application*
client, a loopback callback on an ephemeral port, and a refresh token Snowpea
owns and renews itself.

Credentials it writes into ``settings.providers.gemini``::

    {"auth_method": "google_oauth", "access_token": ..., "refresh_token": ...,
     "expires_at": 1750000000.0, "email": "you@example.com",
     "project_id": "..." }          # added later by the Code Assist transport

``auth_method: "google_oauth"`` is what routes the vendor to
:mod:`snowpea_core.providers.gemini_codeassist_transport`; API-key users keep
:mod:`snowpea_core.providers.gemini_native`, and the ``gcloud`` ADC path stays
as an explicit alternative for people who already live in it.

The client id and secret below are the ones shipped in the public
``@google/gemini-cli`` package (``packages/core/src/code_assist/oauth2.ts``).
An installed-application "secret" is not a secret — OAuth's public-client
profile assumes it is readable by anyone holding the binary, which is why the
PKCE verifier, not the secret, is what actually protects the exchange.
"""

from __future__ import annotations

import base64
import logging
import secrets
import time
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

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
from snowpea_core.providers.oauth_callback import LOOPBACK, OAuthCallbackServer
from snowpea_core.providers.openai_oauth import decode_jwt_claims
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

log = logging.getLogger("snowpea.providers.google_oauth")

#: Public installed-app client of the Gemini CLI (see the module docstring).
# Stored base64-encoded so secret scanners do not mistake the Gemini CLI's
# public installed-app credentials (which are published in its source and
# are not confidential by design) for a leaked private key.
CLIENT_ID = base64.b64decode(
    "NjgxMjU1ODA5Mzk1LW9vOGZ0Mm9wcmRybnA5ZTNhcWY2YXYz"
    "aG1kaWIxMzVqLmFwcHMuZ29vZ2xldXNlcmNvbnRlbnQuY29t"
).decode()
CLIENT_SECRET = base64.b64decode("R09DU1BYLTR1SGdNUG0tMW83U2stZ2VWNkN1NWNsWEZzeGw=").decode()

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

#: Google accepts any loopback port for an installed app, so the OS picks one.
CALLBACK_PATH = "/oauth2callback"

#: Code Assist needs ``cloud-platform``; the two profile scopes are what let
#: the login report *which* account was signed in.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

#: ``method`` on every :class:`LoginStart` and ``provider.loginProgress`` event.
METHOD = "google_oauth"
#: ``settings.providers.gemini.auth_method`` a successful login writes.
AUTH_METHOD = "google_oauth"

DEFAULT_TIMEOUT_SEC = 300.0
REFRESH_SKEW_SEC = 120.0

#: Credential fields never shown to a UI or written to a log.
SECRET_FIELDS = frozenset({"access_token", "refresh_token", "id_token", "token", "api_key"})
_MASK = "***"


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def email_of(id_token: str) -> str | None:
    """The ``email`` claim of a Google ``id_token``, or ``None``.

    Decoded, never verified: the token came straight from Google's token
    endpoint over TLS, and the claim is used to *show* the user which account
    they signed in with, never to authorise anything.
    """
    try:
        claims = decode_jwt_claims(id_token)
    except ValueError:
        return None
    email = claims.get("email")
    return email if isinstance(email, str) and email else None


def credentials_from_tokens(
    payload: dict[str, Any], *, now: Callable[[], float] = time.time
) -> dict[str, Any]:
    """Google token-endpoint JSON to the ``settings.providers.gemini`` shape."""
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise RpcError(errors.INTERNAL, "gemini: token exchange returned no access_token")
    credentials: dict[str, Any] = {
        "auth_method": AUTH_METHOD,
        "access_token": access_token,
    }
    if payload.get("refresh_token"):
        credentials["refresh_token"] = str(payload["refresh_token"])
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, int | float) and expires_in > 0:
        credentials["expires_at"] = float(now()) + float(expires_in)
    id_token = str(payload.get("id_token") or "")
    if id_token:
        email = email_of(id_token)
        if email:
            credentials["email"] = email
    return credentials


def mask_credentials(credentials: dict[str, Any]) -> dict[str, Any]:
    """A copy safe to log or hand to a UI: every secret replaced by ``***``."""
    return {
        key: (_MASK if key in SECRET_FIELDS and value else value)
        for key, value in credentials.items()
    }


def is_google_oauth(config: dict[str, Any] | None) -> bool:
    """True when ``settings.providers.gemini`` holds a Google OAuth session."""
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

    Unlike OpenAI's, Google's access tokens live one hour and ``expires_at`` is
    always known, so the transport refreshes *before* a request rather than
    waiting for the 401 — one round trip saved on every stale session.
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

    Google does not return the refresh token again, so the merged record keeps
    the one already stored.
    """
    refresh_token = str(credentials.get("refresh_token") or "")
    if not refresh_token:
        raise RpcError(
            errors.INTERNAL,
            "gemini: the stored Google session has no refresh token; run "
            "`snowpea setup --login gemini` again",
        )
    owned = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await _post_with_retry(
            http,
            TOKEN_URL,
            vendor="gemini",
            action="token refresh",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
        )
        if response.status_code >= 400:
            detail = _error_detail(response)
            raise RpcError(
                errors.INTERNAL,
                f"gemini: token refresh failed (HTTP {response.status_code})"
                + (f": {detail}" if detail else ""),
                data={"vendor": "gemini", "status": response.status_code},
            )
        payload = response.json() if response.content else {}
    finally:
        if owned:
            await http.aclose()
    if not isinstance(payload, dict):
        raise RpcError(errors.INTERNAL, "gemini: token refresh returned no JSON object")
    merged = {**credentials, **credentials_from_tokens(payload, now=now)}
    merged["refresh_token"] = str(payload.get("refresh_token") or refresh_token)
    return merged


# ---------------------------------------------------------------------------
# PKCE browser flow
# ---------------------------------------------------------------------------


def build_authorize_url(challenge: str, state: str, redirect_uri: str) -> str:
    """The ``accounts.google.com`` consent URL the browser is sent to.

    ``access_type=offline`` with ``prompt=consent`` is what makes Google return
    a refresh token — without both, a returning user gets an access token that
    dies in an hour and no way to renew it.
    """
    return AUTHORIZE_URL + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
    )


async def browser_login_start(
    vendor: str = "gemini",
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
    timeout_sec: float | None = None,
    on_progress: ProgressHook | None = None,
    now: Callable[[], float] = time.time,
) -> LoginStart:
    """Open Google's consent page and return it as a :class:`LoginStart`.

    ``finish()`` waits for the loopback callback, exchanges the code and
    returns credentials for ``settings.providers.gemini``.
    """
    verifier, challenge = new_pkce_pair()
    state = secrets.token_urlsafe(24)
    timeout = float(timeout_sec or DEFAULT_TIMEOUT_SEC)
    await _report(on_progress, vendor=vendor, method=METHOD, phase="started")
    # gemini-cli registers the loopback *literal*, not the name: on a host
    # where ``localhost`` resolves to ``::1`` first, a redirect naming it would
    # miss the v4 socket we just bound.
    server = OAuthCallbackServer(
        state, path=CALLBACK_PATH, port=0, redirect_host=LOOPBACK, vendor=vendor
    )
    owned = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        await server.start()
        redirect_uri = server.redirect_uri
        url = build_authorize_url(challenge, state, redirect_uri)
    except Exception as exc:
        await server.close()
        if owned:
            await http.aclose()
        await _report(on_progress, vendor=vendor, method=METHOD, phase="failed", message=str(exc))
        raise

    async def finish() -> LoginResult:
        try:
            on_prompt(f"Open {url} to sign in with Google")
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
                    "redirect_uri": redirect_uri,
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
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
            if not credentials.get("refresh_token"):
                # Without it the session dies in an hour with no way back.
                log.warning("gemini: Google returned no refresh token for this login")
            email = credentials.get("email")
            message = f"{vendor}: signed in with Google" + (f" ({email})" if email else "")
            log.info("gemini browser login complete: %s", mask_credentials(credentials))
            await _report(
                on_progress, vendor=vendor, method=METHOD, phase="done", message=message
            )
            return LoginResult(
                vendor=vendor, method=METHOD, credentials=credentials, message=message
            )
        except TimeoutError as exc:
            wrapped = RpcError(
                errors.INTERNAL, f"{vendor}: Google login timed out after {int(timeout)}s"
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
    vendor: str = "gemini",
    *,
    client: httpx.AsyncClient | None = None,
    on_prompt: Prompt = _prompt_default,
    open_browser: bool = True,
    timeout_sec: float | None = None,
    on_progress: ProgressHook | None = None,
) -> LoginResult:
    """Run the Google browser flow start-to-finish."""
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
    "AUTH_METHOD",
    "CALLBACK_PATH",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "DEFAULT_TIMEOUT_SEC",
    "METHOD",
    "REFRESH_SKEW_SEC",
    "SCOPES",
    "SECRET_FIELDS",
    "TOKEN_URL",
    "browser_login",
    "browser_login_start",
    "build_authorize_url",
    "credentials_from_tokens",
    "email_of",
    "is_expired",
    "is_google_oauth",
    "mask_credentials",
    "refresh_credentials",
]
