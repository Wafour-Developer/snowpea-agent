"""CORE-codex-login: the ChatGPT browser (PKCE) login for the ``openai`` vendor.

The device-code flow already covered by ``tests/test_login_web.py`` stays the
headless fallback; this module covers the flow a user on a real desktop gets:
authorization code + PKCE against ``auth.openai.com``, a ``localhost:1455``
callback, and the ChatGPT credentials the exchange produces.
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

from snowpea_core.providers import openai_oauth
from snowpea_core.providers.auth_web import new_pkce_pair
from snowpea_core.server.errors import RpcError

#: Captured before any test patches httpx.
_RealAsyncClient = httpx.AsyncClient


def _jwt(claims: dict[str, Any]) -> str:
    """A structurally valid (unsigned) JWT carrying ``claims``."""

    def segment(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment(claims)}.signature"


def _id_token(account: str = "acct_123", plan: str = "plus") -> str:
    return _jwt(
        {
            "sub": "user_1",
            "email": "user@example.com",
            openai_oauth.AUTH_CLAIM: {
                "chatgpt_account_id": account,
                "chatgpt_plan_type": plan,
            },
        }
    )


def _client(handler: Any) -> httpx.AsyncClient:
    return _RealAsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


# ---------------------------------------------------------------------------
# PKCE parameters
# ---------------------------------------------------------------------------


def test_pkce_challenge_is_the_s256_of_the_verifier() -> None:
    verifier, challenge = new_pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert challenge == expected
    assert "=" not in challenge and len(verifier) >= 43


def test_authorize_url_carries_every_parameter_codex_requires() -> None:
    url = openai_oauth.build_authorize_url("chal", "st4te")
    parsed = urlparse(url)
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == openai_oauth.AUTHORIZE_URL
    assert query == {
        "response_type": "code",
        "client_id": openai_oauth.CLIENT_ID,
        "redirect_uri": openai_oauth.REDIRECT_URI,
        "scope": openai_oauth.SCOPES,
        "code_challenge": "chal",
        "code_challenge_method": "S256",
        "state": "st4te",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": openai_oauth.AUTH_ORIGINATOR,
    }
    assert openai_oauth.REDIRECT_URI == "http://localhost:1455/auth/callback"


# ---------------------------------------------------------------------------
# JWT claims
# ---------------------------------------------------------------------------


def test_account_claims_read_the_namespaced_auth_claim() -> None:
    assert openai_oauth.account_claims(_id_token("acct_9", "pro")) == ("acct_9", "pro")


def test_account_claims_tolerate_a_token_without_the_claim() -> None:
    assert openai_oauth.account_claims(_jwt({"sub": "u"})) == (None, None)


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b", "a.!!!.c", "a." + "e30" + "x.c"])
def test_decode_jwt_claims_rejects_malformed_tokens(token: str) -> None:
    with pytest.raises(ValueError):
        openai_oauth.decode_jwt_claims(token)


def test_account_id_of_never_raises_on_a_broken_token() -> None:
    assert openai_oauth.account_id_of("garbage") is None


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def test_credentials_from_tokens_shape() -> None:
    credentials = openai_oauth.credentials_from_tokens(
        {
            "access_token": "at",
            "refresh_token": "rt",
            "id_token": _id_token(),
            "expires_in": 3600,
        },
        now=lambda: 1_000.0,
    )
    assert credentials == {
        "auth_method": "chatgpt",
        "access_token": "at",
        "refresh_token": "rt",
        "id_token": _id_token(),
        "expires_at": 4_600.0,
        "account_id": "acct_123",
        "plan_type": "plus",
    }


def test_credentials_without_an_access_token_are_an_error() -> None:
    with pytest.raises(RpcError):
        openai_oauth.credentials_from_tokens({"refresh_token": "rt"})


def test_mask_credentials_hides_every_secret_but_keeps_the_rest() -> None:
    masked = openai_oauth.mask_credentials(
        {
            "auth_method": "chatgpt",
            "access_token": "at",
            "refresh_token": "rt",
            "id_token": "it",
            "account_id": "acct_123",
            "plan_type": "plus",
        }
    )
    assert masked == {
        "auth_method": "chatgpt",
        "access_token": "***",
        "refresh_token": "***",
        "id_token": "***",
        "account_id": "acct_123",
        "plan_type": "plus",
    }


def test_is_expired_uses_the_skew_and_treats_unknown_expiry_as_valid() -> None:
    now = lambda: 1_000.0  # noqa: E731
    assert openai_oauth.is_expired({"access_token": "a", "expires_at": 1_050.0}, now=now)
    assert not openai_oauth.is_expired({"access_token": "a", "expires_at": 2_000.0}, now=now)
    assert not openai_oauth.is_expired({"access_token": "a"}, now=now)
    assert openai_oauth.is_expired({}, now=now)


def test_is_chatgpt_auth_needs_both_the_method_and_a_token() -> None:
    assert openai_oauth.is_chatgpt_auth({"auth_method": "chatgpt", "access_token": "a"})
    assert not openai_oauth.is_chatgpt_auth({"auth_method": "chatgpt"})
    assert not openai_oauth.is_chatgpt_auth({"api_key": "sk-..."})
    assert not openai_oauth.is_chatgpt_auth(None)


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


async def test_refresh_rotates_the_access_token_and_keeps_the_refresh_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(
            200, json={"access_token": "at2", "id_token": _id_token(), "expires_in": 3600}
        )

    async with _client(handler) as client:
        fresh = await openai_oauth.refresh_credentials(
            {"auth_method": "chatgpt", "access_token": "at1", "refresh_token": "rt1"},
            client=client,
            now=lambda: 10.0,
        )
    assert seen["grant_type"] == "refresh_token"
    assert seen["refresh_token"] == "rt1"
    assert seen["client_id"] == openai_oauth.CLIENT_ID
    assert fresh["access_token"] == "at2"
    # Rotation is optional; the old refresh token must survive its absence.
    assert fresh["refresh_token"] == "rt1"
    assert fresh["expires_at"] == 3_610.0


async def test_refresh_adopts_a_rotated_refresh_token() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at2", "refresh_token": "rt2"})

    async with _client(handler) as client:
        fresh = await openai_oauth.refresh_credentials(
            {"access_token": "at1", "refresh_token": "rt1"}, client=client
        )
    assert fresh["refresh_token"] == "rt2"


async def test_refresh_without_a_refresh_token_asks_for_a_new_login() -> None:
    with pytest.raises(RpcError, match="no refresh token"):
        await openai_oauth.refresh_credentials({"access_token": "at"})


async def test_refresh_surfaces_the_server_detail() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error_description": "refresh token expired"})

    async with _client(handler) as client:
        with pytest.raises(RpcError, match="refresh token expired"):
            await openai_oauth.refresh_credentials(
                {"access_token": "at", "refresh_token": "rt"}, client=client
            )


# ---------------------------------------------------------------------------
# the full browser round trip
# ---------------------------------------------------------------------------


async def _browser_login(
    *,
    token_handler: Any,
    callback: Any,
) -> Any:
    """Run ``browser_login_start``, drive the callback, return ``finish()``.

    Skips when port 1455 is occupied: OpenAI registered exactly that redirect,
    so the flow cannot move to another port.
    """
    phases: list[dict[str, Any]] = []

    async def on_progress(params: dict[str, Any]) -> None:
        phases.append(params)

    async with _client(token_handler) as client:
        try:
            started = await openai_oauth.browser_login_start(
                client=client,
                open_browser=False,
                on_prompt=lambda _message: None,
                on_progress=on_progress,
                now=lambda: 0.0,
            )
        except RpcError as exc:  # pragma: no cover - depends on the machine
            if "already in use" in str(exc):
                pytest.skip("port 1455 is busy on this machine")
            raise
        task = asyncio.create_task(started.finish())
        await asyncio.sleep(0)
        response = await callback(started.verification_uri or "")
        try:
            result = await asyncio.wait_for(task, timeout=5.0)
        except Exception:
            task.cancel()
            raise
    return started, result, response, phases


def _state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


async def _hit_callback(url: str, *, code: str = "the-code", state: str | None = None) -> Any:
    params = {"code": code, "state": state if state is not None else _state_of(url)}
    async with _RealAsyncClient(timeout=5.0) as browser:
        return await browser.get(openai_oauth.REDIRECT_URI, params=params)


async def test_browser_login_round_trip_exchanges_the_code_for_chatgpt_credentials() -> None:
    seen: dict[str, str] = {}

    def token_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == openai_oauth.TOKEN_URL
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "id_token": _id_token("acct_7", "pro"),
                "expires_in": 3600,
            },
        )

    started, result, page, phases = await _browser_login(
        token_handler=token_handler, callback=_hit_callback
    )

    assert seen["grant_type"] == "authorization_code"
    assert seen["code"] == "the-code"
    assert seen["redirect_uri"] == openai_oauth.REDIRECT_URI
    assert seen["client_id"] == openai_oauth.CLIENT_ID
    # The verifier that was sent must match the challenge in the authorize URL.
    challenge = parse_qs(urlparse(started.verification_uri or "").query)["code_challenge"][0]
    digest = hashlib.sha256(seen["code_verifier"].encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    assert result.method == openai_oauth.METHOD
    assert result.credentials["auth_method"] == "chatgpt"
    assert result.credentials["account_id"] == "acct_7"
    assert result.credentials["plan_type"] == "pro"
    assert result.credentials["expires_at"] == 3600.0
    assert page.status_code == 200 and "Signed in" in page.text
    assert [params["phase"] for params in phases] == [
        "started",
        "await_user",
        "polling",
        "done",
    ]
    assert phases[1]["verificationUri"] == started.verification_uri


async def test_browser_login_rejects_a_callback_whose_state_does_not_match() -> None:
    def token_handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("the code must never be exchanged")

    async def bad_state(url: str) -> Any:
        return await _hit_callback(url, state="not-the-state")

    with pytest.raises(RpcError, match="state did not match"):
        await _browser_login(token_handler=token_handler, callback=bad_state)


async def test_browser_login_reports_a_refused_consent() -> None:
    def token_handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("the code must never be exchanged")

    async def refused(_url: str) -> Any:
        async with _RealAsyncClient(timeout=5.0) as browser:
            return await browser.get(openai_oauth.REDIRECT_URI, params={"error": "access_denied"})

    with pytest.raises(RpcError, match="access_denied"):
        await _browser_login(token_handler=token_handler, callback=refused)


async def test_browser_login_surfaces_a_failed_token_exchange() -> None:
    def token_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(RpcError, match="invalid_grant"):
        await _browser_login(token_handler=token_handler, callback=_hit_callback)


async def test_the_callback_port_is_released_after_a_login() -> None:
    """Two logins in a row must not collide on port 1455."""

    def token_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt"})

    for _ in range(2):
        _started, result, _page, _phases = await _browser_login(
            token_handler=token_handler, callback=_hit_callback
        )
        assert result.credentials["access_token"] == "at"
