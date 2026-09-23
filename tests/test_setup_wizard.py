"""US-012 — the ``snowpea setup`` wizard (M3 contract §5, AC-02 / AC-02b).

Everything here runs without a TTY, which is the point: the screens must
auto-apply their defaults rather than block on a keystroke that will never
arrive.  The interactive path is exercised through ``ui.ask(interactive=True)``
with a scripted key stream.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.cli import commands as cli_commands
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.setup import catalog, ui, wizard
from snowpea_core.setup.screens import SKIP, Screen, ScreenItem, skip_item
from snowpea_core.setup.screens import browser as browser_screen
from snowpea_core.setup.screens import gateway as gateway_screen
from snowpea_core.setup.screens import providers as providers_screen
from snowpea_core.setup.screens import search as search_screen
from snowpea_core.setup.screens import tools as tools_screen
from snowpea_core.setup.state import WizardState


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "snowpea-home"


def _settings(home: Path) -> Settings:
    return Settings.load(Paths(home=home))


def _json(home: Path) -> dict:
    return json.loads((home / "settings.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# catalogs
# ---------------------------------------------------------------------------


def test_search_catalog_is_free_first_and_starred() -> None:
    """AC-02b: free·no-key → free·key/self-hosted → paid, default first."""
    items = catalog.search_catalog()
    assert items[0].id == "ddgs"
    assert items[0].default is True
    assert sum(1 for item in items if item.default) == 1

    ranks = [item.rank() for item in items]
    assert ranks == sorted(ranks)
    # The three buckets are all populated, so the assertion is not vacuous.
    assert set(ranks) == {0, 1, 2}
    assert items[-1].tier == "paid"
    # assert_free_first is what the builders call; it must accept its own output.
    assert catalog.assert_free_first(items) == items


def test_search_catalog_rejects_a_bad_order() -> None:
    paid = catalog.CatalogItem(id="x", label="X", tier="paid", key="key required", default=True)
    free = catalog.CatalogItem(id="y", label="Y", tier="free", key="no key")
    with pytest.raises(catalog.CatalogOrderError):
        catalog.assert_free_first([paid, free])


def test_browser_catalog_defaults_to_local_chromium() -> None:
    items = catalog.browser_catalog()
    assert items[0].id == "local_chromium"
    assert items[0].default is True
    assert items[0].active is True
    ranks = [item.rank() for item in items]
    assert ranks == sorted(ranks)


def test_tools_catalog_matches_the_contract_table() -> None:
    by_id = {item.id: item for item in catalog.tools_catalog()}
    on = {
        "file",
        "terminal",
        "git",
        "web",
        "browser",
        "delegate",
        "schedule",
        "memory",
        "skills",
        "todo",
        "session-search",
        "clarify",
        "cron",
    }
    for cid in on:
        assert by_id[cid].default is True, cid
        assert by_id[cid].active is True, cid
    for cid in ("media-image", "media-video", "media-tts"):
        assert by_id[cid].default is True, cid
        assert by_id[cid].active is False, cid
    for cid in ("vision", "computer-use", "x-search"):
        assert by_id[cid].default is False, cid
    assert set(catalog.default_enabled_categories()) == on | {
        "media-image",
        "media-video",
        "media-tts",
    }


def test_gateway_catalog_is_all_off() -> None:
    items = catalog.gateway_catalog()
    assert [item.id for item in items] == ["telegram", "discord", "slack"]
    assert not any(item.default for item in items)


def test_vendor_catalog_lists_eleven_with_login_tags() -> None:
    items = catalog.vendor_catalog(Settings())
    assert len(items) == 11
    logins = {item.id: catalog.vendor_auth_tags(item.id) for item in items}
    assert "device_code" in logins["openai"]
    assert "oauth_pkce" in logins["openrouter"]
    assert "google_adc" in logins["gemini"]
    assert "oauth_token" in logins["gemini"]
    assert sum(1 for tags in logins.values() if set(tags) - {"api_key"}) == 3


def test_vendor_catalog_marks_configured_vendors_active() -> None:
    settings = Settings()
    settings.providers["deepseek"] = {"api_key": "sk-test"}
    by_id = {item.id: item for item in catalog.vendor_catalog(settings)}
    assert by_id["deepseek"].active is True


def test_wizard_provider_prompt_accepts_remote_oauth_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gemini's menu is now [1=API key, 2=browser login (Google),
    # 3=gcloud ADC (headless), 4=OAuth token].
    answers = iter(["4", "ya29.remote"])
    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: next(answers))
    state = WizardState.from_settings(Settings())
    state.select_vendor("gemini")
    wizard._ask_for_key(state, interactive=True)  # noqa: SLF001
    state.remember_current_provider()
    assert state.provider_configs["gemini"] == {
        "oauth_token": "ya29.remote",
        "auth_method": "oauth_token",
    }


def test_wizard_provider_prompt_runs_browser_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from snowpea_core.providers import auth_web

    async def logged_in(vendor: str, method: str | None = None):
        assert method == "google_oauth"
        return auth_web.LoginResult(
            vendor=vendor,
            method="google_oauth",
            credentials={
                "auth_method": "google_oauth",
                "access_token": "ya29.at",
                "api_key": None,
            },
            message="signed in",
        )

    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: "2")
    monkeypatch.setattr(auth_web, "login", logged_in)
    state = WizardState.from_settings(Settings())
    state.select_vendor("gemini")
    wizard._ask_for_key(state, interactive=True)  # noqa: SLF001
    # ``api_key: None`` is a clearing instruction, not a credential: it is what
    # removes a stale key from settings.json when the block is merged.
    assert state.provider_configs["gemini"] == {
        "auth_method": "google_oauth",
        "access_token": "ya29.at",
        "api_key": None,
    }
    assert state.notes == ["signed in"]


def test_wizard_browser_login_403_reprompts_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A device-authorization 403 must never end the wizard with a traceback:
    it prints ``login failed: ...`` plus the 403 hint, then re-asks the
    authentication choice — this test picks "1" (API key) on the second ask
    and confirms the wizard keeps going normally."""
    from snowpea_core.providers import auth_web
    from snowpea_core.server.errors import RpcError

    async def failing_login(vendor: str, method: str | None = None):
        raise RpcError(
            "internal",
            f"{vendor}: device authorization failed (HTTP 403): access_denied",
            data={"vendor": vendor, "status": 403, "body": "access_denied"},
        )

    answers = iter(["2", "1", "sk-fallback-key"])
    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: next(answers))
    monkeypatch.setattr(auth_web, "login", failing_login)
    state = WizardState.from_settings(Settings())
    state.select_vendor("openai")

    wizard._ask_for_key(state, interactive=True)  # noqa: SLF001

    out = capsys.readouterr().out
    assert "login failed:" in out
    assert "access_denied" in out or "403" in out
    assert "hint:" in out
    assert "paste an OAuth token" in out
    # The wizard recovered and accepted the fallback API key.
    assert state.api_key == "sk-fallback-key"


def test_wizard_browser_login_ctrl_c_leaves_vendor_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl+C during a browser login must not crash the wizard — it should
    leave the vendor unconfigured and let the rest of setup continue."""
    from snowpea_core.providers import auth_web

    async def interrupted_login(vendor: str, method: str | None = None):
        raise KeyboardInterrupt

    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: "2")
    monkeypatch.setattr(auth_web, "login", interrupted_login)
    state = WizardState.from_settings(Settings())
    state.select_vendor("openai")

    wizard._ask_for_key(state, interactive=True)  # noqa: SLF001

    assert "openai" not in state.provider_configs
    assert state.api_key is None
    assert any("cancelled" in note for note in state.notes)


# ---------------------------------------------------------------------------
# screens (pure build/apply)
# ---------------------------------------------------------------------------


def test_every_screen_ends_with_skip() -> None:
    state = WizardState.from_settings(Settings())
    for module in (
        providers_screen,
        search_screen,
        browser_screen,
        tools_screen,
        gateway_screen,
    ):
        screen = module.build(state)
        assert screen.items[-1].id == SKIP
        # Drawn from state: "Done — keep X" with a selection, "Skip — decide
        # later" without one; the static label is only the placeholder.
        assert screen.items[-1].label == "Skip — keep defaults"
        body = "\n".join(ui.render_lines(screen))
        assert ("Done — keep " in body) or ("Skip — decide later" in body)
        assert "Skip — keep defaults" not in body


def test_search_screen_apply_sets_the_provider() -> None:
    state = WizardState.from_settings(Settings())
    search_screen.apply(state, "tavily")
    assert state.search_provider == "tavily"
    search_screen.apply(state, SKIP)
    assert state.search_provider == "tavily"


def test_providers_screen_apply_and_skip() -> None:
    state = WizardState.from_settings(Settings())
    providers_screen.apply(state, SKIP)
    assert state.vendor is None
    providers_screen.apply(state, "anthropic")
    assert state.vendor == "anthropic"


def test_browser_screen_apply() -> None:
    state = WizardState.from_settings(Settings())
    browser_screen.apply(state, "camoufox")
    assert state.browser_provider == "camoufox"


def test_tools_screen_apply_replaces_the_whole_set() -> None:
    state = WizardState.from_settings(Settings())
    tools_screen.apply(state, {"file", "git"})
    assert state.enabled_categories() == ["file", "git"]
    assert state.tool_categories["web"] is False


def test_gateway_screen_apply_toggles() -> None:
    state = WizardState.from_settings(Settings())
    gateway_screen.apply(state, {"telegram"})
    assert state.gateways["telegram"]["enabled"] is True
    gateway_screen.apply(state, set())
    assert state.gateways["telegram"]["enabled"] is False


def test_screen_default_choice_prefers_the_selection_then_the_default() -> None:
    state = WizardState.from_settings(Settings())
    assert search_screen.build(state).default_choice == "ddgs"
    state.search_provider = "tavily"
    assert search_screen.build(state).default_choice == "tavily"


def test_tools_flag_parsing() -> None:
    state = WizardState.from_settings(Settings())
    assert state.apply_tools_flag("vision,-git") == []
    assert state.tool_categories["vision"] is True
    assert state.tool_categories["git"] is False
    assert state.apply_tools_flag("nope") == ["nope"]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_render_shows_tags_the_star_and_the_radio() -> None:
    state = WizardState.from_settings(Settings())
    lines = ui.render_lines(search_screen.build(state))
    body = "\n".join(lines)
    assert "★" in body
    assert "[free · no key]" in body
    assert "(●)" in body and "(○)" in body
    assert "Done — keep " in body


def test_render_multi_select_uses_checkboxes() -> None:
    state = WizardState.from_settings(Settings())
    body = "\n".join(ui.render_lines(tools_screen.build(state)))
    assert "[✓]" in body and "[ ]" in body
    assert "[inactive]" in body  # the media categories


def test_ask_without_a_tty_returns_the_defaults() -> None:
    state = WizardState.from_settings(Settings())
    screen = search_screen.build(state)
    assert ui.ask(screen, interactive=False) == "ddgs"
    tools = tools_screen.build(state)
    assert ui.ask(tools, interactive=False) == set(catalog.default_enabled_categories())


def test_ask_interactive_moves_with_the_arrow_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = iter(["down", "enter"])
    monkeypatch.setattr(ui, "read_key", lambda stream=None: next(keys))
    state = WizardState.from_settings(Settings())
    screen = search_screen.build(state)
    from rich.console import Console

    console = Console(file=io.StringIO(), width=100, force_terminal=False)
    assert ui.ask(screen, console=console, interactive=True) == screen.items[1].id


def test_ask_interactive_enter_on_skip_returns_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = Screen(
        title="t",
        items=(
            ScreenItem("a", "A", (), False, True),
            ScreenItem(SKIP, "Skip — keep defaults", (), False, False),
        ),
        multi=False,
    )
    keys = iter(["down", "enter"])
    monkeypatch.setattr(ui, "read_key", lambda stream=None: next(keys))
    from rich.console import Console

    console = Console(file=io.StringIO(), width=100, force_terminal=False)
    assert ui.ask(screen, console=console, interactive=True) == "a"


def test_ask_repaints_in_place_without_clearing_the_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keypress must rewrite the menu rows, not redraw the terminal.

    Clearing the screen on every ↑/↓ is what made the whole wizard flicker, so
    this pins the mechanism: no clear-screen escape (and no ``Console.clear``
    call), a cursor-up of exactly the screen's height, erased rows, and the
    cursor hidden for as long as the menu is open.
    """
    from rich.console import Console

    cleared: list[bool] = []
    monkeypatch.setattr(Console, "clear", lambda self, home=True: cleared.append(True))
    keys = iter(["down", "down", "enter"])
    monkeypatch.setattr(ui, "read_key", lambda stream=None: next(keys))
    state = WizardState.from_settings(Settings())
    screen = search_screen.build(state)
    stream = io.StringIO()
    console = Console(file=stream, width=100, force_terminal=False)

    ui.ask(screen, console=console, interactive=True)

    out = stream.getvalue()
    height = len(ui.screen_lines(screen, cursor=0, chosen=set()))
    assert cleared == []
    assert "\x1b[2J" not in out and "\x1b[3J" not in out and "\x1bc" not in out
    assert out.count(f"\x1b[{height}A") == 2  # one per keypress that moved
    assert ui.CLEAR_LINE in out
    assert out.startswith(ui.CURSOR_HIDE)
    assert out.endswith(ui.CURSOR_SHOW)


def test_ask_restores_the_cursor_when_the_screen_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rich.console import Console

    def boom(stream: Any = None) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(ui, "read_key", boom)
    state = WizardState.from_settings(Settings())
    stream = io.StringIO()
    console = Console(file=stream, width=100, force_terminal=False)
    with pytest.raises(KeyboardInterrupt):
        ui.ask(search_screen.build(state), console=console, interactive=True)
    assert stream.getvalue().endswith(ui.CURSOR_SHOW)


# ---------------------------------------------------------------------------
# the shared choice key map (M15b §2, AC-M15b-4)
# ---------------------------------------------------------------------------


def _console() -> Any:
    from rich.console import Console

    return Console(file=io.StringIO(), width=100, force_terminal=False)


def _pick(monkeypatch: pytest.MonkeyPatch, screen: Screen, *keys: str) -> Any:
    stream = iter(keys)
    monkeypatch.setattr(ui, "read_key", lambda stream_=None: next(stream))
    return ui.ask(screen, console=_console(), interactive=True)


def _three(multi: bool = False) -> Screen:
    return Screen(
        title="t",
        items=(
            ScreenItem("a", "A", (), False, True),
            ScreenItem("b", "B", (), False, False),
            ScreenItem("c", "C", (), False, False),
        ),
        multi=multi,
    )


def test_hint_names_exactly_the_keys_the_screen_takes() -> None:
    assert ui.choice_hint(multi=False) == (
        "↑↓ move · Enter confirm · 1-9 pick · Esc cancel"
    )
    assert ui.choice_hint(multi=True) == (
        "↑↓ move · Space toggle · Enter confirm · 1-9 toggle · Esc cancel"
    )


def test_every_screen_prints_the_hint() -> None:
    state = WizardState.from_settings(Settings())
    body = "\n".join(ui.render_lines(search_screen.build(state)))
    assert ui.choice_hint(multi=False) in body
    multi = "\n".join(ui.render_lines(tools_screen.build(state)))
    assert ui.choice_hint(multi=True) in multi


def test_rows_are_numbered_so_the_digit_keys_are_visible() -> None:
    state = WizardState.from_settings(Settings())
    body = "\n".join(ui.render_lines(search_screen.build(state)))
    assert "1. " in body and "2. " in body


def test_a_digit_jumps_to_that_row_without_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _pick(monkeypatch, _three(), "3", "enter") == "c"


def test_a_digit_toggles_in_a_multi_select_and_enter_submits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _pick(monkeypatch, _three(multi=True), "2", "3", "enter") == {"b", "c"}


def test_a_digit_past_the_last_row_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _pick(monkeypatch, _three(), "9", "enter") == "a"


def test_space_toggles_only_in_a_multi_select(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _pick(monkeypatch, _three(multi=True), "space", "enter") == {"a"}
    # Single-select: Space is swallowed, so Enter still takes the cursor row.
    assert _pick(monkeypatch, _three(), "space", "down", "enter") == "b"


def test_escape_declines_to_the_screen_default(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = _three()
    assert _pick(monkeypatch, screen, "down", "escape") == screen.default_choice
    assert _pick(monkeypatch, screen, "down", "quit") == screen.default_choice


def test_left_and_right_are_accepted_and_change_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _pick(monkeypatch, _three(), "right", "left", "down", "enter") == "b"


def test_screen_height_is_stable_across_cursor_moves() -> None:
    state = WizardState.from_settings(Settings())
    screen = tools_screen.build(state)
    chosen = {item.id for item in screen.items if item.selected}
    heights = {
        len(ui.screen_lines(screen, cursor=index, chosen=chosen))
        for index in range(len(screen.items))
    }
    assert len(heights) == 1


# ---------------------------------------------------------------------------
# provider state
# ---------------------------------------------------------------------------


def test_configured_local_provider_reads_active_and_default() -> None:
    """A local server with a base_url and a model is set up — not ``[inactive]``.

    The wizard used to build the vendor list from an empty registry, so the
    provider the user had just configured and selected still printed
    ``[inactive]``.
    """
    settings = Settings()
    settings.providers["local"] = {"base_url": "http://localhost:8000/v1", "model": "qwen3"}
    settings.providers["default"] = "local"
    state = WizardState.from_settings(settings)
    state.add_current_model_profile(make_default=True)

    rows = {item.id: item for item in providers_screen.build(state).items}
    assert rows["local"].active is True
    assert "active" in rows["local"].tags
    assert "inactive" not in rows["local"].tags
    assert "default" in rows["local"].tags
    assert rows["anthropic"].active is False


def test_local_provider_active_state_agrees_with_the_registry() -> None:
    """One helper behind the screen, ``provider list`` and ``provider.list``."""
    from snowpea_core.providers.registry import ProviderRegistry

    settings = Settings()
    settings.providers["local"] = {"base_url": "http://localhost:8000/v1"}
    registry = ProviderRegistry(settings)
    assert registry.is_configured("local")
    assert registry.auth_status("local") == "active"
    info = {item.vendor: item for item in registry.list()}
    assert info["local"].configured is True
    by_id = {item.id: item for item in catalog.vendor_catalog(settings)}
    assert by_id["local"].active is True


def test_unconfigured_local_provider_stays_inactive() -> None:
    by_id = {item.id: item for item in catalog.vendor_catalog(Settings())}
    assert by_id["local"].active is False


# ---------------------------------------------------------------------------
# the wizard
# ---------------------------------------------------------------------------


def test_quick_non_interactive_writes_the_vendor_and_key(home: Path) -> None:
    """AC-02: Quick + a vendor key creates the provider entry."""
    result = wizard.run(
        "quick",
        home=home,
        vendor="anthropic",
        key="sk-ant-test",
        model="claude-haiku-4-5",
        interactive=False,
    )
    assert result.screens_shown == ["done"]
    assert "providers" in result.screens_answered

    data = _json(home)
    assert data["providers"]["anthropic"]["api_key"] == "sk-ant-test"
    assert data["providers"]["anthropic"]["model"] == "claude-haiku-4-5"
    assert data["providers"]["default"] == "anthropic"

    reloaded = _settings(home)
    assert reloaded.providers["anthropic"]["api_key"] == "sk-ant-test"
    assert reloaded.search.provider == "ddgs"


def test_full_with_every_screen_skipped_writes_the_free_defaults(home: Path) -> None:
    """AC-02b: no TTY → every screen auto-applies its default."""
    result = wizard.run("full", home=home, interactive=False)
    assert result.screens_shown == [
        "providers",
        "search",
        "browser",
        "audio",
        "tools",
        "gateway",
        "done",
    ]

    settings = _settings(home)
    assert settings.search.provider == "ddgs"
    assert settings.browser.provider == "local_chromium"
    # Voice stays unset, which means off: skipping every screen installs
    # nothing, and unset is the honest description of a machine with nothing.
    assert settings.audio.stt.provider is None
    assert settings.audio.tts.enabled is True
    assert settings.audio.tts.provider is None
    assert settings.audio.tts.autoSpeak is False
    assert settings.tools.enabled_categories == catalog.default_enabled_categories()
    assert settings.gateway == {}
    assert "default" not in settings.providers


def test_full_order_matches_the_contract() -> None:
    assert [name for name, _ in wizard.FULL_ORDER] == [
        "providers",
        "search",
        "browser",
        # Audio sits before tools: whether snowpea can listen and talk is part
        # of how it is used, not one of the tool categories (CORE-multimodal).
        "audio",
        "tools",
        "gateway",
        "done",
    ]


def test_blank_asks_nothing(home: Path) -> None:
    result = wizard.run("blank", home=home, interactive=False)
    assert result.screens_shown == []
    settings = _settings(home)
    assert settings.search.provider == "ddgs"
    assert settings.tools.enabled_categories == catalog.default_enabled_categories()


def test_search_provider_flag_answers_screen_two(home: Path) -> None:
    result = wizard.run("full", home=home, search_provider="tavily", interactive=False)
    assert "search" in result.screens_answered
    assert "search" not in result.screens_shown
    assert _settings(home).search.provider == "tavily"


def test_browser_and_tools_and_gateway_flags(home: Path) -> None:
    wizard.run(
        "full",
        home=home,
        browser_provider="camoufox",
        tools="vision,-git",
        gateway="telegram",
        token="bot-token",
        interactive=False,
    )
    settings = _settings(home)
    assert settings.browser.provider == "camoufox"
    assert "vision" in settings.tools.enabled_categories
    assert "git" not in settings.tools.enabled_categories
    assert settings.gateway["telegram"] == {"enabled": True, "token": "bot-token"}


@pytest.mark.parametrize(
    ("flags", "needle"),
    [
        ({"vendor": "nope"}, "unknown vendor"),
        ({"key": "sk-x"}, "--key needs --vendor"),
        ({"search_provider": "nope"}, "unknown search provider"),
        ({"browser_provider": "nope"}, "unknown browser provider"),
        ({"tools": "nope"}, "unknown tool categories"),
        ({"gateway": "nope"}, "unknown gateway"),
        ({"token": "t"}, "--token needs --gateway"),
    ],
)
def test_bad_flags_raise_setup_error(home: Path, flags: dict, needle: str) -> None:
    with pytest.raises(wizard.SetupError, match=needle):
        wizard.run("blank", home=home, interactive=False, **flags)


def test_unknown_mode_raises(home: Path) -> None:
    with pytest.raises(wizard.SetupError):
        wizard.run("sideways", home=home, interactive=False)  # type: ignore[arg-type]


def test_a_second_run_keeps_the_earlier_answers(home: Path) -> None:
    wizard.run("quick", home=home, vendor="deepseek", key="sk-1", interactive=False)
    wizard.run("full", home=home, search_provider="tavily", interactive=False)
    settings = _settings(home)
    assert settings.providers["deepseek"]["api_key"] == "sk-1"
    assert settings.providers["default"] == "deepseek"
    assert settings.search.provider == "tavily"


def test_detect_reports_env_keys_as_hints(home: Path) -> None:
    result = wizard.run(
        "blank",
        home=home,
        interactive=False,
        env={"ANTHROPIC_API_KEY": "sk-ant-env"},
    )
    assert any("ANTHROPIC_API_KEY" in hint for hint in result.state.hints)
    # A detected key is a hint, never a copy into settings.json.
    assert "anthropic" not in _settings(home).providers


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    from snowpea_core.cli.main import build_parser

    return build_parser().parse_args(argv)


def test_setup_is_no_longer_a_placeholder() -> None:
    assert "setup" not in cli_commands.PLACEHOLDER_SUBCOMMANDS


def test_cli_setup_blank_writes_settings(home: Path, capsys: pytest.CaptureFixture) -> None:
    code = cli_commands.setup_command(_parse(["setup", "--blank"]), home)
    assert code == 0
    out = capsys.readouterr().out
    assert "settings written to" in out
    assert "search     ddgs" in out
    assert (home / "settings.json").exists()


def test_cli_setup_quick_with_vendor(home: Path) -> None:
    code = cli_commands.setup_command(
        _parse(["setup", "--quick", "--vendor", "openai", "--key", "sk-test"]), home
    )
    assert code == 0
    assert _settings(home).providers["openai"]["api_key"] == "sk-test"


def test_cli_setup_bad_flag_exits_two(home: Path) -> None:
    assert cli_commands.setup_command(_parse(["setup", "--vendor", "nope"]), home) == 2


def test_cli_setup_login_unsupported_exits_two(home: Path, capsys: pytest.CaptureFixture) -> None:
    """AC-02: the nine API-key-only vendors answer ``login_unsupported``."""
    code = cli_commands.setup_command(_parse(["setup", "--login", "deepseek"]), home)
    assert code == 2
    err = capsys.readouterr().err
    assert 'code:"login_unsupported"' in err
    assert "--vendor deepseek --key" in err


def test_cli_setup_login_unknown_vendor_exits_two(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    assert cli_commands.setup_command(_parse(["setup", "--login", "bogus"]), home) == 2
    assert "login_unsupported" in capsys.readouterr().err


def test_cli_setup_login_delegates_to_provider_login(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two supported vendors go through ``snowpea provider login``."""
    seen: list[str] = []

    async def fake_provider_login(vendor: str, where=None) -> int:
        seen.append(vendor)
        return 0

    monkeypatch.setattr(cli_commands, "provider_login", fake_provider_login)
    assert cli_commands.setup_command(_parse(["setup", "--login", "openai"]), home) == 0
    assert cli_commands.setup_command(_parse(["setup", "--login", "openrouter"]), home) == 0
    assert seen == ["openai", "openrouter"]


def test_parser_accepts_every_documented_flag() -> None:
    args = _parse(
        [
            "setup",
            "--full",
            "--vendor",
            "glm",
            "--key",
            "k",
            "--model",
            "m",
            "--search-provider",
            "tavily",
            "--browser-provider",
            "camoufox",
            "--tools",
            "vision,-git",
            "--gateway",
            "telegram",
            "--token",
            "t",
        ]
    )
    assert args.full is True
    assert (args.vendor, args.key, args.model) == ("glm", "k", "m")
    assert args.search_provider == "tavily"
    assert args.browser_provider == "camoufox"
    assert args.tools == "vision,-git"
    assert (args.gateway, args.token) == ("telegram", "t")


# ---------------------------------------------------------------------------
# the search key prompt (CORE-search-fix)
# ---------------------------------------------------------------------------


def test_search_key_flag_is_saved_under_the_provider(home: Path) -> None:
    wizard.run("full", home=home, search_provider="exa", search_key="exa-secret",
               interactive=False)
    settings = _settings(home)
    assert settings.search.provider == "exa"
    assert settings.search.credentials["exa"]["api_key"] == "exa-secret"


def test_interactive_search_provider_flag_still_prompts_for_its_key(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag skips provider selection, but must not skip credentials."""
    asked: list[tuple[str, bool]] = []

    def fake_ask_text(prompt: str, *, secret: bool = False) -> str:
        asked.append((prompt, secret))
        return "flag-selected-key"

    monkeypatch.setattr(ui, "ask_text", fake_ask_text)
    wizard.run(
        "full",
        home=home,
        search_provider="exa",
        interactive=True,
        ask=lambda screen, **kwargs: SKIP,
    )

    assert any("Exa" in prompt and secret for prompt, secret in asked), asked
    settings = _settings(home)
    assert settings.search.credentials["exa"]["api_key"] == "flag-selected-key"


def test_search_key_flag_does_not_prompt_for_the_key_again(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        ui, "ask_text", lambda prompt, *, secret=False: asked.append(prompt) or "unexpected"
    )

    wizard.run(
        "full",
        home=home,
        search_provider="exa",
        search_key="provided-key",
        interactive=True,
        ask=lambda screen, **kwargs: SKIP,
    )

    assert not any("Exa" in prompt and "API key" in prompt for prompt in asked)
    assert _settings(home).search.credentials["exa"]["api_key"] == "provided-key"


def test_exa_free_mcp_never_prompts_for_an_api_key(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        ui, "ask_text", lambda prompt, *, secret=False: asked.append(prompt) or "unexpected"
    )

    result = wizard.run(
        "full",
        home=home,
        search_provider="exa_free",
        interactive=True,
        ask=lambda screen, **kwargs: SKIP,
    )

    assert not any("API key" in prompt for prompt in asked)
    assert result.settings.search.provider == "exa_free"
    assert "no API key" not in "\n".join(result.summary())


def test_search_key_without_a_provider_is_a_usage_error(home: Path) -> None:
    with pytest.raises(wizard.SetupError, match="--search-key needs --search-provider"):
        wizard.run("full", home=home, search_key="orphan", interactive=False)


def test_a_key_required_provider_without_a_key_warns_in_the_summary(home: Path) -> None:
    """The silent-fallback bug, caught at setup time instead of at search time."""
    result = wizard.run("full", home=home, search_provider="exa", interactive=False)
    notes = "\n".join(result.summary())
    assert "exa: no API key" in notes
    assert "EXA_API_KEY" in notes


def test_the_search_screen_prompts_for_the_key(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Choosing a key-required provider asks for the key, masked."""
    asked: list[tuple[str, bool]] = []

    def fake_ask_text(prompt: str, *, secret: bool = False) -> str:
        asked.append((prompt, secret))
        return "typed-key"

    monkeypatch.setattr(ui, "ask_text", fake_ask_text)

    def asker(screen: Screen, console=None, interactive=True):  # type: ignore[no-untyped-def]
        if screen.title == search_screen.TITLE:
            return "exa"
        return SKIP

    wizard.run("full", home=home, interactive=True, ask=asker)

    assert any("Exa" in prompt and secret for prompt, secret in asked), asked
    settings = _settings(home)
    assert settings.search.provider == "exa"
    assert settings.search.credentials["exa"]["api_key"] == "typed-key"


def test_an_empty_answer_leaves_the_warning(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui, "ask_text", lambda prompt, *, secret=False: "")

    def asker(screen: Screen, console=None, interactive=True):  # type: ignore[no-untyped-def]
        return "tavily" if screen.title == search_screen.TITLE else SKIP

    result = wizard.run("full", home=home, interactive=True, ask=asker)

    notes = "\n".join(result.summary())
    assert "tavily: no API key" in notes
    assert not _settings(home).search.credentials.get("tavily", {}).get("api_key")


def test_a_keyless_provider_is_never_asked_for_a_key(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        ui, "ask_text", lambda prompt, *, secret=False: asked.append(prompt) or ""
    )

    def asker(screen: Screen, console=None, interactive=True):  # type: ignore[no-untyped-def]
        return "ddgs" if screen.title == search_screen.TITLE else SKIP

    result = wizard.run("full", home=home, interactive=True, ask=asker)

    assert not any("API key" in prompt for prompt in asked)
    assert "no API key" not in "\n".join(result.summary())


# ---------------------------------------------------------------------------
# CORE-codex-login phase B: the two credential bugs the wizard used to have
# ---------------------------------------------------------------------------


def test_openrouter_browser_login_keeps_the_api_key_it_just_minted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug: the wizard cleared stale credentials *after* storing the fresh
    ones, so OpenRouter's PKCE login deleted the key it had just been given —
    the run ended with the default vendor unconfigured while the summary said
    ``API key stored`` (report §6.7 A-P1-1)."""
    from snowpea_core.providers import auth_web

    async def logged_in(vendor: str, method: str | None = None):
        return auth_web.LoginResult(
            vendor=vendor,
            method="oauth_pkce",
            credentials={
                "api_key": "sk-or-v1-fresh",
                "oauth_token": None,
                "token": None,
                "auth_method": None,
            },
            message="openrouter: API key stored",
        )

    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: "2")
    monkeypatch.setattr(auth_web, "login", logged_in)
    state = WizardState.from_settings(Settings())
    state.select_vendor("openrouter")

    wizard._ask_for_key(state, interactive=True)  # noqa: SLF001
    state.remember_current_provider()

    assert state.api_key == "sk-or-v1-fresh"
    assert state.provider_configs["openrouter"]["api_key"] == "sk-or-v1-fresh"
    assert state.notes == ["openrouter: API key stored"]


def test_a_browser_login_clears_the_api_key_it_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror image (A-P1-2): after signing in with ChatGPT, an API key
    from an earlier setup must not survive to win in ``api_key_for``."""
    from snowpea_core.providers import auth_web

    async def logged_in(vendor: str, method: str | None = None):
        return auth_web.LoginResult(
            vendor=vendor,
            method="browser_pkce",
            credentials={
                "auth_method": "chatgpt",
                "access_token": "at",
                "refresh_token": "rt",
                "api_key": None,
                "oauth_token": None,
            },
            message="openai: signed in with ChatGPT",
        )

    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: "2")
    monkeypatch.setattr(auth_web, "login", logged_in)
    state = WizardState.from_settings(Settings())
    state.provider_configs["openai"] = {"api_key": "sk-old", "model": "gpt-4.1"}
    state.select_vendor("openai")

    wizard._ask_for_key(state, interactive=True)  # noqa: SLF001
    state.remember_current_provider()

    block = state.provider_configs["openai"]
    # ``None`` is what removes the field from settings.json on merge.
    assert block["api_key"] is None
    assert block["auth_method"] == "chatgpt"
    assert block["access_token"] == "at"
    assert state.api_key is None
    # Unrelated settings in the block survive the login.
    assert block["model"] == "gpt-4.1"


def test_a_pasted_oauth_token_is_probed_before_it_is_stored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pasted token is the one credential nobody can check by eye, so the
    wizard makes one authenticated call and warns rather than waiting for an
    opaque 401 on the first prompt (report §6.7 A-P2-2)."""
    from snowpea_core.providers import models as model_discovery

    probed: list[str | None] = []

    async def listing(preset, *, api_key=None, **_kwargs):
        probed.append(api_key)
        raise RuntimeError("HTTP 401: invalid authentication")

    monkeypatch.setattr(model_discovery, "list_models", listing)
    answers = iter(["4", "ya29.expired"])
    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: next(answers))
    state = WizardState.from_settings(Settings())
    state.select_vendor("gemini")

    wizard._ask_for_key(state, interactive=True)  # noqa: SLF001

    assert probed == ["ya29.expired"]
    out = capsys.readouterr().out
    assert "warning:" in out and "401" in out
    # Warned, not refused: the probe can fail for reasons unrelated to the token.
    assert state.oauth_token == "ya29.expired"
    assert state.auth_method == "oauth_token"


def test_the_authentication_menu_lists_every_flow_the_vendor_supports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    def ask(prompt: str, **_kwargs: Any) -> str:
        prompts.append(prompt)
        return "1" if "authentication" in prompt else ""

    monkeypatch.setattr(ui, "ask_text", ask)
    state = WizardState.from_settings(Settings())
    state.select_vendor("openai")
    wizard._ask_for_key(state, interactive=True)  # noqa: SLF001

    menu = next(p for p in prompts if "authentication" in p)
    assert "1=API key" in menu
    assert "browser login" in menu
    assert "device code (headless)" in menu
    assert "OAuth token" in menu


def test_a_browser_login_uses_the_declared_model_list_instead_of_the_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ChatGPT sign-in has no API key and no ``/models`` endpoint; asking
    api.openai.com right after the login used to print a 401 and demand a
    model id by hand (live report, 2026-09-13)."""
    from snowpea_core.providers import models as model_discovery
    from snowpea_core.providers.codex_transport import CODEX_MODELS

    async def listing(preset, **_kwargs):
        raise AssertionError("the vendor API must not be asked for an OAuth account")

    async def no_codex_catalog(_credentials, **_kwargs):
        # The Codex backend is unreachable in a unit test; the curated list is
        # the rung that must answer, and silently (the live path has its own
        # MockTransport tests in tests/test_model_resolution.py).
        return []

    monkeypatch.setattr(model_discovery, "list_models", listing)
    monkeypatch.setattr(model_discovery, "codex_catalog", no_codex_catalog)
    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: "")
    state = WizardState.from_settings(Settings())
    state.select_vendor("openai")
    wizard._apply_login(  # noqa: SLF001
        state, {"auth_method": "chatgpt", "access_token": "tok", "refresh_token": "r"}
    )

    wizard._ask_for_model(state, interactive=True)  # noqa: SLF001

    out = capsys.readouterr().out
    assert "could not list models" not in out
    assert state.model == CODEX_MODELS[0]


def test_add_another_model_accepts_a_row_number_and_rejects_unknown_ids(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Typing ``1`` at the provider-id prompt used to raise ``KeyError: '1'``
    (live report, 2026-09-13); a number now means the row, and a typo is
    reported and re-asked rather than crashing the wizard."""
    monkeypatch.setattr(ui, "is_interactive", lambda *a, **kw: False)
    # bogus id → re-ask; Enter ends the loop; Enter keeps the default model;
    # Enter finishes the agent assignments.
    answers = iter(["bogus", "", "", ""])
    monkeypatch.setattr(ui, "ask_text", lambda *a, **kw: next(answers, ""))
    state = WizardState.from_settings(Settings())
    state.select_vendor("anthropic")
    state.api_key = "k"
    state.model = "claude"

    wizard._configure_models(state, interactive=True, console=None, home=Path("/tmp"))  # noqa: SLF001

    assert "unknown provider id: bogus" in capsys.readouterr().out


def test_configured_vendors_render_as_filled_circles() -> None:
    from snowpea_core.setup.screens import ScreenItem

    configured = ScreenItem("openai", "OpenAI", ("paid", "active", ui.CONFIGURED), False, False)
    plain = ScreenItem("xai", "xAI", ("paid",), False, False)
    assert "(●)" in ui.render_item(configured, multi=False, selected=False, cursor=False).plain
    assert "(○)" in ui.render_item(plain, multi=False, selected=False, cursor=False).plain
    rendered = ui.render_item(configured, multi=False, selected=False, cursor=False).plain
    assert "[configured]" not in rendered


def test_first_run_model_setup_connects_every_configured_vendor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wizard registers every chosen vendor/model, not a vendor allowlist."""
    models = {
        "openai": "gpt-5",
        "anthropic": "claude-sonnet-4-5",
        "gemini": "gemini-2.5-pro",
        "xai": "grok-4",
        "minimax": "MiniMax-M2.1",
        "deepseek": "deepseek-reasoner",
    }
    pending = iter(["anthropic", "gemini", "xai", "minimax", "deepseek", None])

    def pick(title: str, *_args: Any, **_kwargs: Any) -> str | None:
        if title == "add another model":
            return next(pending)
        if title == "default model":
            return "deepseek:deepseek-reasoner"
        if title == "assign model to agent":
            return None
        return None

    def credentials(state: WizardState, **_kwargs: Any) -> None:
        state.api_key = f"{state.vendor}-key"

    def choose_model(state: WizardState, **_kwargs: Any) -> None:
        state.model = models[state.vendor or ""]

    monkeypatch.setattr(ui, "is_interactive", lambda: True)
    monkeypatch.setattr(wizard, "_menu_pick", pick)
    monkeypatch.setattr(wizard, "_ask_for_key", credentials)
    monkeypatch.setattr(wizard, "_ask_for_model", choose_model)

    state = WizardState.from_settings(Settings())
    state.select_vendor("openai")
    state.api_key = "openai-key"
    state.model = models["openai"]
    wizard._configure_models(state, interactive=True, console=None, home=tmp_path)  # noqa: SLF001

    assert set(state.model_profiles) == {f"{vendor}:{model}" for vendor, model in models.items()}
    assert state.default_model == "deepseek:deepseek-reasoner"


def test_vendor_menu_rows_carry_each_tag_once() -> None:
    state = WizardState.from_settings(Settings())
    state.select_vendor("local")
    state.base_url = "http://localhost:11434/v1"
    state.model = "llama"
    rows = {vendor: tags for vendor, _label, tags in wizard._vendor_options(state)}  # noqa: SLF001
    assert rows["local"].count("active") == 1
    assert ui.CONFIGURED in rows["local"]


# ---------------------------------------------------------------------------
# the browser key prompt (CORE-setup-browser-key)
# ---------------------------------------------------------------------------


def test_picking_a_paid_browser_provider_asks_for_its_key(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug: the choice was saved and the credential never asked for."""
    asked: list[tuple[str, bool]] = []

    def fake_ask_text(prompt: str, *, secret: bool = False) -> str:
        asked.append((prompt, secret))
        return "fc-secret" if "API key" in prompt else "answer"

    monkeypatch.setattr(ui, "ask_text", fake_ask_text)
    monkeypatch.setattr(wizard, "_probe_browser", lambda *a, **kw: None)

    wizard.run(
        "full",
        home=home,
        interactive=True,
        ask=lambda screen, **kwargs: (
            "firecrawl_cloud" if screen.title.startswith("③") else SKIP
        ),
    )

    assert any("Firecrawl" in prompt and "API key" in prompt and secret
               for prompt, secret in asked), asked
    settings = _settings(home)
    assert settings.browser.provider == "firecrawl_cloud"
    assert settings.browser.credentials["firecrawl_cloud"]["api_key"] == "fc-secret"


def test_a_provider_needing_two_values_is_asked_for_both(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Browserbase wants a project id as well; half a credential is no credential."""
    asked: list[tuple[str, bool]] = []

    def fake_ask_text(prompt: str, *, secret: bool = False) -> str:
        asked.append((prompt, secret))
        return "bb-key" if "API key" in prompt else "proj-123"

    monkeypatch.setattr(ui, "ask_text", fake_ask_text)
    monkeypatch.setattr(wizard, "_probe_browser", lambda *a, **kw: None)

    wizard.run(
        "full",
        home=home,
        interactive=True,
        ask=lambda screen, **kwargs: "browserbase" if screen.title.startswith("③") else SKIP,
    )

    assert any("BROWSERBASE_PROJECT_ID" in prompt for prompt, _ in asked), asked
    # The project id is an identifier, not a secret, so it is not masked.
    assert all(
        not secret for prompt, secret in asked if "BROWSERBASE_PROJECT_ID" in prompt
    )
    block = _settings(home).browser.credentials["browserbase"]
    assert block["api_key"] == "bb-key"
    assert block["browserbase_project_id"] == "proj-123"


def test_a_keyless_browser_provider_is_never_asked_for_one(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        ui, "ask_text", lambda prompt, *, secret=False: asked.append(prompt) or ""
    )

    wizard.run(
        "full",
        home=home,
        interactive=True,
        ask=lambda screen, **kwargs: (
            "local_chromium" if screen.title.startswith("③") else SKIP
        ),
    )

    assert not any("API key" in prompt and "Chromium" in prompt for prompt in asked)
    assert _settings(home).browser.provider == "local_chromium"


def test_enter_keeps_an_already_saved_browser_key(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wizard.run(
        "full",
        home=home,
        browser_provider="firecrawl_cloud",
        browser_key="first-key",
        interactive=False,
    )
    assert _settings(home).browser.credentials["firecrawl_cloud"]["api_key"] == "first-key"

    prompts: list[str] = []
    monkeypatch.setattr(
        ui, "ask_text", lambda prompt, *, secret=False: prompts.append(prompt) or ""
    )
    monkeypatch.setattr(wizard, "_probe_browser", lambda *a, **kw: None)
    wizard.run(
        "full",
        home=home,
        interactive=True,
        ask=lambda screen, **kwargs: (
            "firecrawl_cloud" if screen.title.startswith("③") else SKIP
        ),
    )

    assert any("saved — Enter to keep" in prompt for prompt in prompts), prompts
    assert _settings(home).browser.credentials["firecrawl_cloud"]["api_key"] == "first-key"


def test_an_empty_answer_with_nothing_saved_is_refused_in_words(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    said: list[str] = []
    monkeypatch.setattr(ui, "ask_text", lambda prompt, *, secret=False: "")

    result = wizard.run(
        "full",
        home=home,
        interactive=True,
        console=SimpleNamespace(print=lambda text="", **kw: said.append(str(text))),
        ask=lambda screen, **kwargs: (
            "firecrawl_cloud" if screen.title.startswith("③") else SKIP
        ),
    )

    assert any("a key is required" in line for line in said), said
    notes = "\n".join(result.summary())
    assert "firecrawl_cloud: no API key" in notes
    assert "FIRECRAWL_API_KEY" in notes


def test_the_browser_key_flag_is_saved_and_does_not_prompt_again(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        ui, "ask_text", lambda prompt, *, secret=False: asked.append(prompt) or "unexpected"
    )

    wizard.run(
        "full",
        home=home,
        browser_provider="browserbase",
        browser_key="flag-key",
        interactive=True,
        ask=lambda screen, **kwargs: SKIP,
    )

    assert not any("API key" in prompt and "Browserbase" in prompt for prompt in asked)
    assert _settings(home).browser.credentials["browserbase"]["api_key"] == "flag-key"


def test_the_browser_provider_flag_alone_still_prompts_interactively(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag answers the screen; it must not answer the credential."""
    asked: list[str] = []

    def fake_ask_text(prompt: str, *, secret: bool = False) -> str:
        asked.append(prompt)
        return "prompted-key" if "API key" in prompt else "p-1"

    monkeypatch.setattr(ui, "ask_text", fake_ask_text)
    monkeypatch.setattr(wizard, "_probe_browser", lambda *a, **kw: None)

    wizard.run(
        "full",
        home=home,
        browser_provider="firecrawl_cloud",
        interactive=True,
        ask=lambda screen, **kwargs: SKIP,
    )

    assert any("Firecrawl" in prompt and "API key" in prompt for prompt in asked), asked
    assert _settings(home).browser.credentials["firecrawl_cloud"]["api_key"] == "prompted-key"


def test_a_browser_key_without_a_provider_is_a_usage_error(home: Path) -> None:
    with pytest.raises(wizard.SetupError, match="--browser-key needs --browser-provider"):
        wizard.run("full", home=home, browser_key="orphan", interactive=False)


def test_a_paid_browser_provider_without_a_key_warns_in_the_summary(home: Path) -> None:
    result = wizard.run("full", home=home, browser_provider="browserbase", interactive=False)
    notes = "\n".join(result.summary())
    assert "browserbase: no API key" in notes
    assert "BROWSERBASE_API_KEY" in notes


def test_a_failing_probe_warns_but_still_saves(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe is a courtesy: an offline machine must not lose the key."""
    said: list[str] = []
    monkeypatch.setattr(
        ui, "ask_text", lambda prompt, *, secret=False: "fc-key" if "API key" in prompt else ""
    )
    monkeypatch.setattr(
        wizard, "_probe_browser", lambda *a, **kw: "firecrawl_cloud rejected the key (HTTP 401)"
    )

    wizard.run(
        "full",
        home=home,
        interactive=True,
        console=SimpleNamespace(print=lambda text="", **kw: said.append(str(text))),
        ask=lambda screen, **kwargs: (
            "firecrawl_cloud" if screen.title.startswith("③") else SKIP
        ),
    )

    assert any("warning:" in line and "401" in line for line in said), said
    assert _settings(home).browser.credentials["firecrawl_cloud"]["api_key"] == "fc-key"


def test_the_runtime_reads_the_slot_the_wizard_writes(home: Path) -> None:
    """The contract between the two halves: browser.credentials.<id>."""
    from snowpea_core.tools import browser_providers

    wizard.run(
        "full",
        home=home,
        browser_provider="browserbase",
        browser_key="bb-key",
        interactive=False,
    )
    settings = _settings(home)
    settings.browser.credentials["browserbase"]["browserbase_project_id"] = "p-9"

    found = browser_providers.credentials_for("browserbase", settings)
    assert found["api_key"] == "bb-key"
    assert found["browserbase_project_id"] == "p-9"
    assert browser_providers.configured("browserbase", settings) is True
    assert browser_providers.configured("firecrawl_cloud", settings) is False
    assert browser_providers.configured("local_chromium", settings) is True


def test_ctrl_c_raises_keyboard_interrupt(monkeypatch):
    """Raw mode swallows SIGINT, so Ctrl+C has to leave the wizard itself
    rather than decline one screen (which on the voice screen only redraws)."""
    import io

    class _Raw(io.StringIO):
        def fileno(self):
            return 0

    monkeypatch.setattr("termios.tcgetattr", lambda fd: None)
    monkeypatch.setattr("termios.tcsetattr", lambda fd, when, saved: None)
    monkeypatch.setattr("tty.setraw", lambda fd: None)
    with pytest.raises(KeyboardInterrupt):
        ui.read_key(_Raw("\x03"))
    assert ui.read_key(_Raw("q")) == "quit"


def test_ctrl_c_on_a_screen_cancels_the_run(tmp_path, monkeypatch):
    def interrupted(screen, **_):
        raise KeyboardInterrupt

    result = wizard.run("quick", home=tmp_path, interactive=True, ask=interrupted)
    assert result.cancelled is True
    assert not (tmp_path / "settings.json").exists()


def test_the_last_row_reads_done_with_a_selection_and_skip_without() -> None:
    from snowpea_core.setup.screens import last_row_label

    picked = Screen(
        title="t",
        items=(ScreenItem("a", "Alpha", (), True, True), skip_item()),
        multi=False,
    )
    assert last_row_label(picked, {"a"}) == "Done — keep Alpha"
    assert last_row_label(picked, set()) == "Skip — decide later"
    boxes = Screen(
        title="t",
        items=(ScreenItem("a", "A", (), True, False), ScreenItem("b", "B", (), True, False), skip_item()),
        multi=True,
    )
    assert last_row_label(boxes, {"a", "b"}) == "Done — keep these 2"


def test_done_in_a_multi_select_keeps_what_was_ticked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ticking boxes and then pressing Enter on the last row used to return the
    defaults, throwing the ticks away."""
    screen = Screen(
        title="t",
        items=(
            ScreenItem("a", "A", (), True, False),
            ScreenItem("b", "B", (), False, False),
            skip_item(),
        ),
        multi=True,
    )
    # Tick b, move to the last row, confirm.
    assert _pick(monkeypatch, screen, "2", "3", "enter") == {"a", "b"}
