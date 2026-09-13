"""CORE-search-fix: the search provider a user picks is the one that is used.

The bug: ``search.provider: exa_free`` was a catalog entry with an endpoint
string and no client, ``available()`` said "yes" for the ids tagged ``no key``,
and ``web_search`` quietly answered from ddgs.  Nothing in the result, the
session events or ``snowpea tools list`` said so.

These tests pin the three halves of the fix: honest catalog tags, a result that
names the provider that answered and the one it fell back from, and a one-time
session event so the TUI can show it.  No test reaches the network — providers
are driven through a mocked ``httpx`` transport or a monkeypatched ``ddgs``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools import http_util, search_providers, web
from snowpea_core.tools.registry import ToolContext, ToolRegistry, register_builtin_tools
from snowpea_core.tools.search_providers.base import SearchHit, SearchProviderUnavailable

#: Ids that must refuse to run until an API key is configured.
KEY_REQUIRED = (
    "brave_free",
    "keenable_free",
    "parallel_free",
    "tavily",
    "exa",
    "keenable",
    "parallel",
    "xai_grok",
)


class RecordingHub:
    """Collects the session events ``web_search`` emits."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit_event(self, session_id: str, event: tuple[str, dict[str, Any]]) -> None:
        kind, payload = event
        self.events.append((session_id, kind, payload))

    def errors(self) -> list[dict[str, Any]]:
        return [payload for _sid, kind, payload in self.events if kind == "error"]


@pytest.fixture
def core(tmp_path: Path) -> Core:
    built = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    register_builtin_tools(built.tools)
    built.hub = RecordingHub()  # type: ignore[assignment]
    return built


@pytest.fixture
def ctx(core: Core, tmp_path: Path) -> ToolContext:
    workdir = tmp_path / "work"
    workdir.mkdir()
    session = Session(id="s-search", workdir=workdir)
    return ToolContext(session=session, core=core, backend=LocalBackend(workdir))


async def run_search(ctx: ToolContext, **args: Any) -> Any:
    tool = ctx.core.tools.get("web_search")
    assert tool is not None
    return await tool.run(ctx, args)


def mock_transport(handler: Any) -> Any:
    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("guard_ssrf", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def fake_ddgs(monkeypatch: pytest.MonkeyPatch, title: str = "From ddgs") -> None:
    async def search(query: str, *, limit: int) -> list[SearchHit]:
        return [SearchHit(title=title, url="https://example.com/ddgs", snippet="s")]

    monkeypatch.setattr(search_providers.get("ddgs"), "search", search)


# ---------------------------------------------------------------------------
# the catalog tells the truth
# ---------------------------------------------------------------------------


def test_keyless_options_include_exa_hosted_mcp() -> None:
    keyless = [meta.id for meta in search_providers.metas() if meta.key == "no key"]
    assert keyless == ["ddgs", "exa_free"]


@pytest.mark.parametrize("provider_id", KEY_REQUIRED)
def test_key_required_providers_are_unavailable_without_a_key(provider_id: str) -> None:
    provider = search_providers.get(provider_id)
    assert provider is not None
    assert provider.meta.key == "key required"
    assert search_providers.needs_key(provider_id) is True
    assert provider.available(Settings()) is False


@pytest.mark.parametrize("provider_id", ("searxng", "firecrawl_selfhost"))
def test_self_hosted_providers_are_unavailable_without_a_url(provider_id: str) -> None:
    provider = search_providers.get(provider_id)
    assert provider is not None
    assert provider.meta.key == "self-hosted"
    assert provider.available(Settings()) is False


async def test_exa_free_uses_the_anonymous_hosted_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = search_providers.get("exa_free")
    assert provider is not None
    assert provider.meta.endpoint == "https://mcp.exa.ai/mcp"
    assert provider.available(Settings()) is True

    async def fake_call(tool: str, arguments: dict[str, Any]) -> str:
        assert tool == "web_search_exa"
        assert arguments == {"query": "anything", "numResults": 1}
        return "Title: MCP hit\nURL: https://example.com/mcp\nHighlights:\nbody"

    monkeypatch.setattr(provider, "_call", fake_call)
    hits = await provider.search("anything", limit=1)
    assert hits == [SearchHit("MCP hit", "https://example.com/mcp", "body")]


def test_every_catalog_id_has_a_real_client() -> None:
    """No id may be a bare endpoint string any more."""
    from snowpea_core.tools.search_providers.providers import ThinProvider

    for provider in search_providers.all_providers():
        assert not isinstance(provider, ThinProvider), provider.meta.id
        assert callable(getattr(provider, "search", None)), provider.meta.id


# ---------------------------------------------------------------------------
# web_search reports what actually answered
# ---------------------------------------------------------------------------


async def test_fallback_is_named_in_the_output_and_the_result(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.core.settings.search.provider = "exa_free"
    fake_ddgs(monkeypatch)

    async def refuse(query: str, *, limit: int) -> list[SearchHit]:
        raise SearchProviderUnavailable("anonymous MCP rate limit")

    monkeypatch.setattr(search_providers.get("exa_free"), "search", refuse)

    result = await run_search(ctx, query="snowpea agent github")

    assert result.ok, result.error
    assert result.output.startswith("[search via ddgs — fallback from exa_free:")
    assert "anonymous MCP rate limit" in result.output.splitlines()[0]
    assert result.meta["provider"] == "ddgs"
    assert result.meta["fallback_from"] == "exa_free"
    assert "rate limit" in result.meta["reason"]


async def test_the_fallback_event_is_emitted_once_per_session(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.core.settings.search.provider = "exa_free"
    fake_ddgs(monkeypatch)

    async def refuse(query: str, *, limit: int) -> list[SearchHit]:
        raise SearchProviderUnavailable("anonymous MCP rate limit")

    monkeypatch.setattr(search_providers.get("exa_free"), "search", refuse)

    await run_search(ctx, query="one")
    await run_search(ctx, query="two")

    errors = ctx.core.hub.errors()  # type: ignore[attr-defined]
    assert len(errors) == 1
    assert errors[0]["code"] == web.SEARCH_UNAVAILABLE
    assert "exa_free" in errors[0]["message"] and "ddgs" in errors[0]["message"]


async def test_no_fallback_marker_when_the_configured_provider_answers(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ddgs(monkeypatch)

    result = await run_search(ctx, query="anything")

    assert result.output.startswith("[search via ddgs] ")
    assert result.meta == {"provider": "ddgs", "fallback_from": None, "reason": None}
    assert ctx.core.hub.errors() == []  # type: ignore[attr-defined]


async def test_a_configured_exa_key_makes_the_exa_client_answer(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Exa hit", "url": "https://example.com/exa", "text": "body"}
                ]
            },
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    ctx.core.settings.search.provider = "exa"
    ctx.core.settings.search.credentials = {"exa": {"api_key": "exa-secret"}}

    result = await run_search(ctx, query="x")

    assert result.ok, result.error
    assert seen["url"] == "https://api.exa.ai/search"
    assert seen["key"] == "exa-secret"
    assert result.meta["provider"] == "exa"
    assert result.meta["fallback_from"] is None
    assert "https://example.com/exa" in result.output
    assert ctx.core.hub.errors() == []  # type: ignore[attr-defined]


async def test_the_key_may_come_from_the_environment(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "from-env")
    provider = search_providers.get("exa")
    assert provider is not None
    assert provider.available(ctx.core.settings) is True


async def test_every_provider_failing_reports_the_configured_one(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.core.settings.search.provider = "exa_free"

    async def refuse(query: str, *, limit: int) -> list[SearchHit]:
        raise SearchProviderUnavailable("no network in tests")

    monkeypatch.setattr(search_providers.get("ddgs"), "search", refuse)
    monkeypatch.setattr(search_providers.get("exa_free"), "search", refuse)
    monkeypatch.setattr(search_providers.get("firecrawl"), "search", refuse)

    result = await run_search(ctx, query="anything")

    assert not result.ok
    assert web.SEARCH_UNAVAILABLE in (result.error or "")
    assert "exa_free" in (result.error or "")
    assert ctx.core.hub.errors()[0]["code"] == web.SEARCH_UNAVAILABLE  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# tool.list shows which provider would answer
# ---------------------------------------------------------------------------


def test_tool_list_reports_the_configured_provider() -> None:
    settings = Settings()
    registry = register_builtin_tools(ToolRegistry())
    infos = web.annotate(registry.list(), settings)
    by_name = {info.name: info for info in infos}
    assert by_name["web_search"].provider == "ddgs"
    assert by_name["web_search"].state == "active"


def test_tool_list_shows_keyless_exa_free_as_active() -> None:
    settings = Settings()
    settings.search.provider = "exa_free"
    registry = register_builtin_tools(ToolRegistry())
    infos = web.annotate(registry.list(), settings)
    by_name = {info.name: info for info in infos}
    assert by_name["web_search"].provider == "exa_free"
    assert by_name["web_search"].state == "active"
