"""Codex Responses-API transport for ChatGPT-subscription accounts.

A ChatGPT/Codex OAuth session is *not* an API key: ``api.openai.com`` rejects
it, so a user who signed in with :mod:`snowpea_core.providers.openai_oauth`
cannot be served by :mod:`snowpea_core.providers.openai_compat`.  Their turns
go to the Codex backend instead — ``POST
https://chatgpt.com/backend-api/codex/responses``, the OpenAI *Responses* API,
authenticated with the OAuth access token plus the ``chatgpt-account-id`` of
the account that owns the subscription.

This adapter is a drop-in :class:`~snowpea_core.providers.base.ChatProvider`:
it converts ``ChatMessage``/``ToolSpec`` into Responses ``input`` items and
function ``tools``, streams the SSE event types back, and yields the same
``text_delta* → tool_call* → usage → done`` :class:`StreamEvent` sequence the
agent loop already consumes from every other vendor.

Behaviour specific to this backend:

* ``store: false`` — the Codex backend does not keep server-side state for us,
  so the whole conversation is re-sent as ``input`` on every turn;
* the access token is refreshed once, automatically, on a 401, and the fresh
  credentials are handed back to the caller through ``on_credentials`` so they
  reach ``settings.json``;
* there is no ``/models`` endpoint — :data:`CODEX_MODELS` is the static list.

Endpoint, header and account-claim details are ported from hermes-agent (MIT):
``agent/codex_headers.py`` and ``agent/transports/codex.py``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from snowpea_core import __version__ as _VERSION
from snowpea_core.providers import content as content_parts
from snowpea_core.providers import openai_oauth
from snowpea_core.providers.base import (
    ChatMessage,
    ProviderError,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
)
from snowpea_core.providers.normalize import parse_arguments, text_of
from snowpea_core.providers.openai_compat import sse_payloads
from snowpea_core.server.errors import AUTH_EXPIRED, RpcError

log = logging.getLogger("snowpea.providers.codex")

#: The Codex backend root; ``/responses`` hangs off it.
BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_TIMEOUT = 300.0
DEFAULT_MODEL = "gpt-5-codex"

#: How Snowpea identifies itself to the Codex backend.  OpenAI asks third-party
#: harnesses to say who they are; override with ``SNOWPEA_CODEX_ORIGINATOR`` if
#: the backend ever refuses an unknown originator.
ORIGINATOR = os.environ.get("SNOWPEA_CODEX_ORIGINATOR", "").strip() or "snowpea"
USER_AGENT = f"snowpea-agent/{_VERSION}"

#: The backend publishes no ``/models``; this is what a ChatGPT account can
#: select.  Reference: hermes-agent's Codex model metadata.
CODEX_MODELS: tuple[str, ...] = (
    "gpt-5-codex",
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "gpt-5",
    "gpt-5-mini",
    "o4-mini",
)

#: Models that accept a ``reasoning`` block.  Anything else would 400 on it.
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4", "codex")
REASONING_EFFORTS: tuple[str, ...] = ("minimal", "low", "medium", "high")
DEFAULT_REASONING_EFFORT = "medium"

#: Renew this long before the stored token expires.
REFRESH_SKEW_SEC = 60.0

#: Response headers worth surfacing: the ChatGPT plan's rolling quota.
_QUOTA_HEADER_PREFIXES = ("x-codex-", "x-ratelimit-")


class _Unauthorized(Exception):
    """Internal: the backend answered 401 before any event was streamed."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def supports_reasoning(model: str) -> bool:
    """True when ``model`` takes a ``reasoning`` request block."""
    name = (model or "").lower()
    return any(fragment in name for fragment in _REASONING_PREFIXES)


# ---------------------------------------------------------------------------
# request building
# ---------------------------------------------------------------------------


def tool_specs_to_responses(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """``ToolSpec`` list to the Responses ``tools`` array.

    The Responses shape is flat — ``{"type": "function", "name": ...}`` — where
    ``/chat/completions`` nests everything under ``function``.
    """
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema or {"type": "object", "properties": {}},
            "strict": False,
        }
        for tool in tools
    ]


def _content_items(message: ChatMessage, *, vision: bool) -> list[dict[str, Any]]:
    """A user turn's content as Responses ``input_text``/``input_image`` parts."""
    if not content_parts.has_blocks(message.content):
        return [{"type": "input_text", "text": text_of(message.content)}]
    parts = content_parts.parts_from_blocks(message.content)  # type: ignore[arg-type]
    converted = content_parts.to_openai(parts, vision=vision)
    if isinstance(converted, str):
        return [{"type": "input_text", "text": converted}]
    items: list[dict[str, Any]] = []
    for part in converted:
        if part.get("type") == "text":
            items.append({"type": "input_text", "text": str(part.get("text", ""))})
        elif part.get("type") == "image_url":
            url = str((part.get("image_url") or {}).get("url", ""))
            if url:
                items.append({"type": "input_image", "image_url": url})
    return items or [{"type": "input_text", "text": ""}]


def messages_to_responses(
    messages: list[ChatMessage], *, vision: bool = True
) -> tuple[str, list[dict[str, Any]]]:
    """``(instructions, input)`` for the Responses API.

    System turns become ``instructions`` (the Responses API has no system role);
    assistant tool calls become ``function_call`` items and tool results
    ``function_call_output`` items, which is how the backend re-associates a
    result with the call that asked for it.
    """
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            text = text_of(message.content)
            if text:
                instructions.append(text)
            continue
        if message.role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id or "",
                    "output": text_of(message.content) or "(no output)",
                }
            )
            continue
        if message.role == "assistant":
            text = text_of(message.content)
            if text:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
            for call in message.tool_calls or ():
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    }
                )
            continue
        items.append(
            {
                "type": "message",
                "role": "user",
                "content": _content_items(message, vision=vision),
            }
        )
    return "\n\n".join(instructions), items


# ---------------------------------------------------------------------------
# stream normalisation
# ---------------------------------------------------------------------------


class CodexStreamNormalizer:
    """Responses SSE events to :class:`StreamEvent`, in contract order.

    Kept separate from the HTTP code so the event vocabulary can be tested
    against recorded fixtures without a transport.
    """

    def __init__(self) -> None:
        self.usage: Usage | None = None
        self.stop_reason: str | None = None
        self.had_tool_calls = False
        self.error: str | None = None
        #: ``item_id`` of every ``function_call`` announced but not yet done.
        self._pending: set[str] = set()

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        kind = str(event.get("type") or "")
        if kind == "response.output_text.delta":
            delta = str(event.get("delta") or "")
            return [StreamEvent(kind="text_delta", text=delta)] if delta else []
        if kind == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                self._pending.add(str(item.get("id") or item.get("call_id") or ""))
            return []
        if kind == "response.output_item.done":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "function_call":
                return []
            self._pending.discard(str(item.get("id") or item.get("call_id") or ""))
            self.had_tool_calls = True
            return [
                StreamEvent(
                    kind="tool_call",
                    tool_call=ToolCall(
                        id=str(item.get("call_id") or item.get("id") or ""),
                        name=str(item.get("name") or ""),
                        arguments=parse_arguments(item.get("arguments")),
                    ),
                )
            ]
        if kind in ("response.completed", "response.incomplete"):
            response = event.get("response")
            response = response if isinstance(response, dict) else {}
            self.usage = _usage_of(response.get("usage"))
            if kind == "response.incomplete":
                reason = (response.get("incomplete_details") or {}).get("reason")
                self.stop_reason = "max_tokens" if reason == "max_output_tokens" else "error"
            else:
                self.stop_reason = "tool_use" if self.had_tool_calls else "end_turn"
            return []
        if kind in ("response.failed", "error"):
            self.error = _error_message(event)
            self.stop_reason = "error"
            return []
        return []

    def finish(self) -> list[StreamEvent]:
        """The trailing ``usage`` and ``done`` events."""
        if self._pending:
            log.warning("codex: %d tool call(s) never completed", len(self._pending))
        out: list[StreamEvent] = []
        if self.usage is not None:
            out.append(StreamEvent(kind="usage", usage=self.usage))
        reason = self.stop_reason or ("tool_use" if self.had_tool_calls else "end_turn")
        out.append(StreamEvent(kind="done", stop_reason=reason, error=self.error))
        return out


def _usage_of(raw: Any) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    return Usage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
    )


def _error_message(event: dict[str, Any]) -> str:
    """The most specific message a ``response.failed``/``error`` event carries."""
    candidates: list[Any] = [event.get("message"), event.get("error")]
    response = event.get("response")
    if isinstance(response, dict):
        candidates.append(response.get("error"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            message = candidate.get("message") or candidate.get("code")
            if message:
                return str(message)[:400]
        elif isinstance(candidate, str) and candidate:
            return candidate[:400]
    return "the Codex backend reported an error"


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------


class CodexProvider:
    """Streaming ``ChatProvider`` for a ChatGPT/Codex OAuth session."""

    vendor = "openai"
    #: No thinking switch on this backend; the agent loop does not offer one.
    supports_thinking_option = False

    def __init__(
        self,
        credentials: dict[str, Any],
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        reasoning_effort: str | None = None,
        session_id: str | None = None,
        originator: str = ORIGINATOR,
        on_credentials: Callable[[dict[str, Any]], Any] | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.credentials = openai_oauth.normalize_stored_credentials(credentials)
        self.model = model or DEFAULT_MODEL
        self._base_url = (base_url or BASE_URL).rstrip("/")
        self._timeout = timeout
        self._effort = (reasoning_effort or DEFAULT_REASONING_EFFORT).strip().lower()
        if self._effort not in REASONING_EFFORTS:
            self._effort = DEFAULT_REASONING_EFFORT
        self._session_id = session_id
        self._originator = originator
        self._on_credentials = on_credentials
        self._transport = transport
        #: Latest quota/rate-limit headers the backend sent, for the status line.
        self.rate_limits: dict[str, str] = {}
        if not self.credentials.get("access_token"):
            raise ProviderError(
                "invalid_params",
                "openai: no ChatGPT session stored; run `snowpea setup --login openai`",
            )

    def _tag(self) -> str:
        return f"openai/codex ({self.model})"

    # -- HTTP ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        access_token = str(self.credentials.get("access_token") or "")
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "authorization": f"Bearer {access_token}",
            "openai-beta": "responses=experimental",
            "originator": self._originator,
            "user-agent": USER_AGENT,
        }
        account_id = self.credentials.get("account_id") or openai_oauth.account_id_of(access_token)
        if account_id:
            headers["chatgpt-account-id"] = str(account_id)
        if self._session_id:
            headers["session_id"] = self._session_id
        return headers

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "headers": self._headers(),
            "timeout": self._timeout,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def build_request(
        self, messages: list[ChatMessage], tools: list[ToolSpec], *, max_tokens: int = 4096
    ) -> dict[str, Any]:
        """The ``POST /responses`` body for one turn."""
        vision = content_parts.supports_vision(self.vendor, self.model)
        instructions, items = messages_to_responses(messages, vision=vision)
        body: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": items,
            "stream": True,
            "store": False,
        }
        if tools:
            body["tools"] = tool_specs_to_responses(tools)
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = True
        # The ChatGPT Codex backend rejects ``max_output_tokens`` outright
        # ("Unsupported parameter"), and the Codex CLI itself never sends it,
        # so the budget the agent loop passes is accepted and ignored here.
        del max_tokens
        if supports_reasoning(self.model):
            body["reasoning"] = {"effort": self._effort}
        return body

    async def _refresh(self) -> None:
        """Renew the access token and hand the fresh credentials to the caller.

        A refresh that fails is the end of the session: it becomes
        ``auth_expired`` — a distinct code, so a surface can offer the login
        instead of reporting a malformed request.
        """
        log.info("codex: access token rejected, refreshing")
        try:
            self.credentials = await openai_oauth.refresh_credentials(self.credentials)
        except RpcError as exc:
            raise ProviderError(
                AUTH_EXPIRED,
                f"{self._tag()}: your ChatGPT login expired and could not be renewed "
                f"({exc.message}) — run `snowpea provider login openai` to sign in again",
            ) from exc
        if self._on_credentials is not None:
            result = self._on_credentials(dict(self.credentials))
            if result is not None and hasattr(result, "__await__"):
                await result

    def _note_quota(self, headers: httpx.Headers) -> None:
        seen = {
            name.lower(): value
            for name, value in headers.items()
            if name.lower().startswith(_QUOTA_HEADER_PREFIXES)
        }
        if seen:
            self.rate_limits = seen
            log.debug("codex quota: %s", seen)

    async def _stream_once(self, body: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        normalizer = CodexStreamNormalizer()
        async with self._client() as client:
            async with client.stream("POST", "/responses", json=body) as response:
                self._note_quota(response.headers)
                if response.status_code == 401:
                    detail = (await response.aread()).decode("utf-8", "replace")[:200]
                    raise _Unauthorized(detail)
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")[:400]
                    raise ProviderError(
                        "internal", f"{self._tag()}: HTTP {response.status_code}: {detail}"
                    )
                async for payload in sse_payloads(response):
                    for event in normalizer.feed(payload):
                        yield event
        if normalizer.error:
            raise ProviderError("internal", f"{self._tag()}: {normalizer.error}")
        for event in normalizer.finish():
            yield event

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one assistant turn from the Codex backend."""
        body = self.build_request(messages, tools, max_tokens=max_tokens)
        refreshed = False
        # A token that dies mid-request costs a whole turn; renew it first
        # when it is already inside the skew.
        if openai_oauth.is_expired(self.credentials, skew_sec=REFRESH_SKEW_SEC) and (
            self.credentials.get("refresh_token")
        ):
            await self._refresh()
            refreshed = True
        while True:
            try:
                async for event in self._stream_once(body):
                    yield event
                return
            except _Unauthorized as exc:
                # The 401 is raised before any event is yielded, so a retry
                # cannot duplicate output the caller has already seen.
                if refreshed or not self.credentials.get("refresh_token"):
                    raise ProviderError(
                        AUTH_EXPIRED,
                        f"{self._tag()}: your ChatGPT login expired "
                        f"({exc.detail or 'HTTP 401'}) — run "
                        f"`snowpea provider login openai` to sign in again",
                    ) from exc
                refreshed = True
                await self._refresh()
            except ProviderError:
                raise
            except httpx.HTTPError as exc:
                log.warning("%s stream failed: %s", self._tag(), exc)
                raise ProviderError(
                    "internal", f"{self._tag()}: {type(exc).__name__}: {exc}"
                ) from exc


def list_models() -> list[str]:
    """The models a ChatGPT account may select (no ``/models`` endpoint exists)."""
    return list(CODEX_MODELS)


__all__ = [
    "BASE_URL",
    "CODEX_MODELS",
    "DEFAULT_MODEL",
    "DEFAULT_REASONING_EFFORT",
    "DEFAULT_TIMEOUT",
    "ORIGINATOR",
    "REASONING_EFFORTS",
    "CodexProvider",
    "CodexStreamNormalizer",
    "list_models",
    "messages_to_responses",
    "supports_reasoning",
    "tool_specs_to_responses",
]
