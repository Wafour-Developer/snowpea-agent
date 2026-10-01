"""The agent can see the page it built: screenshot, console, key presses, url policy.

Most tests drive ``LocalChromiumProvider`` with a fake Playwright page so they
run without Chromium; one test at the end uses the real browser and skips when
it has not been downloaded.
"""

from __future__ import annotations

import base64
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.providers.base import ChatMessage
from snowpea_core.session.session import Session
from snowpea_core.tools import browser, browser_providers
from snowpea_core.tools.browser_providers import url_policy
from snowpea_core.tools.browser_providers.base import CONSOLE_BUFFER, PageState
from snowpea_core.tools.browser_providers.host import HostBrowserProvider
from snowpea_core.tools.browser_providers.local_chromium import LocalChromiumProvider
from snowpea_core.tools.host_tools import HOST_TOOLS
from snowpea_core.tools.registry import ToolContext
from snowpea_core.tools.view_image import _image_metas

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FakeKeyboard:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def press(self, key: str) -> None:
        self.log.append(f"press:{key}")

    async def down(self, key: str) -> None:
        self.log.append(f"down:{key}")

    async def up(self, key: str) -> None:
        self.log.append(f"up:{key}")


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> FakeLocator:
        return self

    async def screenshot(self, **options: Any) -> bytes:
        self.page.log.append(f"element-shot:{self.selector}:{options.get('type')}")
        return PNG


class FakePage:
    """Just enough of a Playwright ``Page`` for the provider."""

    def __init__(self, url: str = "about:blank", redirect: str | None = None) -> None:
        self.url = url
        self.redirect = redirect
        self.log: list[str] = []
        self.handlers: dict[str, Any] = {}
        self.keyboard = FakeKeyboard(self.log)

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    def emit_console(self, kind: str, text: str) -> None:
        self.handlers["console"](SimpleNamespace(type=kind, text=text))

    async def title(self) -> str:
        return "Game"

    async def inner_text(self, selector: str) -> str:
        return "score 0"

    async def goto(self, url: str, **_: Any) -> None:
        self.log.append(f"goto:{url}")
        self.url = self.redirect or url

    async def screenshot(self, **options: Any) -> bytes:
        self.log.append(f"shot:full={options.get('full_page')}:{options.get('type')}")
        return PNG

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)


def provider_with(page: FakePage, session_id: str = "s1") -> LocalChromiumProvider:
    provider = LocalChromiumProvider()
    provider._pages[session_id] = (SimpleNamespace(), page)
    provider._watch_console(session_id, page)
    return provider


def ctx_for(
    session: Session, provider: Any, monkeypatch: pytest.MonkeyPatch, *, core: Any = None
) -> ToolContext:
    monkeypatch.setattr(HOST_TOOLS, "names_for", lambda _session: [])
    monkeypatch.setattr(browser_providers, "resolve_for_session", lambda _s, _sess: provider)
    monkeypatch.setattr(provider, "available", lambda _settings=None: True)
    core = core or SimpleNamespace(settings=SimpleNamespace(browser=SimpleNamespace()))
    return ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]


def ide_session(tmp_path: Path) -> Session:
    return Session(id="s1", workdir=tmp_path)


def browser_session(tmp_path: Path) -> Session:
    """A Snowpea-browser session the user switched to core's own Chromium."""
    session = Session(id="s1", workdir=tmp_path)
    session.origin_surface = "browser"
    session.host_tools_from = "snowpea-browser-A"
    session.browser_provider = "local"
    return session


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Resolve names from a table; literal addresses resolve to themselves."""
    table: dict[str, str] = {
        "example.com": "93.184.216.34",
        "localhost": "127.0.0.1",
        "router.lan": "192.168.0.1",
    }
    real = socket.getaddrinfo

    def fake(host: str, port: Any = None, *args: Any, **kwargs: Any) -> Any:
        address = table.get(str(host).lower())
        if address is None:
            try:
                return real(host, port, *args, **kwargs) if host[0].isdigit() else _fail(host)
            except IndexError:
                _fail(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    def _fail(host: str) -> Any:
        raise socket.gaierror(f"no such host {host}")

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


# ---------------------------------------------------------------------------
# PageState
# ---------------------------------------------------------------------------


def test_page_state_renders_console_errors_only_when_there_are_some() -> None:
    plain = PageState(url="http://x/", title="T", text="body")
    assert "Console errors" not in plain.render()
    noisy = PageState(url="http://x/", title="T", text="body", console=("[error] boom",))
    rendered = noisy.render()
    assert rendered.endswith("Console errors:\n- [error] boom")
    assert rendered.startswith("T\nhttp://x/\n\nbody")


# ---------------------------------------------------------------------------
# console buffering
# ---------------------------------------------------------------------------


async def test_console_errors_are_reported_once_and_logs_are_ignored() -> None:
    page = FakePage("http://game.test/")
    provider = provider_with(page)
    page.emit_console("log", "hello")
    page.emit_console("error", "Uncaught TypeError: x is undefined")
    page.emit_console("warning", "WebGL: INVALID_OPERATION")
    page.handlers["pageerror"](RuntimeError("ReferenceError: foo is not defined"))

    first = await provider.snapshot("s1")
    assert first.console == (
        "[error] Uncaught TypeError: x is undefined",
        "[warning] WebGL: INVALID_OPERATION",
        "[pageerror] ReferenceError: foo is not defined",
    )
    assert "Console errors:" in first.render()
    again = await provider.snapshot("s1")
    assert again.console == ()


async def test_the_console_buffer_is_bounded_and_lines_are_capped() -> None:
    page = FakePage()
    provider = provider_with(page)
    for index in range(CONSOLE_BUFFER + 20):
        page.emit_console("error", f"e{index}")
    page.emit_console("error", "x" * 5000)
    state = await provider.console("s1")
    assert len(state.console) == CONSOLE_BUFFER
    assert state.console[0] == "[error] e21"
    assert len(state.console[-1]) < 600


async def test_closing_a_session_drops_its_console_buffer() -> None:
    page = FakePage()
    provider = provider_with(page)
    page.emit_console("error", "boom")
    await provider.close_session("s1")
    assert "s1" not in provider._console


async def test_browser_console_tool_reports_and_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakePage("http://game.test/")
    provider = provider_with(page)
    ctx = ctx_for(ide_session(tmp_path), provider, monkeypatch)
    page.emit_console("error", "shader failed to compile")
    result = await browser.browser_console(ctx, {})
    assert result.ok
    assert "- [error] shader failed to compile" in result.output
    empty = await browser.browser_console(ctx, {})
    assert empty.ok and "No console errors or warnings" in empty.output


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------


async def test_screenshot_returns_an_image_the_loop_attaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakePage("http://game.test/")
    provider = provider_with(page)
    ctx = ctx_for(ide_session(tmp_path), provider, monkeypatch)
    page.emit_console("error", "boom")

    result = await browser.browser_screenshot(ctx, {})
    assert result.ok, result.error
    assert result.output.startswith("screenshot attached:")
    assert "Console errors:\n- [error] boom" in result.output
    assert "base64" not in result.output and len(result.output) < 500
    image = (result.meta or {})["image"]
    assert base64.b64decode(image["bytes_b64"]) == PNG
    assert image["mime"] == "image/png"
    # The same shape view_image produces, so the agent loop's image path picks it up.
    assert _image_metas(result) == [image]
    assert page.log == ["shot:full=False:png"]


async def test_screenshot_full_page_and_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakePage("http://game.test/")
    provider = provider_with(page)
    ctx = ctx_for(ide_session(tmp_path), provider, monkeypatch)
    assert (await browser.browser_screenshot(ctx, {"full_page": True})).ok
    assert (await browser.browser_screenshot(ctx, {"selector": "canvas#game"})).ok
    assert page.log == ["shot:full=True:png", "element-shot:canvas#game:png"]


async def test_an_oversized_screenshot_is_retaken_as_jpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.tools.browser_providers import local_chromium

    page = FakePage()
    provider = provider_with(page)
    monkeypatch.setattr(local_chromium, "SCREENSHOT_MAX_BYTES", 8)
    state = await provider.screenshot("s1")
    assert page.log == ["shot:full=False:png", "shot:full=False:jpeg"]
    assert state.image_mime == "image/jpeg"


async def test_screenshot_refuses_a_model_that_cannot_see(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakePage()
    provider = provider_with(page)
    registry = SimpleNamespace(
        default_vendor=lambda: "x",
        vision_for=lambda vendor, model: False,
        is_local_style=lambda vendor: False,
    )
    core = SimpleNamespace(settings=SimpleNamespace(browser=SimpleNamespace()), providers=registry)
    ctx = ctx_for(ide_session(tmp_path), provider, monkeypatch, core=core)
    result = await browser.browser_screenshot(ctx, {})
    assert not result.ok
    assert "cannot see images" in (result.error or "")
    assert page.log == []


# ---------------------------------------------------------------------------
# press
# ---------------------------------------------------------------------------


async def test_press_taps_a_key_or_holds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = FakePage("http://game.test/")
    provider = provider_with(page)
    ctx = ctx_for(ide_session(tmp_path), provider, monkeypatch)

    tapped = await browser.browser_press(ctx, {"key": "ArrowUp"})
    assert tapped.ok and "score 0" in tapped.output
    assert (await browser.browser_press(ctx, {"key": "space"})).ok
    assert (await browser.browser_press(ctx, {"key": "w", "hold_ms": 30})).ok
    assert (await browser.browser_press(ctx, {"key": "Shift+d", "hold_ms": 10})).ok
    assert page.log == [
        "press:ArrowUp",
        "press:Space",
        "down:w",
        "up:w",
        "down:Shift",
        "down:d",
        "up:d",
        "up:Shift",
    ]


async def test_press_needs_a_key_and_a_numeric_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = provider_with(FakePage())
    ctx = ctx_for(ide_session(tmp_path), provider, monkeypatch)
    assert (await browser.browser_press(ctx, {})).error == "key is required"
    bad = await browser.browser_press(ctx, {"key": "w", "hold_ms": "long"})
    assert not bad.ok and "hold_ms" in (bad.error or "")


# ---------------------------------------------------------------------------
# host provider and catalog-only providers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "phrase"),
    [
        (browser.browser_screenshot, {}, "cannot take screenshots yet"),
        (browser.browser_console, {}, "does not report console messages yet"),
        (browser.browser_press, {"key": "w"}, "cannot press keys yet"),
    ],
)
async def test_the_host_browser_says_it_cannot_do_this_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: Any, args: dict[str, Any], phrase: str
) -> None:
    provider = HostBrowserProvider().bind(SimpleNamespace())
    ctx = ctx_for(ide_session(tmp_path), provider, monkeypatch)
    result = await tool(ctx, args)
    assert not result.ok
    assert result.error and result.error.startswith(browser.BROWSER_UNAVAILABLE)
    assert phrase in result.error


async def test_catalog_only_providers_refuse_the_new_actions() -> None:
    thin = browser_providers.get("camoufox")
    assert thin is not None
    for call in (thin.screenshot("s"), thin.console("s"), thin.press("s", "w")):
        with pytest.raises(browser_providers.BrowserProviderUnavailable):
            await call


def test_new_tools_are_registered_with_the_browser_permission() -> None:
    tools = {tool.name: tool for tool in browser.TOOLS}
    for name in ("browser_screenshot", "browser_console", "browser_press"):
        assert tools[name].category == "browser"
        assert tools[name].permission == "network"


# ---------------------------------------------------------------------------
# url policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://169.254.1.1/",
    ],
)
async def test_cloud_metadata_is_refused_for_every_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_dns: dict[str, str], url: str
) -> None:
    page = FakePage()
    provider = provider_with(page)
    for session in (ide_session(tmp_path), browser_session(tmp_path)):
        ctx = ctx_for(session, provider, monkeypatch)
        result = await browser.browser_navigate(ctx, {"url": url})
        assert not result.ok
        assert (result.error or "").startswith("url_blocked:")
        assert "cloud-metadata" in (result.error or "")
    assert page.log == []


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:5173/",
        "http://127.0.0.1:8000/",
        "http://192.168.0.1/",
        "http://10.0.0.5/",
        "http://172.16.3.4/",
        "http://router.lan/admin",
        "http://[::1]:3000/",
    ],
)
async def test_a_browser_session_refuses_private_addresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_dns: dict[str, str], url: str
) -> None:
    page = FakePage()
    ctx = ctx_for(browser_session(tmp_path), provider_with(page), monkeypatch)
    result = await browser.browser_navigate(ctx, {"url": url})
    assert not result.ok
    assert (result.error or "").startswith("url_blocked:")
    assert "private, loopback or link-local" in (result.error or "")
    assert page.log == []


async def test_ide_and_cli_sessions_keep_loopback_and_the_lan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_dns: dict[str, str]
) -> None:
    page = FakePage()
    ctx = ctx_for(ide_session(tmp_path), provider_with(page), monkeypatch)
    for url in ("http://localhost:5173/", "http://192.168.0.1/", "http://router.lan/"):
        result = await browser.browser_navigate(ctx, {"url": url})
        assert result.ok, result.error


async def test_a_public_site_is_fine_in_a_browser_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_dns: dict[str, str]
) -> None:
    ctx = ctx_for(browser_session(tmp_path), provider_with(FakePage()), monkeypatch)
    result = await browser.browser_navigate(ctx, {"url": "https://example.com/"})
    assert result.ok, result.error


async def test_the_user_naming_the_exact_address_allows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_dns: dict[str, str]
) -> None:
    session = browser_session(tmp_path)
    session.history.append(
        ChatMessage(role="user", content="Open my game at localhost:5173 and play it")
    )
    session.history.append(ChatMessage(role="assistant", content="also 192.168.0.1 maybe?"))
    ctx = ctx_for(session, provider_with(FakePage()), monkeypatch)
    assert (await browser.browser_navigate(ctx, {"url": "http://localhost:5173/game"})).ok
    # Another port, another address, or one only the model said: still refused.
    assert not (await browser.browser_navigate(ctx, {"url": "http://localhost:8080/"})).ok
    assert not (await browser.browser_navigate(ctx, {"url": "http://127.0.0.1:5173/"})).ok
    assert not (await browser.browser_navigate(ctx, {"url": "http://192.168.0.1/"})).ok


def test_user_named_matches_whole_addresses_only(tmp_path: Path) -> None:
    session = browser_session(tmp_path)
    session.history.append(
        ChatMessage(
            role="user",
            content=[{"type": "text", "text": "see http://10.0.0.5/ and [::1]:3000"}],
        )
    )
    core = SimpleNamespace()
    assert url_policy.user_named("http://10.0.0.5/x", session, core)
    assert not url_policy.user_named("http://10.0.0.50/", session, core)
    assert not url_policy.user_named("http://0.0.5/", session, core)
    assert url_policy.user_named("http://[::1]:3000/", session, core)
    assert not url_policy.user_named("http://[::1]:30/", session, core)


def test_a_subagent_uses_its_root_session_s_user_words(tmp_path: Path) -> None:
    root = browser_session(tmp_path)
    root.history.append(ChatMessage(role="user", content="test 127.0.0.1:8000 please"))
    child = browser_session(tmp_path)
    child.id = "child"
    child.parent_session_id = root.id
    child.history.append(ChatMessage(role="user", content="brief: open 192.168.0.1"))
    sessions = SimpleNamespace(get=lambda sid: root if sid == root.id else None)
    core = SimpleNamespace(sessions=sessions)
    assert url_policy.user_named("http://127.0.0.1:8000/", child, core)
    # The lead's brief is not the user's say-so.
    assert not url_policy.user_named("http://192.168.0.1/", child, core)


async def test_a_redirect_into_a_refused_address_returns_no_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_dns: dict[str, str]
) -> None:
    page = FakePage(redirect="http://192.168.0.1/admin")
    ctx = ctx_for(browser_session(tmp_path), provider_with(page), monkeypatch)
    result = await browser.browser_navigate(ctx, {"url": "https://example.com/"})
    assert not result.ok
    assert "url_blocked" in (result.error or "")
    assert "score 0" not in (result.error or "")


async def test_a_connected_browser_is_not_policed_by_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Host:
        meta = SimpleNamespace(id="host", label="host")

        def available(self, settings: Any = None) -> bool:
            return True

        async def navigate(self, session_id: str, url: str) -> PageState:
            calls.append(url)
            return PageState(text="ok")

    ctx = ctx_for(browser_session(tmp_path), Host(), monkeypatch)
    result = await browser.browser_navigate(ctx, {"url": "http://192.168.0.1/"})
    assert result.ok and calls == ["http://192.168.0.1/"]


# ---------------------------------------------------------------------------
# the real browser
# ---------------------------------------------------------------------------

GAME = """<!doctype html><html><body style="margin:0">
<canvas id="game" width="200" height="120"></canvas>
<p id="out">idle</p>
<script>
  const out = document.getElementById("out");
  const gl = document.getElementById("game").getContext("2d");
  gl.fillStyle = "#3a3"; gl.fillRect(0, 0, 200, 120);
  window.addEventListener("keydown", e => { out.textContent = "down " + e.key; });
  window.addEventListener("keyup", e => { out.textContent += " up " + e.key; });
  console.error("texture missing: hero.png");
  console.log("just a log");
</script></body></html>"""


@pytest.mark.timeout(90)
@pytest.mark.skipif(
    os.environ.get("SNOWPEA_SKIP_BROWSER_TESTS") == "1", reason="SNOWPEA_SKIP_BROWSER_TESTS=1"
)
async def test_real_chromium_screenshots_presses_and_reports_console(tmp_path: Path) -> None:
    pytest.importorskip("playwright.async_api")
    page_file = tmp_path / "game.html"
    page_file.write_text(GAME)
    provider = LocalChromiumProvider()
    try:
        try:
            state = await provider.navigate("real", page_file.as_uri())
        except browser_providers.BrowserNotInstalled:
            pytest.skip("run `uv run playwright install chromium` first")
        assert state.console == ("[error] texture missing: hero.png",)

        shot = await provider.screenshot("real")
        assert shot.image and shot.image.startswith(b"\x89PNG")
        element = await provider.screenshot("real", selector="#game")
        assert element.image and len(element.image) < len(shot.image)

        pressed = await provider.press("real", "w", hold_ms=50)
        assert "down w up w" in pressed.text
        assert (await provider.console("real")).console == ()
    finally:
        await provider.close()
