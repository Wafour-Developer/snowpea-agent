"""CORE-codex-login phase B: an OAuth session reaches the backend that accepts it.

Signing in with a ChatGPT or Google account does not produce an API key, so
these vendors cannot be served by the adapter their preset names.  This module
covers the routing, the credential precedence that used to defeat it, and the
token lifecycle the registry and the wizard now share.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.providers import auth_web
from snowpea_core.providers.codex_transport import CodexProvider
from snowpea_core.providers.gemini_codeassist_transport import CodeAssistProvider
from snowpea_core.providers.gemini_native import GeminiProvider
from snowpea_core.providers.openai_compat import OpenAICompatProvider
from snowpea_core.providers.registry import ProviderRegistry
from snowpea_core.server.errors import ERROR_CODES, RpcError


def _jwt(account: str = "acct_1") -> str:
    def segment(payload: dict[str, Any]) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )

    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": account}}
    return f"{segment({'alg': 'none'})}.{segment(claims)}.sig"


def _registry(providers: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> ProviderRegistry:
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    settings.providers.update(providers)
    return ProviderRegistry(settings)


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def test_a_chatgpt_session_is_served_by_the_codex_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(
        {"openai": {"auth_method": "chatgpt", "access_token": _jwt(), "refresh_token": "rt"}},
        monkeypatch,
    )
    provider = registry.get("openai")
    assert isinstance(provider, CodexProvider)
    # ``gpt-4.1`` is an API-key model id; Codex serves its own set.
    from snowpea_core.providers.codex_transport import CODEX_MODELS

    assert provider.model in CODEX_MODELS


def test_a_device_code_session_routes_there_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The headless flow signs into the same account, so it must route the same."""
    registry = _registry({"openai": {"token": _jwt("acct_9"), "refresh_token": "rt"}}, monkeypatch)
    assert isinstance(registry.get("openai"), CodexProvider)


def test_an_api_key_still_gets_the_openai_compatible_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry({"openai": {"api_key": "sk-proj-real"}}, monkeypatch)
    assert isinstance(registry.get("openai"), OpenAICompatProvider)


def test_a_google_session_is_served_by_code_assist(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry(
        {
            "gemini": {
                "auth_method": "google_oauth",
                "access_token": "ya29.at",
                "refresh_token": "1//rt",
                "project_id": "proj-1",
            }
        },
        monkeypatch,
    )
    provider = registry.get("gemini")
    assert isinstance(provider, CodeAssistProvider)
    assert provider.project_id == "proj-1"


def test_a_gemini_api_key_still_gets_the_native_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry({"gemini": {"api_key": "AIza-real"}}, monkeypatch)
    assert isinstance(registry.get("gemini"), GeminiProvider)


def test_a_refreshed_token_is_written_back_to_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(
        {"openai": {"auth_method": "chatgpt", "access_token": _jwt(), "refresh_token": "rt"}},
        monkeypatch,
    )
    provider = registry.get("openai")
    assert isinstance(provider, CodexProvider)
    provider._on_credentials({"auth_method": "chatgpt", "access_token": "fresh"})  # type: ignore[misc]  # noqa: SLF001
    assert registry.vendor_config("openai")["access_token"] == "fresh"


# ---------------------------------------------------------------------------
# credential precedence (report §6.7 A-P1-2)
# ---------------------------------------------------------------------------


def test_a_stale_api_key_no_longer_overrides_an_oauth_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug: the login succeeded, ``auth_method`` said OAuth, and every
    request kept using the API key left over from an earlier setup."""
    registry = _registry(
        {
            "gemini": {
                "api_key": "AIza-OLD-KEY",
                "auth_method": "google_oauth",
                "access_token": "ya29.at",
            }
        },
        monkeypatch,
    )
    assert registry.api_key_for("gemini") is None
    assert isinstance(registry.get("gemini"), CodeAssistProvider)


def test_an_api_key_wins_again_once_the_oauth_method_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry({"gemini": {"api_key": "AIza-real"}}, monkeypatch)
    assert registry.api_key_for("gemini") == "AIza-real"


def test_a_login_result_clears_the_credentials_it_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``configure`` deletes a field whose incoming value is ``None`` — which
    is how a login removes the key of the method it supersedes."""
    registry = _registry({"openai": {"api_key": "sk-old"}}, monkeypatch)
    registry.configure(
        "openai", {"auth_method": "chatgpt", "access_token": _jwt(), "api_key": None}
    )
    assert "api_key" not in registry.vendor_config("openai")
    assert registry.api_key_for("openai") is None


# ---------------------------------------------------------------------------
# lifecycle: expiry, status, models
# ---------------------------------------------------------------------------


def test_auth_status_reports_expired_instead_of_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(
        {
            "openai": {
                "auth_method": "chatgpt",
                "access_token": _jwt(),
                "refresh_token": "rt",
                "expires_at": time.time() - 1,
            }
        },
        monkeypatch,
    )
    assert registry.is_configured("openai")
    assert registry.auth_status("openai") == "expired"
    info = next(i for i in registry.list() if i.vendor == "openai")
    assert info.configured is True
    assert info.authStatus == "expired"


def test_a_token_inside_the_refresh_skew_already_counts_as_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(
        {
            "openai": {
                "auth_method": "chatgpt",
                "access_token": _jwt(),
                "expires_at": time.time() + 30,
            }
        },
        monkeypatch,
    )
    assert registry.auth_status("openai") == "expired"


def test_a_live_session_and_an_api_key_are_both_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(
        {
            "openai": {
                "auth_method": "chatgpt",
                "access_token": _jwt(),
                "expires_at": time.time() + 3600,
            },
            "anthropic": {"api_key": "sk-ant"},
        },
        monkeypatch,
    )
    assert registry.auth_status("openai") == "active"
    assert registry.auth_status("anthropic") == "active"
    assert registry.auth_status("kimi") == "unconfigured"


async def test_model_listing_for_an_oauth_account_never_calls_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery right after an OAuth login used to fire an unauthenticated
    request and show ``could not list models (… 401 …)`` (report A-P2-1)."""
    from snowpea_core.providers import models as model_discovery

    async def explode(*_args: Any, **_kwargs: Any) -> list[str]:
        raise AssertionError("no HTTP call may be made for an OAuth account")

    monkeypatch.setattr(model_discovery, "list_models", explode)
    registry = _registry(
        {
            "openai": {"auth_method": "chatgpt", "access_token": _jwt()},
            "gemini": {"auth_method": "google_oauth", "access_token": "ya29.at"},
        },
        monkeypatch,
    )
    assert "gpt-5-codex" in await registry.list_models("openai")
    assert "gemini-2.5-pro" in await registry.list_models("gemini")


def test_auth_expired_is_a_wire_error_code() -> None:
    assert "auth_expired" in ERROR_CODES


# ---------------------------------------------------------------------------
# which flow a machine gets
# ---------------------------------------------------------------------------


def test_a_desktop_gets_the_browser_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(auth_web.FORCE_HEADLESS_ENV, raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(auth_web.sys, "platform", "linux")
    assert auth_web.browser_available()
    assert auth_web.default_method("openai") == "browser_pkce"
    assert auth_web.default_method("gemini") == "google_oauth"


@pytest.mark.parametrize(
    "env",
    [
        {"SSH_CONNECTION": "10.0.0.1 22 10.0.0.2 22"},
        {"SSH_TTY": "/dev/pts/0"},
        {auth_web.FORCE_HEADLESS_ENV: "1"},
    ],
)
def test_a_machine_without_a_usable_browser_gets_the_headless_flow(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consent page opened where nobody can see it is worse than a code."""
    for name in ("SSH_CONNECTION", "SSH_TTY", auth_web.FORCE_HEADLESS_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(auth_web.sys, "platform", "linux")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert not auth_web.browser_available()
    assert auth_web.default_method("openai") == "device_code"
    assert auth_web.default_method("gemini") == "google_adc"


def test_a_linux_session_with_no_display_gets_the_headless_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("SSH_CONNECTION", "SSH_TTY", auth_web.FORCE_HEADLESS_ENV, "DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(auth_web.sys, "platform", "linux")
    assert auth_web.default_method("openai") == "device_code"


def test_an_explicit_method_is_honoured_and_an_unknown_one_refused() -> None:
    assert auth_web.method_for("openai", "device_code") == "device_code"
    assert auth_web.method_for("openai", "browser_pkce") == "browser_pkce"
    assert auth_web.method_for("gemini", "google_adc") == "google_adc"
    with pytest.raises(RpcError, match="openrouter supports oauth_pkce"):
        auth_web.method_for("openrouter", "device_code")
    # An API-key-only vendor still gets the API-key instructions, not a flow.
    with pytest.raises(RpcError, match="has no browser login") as refused:
        auth_web.method_for("anthropic")
    assert refused.value.code == "login_unsupported"
