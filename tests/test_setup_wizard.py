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

import pytest

from snowpea_core.cli import commands as cli_commands
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.setup import catalog, ui, wizard
from snowpea_core.setup.screens import SKIP, Screen, ScreenItem
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
    assert sum(1 for tags in logins.values() if set(tags) - {"api_key"}) == 2


def test_vendor_catalog_marks_configured_vendors_active() -> None:
    settings = Settings()
    settings.providers["deepseek"] = {"api_key": "sk-test"}
    by_id = {item.id: item for item in catalog.vendor_catalog(settings)}
    assert by_id["deepseek"].active is True


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
        assert screen.items[-1].label == "Skip — keep defaults"


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
    assert "Skip — keep defaults" in body


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
    # Audio stays on "auto": the daemon works out what this machine has.
    assert settings.audio.stt.provider == "auto"
    assert settings.audio.tts.enabled is True
    assert settings.audio.tts.provider == "auto"
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
