"""Gemini Code Assist transport for Google-OAuth accounts (CORE-codex-login).

A Google account signed in through :mod:`snowpea_core.providers.google_oauth`
has no Gemini *API key*, so ``generativelanguage.googleapis.com`` — which
authenticates with ``x-goog-api-key`` — is not where its turns can go.  The
Gemini CLI sends them to the Code Assist API instead, and so does this
adapter::

    POST https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist
    POST https://cloudcode-pa.googleapis.com/v1internal:onboardUser
    POST https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse

The wire format inside is the ordinary Gemini one, wrapped: the request is
``{"model": ..., "project": ..., "request": <GenerateContentRequest>}`` and
every streamed chunk is ``{"response": <GenerateContentResponse>}``.  That is
the whole difference, so
:func:`~snowpea_core.providers.normalize.build_gemini_request` and
:class:`~snowpea_core.providers.normalize.GeminiStreamNormalizer` — the code
the API-key adapter already uses — do the actual translation here too, and the
agent loop sees the same :class:`StreamEvent` stream as every other vendor.

A free-tier account has no Cloud project of its own; ``loadCodeAssist``
either names the managed one or ``onboardUser`` provisions it (a long-running
operation that is polled).  The resulting ``project_id`` is handed back
through ``on_credentials`` so it is discovered once, not once per turn.

Endpoint, envelope and onboarding shapes follow the public Gemini CLI
(``packages/core/src/code_assist/{server,setup,converter}.ts``, Apache-2.0).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from snowpea_core.providers import google_oauth
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent, ToolSpec
from snowpea_core.providers.normalize import GeminiStreamNormalizer, build_gemini_request
from snowpea_core.providers.openai_compat import sse_payloads
from snowpea_core.providers.presets import PRESETS
from snowpea_core.server.errors import AUTH_EXPIRED, RpcError

log = logging.getLogger("snowpea.providers.gemini_codeassist")

BASE_URL = "https://cloudcode-pa.googleapis.com"
API_VERSION = "v1internal"
DEFAULT_TIMEOUT = 300.0
DEFAULT_MODEL = "gemini-2.5-pro"

#: What the Gemini CLI offers an OAuth account; the Code Assist API publishes
#: no model listing of its own.
CODE_ASSIST_MODELS: tuple[str, ...] = (
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

#: Sent with every onboarding call so Google's telemetry knows the client.
CLIENT_METADATA: dict[str, str] = {
    "ideType": "IDE_UNSPECIFIED",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
}

#: Tier used when ``loadCodeAssist`` offers no default of its own.
FREE_TIER_ID = "free-tier"

#: Renew this long before the stored token expires.
REFRESH_SKEW_SEC = 60.0

#: How long onboarding may take, and how often the operation is polled.
ONBOARD_TIMEOUT_SEC = 120.0
ONBOARD_POLL_SEC = 5.0


class _Unauthorized(Exception):
    """Internal: the API answered 401/403 before any event was streamed."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def wrap_request(
    model: str, project: str | None, request: dict[str, Any], *, user_prompt_id: str | None = None
) -> dict[str, Any]:
    """The Code Assist envelope around a plain ``GenerateContentRequest``."""
    body: dict[str, Any] = {"model": model, "request": request}
    if project:
        body["project"] = project
    if user_prompt_id:
        body["user_prompt_id"] = user_prompt_id
    return body


def unwrap_chunk(payload: dict[str, Any]) -> dict[str, Any]:
    """The ``GenerateContentResponse`` inside one streamed Code Assist chunk.

    Chunks are ``{"response": {...}}``; anything already unwrapped (which is
    what the plain Gemini endpoint sends) is passed straight through, so one
    normaliser can read both.
    """
    inner = payload.get("response")
    return inner if isinstance(inner, dict) else payload


class CodeAssistProvider:
    """Streaming ``ChatProvider`` for a Google-OAuth Gemini account."""

    vendor = "gemini"
    #: No thinking switch on this backend; the agent loop does not offer one.
    supports_thinking_option = False

    def __init__(
        self,
        credentials: dict[str, Any],
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        project_id: str | None = None,
        on_credentials: Callable[[dict[str, Any]], Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.credentials = dict(credentials or {})
        self.preset = PRESETS["gemini"]
        self.model = model or DEFAULT_MODEL
        self._base_url = (base_url or BASE_URL).rstrip("/")
        self._timeout = timeout
        self._project_id = project_id or str(self.credentials.get("project_id") or "") or None
        self._on_credentials = on_credentials
        self._transport = transport
        self._sleep = sleep
        if not self.credentials.get("access_token"):
            raise ProviderError(
                "invalid_params",
                "gemini: no Google session stored; run `snowpea setup --login gemini`",
            )

    def _tag(self) -> str:
        return f"gemini/code-assist ({self.model})"

    @property
    def project_id(self) -> str | None:
        """The Cloud project this account's turns are billed to, once known."""
        return self._project_id

    # -- HTTP ---------------------------------------------------------
    def _client(self, *, stream: bool) -> httpx.AsyncClient:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
            "authorization": f"Bearer {self.credentials.get('access_token')}",
        }
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "headers": headers,
            "timeout": self._timeout,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        """One non-streaming ``:method`` call, with the usual error mapping."""
        async with self._client(stream=False) as client:
            response = await client.post(f"/{API_VERSION}:{method}", json=body)
        if response.status_code in (401, 403):
            raise _Unauthorized(response.text[:200])
        if response.status_code >= 400:
            raise ProviderError(
                "internal",
                f"{self._tag()}: HTTP {response.status_code} from :{method}: "
                f"{response.text[:400]}",
            )
        payload = response.json() if response.content else {}
        return payload if isinstance(payload, dict) else {}

    async def _persist(self) -> None:
        if self._on_credentials is None:
            return
        result = self._on_credentials(dict(self.credentials))
        if result is not None and hasattr(result, "__await__"):
            await result

    async def _refresh(self) -> None:
        """Renew the access token and hand the fresh record to the caller."""
        log.info("code assist: access token rejected, refreshing")
        try:
            self.credentials = await google_oauth.refresh_credentials(self.credentials)
        except RpcError as exc:
            raise ProviderError(
                AUTH_EXPIRED,
                f"{self._tag()}: your Google login expired and could not be renewed "
                f"({exc.message}) — run `snowpea provider login gemini` to sign in again",
            ) from exc
        await self._persist()

    # -- onboarding ----------------------------------------------------
    async def ensure_project(self) -> str | None:
        """The Cloud project for this account, onboarding it if it has none.

        Free-tier accounts are assigned a Google-managed project, which is why
        the answer may legitimately stay ``None`` on some responses: the API
        then infers the project from the token.
        """
        if self._project_id:
            return self._project_id
        loaded = await self._call(
            "loadCodeAssist",
            {
                "cloudaicompanionProject": self._project_id,
                "metadata": dict(CLIENT_METADATA),
            },
        )
        project = _project_of(loaded.get("cloudaicompanionProject"))
        if project:
            return await self._remember_project(project)
        tier_id = _tier_id(loaded)
        operation = await self._call(
            "onboardUser",
            {
                "tierId": tier_id,
                # A free-tier user has no project to name; naming one they do
                # not own is what makes onboarding fail with PERMISSION_DENIED.
                **({"cloudaicompanionProject": self._project_id} if self._project_id else {}),
                "metadata": dict(CLIENT_METADATA),
            },
        )
        waited = 0.0
        while not operation.get("done"):
            if waited >= ONBOARD_TIMEOUT_SEC:
                raise ProviderError(
                    "internal",
                    f"{self._tag()}: Google Code Assist onboarding did not finish in "
                    f"{int(ONBOARD_TIMEOUT_SEC)}s",
                )
            await self._sleep(ONBOARD_POLL_SEC)
            waited += ONBOARD_POLL_SEC
            operation = await self._call(
                "onboardUser",
                {"tierId": tier_id, "metadata": dict(CLIENT_METADATA)},
            )
        response = operation.get("response")
        response = response if isinstance(response, dict) else {}
        project = _project_of(response.get("cloudaicompanionProject"))
        return await self._remember_project(project) if project else None

    async def _remember_project(self, project: str) -> str:
        self._project_id = project
        if self.credentials.get("project_id") != project:
            self.credentials["project_id"] = project
            await self._persist()
        return project

    # -- streaming -----------------------------------------------------
    def build_request(
        self, messages: list[ChatMessage], tools: list[ToolSpec], *, max_tokens: int = 4096
    ) -> dict[str, Any]:
        """The ``:streamGenerateContent`` body for one turn."""
        inner = build_gemini_request(messages, tools, max_tokens=max_tokens)
        return wrap_request(self.model, self._project_id, inner)

    async def _stream_once(self, body: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        normalizer = GeminiStreamNormalizer(self.preset)
        async with self._client(stream=True) as client:
            async with client.stream(
                "POST",
                f"/{API_VERSION}:streamGenerateContent",
                params={"alt": "sse"},
                json=body,
            ) as response:
                if response.status_code in (401, 403):
                    detail = (await response.aread()).decode("utf-8", "replace")[:200]
                    raise _Unauthorized(detail)
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")[:400]
                    raise ProviderError(
                        "internal", f"{self._tag()}: HTTP {response.status_code}: {detail}"
                    )
                async for payload in sse_payloads(response):
                    for event in normalizer.feed(unwrap_chunk(payload)):
                        yield event
        for event in normalizer.finish():
            yield event

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one assistant turn from the Code Assist API."""
        refreshed = False
        # Google's access tokens last an hour and always carry an expiry, so a
        # stale one is renewed up front rather than costing a doomed request.
        if google_oauth.is_expired(self.credentials, skew_sec=REFRESH_SKEW_SEC) and (
            self.credentials.get("refresh_token")
        ):
            await self._refresh()
            refreshed = True
        while True:
            try:
                await self.ensure_project()
                body = self.build_request(messages, tools, max_tokens=max_tokens)
                async for event in self._stream_once(body):
                    yield event
                return
            except _Unauthorized as exc:
                # Raised before any event is yielded, so a retry cannot
                # duplicate output the caller has already seen.
                if refreshed or not self.credentials.get("refresh_token"):
                    raise ProviderError(
                        AUTH_EXPIRED,
                        f"{self._tag()}: your Google login expired "
                        f"({exc.detail or 'HTTP 401'}) — run "
                        f"`snowpea provider login gemini` to sign in again",
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


def _project_of(value: Any) -> str | None:
    """The project id, whether the field is a string or ``{"id": ...}``."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("id", "projectId", "name"):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
    return None


def _tier_id(loaded: dict[str, Any]) -> str:
    """The tier to onboard into: the current one, else the marked default."""
    current = loaded.get("currentTier")
    if isinstance(current, dict) and isinstance(current.get("id"), str):
        return str(current["id"])
    tiers = loaded.get("allowedTiers")
    for tier in tiers if isinstance(tiers, list) else []:
        if isinstance(tier, dict) and tier.get("isDefault") and isinstance(tier.get("id"), str):
            return str(tier["id"])
    return FREE_TIER_ID


def list_models() -> list[str]:
    """The models an OAuth account may select (the API publishes no listing)."""
    return list(CODE_ASSIST_MODELS)


__all__ = [
    "API_VERSION",
    "BASE_URL",
    "CLIENT_METADATA",
    "CODE_ASSIST_MODELS",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "FREE_TIER_ID",
    "ONBOARD_POLL_SEC",
    "ONBOARD_TIMEOUT_SEC",
    "CodeAssistProvider",
    "list_models",
    "unwrap_chunk",
    "wrap_request",
]
