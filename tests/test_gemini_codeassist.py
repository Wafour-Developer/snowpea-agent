"""CORE-codex-login: the Code Assist transport a Google-OAuth Gemini account uses.

An OAuth account has no API key, so its turns go to
``cloudcode-pa.googleapis.com/v1internal:streamGenerateContent`` — the ordinary
Gemini wire format inside a ``{"request": ...}`` / ``{"response": ...}``
envelope — and the account may first have to be onboarded onto a project.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from snowpea_core.providers import gemini_codeassist_transport as code_assist
from snowpea_core.providers import google_oauth, replay
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent, ToolSpec

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "providers"
    / "gemini-codeassist"
    / "tool_call_once.json"
)

SHELL = ToolSpec(
    name="shell",
    description="Run a shell command.",
    input_schema={
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
)

FIRST_TURN = [
    ChatMessage(role="system", content="You are Snowpea."),
    ChatMessage(role="user", content="list the files"),
]


def _credentials(**overrides: Any) -> dict[str, Any]:
    credentials: dict[str, Any] = {
        "auth_method": "google_oauth",
        "access_token": "ya29.at",
        "refresh_token": "1//rt",
        "email": "user@example.com",
    }
    credentials.update(overrides)
    return credentials


def _exchanges() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["exchanges"]


def _sse(chunks: list[Any]) -> bytes:
    return replay.sse_bytes(chunks, "openai")


def _provider(handler: Any, **kwargs: Any) -> code_assist.CodeAssistProvider:
    kwargs.setdefault("project_id", "proj-1")

    async def _no_sleep(_seconds: float) -> None:
        return None

    return code_assist.CodeAssistProvider(
        kwargs.pop("credentials", _credentials()),
        model=kwargs.pop("model", "gemini-2.5-pro"),
        transport=httpx.MockTransport(handler),
        sleep=kwargs.pop("sleep", _no_sleep),
        **kwargs,
    )


async def drain(provider: Any, messages: list[ChatMessage]) -> list[StreamEvent]:
    return [event async for event in provider.stream(messages, [SHELL], max_tokens=4096)]


def kinds(events: list[StreamEvent]) -> list[str]:
    return [event.kind for event in events]


# ---------------------------------------------------------------------------
# the envelope
# ---------------------------------------------------------------------------


def test_request_wraps_a_plain_gemini_request() -> None:
    provider = _provider(lambda _request: httpx.Response(200))
    body = provider.build_request(FIRST_TURN, [SHELL], max_tokens=4096)
    assert body["model"] == "gemini-2.5-pro"
    assert body["project"] == "proj-1"
    inner = body["request"]
    # The inner body is exactly what the API-key adapter would have sent.
    assert inner["systemInstruction"] == {"parts": [{"text": "You are Snowpea."}]}
    assert inner["contents"][0]["role"] == "user"
    assert inner["generationConfig"] == {"maxOutputTokens": 4096}
    assert inner["tools"][0]["functionDeclarations"][0]["name"] == "shell"


def test_the_project_is_omitted_while_it_is_unknown() -> None:
    body = code_assist.wrap_request("gemini-2.5-flash", None, {"contents": []})
    assert body == {"model": "gemini-2.5-flash", "request": {"contents": []}}


def test_unwrap_chunk_accepts_both_envelopes() -> None:
    assert code_assist.unwrap_chunk({"response": {"candidates": []}}) == {"candidates": []}
    assert code_assist.unwrap_chunk({"candidates": []}) == {"candidates": []}


def test_a_session_without_an_access_token_is_refused_at_construction() -> None:
    with pytest.raises(ProviderError):
        code_assist.CodeAssistProvider({"auth_method": "google_oauth"})


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


async def test_golden_scenario_normalises_to_the_contract_order() -> None:
    exchanges = _exchanges()
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1internal:streamGenerateContent"
        assert request.url.params["alt"] == "sse"
        assert request.headers["authorization"] == "Bearer ya29.at"
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=_sse(exchanges[len(requests) - 1]["response_stream"]),
            headers={"content-type": "text/event-stream"},
        )

    provider = _provider(handler)
    first = await drain(provider, FIRST_TURN)
    assert kinds(first) == ["text_delta", "tool_call", "usage", "done"]
    assert first[0].text == "Checking the files."
    call = first[1].tool_call
    assert call is not None
    assert (call.name, call.arguments) == ("shell", {"command": "ls"})
    assert first[2].usage is not None and first[2].usage.input_tokens == 412
    assert first[-1].stop_reason == "tool_use"

    second = await drain(
        provider,
        [
            *FIRST_TURN,
            ChatMessage(role="assistant", content="Checking the files.", tool_calls=[call]),
            ChatMessage(role="tool", content="a.txt\nb.txt\nc.txt", tool_call_id=call.id),
        ],
    )
    assert kinds(second) == ["text_delta", "usage", "done"]
    assert second[-1].stop_reason == "end_turn"
    assert len(requests) == 2


async def test_an_http_error_names_the_vendor_and_model() -> None:
    provider = _provider(lambda _request: httpx.Response(500, text="backend down"))
    with pytest.raises(ProviderError, match=r"gemini/code-assist \(gemini-2.5-pro\).*500"):
        await drain(provider, FIRST_TURN)


# ---------------------------------------------------------------------------
# onboarding
# ---------------------------------------------------------------------------


async def test_load_code_assist_supplies_the_project_when_the_account_has_one() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith(":loadCodeAssist"):
            body = json.loads(request.content)
            assert body["metadata"]["pluginType"] == "GEMINI"
            return httpx.Response(
                200,
                json={
                    "currentTier": {"id": "legacy-tier"},
                    "cloudaicompanionProject": "managed-project-7",
                },
            )
        raise AssertionError(f"unexpected call: {request.url.path}")

    saved: list[dict[str, Any]] = []
    provider = _provider(handler, project_id=None, on_credentials=saved.append)
    assert await provider.ensure_project() == "managed-project-7"
    assert provider.project_id == "managed-project-7"
    # Discovered once and written back, not rediscovered every turn.
    assert saved[0]["project_id"] == "managed-project-7"
    assert await provider.ensure_project() == "managed-project-7"
    assert calls == ["/v1internal:loadCodeAssist"]


async def test_a_free_tier_account_is_onboarded_and_the_operation_is_polled() -> None:
    calls: list[str] = []
    onboards = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal onboards
        calls.append(request.url.path)
        if request.url.path.endswith(":loadCodeAssist"):
            return httpx.Response(
                200,
                json={
                    "allowedTiers": [
                        {"id": "standard-tier"},
                        {"id": "free-tier", "isDefault": True},
                    ]
                },
            )
        if request.url.path.endswith(":onboardUser"):
            onboards += 1
            body = json.loads(request.content)
            assert body["tierId"] == "free-tier"
            # A free-tier user owns no project; naming one would be denied.
            assert "cloudaicompanionProject" not in body
            if onboards == 1:
                return httpx.Response(200, json={"done": False})
            return httpx.Response(
                200,
                json={
                    "done": True,
                    "response": {"cloudaicompanionProject": {"id": "free-project-9"}},
                },
            )
        raise AssertionError(f"unexpected call: {request.url.path}")

    provider = _provider(handler, project_id=None)
    assert await provider.ensure_project() == "free-project-9"
    assert onboards == 2
    assert calls[0].endswith(":loadCodeAssist")


async def test_onboarding_that_never_finishes_gives_up_with_a_clear_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":loadCodeAssist"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"done": False})

    provider = _provider(handler, project_id=None)
    with pytest.raises(ProviderError, match="onboarding did not finish"):
        await provider.ensure_project()


def test_tier_selection_prefers_the_current_tier_then_the_default() -> None:
    assert code_assist._tier_id({"currentTier": {"id": "paid"}}) == "paid"  # noqa: SLF001
    assert (  # noqa: SLF001
        code_assist._tier_id({"allowedTiers": [{"id": "a"}, {"id": "b", "isDefault": True}]}) == "b"
    )
    assert code_assist._tier_id({}) == code_assist.FREE_TIER_ID  # noqa: SLF001


# ---------------------------------------------------------------------------
# token renewal
# ---------------------------------------------------------------------------


async def test_an_expiring_token_is_refreshed_before_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_refresh(credentials: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {**credentials, "access_token": "ya29.fresh", "expires_at": 1e12}

    monkeypatch.setattr(google_oauth, "refresh_credentials", fake_refresh)
    seen: list[str] = []
    saved: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(
            200,
            content=_sse(
                [{"response": {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}}]
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = _provider(
        handler,
        credentials=_credentials(expires_at=0.0),
        on_credentials=saved.append,
    )
    events = await drain(provider, FIRST_TURN)
    assert kinds(events) == ["text_delta", "usage", "done"]
    # One request only: the stale token never reached the server.
    assert seen == ["Bearer ya29.fresh"]
    assert saved[0]["access_token"] == "ya29.fresh"


async def test_a_401_refreshes_once_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_refresh(credentials: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {**credentials, "access_token": "ya29.fresh"}

    monkeypatch.setattr(google_oauth, "refresh_credentials", fake_refresh)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if len(seen) == 1:
            return httpx.Response(401, text="Invalid Credentials")
        return httpx.Response(
            200,
            content=_sse(
                [{"response": {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}}]
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = _provider(handler)
    events = await drain(provider, FIRST_TURN)
    assert kinds(events) == ["text_delta", "usage", "done"]
    assert seen == ["Bearer ya29.at", "Bearer ya29.fresh"]


async def test_a_second_401_asks_for_a_new_login(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_refresh(credentials: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {**credentials, "access_token": "ya29.fresh"}

    monkeypatch.setattr(google_oauth, "refresh_credentials", fake_refresh)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="PERMISSION_DENIED")

    provider = _provider(handler)
    with pytest.raises(ProviderError, match="setup --login gemini"):
        await drain(provider, FIRST_TURN)


# ---------------------------------------------------------------------------
# model listing
# ---------------------------------------------------------------------------


def test_the_static_model_list_covers_what_the_cli_offers() -> None:
    models = code_assist.list_models()
    assert code_assist.DEFAULT_MODEL in models
    assert "gemini-2.5-flash" in models
    assert models == list(code_assist.CODE_ASSIST_MODELS)
