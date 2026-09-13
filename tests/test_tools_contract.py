"""The M2 tools contract (``docs/design/m2-tools-contract.md`` §8).

Everything here runs against a ``Core`` built in a temporary home with a local
backend.  No test reaches the network: the search providers are driven through
a mocked ``httpx`` transport or a monkeypatched ``ddgs``, and the browser tests
skip when Chromium has not been downloaded.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools import (
    browser,
    browser_providers,
    http_util,
    mcp_client,
    media,
    process,
    search_providers,
    web,
)
from snowpea_core.tools.registry import ToolContext, ToolRegistry, register_builtin_tools
from snowpea_core.tools.search_providers.base import SearchHit

FIXTURE_ECHO = Path(__file__).parent / "fixtures" / "mcp" / "echo_server.py"

#: Contract §2: every builtin tool, with the category and permission it must
#: carry.  ``tool.list`` is the API, so a rename here is a breaking change.
CATALOG: dict[str, tuple[str, str]] = {
    "read_file": ("file", "read"),
    "write_file": ("file", "write"),
    "edit_file": ("file", "write"),
    "list_dir": ("file", "read"),
    "glob": ("file", "read"),
    "grep": ("file", "read"),
    "shell": ("terminal", "exec"),
    "process_list": ("terminal", "read"),
    "process_kill": ("terminal", "exec"),
    "git_status": ("git", "read"),
    "git_diff": ("git", "read"),
    "git_log": ("git", "read"),
    "git_commit": ("git", "write"),
    "web_search": ("web", "network"),
    "web_extract": ("web", "network"),
    "settings_get": ("settings", "read"),
    "settings_set": ("settings", "config"),
    "browser_navigate": ("browser", "network"),
    "browser_click": ("browser", "network"),
    "browser_type": ("browser", "network"),
    "browser_scroll": ("browser", "network"),
    "browser_snapshot": ("browser", "network"),
    "delegate_task": ("delegate", "exec"),
    # ask_user blocks on a human and changes nothing, so it is read-class; the
    # command it queues is chosen by that human, never by the model alone.
    "ask_user": ("interaction", "read"),
    "queue_command": ("interaction", "read"),
    # set_mode only *asks* to change the mode; the switch is the user's answer.
    "set_mode": ("interaction", "read"),
    "schedule_create": ("schedule", "send"),
    "schedule_list": ("schedule", "send"),
    "schedule_cancel": ("schedule", "send"),
    "memory_write": ("memory", "write"),
    "memory_search": ("memory", "read"),
    "image_generate": ("media", "network"),
    "video_generate": ("media", "network"),
    "music_generate": ("media", "network"),
    # Voice moved out of media: text_to_speech runs a provider chain that works
    # without the studio MCP server, and transcription joined it
    # (CORE-multimodal).  transcribe_audio is tagged ``read`` because that is
    # its guaranteed effect — reading a local file; a hosted backend also
    # uploads it, which its description says.
    "text_to_speech": ("audio", "network"),
    "transcribe_audio": ("audio", "read"),
    # The LSP tools land in the same builtin catalog (M13 contract §3); they
    # are listed here so a rename or a retag shows up as a contract change.
    "lsp_diagnostics": ("lsp", "read"),
    "lsp_definition": ("lsp", "read"),
    "lsp_references": ("lsp", "read"),
    "lsp_symbols": ("lsp", "read"),
    "lsp_workspace_symbols": ("lsp", "read"),
    "lsp_hover": ("lsp", "read"),
    "lsp_rename": ("lsp", "write"),
}

INACTIVE_AT_M2 = {
    "delegate_task",
    "schedule_create",
    "schedule_list",
    "schedule_cancel",
    "memory_write",
    "memory_search",
    "image_generate",
    "video_generate",
    "music_generate",
    "text_to_speech",
    "transcribe_audio",
}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def core(tmp_path: Path) -> Core:
    """A ``Core`` with the builtin tools registered, in a throwaway home."""
    home = tmp_path / "home"
    built = Core(settings=Settings(), paths=Paths.create(home), token="test-token")
    register_builtin_tools(built.tools)
    media.refresh_state(built)
    return built


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    target = tmp_path / "work"
    target.mkdir()
    return target


@pytest.fixture
def ctx(core: Core, workdir: Path) -> ToolContext:
    session = Session(id="s-test", workdir=workdir)
    return ToolContext(session=session, core=core, backend=LocalBackend(workdir))


@pytest.fixture(autouse=True)
def _clean_process_registry() -> Iterator[None]:
    process.REGISTRY.clear()
    yield
    process.REGISTRY.clear()


async def run(ctx: ToolContext, name: str, **args: Any) -> Any:
    tool = ctx.core.tools.get(name)
    assert tool is not None, f"{name} is not registered"
    return await tool.run(ctx, args)


def mock_transport(handler: Any) -> Any:
    """Patch :func:`http_util.new_client` to answer from ``handler``."""

    def factory(*, timeout: float = 20.0, guard_ssrf: bool = False) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return factory


# ---------------------------------------------------------------------------
# §2 catalog
# ---------------------------------------------------------------------------


def test_every_catalog_tool_is_registered(core: Core) -> None:
    registered = {info.name for info in core.tools.list()}
    missing = sorted(set(CATALOG) - registered)
    assert not missing, f"tools missing from tool.list: {missing}"


def test_categories_and_permission_tags_match_the_contract(core: Core) -> None:
    listed = {info.name: info for info in core.tools.list()}
    wrong = {
        name: (listed[name].category, listed[name].permissionTag)
        for name, expected in CATALOG.items()
        if (listed[name].category, listed[name].permissionTag) != expected
    }
    assert not wrong, f"category/permission mismatches: {wrong}"


def test_stub_and_media_tools_start_inactive(core: Core) -> None:
    listed = {info.name: info.state for info in core.tools.list()}
    inactive = {name for name, state in listed.items() if state == "inactive"}
    # Media tools are inactive until credentials exist. The M5/M7 stubs (schedule, memory,
    # delegate) may already have been replaced by real, active implementations.
    media = {"image_generate", "video_generate", "music_generate"}
    assert media <= inactive
    assert inactive <= INACTIVE_AT_M2


async def test_inactive_media_tool_returns_tool_inactive(ctx: ToolContext) -> None:
    result = await run(ctx, "image_generate", prompt="a cat")
    assert not result.ok
    assert "tool_inactive" in (result.error or "")


def test_registry_set_state_and_unregister() -> None:
    registry = register_builtin_tools(ToolRegistry())
    assert registry.set_state("image_generate", "active") is not None
    assert registry.get("image_generate").state == "active"
    assert registry.unregister("image_generate") is not None
    assert registry.get("image_generate") is None


# ---------------------------------------------------------------------------
# file tools: glob, grep
# ---------------------------------------------------------------------------


async def test_glob_matches_nested_paths(ctx: ToolContext, workdir: Path) -> None:
    (workdir / "src" / "deep").mkdir(parents=True)
    (workdir / "src" / "a.py").write_text("print(1)\n")
    (workdir / "src" / "deep" / "b.py").write_text("print(2)\n")
    (workdir / "src" / "c.txt").write_text("no\n")
    (workdir / "node_modules").mkdir()
    (workdir / "node_modules" / "d.py").write_text("ignored\n")

    result = await run(ctx, "glob", pattern="**/*.py")
    assert result.ok
    found = set(result.output.splitlines())
    assert found == {"src/a.py", "src/deep/b.py"}


async def test_grep_finds_matches_with_ripgrep_or_python(
    ctx: ToolContext, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workdir / "one.py").write_text("alpha = 1\nbeta = 2\n")
    (workdir / "two.py").write_text("gamma = 3\n")

    result = await run(ctx, "grep", pattern="beta", glob="*.py")
    assert result.ok, result.error
    assert "one.py" in result.output and "beta" in result.output

    # Force the pure-Python path and require the same finding.
    from snowpea_core.tools import grep as grep_tools

    monkeypatch.setattr(grep_tools, "has_ripgrep", _false)
    fallback = await run(ctx, "grep", pattern="beta", glob="*.py")
    assert fallback.ok
    assert "one.py" in fallback.output


async def _false(_ctx: ToolContext) -> bool:
    return False


# ---------------------------------------------------------------------------
# git tools
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_git_tools_round_trip_through_the_backend(
    ctx: ToolContext, workdir: Path
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, check=True)
    (workdir / "file.txt").write_text("one\n")

    status = await run(ctx, "git_status")
    assert status.ok and "file.txt" in status.output

    committed = await run(ctx, "git_commit", message="add file")
    assert committed.ok, committed.error

    log = await run(ctx, "git_log", count=5)
    assert log.ok and "add file" in log.output

    (workdir / "file.txt").write_text("two\n")
    diff = await run(ctx, "git_diff")
    assert diff.ok and "-one" in diff.output


# ---------------------------------------------------------------------------
# background processes
# ---------------------------------------------------------------------------


async def test_background_shell_is_listed_then_killed(ctx: ToolContext) -> None:
    started = await run(ctx, "shell", command="sleep 30", background=True)
    assert started.ok, started.error
    listing = await run(ctx, "process_list")
    assert listing.ok and "running" in listing.output

    process_id = process.REGISTRY.list(ctx.session.id)[0].id
    killed = await run(ctx, "process_kill", id=process_id)
    assert killed.ok, killed.error
    assert process.REGISTRY.get(process_id) is None


# ---------------------------------------------------------------------------
# §3 search providers
# ---------------------------------------------------------------------------


def test_provider_registry_order_matches_the_contract() -> None:
    assert [p.meta.id for p in search_providers.all_providers()] == [
        "ddgs",
        "brave_free",
        "exa_free",
        "keenable_free",
        "parallel_free",
        "tavily",
        "searxng",
        "firecrawl_selfhost",
        "exa",
        "keenable",
        "parallel",
        "firecrawl",
        "xai_grok",
    ]


def test_provider_tags_match_the_contract() -> None:
    """Tags say what a provider really needs (CORE-search-fix).

    ``ddgs`` and Exa's hosted MCP are keyless; the Firecrawl cloud search
    endpoint answers without credentials too, which is ``key optional``, not
    ``no key``.  Everything else needs a key or a URL.
    """
    tags = {m.id: (m.tier, m.key) for m in search_providers.metas()}
    assert tags["ddgs"] == ("free", "no key")
    assert tags["brave_free"] == ("free", "key required")
    assert tags["tavily"] == ("free", "key required")
    assert tags["searxng"] == ("free", "self-hosted")
    assert tags["firecrawl_selfhost"] == ("free", "self-hosted")
    assert tags["exa_free"] == ("free", "no key")
    for free_tier in ("keenable_free", "parallel_free"):
        assert tags[free_tier] == ("free", "key required"), free_tier
    for paid in ("exa", "keenable", "parallel", "xai_grok"):
        assert tags[paid] == ("paid", "key required"), paid
    assert tags["firecrawl"] == ("paid", "key optional")
    assert search_providers.get("ddgs").meta.default is True
    assert [m.id for m in search_providers.metas() if m.key == "no key"] == ["ddgs", "exa_free"]


def test_free_chain_starts_with_the_preferred_provider() -> None:
    ids = [p.meta.id for p in search_providers.chain("searxng")]
    assert ids[0] == "searxng"
    assert ids.count("searxng") == 1
    assert "ddgs" in ids


async def test_web_search_returns_hits_from_ddgs(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_search(query: str, *, limit: int) -> list[SearchHit]:
        return [SearchHit(title="Snowpea", url="https://example.com/a", snippet="a local agent")]

    provider = search_providers.get("ddgs")
    monkeypatch.setattr(provider, "search", fake_search)

    result = await run(ctx, "web_search", query="snowpea agent")
    assert result.ok, result.error
    assert "[search via ddgs]" in result.output
    assert "https://example.com/a" in result.output


async def test_web_search_falls_back_past_a_failing_provider(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.core.settings.search.provider = "brave_free"
    ctx.core.settings.search.credentials = {"brave_free": {"api_key": "k"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))

    async def fake_ddgs(query: str, *, limit: int) -> list[SearchHit]:
        return [SearchHit(title="From ddgs", url="https://example.com/b")]

    monkeypatch.setattr(search_providers.get("ddgs"), "search", fake_ddgs)

    result = await run(ctx, "web_search", query="anything")
    assert result.ok, result.error
    # The fallback is reported, not hidden (CORE-search-fix).
    assert result.output.startswith("[search via ddgs — fallback from brave_free:")
    assert result.meta == {
        "provider": "ddgs",
        "fallback_from": "brave_free",
        "reason": "Brave Search returned HTTP 429",
    }


async def test_brave_provider_parses_its_payload(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("x-subscription-token")
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "T", "url": "https://example.com/x", "description": "D"}
                    ]
                }
            },
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    ctx.core.settings.search.provider = "brave_free"
    ctx.core.settings.search.credentials = {"brave_free": {"api_key": "secret"}}

    result = await run(ctx, "web_search", query="x")
    assert result.ok, result.error
    assert seen["token"] == "secret"
    assert "api.search.brave.com" in seen["url"]
    assert "https://example.com/x" in result.output


async def test_searxng_provider_uses_the_configured_instance(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "searx.internal"
        return httpx.Response(
            200, json={"results": [{"title": "S", "url": "https://example.com/s", "content": "C"}]}
        )

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    ctx.core.settings.search.provider = "searxng"
    ctx.core.settings.search.credentials = {"searxng": {"url": "http://searx.internal:8080"}}

    result = await run(ctx, "web_search", query="s")
    assert result.ok, result.error
    assert "[search via searxng]" in result.output


async def test_unimplemented_provider_reports_search_provider_unavailable(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(query: str, *, limit: int) -> list[SearchHit]:
        raise RuntimeError("no network in tests")

    for provider in search_providers.chain("exa"):
        monkeypatch.setattr(provider, "search", refuse, raising=False)
        monkeypatch.setattr(provider, "available", lambda _s=None: True, raising=False)

    result = await run(ctx, "web_search", query="anything", provider="exa")
    assert not result.ok
    assert web.SEARCH_UNAVAILABLE in (result.error or "")


# ---------------------------------------------------------------------------
# web_extract and the SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
    ],
)
async def test_web_extract_blocks_private_destinations(ctx: ToolContext, url: str) -> None:
    result = await run(ctx, "web_extract", url=url)
    assert not result.ok
    assert web.URL_BLOCKED in (result.error or "")


async def test_web_extract_rejects_non_http_schemes(ctx: ToolContext) -> None:
    result = await run(ctx, "web_extract", url="file:///etc/passwd")
    assert not result.ok
    assert web.URL_BLOCKED in (result.error or "")


async def test_web_extract_returns_readable_text(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = (
        "<html><head><title>T</title><style>b{}</style></head>"
        "<body><p>Hello &amp; bye</p><script>x()</script></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=html)

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    result = await run(ctx, "web_extract", url="https://example.com/page")
    assert result.ok, result.error
    assert "Hello & bye" in result.output
    assert "x()" not in result.output


async def test_web_extract_truncates_to_the_settings_limit(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.core.settings.tools.max_output_chars = 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 50_000)

    monkeypatch.setattr(http_util, "new_client", mock_transport(handler))
    result = await run(ctx, "web_extract", url="https://example.com/big")
    assert result.ok
    assert "truncated" in result.output
    assert len(result.output) < 2000


def test_default_max_output_chars_is_twenty_thousand() -> None:
    assert Settings().tools.max_output_chars == 20_000


# ---------------------------------------------------------------------------
# §4 browser providers
# ---------------------------------------------------------------------------


def test_browser_catalog_ids_and_tags() -> None:
    metas = {m.id: m for m in browser_providers.metas()}
    assert list(metas) == [
        "local_chromium",
        "camoufox",
        "browser_use_local",
        "browserbase",
        "firecrawl_cloud",
    ]
    assert metas["local_chromium"].default is True
    assert metas["local_chromium"].tier == "free"
    assert metas["browserbase"].tier == "paid"
    assert metas["firecrawl_cloud"].key == "key required"


async def test_catalog_only_browser_provider_refuses(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.core.settings.browser.provider = "camoufox"
    provider = browser_providers.get("camoufox")
    monkeypatch.setattr(provider, "available", lambda _s=None: True)
    result = await run(ctx, "browser_navigate", url="https://example.com")
    assert not result.ok
    assert "browser_provider_unavailable" in (result.error or "")


@pytest.mark.timeout(60)
@pytest.mark.skipif(
    os.environ.get("SNOWPEA_SKIP_BROWSER_TESTS") == "1", reason="SNOWPEA_SKIP_BROWSER_TESTS=1"
)
async def test_local_chromium_navigates_and_snapshots(
    ctx: ToolContext, workdir: Path
) -> None:
    """Skips itself when the Chromium binaries have not been downloaded."""
    page = workdir / "page.html"
    page.write_text("<html><body><h1>Snowpea test page</h1></body></html>")
    try:
        result = await run(ctx, "browser_navigate", url=page.as_uri())
        if not result.ok and browser.BROWSER_NOT_INSTALLED in (result.error or ""):
            pytest.skip("run `uv run playwright install chromium` first")
        assert result.ok, result.error
        assert "Snowpea test page" in result.output

        snapshot = await run(ctx, "browser_snapshot")
        assert snapshot.ok and "Snowpea test page" in snapshot.output
    finally:
        await browser_providers.close_all_sessions(ctx.session.id)


# ---------------------------------------------------------------------------
# §5 media
# ---------------------------------------------------------------------------


async def test_media_tools_are_inactive_and_refuse_without_credentials(
    ctx: ToolContext,
) -> None:
    assert ctx.core.tools.get("image_generate").state == "inactive"
    result = await run(ctx, "image_generate", prompt="a cat")
    assert not result.ok
    assert media.INACTIVE in (result.error or "")


async def test_provider_configure_activates_media_without_restart(core: Core) -> None:
    assert all(core.tools.get(name).state == "inactive" for name in media.REGISTERED)

    state = await media.configure(
        core, {"command": sys.executable, "args": ["-c", "pass"], "api_key": "k"}
    )
    assert state == "active"
    assert all(core.tools.get(name).state == "active" for name in media.REGISTERED)

    entry = media.server_config(core)
    assert entry is not None
    assert entry.name == media.SERVER_NAME
    assert entry.env["SNOWPEA_STUDIO_API_KEY"] == "k"

    saved = json.loads(core.paths.settings_json.read_text(encoding="utf-8"))
    assert saved["media"]["mcp"]["command"] == sys.executable


async def test_media_tools_forward_to_the_studio_server(
    core: Core, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await media.configure(core, {"command": sys.executable, "args": ["-c", "pass"]})
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeServer:
        async def call(self, tool: str, args: dict[str, Any]) -> str:
            calls.append((tool, args))
            return "asset-123"

    monkeypatch.setattr(mcp_client.MANAGER, "register", lambda config: FakeServer())
    ctx = ToolContext(
        session=Session(id="s-media", workdir=workdir),
        core=core,
        backend=LocalBackend(workdir),
    )
    result = await run(ctx, "video_generate", prompt="a walk cycle")
    assert result.ok, result.error
    assert result.output == "asset-123"
    assert calls == [("generate_video", {"prompt": "a walk cycle"})]


# ---------------------------------------------------------------------------
# §6 MCP
# ---------------------------------------------------------------------------


def test_mcp_config_is_read_from_home_and_workdir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    (home / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"from-home": {"command": "a"}, "shared": {"command": "home"}}})
    )
    (work / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"from-work": {"command": "b"}, "shared": {"command": "work"}}})
    )
    found = mcp_client.discover(home, work)
    assert set(found) == {"from-home", "from-work", "shared"}
    assert found["shared"].command == "work"


async def test_echo_server_registers_and_round_trips(
    core: Core, workdir: Path
) -> None:
    (workdir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fixture-echo": {"command": sys.executable, "args": [str(FIXTURE_ECHO)]}
                }
            }
        )
    )
    try:
        registered = await mcp_client.sync_tools(core, workdir)
        assert "mcp__fixture-echo__echo" in registered

        tool = core.tools.get("mcp__fixture-echo__echo")
        assert tool is not None
        assert tool.category == "mcp"
        assert tool.permission == "network"
        assert tool.source == "mcp:fixture-echo"

        ctx = ToolContext(
            session=Session(id="s-mcp", workdir=workdir),
            core=core,
            backend=LocalBackend(workdir),
        )
        result = await tool.run(ctx, {"text": "round trip"})
        assert result.ok, result.error
        assert result.output == "round trip"
    finally:
        await mcp_client.MANAGER.close_all()


async def test_mcp_permission_comes_from_settings(core: Core, workdir: Path) -> None:
    core.settings.mcp.permissions = {"fixture-echo": "read"}
    (workdir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fixture-echo": {"command": sys.executable, "args": [str(FIXTURE_ECHO)]}
                }
            }
        )
    )
    try:
        await mcp_client.sync_tools(core, workdir)
        assert core.tools.get("mcp__fixture-echo__echo").permission == "read"
    finally:
        await mcp_client.MANAGER.close_all()


async def test_a_broken_mcp_server_is_skipped_not_fatal(core: Core, workdir: Path) -> None:
    (workdir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"broken": {"command": "definitely-not-a-real-binary"}}})
    )
    try:
        assert await mcp_client.sync_tools(core, workdir) == []
    finally:
        await mcp_client.MANAGER.close_all()


# ---------------------------------------------------------------------------
# session lifecycle
# ---------------------------------------------------------------------------


async def test_closing_a_session_releases_its_browser_context(workdir: Path) -> None:
    from snowpea_core.session.manager import SessionManager

    released: list[str] = []
    manager = SessionManager()
    manager.on_close.append(_recorder(released))
    session = await manager.create(workdir)
    assert await manager.close(session.id) is True
    assert released == [session.id]


def _recorder(sink: list[str]) -> Any:
    async def hook(session_id: str) -> None:
        sink.append(session_id)

    return hook


def test_wire_core_registers_the_browser_close_hook(tmp_path: Path) -> None:
    from snowpea_core.server.session_handlers import wire_core

    built = Core(
        settings=Settings(), paths=Paths.create(tmp_path / "home2"), token="test-token"
    )
    try:
        wire_core(built)
        assert browser_providers.close_all_sessions in built.sessions.on_close
        assert built.tools.get("glob") is not None
    finally:
        if built.store is not None:
            built.store.close()
