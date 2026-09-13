"""CORE-codex-login: the Google browser login for the ``gemini`` vendor.

The existing ``google_adc`` login shells out to ``gcloud``; this is the one a
user with nothing but a browser can complete — authorization code + PKCE
against ``accounts.google.com`` on a loopback callback.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from snowpea_core.providers import google_oauth
from snowpea_core.server.errors import RpcError

_RealAsyncClient = httpx.AsyncClient


def _jwt(claims: dict[str, Any]) -> str:
    def segment(payload: dict[str, Any]) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )

    return f"{segment({'alg': 'none'})}.{segment(claims)}.sig"


def _client(handler: Any) -> httpx.AsyncClient:
    return _RealAsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


# ---------------------------------------------------------------------------
# authorize URL
# ---------------------------------------------------------------------------


def test_authorize_url_asks_for_offline_access_and_the_code_assist_scope() -> None:
    url = google_oauth.build_authorize_url("chal", "st4te", "http://localhost:5000/oauth2callback")
    parsed = urlparse(url)
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == google_oauth.AUTHORIZE_URL
    assert query["client_id"] == google_oauth.CLIENT_ID
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["state"] == "st4te"
    assert query["redirect_uri"] == "http://localhost:5000/oauth2callback"
    # Without both of these Google returns no refresh token at all.
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"
    assert query["scope"].split(" ") == list(google_oauth.SCOPES)
    assert "https://www.googleapis.com/auth/cloud-platform" in google_oauth.SCOPES


def test_the_client_is_the_public_gemini_cli_installed_app() -> None:
    assert google_oauth.CLIENT_ID.endswith(".apps.googleusercontent.com")
    assert google_oauth.CLIENT_SECRET.startswith("GOCSPX-")
    assert google_oauth.TOKEN_URL == "https://oauth2.googleapis.com/token"


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def test_credentials_carry_the_email_from_the_id_token() -> None:
    credentials = google_oauth.credentials_from_tokens(
        {
            "access_token": "ya29.at",
            "refresh_token": "1//rt",
            "expires_in": 3599,
            "id_token": _jwt({"email": "user@example.com", "email_verified": True}),
        },
        now=lambda: 100.0,
    )
    assert credentials["auth_method"] == "google_oauth"
    assert credentials["access_token"] == "ya29.at"
    assert credentials["refresh_token"] == "1//rt"
    assert credentials["expires_at"] == 3699.0
    assert credentials["email"] == "user@example.com"


def test_a_broken_id_token_costs_the_email_but_not_the_login() -> None:
    credentials = google_oauth.credentials_from_tokens(
        {"access_token": "at", "id_token": "not-a-jwt"}
    )
    assert credentials["access_token"] == "at"
    assert "email" not in credentials


def test_missing_access_token_is_an_error() -> None:
    with pytest.raises(RpcError):
        google_oauth.credentials_from_tokens({"refresh_token": "rt"})


def test_mask_credentials_keeps_the_identifying_fields() -> None:
    masked = google_oauth.mask_credentials(
        {
            "auth_method": "google_oauth",
            "access_token": "at",
            "refresh_token": "rt",
            "email": "user@example.com",
            "project_id": "proj-1",
        }
    )
    assert masked == {
        "auth_method": "google_oauth",
        "access_token": "***",
        "refresh_token": "***",
        "email": "user@example.com",
        "project_id": "proj-1",
    }


def test_is_google_oauth_needs_the_method_and_a_token() -> None:
    assert google_oauth.is_google_oauth({"auth_method": "google_oauth", "access_token": "a"})
    assert not google_oauth.is_google_oauth({"auth_method": "google_adc"})
    assert not google_oauth.is_google_oauth({"api_key": "AIza..."})
    assert not google_oauth.is_google_oauth(None)


def test_is_expired_honours_the_skew() -> None:
    now = lambda: 1_000.0  # noqa: E731
    assert google_oauth.is_expired({"access_token": "a", "expires_at": 1_060.0}, now=now)
    assert not google_oauth.is_expired({"access_token": "a", "expires_at": 5_000.0}, now=now)
    assert google_oauth.is_expired({}, now=now)


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


async def test_refresh_sends_the_client_secret_and_keeps_the_refresh_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == google_oauth.TOKEN_URL
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json={"access_token": "at2", "expires_in": 3599})

    async with _client(handler) as client:
        fresh = await google_oauth.refresh_credentials(
            {
                "auth_method": "google_oauth",
                "access_token": "at1",
                "refresh_token": "rt1",
                "email": "user@example.com",
            },
            client=client,
            now=lambda: 0.0,
        )
    assert seen["grant_type"] == "refresh_token"
    assert seen["client_id"] == google_oauth.CLIENT_ID
    assert seen["client_secret"] == google_oauth.CLIENT_SECRET
    assert fresh["access_token"] == "at2"
    # Google never re-sends the refresh token; losing it would end the session.
    assert fresh["refresh_token"] == "rt1"
    assert fresh["email"] == "user@example.com"
    assert fresh["expires_at"] == 3599.0


async def test_refresh_without_a_refresh_token_asks_for_a_new_login() -> None:
    with pytest.raises(RpcError, match="no refresh token"):
        await google_oauth.refresh_credentials({"access_token": "at"})


async def test_refresh_surfaces_a_revoked_grant() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with _client(handler) as client:
        with pytest.raises(RpcError, match="invalid_grant"):
            await google_oauth.refresh_credentials(
                {"access_token": "at", "refresh_token": "rt"}, client=client
            )


# ---------------------------------------------------------------------------
# the full browser round trip
# ---------------------------------------------------------------------------


async def _browser_login(*, token_handler: Any, callback: Any) -> Any:
    phases: list[dict[str, Any]] = []

    async def on_progress(params: dict[str, Any]) -> None:
        phases.append(params)

    async with _client(token_handler) as client:
        started = await google_oauth.browser_login_start(
            client=client,
            open_browser=False,
            on_prompt=lambda _message: None,
            on_progress=on_progress,
            now=lambda: 0.0,
        )
        task = asyncio.create_task(started.finish())
        await asyncio.sleep(0)
        page = await callback(started.verification_uri or "")
        try:
            result = await asyncio.wait_for(task, timeout=5.0)
        except Exception:
            task.cancel()
            raise
    return started, result, page, phases


def _redirect_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["redirect_uri"][0]


async def _hit_callback(url: str, **overrides: Any) -> Any:
    params = {"code": "the-code", "state": parse_qs(urlparse(url).query)["state"][0]}
    params.update(overrides)
    async with _RealAsyncClient(timeout=5.0) as browser:
        return await browser.get(_redirect_of(url), params=params)


async def test_browser_login_round_trip_on_an_ephemeral_port() -> None:
    seen: dict[str, str] = {}

    def token_handler(request: httpx.Request) -> httpx.Response:
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.at",
                "refresh_token": "1//rt",
                "expires_in": 3599,
                "id_token": _jwt({"email": "user@example.com"}),
            },
        )

    started, result, page, phases = await _browser_login(
        token_handler=token_handler, callback=_hit_callback
    )

    redirect = _redirect_of(started.verification_uri or "")
    # Google accepts any loopback port, so the OS picks a free one.
    assert redirect.startswith("http://127.0.0.1:") and redirect.endswith("/oauth2callback")
    assert int(urlparse(redirect).port or 0) > 0
    assert seen["grant_type"] == "authorization_code"
    assert seen["code"] == "the-code"
    assert seen["redirect_uri"] == redirect
    assert seen["client_secret"] == google_oauth.CLIENT_SECRET
    challenge = parse_qs(urlparse(started.verification_uri or "").query)["code_challenge"][0]
    digest = hashlib.sha256(seen["code_verifier"].encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    assert result.method == "google_oauth"
    assert result.credentials["email"] == "user@example.com"
    assert result.credentials["refresh_token"] == "1//rt"
    assert page.status_code == 200 and "Signed in" in page.text
    assert [params["phase"] for params in phases] == ["started", "await_user", "polling", "done"]


async def test_browser_login_rejects_a_mismatched_state() -> None:
    def token_handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("the code must never be exchanged")

    async def bad_state(url: str) -> Any:
        return await _hit_callback(url, state="not-the-state")

    with pytest.raises(RpcError, match="state did not match"):
        await _browser_login(token_handler=token_handler, callback=bad_state)


async def test_browser_login_reports_a_refused_consent() -> None:
    def token_handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("the code must never be exchanged")

    async def refused(url: str) -> Any:
        async with _RealAsyncClient(timeout=5.0) as browser:
            return await browser.get(_redirect_of(url), params={"error": "access_denied"})

    with pytest.raises(RpcError, match="access_denied"):
        await _browser_login(token_handler=token_handler, callback=refused)


async def test_browser_login_surfaces_a_failed_exchange() -> None:
    def token_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(RpcError, match="invalid_grant"):
        await _browser_login(token_handler=token_handler, callback=_hit_callback)
