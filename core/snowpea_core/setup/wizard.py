"""``snowpea setup`` — Quick, Full and Blank (M3 contract §5, AC-02/AC-02b).

* **Quick** asks screen ① only and takes the free defaults for everything else.
* **Full** walks ① providers → ② search → ③ browser → ④ tools → ⑤ gateway → ⑥ done.
* **Blank** asks nothing and writes the defaults.

Every screen has a matching flag, and a flag *answers* its screen: passing
``--search-provider tavily`` to a Full run skips screen ② rather than asking
again.  With no TTY the screens return their defaults, so the same Full run is
scriptable — that is the AC-02b "all Skip" path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from rich.console import Console

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.setup import ui
from snowpea_core.setup.detect import detect as detect_runtime
from snowpea_core.setup.screens import Screen
from snowpea_core.setup.screens import browser as browser_screen
from snowpea_core.setup.screens import done as done_screen
from snowpea_core.setup.screens import gateway as gateway_screen
from snowpea_core.setup.screens import providers as providers_screen
from snowpea_core.setup.screens import search as search_screen
from snowpea_core.setup.screens import tools as tools_screen
from snowpea_core.setup.state import WizardState

Mode = Literal["quick", "full", "blank"]

#: ``(name, module)`` in the order a Full run shows them.
FULL_ORDER: tuple[tuple[str, Any], ...] = (
    ("providers", providers_screen),
    ("search", search_screen),
    ("browser", browser_screen),
    ("tools", tools_screen),
    ("gateway", gateway_screen),
    ("done", done_screen),
)

QUICK_ORDER: tuple[tuple[str, Any], ...] = (
    ("providers", providers_screen),
    ("done", done_screen),
)

#: ``snowpea setup <section>`` names → screen (Hermes-style per-section setup).
SECTIONS: dict[str, tuple[str, Any]] = {
    "provider": ("providers", providers_screen),
    "providers": ("providers", providers_screen),
    "search": ("search", search_screen),
    "browser": ("browser", browser_screen),
    "tools": ("tools", tools_screen),
    "gateway": ("gateway", gateway_screen),
}


class SetupError(RuntimeError):
    """A bad flag — the CLI turns this into exit code 2."""


@dataclass
class SetupResult:
    """What one wizard run produced."""

    mode: Mode
    state: WizardState
    settings: Settings
    settings_path: Path
    #: Screen names that were shown (as opposed to answered by a flag).
    screens_shown: list[str] = field(default_factory=list)
    #: Screen names a flag answered.
    screens_answered: list[str] = field(default_factory=list)
    #: True when the user chose Cancel on the summary; nothing was written.
    cancelled: bool = False

    def summary(self) -> list[str]:
        return self.state.summary()


def run(
    mode: Mode = "quick",
    *,
    home: Path | str | None = None,
    vendor: str | None = None,
    key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    search_provider: str | None = None,
    browser_provider: str | None = None,
    tools: str | None = None,
    gateway: str | None = None,
    token: str | None = None,
    user_id: str | None = None,
    interactive: bool | None = None,
    console: Console | None = None,
    env: dict[str, str] | None = None,
    ask: Callable[..., Any] | None = None,
    section: str | None = None,
) -> SetupResult:
    """Run the wizard and write ``$SNOWPEA_HOME/settings.json``.

    ``section`` runs a single screen (``snowpea setup search``) and then the
    summary, like ``hermes setup <section>``.
    """
    if mode not in ("quick", "full", "blank"):
        raise SetupError(f"unknown setup mode: {mode}")
    if section is not None and section not in SECTIONS:
        raise SetupError(f"unknown setup section: {section} (one of {', '.join(SECTIONS)})")

    paths = Paths.create(home)
    settings = Settings.load(paths)
    state = WizardState.from_settings(settings)
    found = detect_runtime(env)
    state.hints = found.hints()

    answered = _apply_flags(
        state,
        vendor=vendor,
        key=key,
        model=model,
        base_url=base_url,
        search_provider=search_provider,
        browser_provider=browser_provider,
        tools=tools,
        gateway=gateway,
        token=token,
        user_id=user_id,
    )

    if interactive is None:
        interactive = ui.is_interactive()
    asker = ask or ui.ask
    shown: list[str] = []

    order: Sequence[tuple[str, Any]] = ()
    if section is not None:
        order = (SECTIONS[section], ("done", done_screen))
    elif mode == "full":
        order = FULL_ORDER
    elif mode == "quick":
        order = QUICK_ORDER
    by_name = {name: module for name, module in FULL_ORDER}

    def _show(name: str, module: Any) -> Any:
        screen: Screen = module.build(state)
        choice = asker(screen, console=console, interactive=interactive)
        module.apply(state, choice)
        shown.append(name)
        if name == "providers" and state.vendor and not state.api_key:
            _ask_for_key(state, interactive=interactive)
        if name == "providers" and state.vendor:
            _ask_for_model(state, interactive=interactive, console=console)
        if name == "gateway":
            _ask_for_gateway(state, interactive=interactive)
        return choice

    for name, module in order:
        if name in answered:
            continue
        choice = _show(name, module)
        # The summary lets the user pick a row to revisit that section (Hermes-style);
        # Save writes and finishes, Cancel discards everything.
        while (
            interactive
            and name == "done"
            and isinstance(choice, str)
            and choice.startswith("section:")
        ):
            target = choice.split(":", 1)[1]
            if target in by_name:
                _show(target, by_name[target])
            choice = _show("done", done_screen)
        if name == "done" and choice == done_screen.CANCEL:
            state.notes.append("cancelled — nothing written")
            return SetupResult(
                mode=mode,
                state=state,
                settings=settings,
                settings_path=paths.settings_json,
                screens_shown=shown,
                screens_answered=sorted(answered),
                cancelled=True,
            )

    settings = state.write(paths, settings)
    return SetupResult(
        mode=mode,
        state=state,
        settings=settings,
        settings_path=paths.settings_json,
        screens_shown=shown,
        screens_answered=sorted(answered),
    )


def _ask_for_key(state: WizardState, *, interactive: bool) -> None:
    """Prompt for the vendor's credentials; silence keeps the environment/default.

    ``local`` (vLLM / Ollama / LM Studio) asks for the server URL first, because
    that is the one thing a local server always needs; its API key is optional.
    """
    if not interactive or not state.vendor:
        return
    if state.vendor == "local":
        from snowpea_core.providers.presets import LOCAL_VARIANTS

        variants = list(LOCAL_VARIANTS)
        saved_idx = variants.index(state.variant) + 1 if state.variant in variants else 1
        labels = ", ".join(f"{i + 1}={LOCAL_VARIANTS[v].label}" for i, v in enumerate(variants))
        picked = ui.ask_text(f"local server type [{labels}] (Enter={saved_idx}): ")
        try:
            variant = variants[int(picked) - 1] if picked else variants[saved_idx - 1]
        except (ValueError, IndexError):
            variant = variants[saved_idx - 1]
        keep_url = state.base_url if variant == state.variant else None
        state.variant = variant
        default_url = keep_url or LOCAL_VARIANTS[variant].base_url or "http://localhost:11434/v1"
        entered_url = ui.ask_text(f"{LOCAL_VARIANTS[variant].label} base URL [{default_url}]: ")
        state.base_url = entered_url or default_url
        key_hint = "saved — Enter to keep" if state.has_saved_key else "optional, Enter to skip"
        entered_key = ui.ask_text(f"API key [{key_hint}]: ", secret=True)
        if entered_key:
            state.api_key = entered_key
        return
    key_hint = "saved — Enter to keep" if state.has_saved_key else "Enter to use the environment"
    entered = ui.ask_text(f"{state.vendor} API key [{key_hint}]: ", secret=True)
    if entered:
        state.api_key = entered


#: Never scroll the terminal: a vLLM node can advertise dozens of aliases.
MODEL_CHOICES_SHOWN = 20


def _run_sync(coro: Any) -> Any:
    """Await ``coro`` from the wizard's synchronous code, loop or no loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _ask_for_model(
    state: WizardState, *, interactive: bool, console: Console | None = None
) -> None:
    """Ask the vendor which models it serves, then let the user pick one.

    This is what keeps ``local`` usable: its preset default is the placeholder
    ``local-model``, which a vLLM server answers with ``HTTP 404``.  A server
    that is down is not an error here — the wizard says so and moves on, and
    the first prompt auto-picks a model instead.
    """
    if not interactive or not state.vendor:
        return
    from snowpea_core.providers import models as model_discovery
    from snowpea_core.providers.presets import preset_for

    out = console.print if console is not None else print
    try:
        preset = preset_for(state.vendor, state.variant)
    except KeyError:
        return
    out("checking models…")
    try:
        available = _run_sync(
            model_discovery.list_models(
                preset,
                api_key=state.api_key or None,
                base_url=state.base_url or None,
                refresh=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a down server must not stop setup
        out(f"could not list models ({exc}); you can set it later with /model")
        return
    if not available:
        out("could not list models (the server listed none); you can set it later with /model")
        return
    shown = available[:MODEL_CHOICES_SHOWN]
    default_idx = shown.index(state.model) + 1 if state.model in shown else 1
    for index, name in enumerate(shown, 1):
        out(f"  {index}. {name}")
    if len(available) > len(shown):
        out(f"  … and {len(available) - len(shown)} more")
    picked = ui.ask_text(f"model (Enter={default_idx}: {shown[default_idx - 1]}): ")
    if not picked:
        state.model = shown[default_idx - 1]
    elif picked.isdigit() and 1 <= int(picked) <= len(shown):
        state.model = shown[int(picked) - 1]
    else:
        state.model = picked


#: Where each platform tells a user their own numeric account id.
USER_ID_HINT: dict[str, str] = {
    "telegram": "send /start to @userinfobot; it replies with your numeric id",
    "discord": "enable Developer Mode, then right-click yourself → Copy User ID",
    "slack": "your profile → ⋮ → Copy member ID (starts with U)",
}


def _ask_for_gateway(state: WizardState, *, interactive: bool) -> None:
    """Ask each enabled messenger for its bot token and its approver user id.

    The user id is what makes chat approvals possible at all: the router
    refuses an allow/deny press from anyone else, and refuses every press on a
    binding that has no approver, so skipping this leaves a messenger that can
    talk but can never authorise a tool.
    """
    if not interactive:
        return
    for gid in state.enabled_gateways():
        if not state.gateway_needs_answers(gid):
            continue
        block = state.gateways.get(gid) or {}
        token = block.get("token") or ui.ask_text(f"{gid} bot token: ", secret=True)
        hint = USER_ID_HINT.get(gid, "your account id on that platform")
        entered_id = block.get("allowed_user_id") or ui.ask_text(
            f"your {gid} user id ({hint}): "
        )
        state.enable_gateway(gid, token or None, entered_id or None)
        if not entered_id:
            state.notes.append(
                f"{gid}: no user id — chat approvals stay blocked until you set one"
            )


def _apply_flags(state: WizardState, **flags: Any) -> set[str]:
    """Fold the non-interactive flags into ``state``; return the screens they answered."""
    answered: set[str] = set()

    vendor = flags.get("vendor")
    if vendor:
        from snowpea_core.providers.presets import PRESETS

        if vendor not in PRESETS:
            raise SetupError(f"unknown vendor: {vendor} (try `snowpea provider list`)")
        state.vendor = vendor
        state.api_key = flags.get("key") or state.api_key
        state.model = flags.get("model") or state.model
        state.base_url = flags.get("base_url") or state.base_url
        if vendor == "local" and not state.base_url:
            from snowpea_core.providers.presets import LOCAL_VARIANTS

            variant = state.variant or "ollama"
            state.variant = variant
            state.base_url = LOCAL_VARIANTS[variant].base_url
        answered.add("providers")
    elif flags.get("key"):
        raise SetupError("--key needs --vendor")

    search_provider = flags.get("search_provider")
    if search_provider:
        from snowpea_core.tools import search_providers

        if search_providers.get(search_provider) is None:
            raise SetupError(f"unknown search provider: {search_provider}")
        state.search_provider = search_provider
        answered.add("search")

    browser_provider = flags.get("browser_provider")
    if browser_provider:
        from snowpea_core.tools import browser_providers

        if browser_providers.get(browser_provider) is None:
            raise SetupError(f"unknown browser provider: {browser_provider}")
        state.browser_provider = browser_provider
        answered.add("browser")

    tools = flags.get("tools")
    if tools:
        unknown = state.apply_tools_flag(tools)
        if unknown:
            raise SetupError("unknown tool categories: " + ", ".join(unknown))
        answered.add("tools")

    gateway = flags.get("gateway")
    if gateway:
        from snowpea_core.setup.catalog import gateway_catalog

        if gateway not in {item.id for item in gateway_catalog()}:
            raise SetupError(f"unknown gateway: {gateway}")
        state.enable_gateway(gateway, flags.get("token"), flags.get("user_id"))
        answered.add("gateway")
    elif flags.get("token"):
        raise SetupError("--token needs --gateway")
    elif flags.get("user_id"):
        raise SetupError("--user-id needs --gateway")

    return answered


# ---------------------------------------------------------------------------
# --login, the alias for `snowpea provider login`
# ---------------------------------------------------------------------------


def login(vendor: str, home: Path | str | None = None) -> int:
    """``snowpea setup --login <vendor>`` — the same code as ``provider login``.

    The unsupported case is decided locally (``auth_web.method_for``) so the
    nine API-key-only vendors answer ``login_unsupported`` without a daemon;
    the two real flows go through ``snowpea provider login``'s RPC path.
    """
    import sys

    from snowpea_core.cli import commands as cli_commands
    from snowpea_core.cli.render import EXIT_USAGE
    from snowpea_core.providers import auth_web
    from snowpea_core.server.errors import RpcError

    try:
        auth_web.method_for(vendor)
    except RpcError as exc:
        # ``method_for`` already appends the API-key instructions for a known
        # vendor, so one line carries both the code and what to do instead.
        print(f'snowpea: error{{code:"{exc.code}"}} {exc.message}', file=sys.stderr)
        return EXIT_USAGE
    return asyncio.run(cli_commands.provider_login(vendor, home))


__all__ = [
    "FULL_ORDER",
    "MODEL_CHOICES_SHOWN",
    "USER_ID_HINT",
    "QUICK_ORDER",
    "Mode",
    "SetupError",
    "SetupResult",
    "login",
    "run",
]
