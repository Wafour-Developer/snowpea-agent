"""CORE-codex-login: the Codex Responses transport used by ChatGPT accounts.

A ChatGPT OAuth session cannot talk to ``api.openai.com``; these turns go to
``chatgpt.com/backend-api/codex/responses`` instead.  The recorded events in
``tests/fixtures/providers/openai-codex/tool_call_once.json`` are replayed
through :class:`CodexProvider` so request building, SSE parsing and
normalisation are the real code — only the socket is fake.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from snowpea_core.providers import codex_transport, openai_oauth, replay
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent, ToolCall, ToolSpec

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "providers"
    / "openai-codex"
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


def _jwt_access_token(account: str = "acct_123") -> str:
    def segment(payload: dict[str, Any]) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )

    claims = {openai_oauth.AUTH_CLAIM: {"chatgpt_account_id": account}}
    return f"{segment({'alg': 'none'})}.{segment(claims)}.sig"


def _credentials(**overrides: Any) -> dict[str, Any]:
    credentials = {
        "auth_method": "chatgpt",
        "access_token": _jwt_access_token(),
        "refresh_token": "rt",
        "plan_type": "pro",
    }
    credentials.update(overrides)
    return credentials


def _exchanges() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["exchanges"]


def _sse(chunks: list[Any]) -> bytes:
    return replay.sse_bytes(chunks, "openai")


def _provider(handler: Any, **kwargs: Any) -> codex_transport.CodexProvider:
    return codex_transport.CodexProvider(
        kwargs.pop("credentials", _credentials()),
        model=kwargs.pop("model", "gpt-5-codex"),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def drain(provider: Any, messages: list[ChatMessage]) -> list[StreamEvent]:
    return [event async for event in provider.stream(messages, [SHELL], max_tokens=4096)]


def kinds(events: list[StreamEvent]) -> list[str]:
    return [event.kind for event in events]


# ---------------------------------------------------------------------------
# request building
# ---------------------------------------------------------------------------


def test_request_is_a_responses_body_with_instructions_and_input_items() -> None:
    provider = _provider(lambda _request: httpx.Response(200))
    body = provider.build_request(FIRST_TURN, [SHELL], max_tokens=4096)
    assert body["model"] == "gpt-5-codex"
    assert body["instructions"] == "You are Snowpea."
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "list the files"}],
        }
    ]
    assert body["stream"] is True
    # The Codex backend keeps no server-side state for us.
    assert body["store"] is False
    # The ChatGPT backend refuses the parameter; the budget must never be sent.
    assert "max_output_tokens" not in body
    assert body["tools"] == [
        {
            "type": "function",
            "name": "shell",
            "description": "Run a shell command.",
            "parameters": SHELL.input_schema,
            "strict": False,
        }
    ]
    assert body["tool_choice"] == "auto"
    assert body["reasoning"] == {"effort": "medium"}


def test_tool_calls_and_results_become_function_call_items() -> None:
    call = ToolCall(id="call_1", name="shell", arguments={"command": "ls"})
    messages = [
        *FIRST_TURN,
        ChatMessage(role="assistant", content="Checking the files.", tool_calls=[call]),
        ChatMessage(role="tool", content="a.txt", tool_call_id="call_1", name="shell"),
    ]
    provider = _provider(lambda _request: httpx.Response(200))
    items = provider.build_request(messages, [SHELL])["input"]
    assert items[1] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Checking the files."}],
    }
    assert items[2] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "shell",
        "arguments": '{"command": "ls"}',
    }
    assert items[3] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "a.txt",
    }


def test_a_model_without_reasoning_gets_no_reasoning_block() -> None:
    provider = _provider(lambda _request: httpx.Response(200), model="gpt-4.1")
    assert "reasoning" not in provider.build_request(FIRST_TURN, [])


def test_reasoning_effort_is_validated() -> None:
    assert _provider(lambda _r: httpx.Response(200), reasoning_effort="high").build_request(
        FIRST_TURN, []
    )["reasoning"] == {"effort": "high"}
    assert _provider(lambda _r: httpx.Response(200), reasoning_effort="nonsense").build_request(
        FIRST_TURN, []
    )["reasoning"] == {"effort": "medium"}


def test_headers_identify_the_account_and_the_harness() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, content=_sse([]), headers={"content-type": "text/event-stream"})

    provider = _provider(handler, session_id="sess-1")
    return_value = provider._headers()  # noqa: SLF001 - the header set is the contract
    assert return_value["authorization"].startswith("Bearer ")
    assert return_value["chatgpt-account-id"] == "acct_123"
    assert return_value["openai-beta"] == "responses=experimental"
    assert return_value["originator"] == codex_transport.ORIGINATOR
    assert return_value["session_id"] == "sess-1"


def test_account_id_is_read_from_the_token_when_not_stored() -> None:
    provider = _provider(
        lambda _r: httpx.Response(200),
        credentials={"auth_method": "chatgpt", "access_token": _jwt_access_token("acct_42")},
    )
    assert provider._headers()["chatgpt-account-id"] == "acct_42"  # noqa: SLF001


def test_a_session_without_an_access_token_is_refused_at_construction() -> None:
    with pytest.raises(ProviderError):
        codex_transport.CodexProvider({"auth_method": "chatgpt"})


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


async def test_golden_scenario_normalises_to_the_contract_order() -> None:
    exchanges = _exchanges()
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/responses")
        requests.append(json.loads(request.content))
        stream = exchanges[len(requests) - 1]["response_stream"]
        return httpx.Response(
            200,
            content=_sse(stream),
            headers={
                "content-type": "text/event-stream",
                "x-codex-primary-used-percent": "12",
            },
        )

    provider = _provider(handler)
    first = await drain(provider, FIRST_TURN)
    assert kinds(first) == ["text_delta", "text_delta", "tool_call", "usage", "done"]
    assert "".join(event.text for event in first if event.kind == "text_delta") == (
        "Checking the files."
    )
    call = first[2].tool_call
    assert call is not None
    assert (call.id, call.name, call.arguments) == ("call_1", "shell", {"command": "ls"})
    assert first[3].usage is not None and first[3].usage.input_tokens == 412
    assert first[-1].stop_reason == "tool_use"
    # Quota headers are surfaced rather than dropped.
    assert provider.rate_limits["x-codex-primary-used-percent"] == "12"

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
    assert requests[1]["input"][-1]["type"] == "function_call_output"


async def test_an_incomplete_response_stops_with_max_tokens() -> None:
    stream = [
        {"type": "response.output_text.delta", "delta": "half a th"},
        {
            "type": "response.incomplete",
            "response": {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 10, "output_tokens": 4096},
            },
        },
    ]
    provider = _provider(
        lambda _request: httpx.Response(
            200, content=_sse(stream), headers={"content-type": "text/event-stream"}
        )
    )
    events = await drain(provider, FIRST_TURN)
    assert kinds(events) == ["text_delta", "usage", "done"]
    assert events[-1].stop_reason == "max_tokens"


async def test_a_failed_response_becomes_a_provider_error() -> None:
    stream = [
        {
            "type": "response.failed",
            "response": {"status": "failed", "error": {"message": "usage limit reached"}},
        }
    ]
    provider = _provider(
        lambda _request: httpx.Response(
            200, content=_sse(stream), headers={"content-type": "text/event-stream"}
        )
    )
    with pytest.raises(ProviderError, match="usage limit reached"):
        await drain(provider, FIRST_TURN)


async def test_an_http_error_names_the_vendor_and_model() -> None:
    provider = _provider(lambda _request: httpx.Response(500, text="boom"))
    with pytest.raises(ProviderError, match=r"openai/codex \(gpt-5-codex\).*500"):
        await drain(provider, FIRST_TURN)


# ---------------------------------------------------------------------------
# 401 -> refresh -> retry
# ---------------------------------------------------------------------------


async def test_a_401_refreshes_the_token_once_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []
    saved: list[dict[str, Any]] = []

    async def fake_refresh(credentials: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {**credentials, "access_token": _jwt_access_token("acct_123") + "2"}

    monkeypatch.setattr(openai_oauth, "refresh_credentials", fake_refresh)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["authorization"])
        if len(attempts) == 1:
            return httpx.Response(401, json={"detail": "token expired"})
        return httpx.Response(
            200,
            content=_sse(
                [
                    {"type": "response.output_text.delta", "delta": "hi"},
                    {"type": "response.completed", "response": {"status": "completed"}},
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = _provider(handler, on_credentials=saved.append)
    events = await drain(provider, FIRST_TURN)
    assert kinds(events) == ["text_delta", "done"]
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    assert saved and saved[0]["access_token"].endswith("2")


async def test_a_second_401_asks_for_a_new_login(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_refresh(credentials: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {**credentials, "access_token": _jwt_access_token() + "2"}

    monkeypatch.setattr(openai_oauth, "refresh_credentials", fake_refresh)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"detail": "nope"})

    provider = _provider(handler)
    with pytest.raises(ProviderError) as raised:
        await drain(provider, FIRST_TURN)
    assert raised.value.code == "auth_expired"
    assert "provider login openai" in str(raised.value)
    assert calls == 2


async def test_a_401_without_a_refresh_token_does_not_retry() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="expired")

    provider = _provider(
        handler,
        credentials={"auth_method": "chatgpt", "access_token": _jwt_access_token()},
    )
    with pytest.raises(ProviderError, match="your ChatGPT login expired"):
        await drain(provider, FIRST_TURN)
    assert calls == 1


# ---------------------------------------------------------------------------
# model listing
# ---------------------------------------------------------------------------


def test_the_static_model_list_is_what_a_chatgpt_account_can_pick() -> None:
    models = codex_transport.list_models()
    assert codex_transport.DEFAULT_MODEL in models
    assert "gpt-5" in models and "o4-mini" in models
    assert models == list(codex_transport.CODEX_MODELS)


async def test_a_device_code_session_streams_without_conversion_by_the_caller() -> None:
    """A session stored by the device-code flow (``token``, no ``auth_method``)
    is the same ChatGPT session and must work unchanged."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {_jwt_access_token('acct_9')}"
        assert request.headers["chatgpt-account-id"] == "acct_9"
        return httpx.Response(
            200,
            content=_sse([{"type": "response.completed", "response": {"status": "completed"}}]),
            headers={"content-type": "text/event-stream"},
        )

    provider = _provider(
        handler, credentials={"token": _jwt_access_token("acct_9"), "refresh_token": "rt"}
    )
    assert kinds(await drain(provider, FIRST_TURN)) == ["done"]
