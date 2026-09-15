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
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from rich.console import Console

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.setup import ui
from snowpea_core.setup.detect import detect as detect_runtime
from snowpea_core.setup.screens import SKIP, Screen, ScreenItem
from snowpea_core.setup.screens import audio as audio_screen
from snowpea_core.setup.screens import browser as browser_screen
from snowpea_core.setup.screens import done as done_screen
from snowpea_core.setup.screens import gateway as gateway_screen
from snowpea_core.setup.screens import providers as providers_screen
from snowpea_core.setup.screens import search as search_screen
from snowpea_core.setup.screens import tools as tools_screen
from snowpea_core.setup.state import WizardState, profile_id
from snowpea_core.setup.sync import run_sync as _run_sync

log = logging.getLogger("snowpea.setup.wizard")

Mode = Literal["quick", "full", "blank"]

#: ``(name, module)`` in the order a Full run shows them.
FULL_ORDER: tuple[tuple[str, Any], ...] = (
    ("providers", providers_screen),
    ("search", search_screen),
    ("browser", browser_screen),
    ("audio", audio_screen),
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
    "audio": ("audio", audio_screen),
    "voice": ("audio", audio_screen),
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
    search_key: str | None = None,
    browser_provider: str | None = None,
    browser_key: str | None = None,
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
        search_key=search_key,
        browser_provider=browser_provider,
        browser_key=browser_key,
        tools=tools,
        gateway=gateway,
        token=token,
        user_id=user_id,
    )

    if interactive is None:
        interactive = ui.is_interactive()
    asker = ask or ui.ask
    shown: list[str] = []

    # A CLI flag answers the provider-selection screen, not the credential
    # step.  Keep those concerns separate (as Hermes does for tool backends):
    # an interactive `--search-provider exa` must still offer a masked
    # key prompt unless the same invocation supplied --search-key.  Previously
    # `answered` skipped `_show("search", ...)`, accidentally skipping the key.
    if "search" in answered and interactive and search_provider and not search_key:
        _ask_for_search_key(state, interactive=True, console=console)
    if "browser" in answered and interactive and browser_provider and not browser_key:
        _ask_for_browser_key(state, interactive=True, console=console)

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
        # The voice screens are action-first (M3 §5): an Install row fetches an
        # engine and asks again, so the user lands back on a screen where it is
        # now installed; the Choose row opens the submenu and comes back here
        # when it is declined, so Esc means "back" rather than "give up".
        while name == "audio" and interactive and _voice_detour(choice):
            _run_voice_detour(
                state,
                choice,
                asker,
                audio_screen.build_choose,
                paths.home,
                console=console,
                interactive=interactive,
            )
            screen = module.build(state)
            choice = asker(screen, console=console, interactive=interactive)
        module.apply(state, choice)
        shown.append(name)
        if name == "providers" and state.vendor and not state.api_key:
            _ask_for_key(state, interactive=interactive)
        if name == "providers" and state.vendor:
            _ask_for_model(state, interactive=interactive, console=console, home=paths.home)
            _configure_models(state, interactive=interactive, console=console, home=paths.home)
        if name == "search":
            _ask_for_search_key(state, interactive=interactive, console=console)
        if name == "browser":
            _ask_for_browser_key(state, interactive=interactive, console=console)
        if name == "tools":
            _ask_for_registry_token(state, interactive=interactive)
        if name == "audio":
            _ask_for_stt_language(
                state,
                asker,
                console=console,
                interactive=interactive,
                reply_language=_configured_reply_language(settings),
            )
            _ask_for_audio(
                state, asker, interactive=interactive, console=console, home=paths.home
            )
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
    if state.is_local_server(state.vendor):
        _ask_for_local_server(state)
        return
    from snowpea_core.providers import auth_web
    from snowpea_core.providers.presets import PRESETS
    from snowpea_core.server.errors import RpcError

    preset = PRESETS.get(state.vendor)
    methods = preset.auth_methods if preset is not None else ("api_key",)
    if len(methods) > 1:
        # One numbered row per flow this vendor really supports, so the
        # headless routes are visible rather than hidden behind "browser
        # login" silently falling back to them.
        choices: list[tuple[str, str | None]] = [("API key", None)]
        for flow in auth_web.methods_for(state.vendor):
            choices.append((_LOGIN_LABELS.get(flow, flow), flow))
        if "oauth_token" in methods:
            choices.append(("OAuth token (remote/headless)", "oauth_token"))
        labels = ", ".join(f"{i + 1}={label}" for i, (label, _) in enumerate(choices))
        prompt = f"authentication [{labels}] (Enter=1): "
        while True:
            try:
                menu = _menu_pick(
                    f"{state.vendor} authentication",
                    [(str(i), label, ()) for i, (label, _) in enumerate(choices)],
                    default_id="0",
                )
                picked = menu if menu is not None else ui.ask_text(prompt).strip()
            except (KeyboardInterrupt, EOFError):
                state.notes.append(f"{state.vendor}: login cancelled — left unconfigured")
                return
            try:
                index = int(picked) - (0 if menu is not None else 1) if picked else 0
            except ValueError:
                index = -1
            if not 0 <= index < len(choices):
                print(f"pick a number between 1 and {len(choices)}")
                continue
            method = choices[index][1]
            if method is None:
                break
            if method == "oauth_token":
                entered = ui.ask_text(f"{state.vendor} OAuth access token: ", secret=True).strip()
                if not entered:
                    return
                # A pasted token is the one credential nobody can sanity-check
                # by eye; one cheap authenticated call now beats an opaque 401
                # on the first prompt (report §6.7 A-P2-2).
                problem = _probe_token(state.vendor, entered)
                if problem:
                    print(f"warning: {problem}")
                state.oauth_token = entered
                state.auth_method = "oauth_token"
                return
            try:
                result = _run_sync(auth_web.login(state.vendor, method))
            except (KeyboardInterrupt, EOFError):
                state.notes.append(f"{state.vendor}: login cancelled — left unconfigured")
                return
            except RpcError as exc:
                print(f"login failed: {exc.message}")
                data = exc.data if isinstance(exc.data, dict) else {}
                if data.get("status") == 403:
                    print(
                        "hint: the vendor refused the request from this network/account; "
                        "try the browser login, an API key, or paste an OAuth token"
                    )
                if data.get("port"):
                    print(
                        f"hint: close whatever is listening on port {data['port']}, "
                        "or pick the headless login instead"
                    )
                continue
            except Exception as exc:  # noqa: BLE001 - interactive setup must remain usable
                print(f"login failed: {exc}")
                continue
            _apply_login(state, result.credentials)
            state.notes.append(result.message)
            return
    key_hint = "saved — Enter to keep" if state.has_saved_key else "Enter to use the environment"
    try:
        entered = ui.ask_text(f"{state.vendor} API key [{key_hint}]: ", secret=True)
    except (KeyboardInterrupt, EOFError):
        state.notes.append(f"{state.vendor}: login cancelled — left unconfigured")
        return
    if entered:
        state.api_key = entered



#: Menu ids the local-server list adds below the configured servers.
ADD_LOCAL = "__add_local__"
REMOVE_LOCAL = "__remove_local__"


def _local_label(state: WizardState, vendor: str) -> str:
    """``hon2 — http://hon2:8000/v1`` for one configured server."""
    block = state.provider_configs.get(vendor) or {}
    label = str(block.get("label") or "") or ("local" if vendor == "local" else vendor)
    url = str(block.get("base_url") or "")
    if vendor == state.vendor and state.base_url:
        url = state.base_url
    return f"{label} — {url}" if url else label


def _ask_for_local_server(state: WizardState) -> None:
    """Pick which OpenAI-compatible server to configure, add one, or remove one.

    ``providers.local`` is one row among the named ones rather than a special
    case: a user who runs vLLM on two machines wants two entries with their own
    names, and the one they already had must keep working untouched.
    """
    servers = state.local_servers()
    current = state.vendor if state.vendor in servers else (servers[0] if servers else None)
    picked = ADD_LOCAL
    if servers:
        rows: list[tuple[str, str, tuple[str, ...]]] = [
            (vendor, _local_label(state, vendor), ("active",) if vendor == current else ())
            for vendor in servers
        ]
        rows.append((ADD_LOCAL, "Add another server…", ()))
        rows.append((REMOVE_LOCAL, "Remove a server…", ()))
        chosen = _menu_pick(
            "Local / OpenAI-compatible servers", rows, default_id=current or ADD_LOCAL
        )
        # Off a TTY there is no menu: configure the server already selected,
        # which is what the single-server wizard always did.
        picked = chosen if chosen is not None else (current or ADD_LOCAL)
    if picked == REMOVE_LOCAL:
        _remove_local_server(state, servers)
        return
    if picked == ADD_LOCAL:
        name = _ask_for_local_name(state)
        if name is None:
            return
        state.add_local_server(name)
    elif picked != state.vendor:
        state.select_vendor(picked)
    _ask_for_local_details(state)


def _ask_for_local_name(state: WizardState) -> str | None:
    """Ask what to call a new server; ``None`` when the user gave up."""
    from snowpea_core.providers.presets import validate_custom_vendor_id

    taken = set(state.local_servers())
    for _ in range(5):
        try:
            entered = ui.ask_text("name for this server (e.g. hon2, vllm-a): ").strip()
        except (KeyboardInterrupt, EOFError):
            return None
        if not entered:
            return None
        if entered in taken:
            print(f"{entered} is already configured; pick another name")
            continue
        try:
            return validate_custom_vendor_id(entered)
        except ValueError as exc:
            print(str(exc))
    return None


def _remove_local_server(state: WizardState, servers: Sequence[str]) -> None:
    """Delete one named server, its model profiles and its agent assignments."""
    if not servers:
        return
    rows = [(vendor, f"Remove {_local_label(state, vendor)}", ()) for vendor in servers]
    picked = _menu_pick("remove a server", rows, default_id=servers[0], finish="Keep them all")
    if picked is None:
        typed = ui.ask_text("remove which server (Enter to keep them all): ").strip()
        if not typed:
            return
        if typed not in set(servers):
            print(f"no server called {typed}")
            return
        picked = typed
    state.remove_provider(picked)
    state.notes.append(f"{picked}: removed")


def _ask_for_local_details(state: WizardState) -> None:
    """Server type, base URL and the optional API key for the selected server."""
    from snowpea_core.providers.presets import LOCAL_VARIANTS

    vendor = state.vendor or "local"
    variants = list(LOCAL_VARIANTS)
    saved_idx = variants.index(state.variant) + 1 if state.variant in variants else 1
    labels = ", ".join(f"{i + 1}={LOCAL_VARIANTS[v].label}" for i, v in enumerate(variants))
    picked = _menu_pick(
        f"{vendor}: server type",
        [(v, LOCAL_VARIANTS[v].label, ()) for v in variants],
        default_id=variants[saved_idx - 1],
    )
    if picked is not None:
        picked = str(variants.index(picked) + 1)
    else:
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


#: What each login flow is called on the authentication menu.
_LOGIN_LABELS: dict[str, str] = {
    "browser_pkce": "browser login",
    "oauth_pkce": "browser login",
    "google_oauth": "browser login (Google)",
    "device_code": "device code (headless)",
    "google_adc": "gcloud ADC (headless)",
}


def _apply_login(state: WizardState, credentials: dict[str, Any]) -> None:
    """Fold a finished login into the vendor's block.

    Order matters. Stale credentials are cleared **first** and the fresh ones
    written afterwards — clearing after the update is what silently deleted the
    API key OpenRouter's browser login had just minted, leaving the run with an
    unconfigured default vendor and a summary claiming success (report §6.7
    A-P1-1).
    """
    block = dict(state.provider_configs.get(state.vendor or "") or {})
    # ``None`` rather than ``pop``: ``write()`` merges this block onto what is
    # already in settings.json, and only an explicit ``None`` removes a field
    # there.  Dropping the key here would leave the old credential on disk —
    # which is how a stale API key used to outlive the login (report §6.6).
    for stale in ("api_key", "token", "oauth_token", "access_token", "refresh_token"):
        if stale in block:
            block[stale] = None
    block.update(credentials)
    state.provider_configs[state.vendor or ""] = block
    state.auth_method = str(credentials.get("auth_method") or "") or None
    if credentials.get("api_key"):
        # A flow that mints a real API key (OpenRouter) keeps using the
        # API-key path; the state field is what ``write()`` persists.
        state.api_key = str(credentials["api_key"])
        state.auth_method = None
    else:
        state.api_key = None
    state.oauth_token = None


def _probe_token(vendor: str, token: str) -> str | None:
    """Ask the vendor one cheap authenticated question about a pasted token.

    Returns a human sentence when the token looks unusable, or ``None`` when it
    worked (or the check itself could not run — an offline machine must not be
    told its token is bad).
    """
    from snowpea_core.providers import models as model_discovery
    from snowpea_core.providers.presets import PRESETS

    preset = PRESETS.get(vendor)
    if preset is None:  # pragma: no cover - guarded by the caller
        return None
    try:
        listed = _run_sync(model_discovery.list_models(preset, api_key=token, refresh=True))
    except Exception as exc:  # noqa: BLE001 - a probe never blocks setup
        return f"could not verify the token ({exc}); saving it anyway"
    return None if listed else f"{vendor} accepted the token but listed no models"


#: Never scroll the terminal: a vLLM node can advertise dozens of aliases.
MODEL_CHOICES_SHOWN = 20


def _ask_for_model(
    state: WizardState,
    *,
    interactive: bool,
    console: Console | None = None,
    home: Path | None = None,
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
    block = dict(state.provider_configs.get(state.vendor) or {})
    if state.base_url and not block.get("base_url"):
        # A URL typed a moment ago is not in the block yet; the synthesized
        # preset for a named server is built from the block, so hand it over.
        block = {**block, "base_url": state.base_url}
    try:
        preset = preset_for(state.vendor, state.variant, block)
    except KeyError:
        return
    oauth_method = state.auth_method or str(block.get("auth_method") or "")
    if oauth_method == "chatgpt":
        out("listing models from your ChatGPT account…")
    elif oauth_method in {"google_oauth", "google_adc"}:
        # Code Assist has no catalog endpoint (checked against gemini-cli's
        # packages/core/src/code_assist, which ships static constants), so say
        # so rather than letting a declared list look like the vendor's answer.
        out(
            "a Google sign-in runs on the Code Assist backend, which publishes no "
            "model listing — these are the models it supports"
        )
    else:
        out("checking models…")
    try:
        listing = _run_sync(
            model_discovery.resolve_models(
                state.vendor,
                preset=preset,
                auth_method=oauth_method or None,
                api_key=state.api_key or str(block.get("api_key") or "") or None,
                base_url=state.base_url or None,
                credentials=block,
                home=home,
                refresh=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a down server must not stop setup
        out(f"could not list models ({exc})")
        entered = ui.ask_text(f"model id [{state.model or 'required'}]: ")
        if entered:
            state.model = entered
        return
    available = listing.models
    # Say what went wrong *and* what is being shown instead: a fallback list
    # that arrives silently looks like the server answered.
    if listing.error and not listing.live:
        out(f"could not list models ({listing.error})")
    if available:
        out(f"{listing.detail} ({len(available)} models)")
    else:
        out(listing.detail)
    if not available:
        out("the server listed no models")
        entered = ui.ask_text(f"model id [{state.model or 'required'}]: ")
        if entered:
            state.model = entered
        return
    shown = available[:MODEL_CHOICES_SHOWN]
    default_idx = shown.index(state.model) + 1 if state.model in shown else 1
    # An eye next to the models that are known to take images; a model nobody
    # has said anything about gets no badge rather than a claim (CORE-vision).
    badges = _vision_badges(state, preset, shown)
    for index, name in enumerate(shown, 1):
        out(f"  {index}. {name}{badges.get(name, '')}")
    if len(available) > len(shown):
        out(f"  … and {len(available) - len(shown)} more")
    menu = _menu_pick(
        "model",
        [(name, f"{name}{badges.get(name, '')}", ()) for name in shown],
        default_id=shown[default_idx - 1],
        console=console,
    )
    if menu is not None:
        state.model = menu
        return
    picked = ui.ask_text(f"model (Enter={default_idx}: {shown[default_idx - 1]}): ")
    if not picked:
        state.model = shown[default_idx - 1]
    elif picked.isdigit() and 1 <= int(picked) <= len(shown):
        state.model = shown[int(picked) - 1]
    else:
        state.model = picked


def _vision_badges(
    state: WizardState, preset: Any, models: Sequence[str]
) -> dict[str, str]:
    """``{model: "  👁"}`` for the models known to take images.

    Resolved through the same registry chain a turn uses, so the wizard cannot
    promise a capability the request path would not act on.
    """
    from snowpea_core.providers.registry import ProviderRegistry

    vendor = state.vendor or preset.id
    registry = ProviderRegistry(state.as_settings())
    badges: dict[str, str] = {}
    for name in models:
        try:
            answer = registry.vision_for(vendor, name)
        except Exception:  # noqa: BLE001 - a badge must never stop the wizard
            continue
        if answer:
            badges[name] = "  \N{EYE}"
    return badges


def _menu_pick(
    title: str,
    options: Sequence[tuple[str, str, tuple[str, ...]]],
    *,
    default_id: str | None = None,
    finish: str | None = None,
    help_text: str = "↑↓ to move, Enter to choose.",
    console: Console | None = None,
) -> str | None:
    """Offer ``options`` as an arrow-key menu; ``None`` when the terminal is not a TTY.

    ``options`` are ``(id, label, tags)`` rows. With ``finish`` a trailing
    row ends the menu and returns ``None`` too, so callers fall through to
    their text prompt (scripted runs, tests) or treat ``None`` as "done".
    """
    if not ui.is_interactive() or not options:
        return None
    items = [
        ScreenItem(id=oid, label=label, tags=tuple(tags), selected=False, default=oid == default_id)
        for oid, label, tags in options
    ]
    if finish:
        items.append(ScreenItem(id=SKIP, label=finish, tags=(), selected=False, default=False))
    screen = Screen(title=title, items=tuple(items), multi=False, help=help_text)
    choice = ui.ask(screen, console=console, interactive=True)
    if not isinstance(choice, str) or choice == SKIP:
        return None
    if finish and choice == screen.default_choice and default_id is None:
        return None
    return choice


def _agent_tags(state: WizardState, name: str) -> tuple[str, ...]:
    """The profile an agent is pinned to, as a menu tag."""
    current = state.agent_models.get(name)
    return (f"uses {current}",) if current else ()


def _ask_yes_no(question: str, *, default: bool) -> bool:
    """A yes/no question as an arrow-key menu, or ``[Y/n]`` text off a TTY."""
    picked = _menu_pick(
        question, [("yes", "Yes", ()), ("no", "No", ())], default_id="yes" if default else "no"
    )
    if picked is not None:
        return picked == "yes"
    hint = "Y/n" if default else "y/N"
    answer = ui.ask_text(f"{question} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def _vendor_defaults(state: WizardState, home: Path | None = None) -> dict[str, Any]:
    """Each vendor's live default model, or ``{}`` when nothing answered in time.

    An empty answer is not a failure: :func:`vendor_catalog` falls back to the
    models.dev catalog already on disk, so the screen still shows a current
    default — just not one confirmed against the account this second.
    """
    from snowpea_core.setup.catalog import vendor_default_models

    try:
        return _run_sync(vendor_default_models(state.as_settings(), home=home))
    except Exception as exc:  # noqa: BLE001 - a default never blocks setup
        log.debug("could not resolve the vendors' default models: %s", exc)
        return {}


def _vendor_options(
    state: WizardState, home: Path | None = None
) -> list[tuple[str, str, tuple[str, ...]]]:
    """Every vendor as a menu row, tagged like the provider screen.

    The row carries ``default: <model> (from models.dev)`` so the model a
    vendor would actually start with is visible before it is picked
    (CORE-default-models).
    """
    from snowpea_core.setup.catalog import vendor_catalog

    rows: list[tuple[str, str, tuple[str, ...]]] = []
    for item in vendor_catalog(state.as_settings(), _vendor_defaults(state, home)):
        # ``item.tags`` already carries ``active``/``default`` from the catalog;
        # only the hidden filled-circle marker is added here.
        tags = tuple(item.tags) + ((ui.CONFIGURED,) if item.active else ())
        rows.append((item.id, item.label, tags))
    return rows


def _configure_models(
    state: WizardState,
    *,
    interactive: bool,
    console: Console | None,
    home: Path,
) -> None:
    """Register more profiles, choose a default, and assign agent overrides."""
    state.add_current_model_profile()
    if not interactive:
        return
    out = console.print if console is not None else print
    from snowpea_core.providers.presets import PRESETS

    while True:
        options = _vendor_options(state, home)
        if ui.is_interactive():
            vendor = _menu_pick(
                "add another model", options, finish="Done — no more models", console=console
            )
            if vendor is None:
                break
        else:
            typed = ui.ask_text("add another model — provider id (Enter to finish): ").strip()
            if not typed:
                break
            if typed.isdigit() and 1 <= int(typed) <= len(options):
                typed = options[int(typed) - 1][0]
            if typed not in PRESETS and not state.is_local_server(typed):
                known = sorted({*PRESETS, *state.local_servers()})
                out(f"unknown provider id: {typed} (one of {', '.join(known)})")
                continue
            vendor = typed
        state.select_vendor(vendor)
        _ask_for_key(state, interactive=True)
        _ask_for_model(state, interactive=True, console=console, home=home)
        profile = state.add_current_model_profile()
        if profile:
            out(f"registered model profile {profile}")

    profiles = list(state.model_profiles)
    if not profiles:
        return
    for index, profile in enumerate(profiles, 1):
        marker = " (current default)" if profile == state.default_model else ""
        out(f"  {index}. {profile}{marker}")
    default_idx = profiles.index(state.default_model) + 1 if state.default_model in profiles else 1
    menu = _menu_pick(
        "default model",
        [(name, name, ()) for name in profiles],
        default_id=profiles[default_idx - 1],
        console=console,
    )
    picked = menu if menu is not None else ui.ask_text(
        f"default model (Enter={default_idx}: {profiles[default_idx - 1]}): "
    ).strip()
    if picked.isdigit() and 1 <= int(picked) <= len(profiles):
        state.set_default_model(profiles[int(picked) - 1])
    elif picked in state.model_profiles:
        state.set_default_model(picked)

    from snowpea_core.agent.definition import builtin_agent_definitions, discover_definitions

    names = sorted(
        {definition.name for definition in builtin_agent_definitions()}
        | {definition.name for definition in discover_definitions(Path.cwd(), home)}
    )
    if names:
        out("agents: " + ", ".join(names))
    while True:
        if ui.is_interactive() and names:
            agent = _menu_pick(
                "assign model to agent",
                [(name, name, _agent_tags(state, name)) for name in names],
                finish="Done — keep the rest on the default",
                console=console,
            )
            if agent is None:
                break
        else:
            agent = ui.ask_text("assign model to agent (Enter to finish): ").strip()
            if not agent:
                break
        current = state.agent_models.get(agent)
        hint = f"; Enter uses default{f' (currently {current})' if current else ''}"
        choice = _menu_pick(
            f"profile for {agent}",
            [("", "default (inherit)", ())] + [(name, name, ()) for name in profiles],
            default_id=current or "",
            console=console,
        )
        if choice is None:
            choice = ui.ask_text(f"profile for {agent} [1-{len(profiles)}{hint}]: ").strip()
        if not choice:
            state.assign_agent_model(agent, None)
        elif choice.isdigit() and 1 <= int(choice) <= len(profiles):
            state.assign_agent_model(agent, profiles[int(choice) - 1])
        elif choice in state.model_profiles:
            state.assign_agent_model(agent, choice)
        else:
            out(f"unknown model profile: {choice}")


def _note_missing_search_key(state: WizardState, search_providers: Any) -> None:
    """Warn in the summary when a key-required provider has no key.

    Without this the run looks successful and ``web_search`` quietly answers
    from ddgs instead — the failure this whole screen exists to prevent.
    """
    pid = state.search_provider
    if not search_providers.needs_key(pid) or state.has_search_key(pid):
        return
    env = search_providers.credential_env(pid)
    state.notes.append(
        f"{pid}: no API key — web_search will fall back to another provider until you set "
        + (f"${env} or " if env else "")
        + f"search.credentials.{pid}.api_key"
    )


def _ask_for_audio(
    state: WizardState,
    asker: Any,
    *,
    interactive: bool,
    console: Console | None = None,
    home: Any = None,
) -> None:
    """Ask the rest of the audio question: voice out, the voice, auto-speak.

    A non-interactive run leaves everything on ``auto``, which is the whole
    point of ``auto``: the daemon picks whatever the machine turns out to have.
    """
    from snowpea_core.setup.catalog import AUDIO_OFF

    if not interactive:
        return
    out = console.print if console is not None else print
    choice = asker(audio_screen.build_tts(state), console=console, interactive=interactive)
    while _voice_detour(choice):
        _run_voice_detour(
            state,
            choice,
            asker,
            audio_screen.build_choose_tts,
            home,
            console=console,
            interactive=interactive,
            apply_choice=audio_screen.apply_tts,
            direction="tts",
        )
        choice = asker(audio_screen.build_tts(state), console=console, interactive=interactive)
    audio_screen.apply_tts(state, choice)
    if state.stt_provider == "command":
        entered = ui.ask_text("speech-to-text command (must contain {path}): ")
        if entered:
            state.stt_command = entered
    # Everything below is only worth asking once the user has said they want a
    # particular voice backend.  Leaving both lists on Skip (or on Automatic)
    # means "work it out from what is installed", and a wizard that then asks
    # three more questions has not listened.
    if not _picked(choice) or state.tts_provider == AUDIO_OFF:
        return
    if state.tts_provider == "command":
        entered = ui.ask_text("text-to-speech command (use {text} and {out}): ")
        if entered:
            state.tts_command = entered
    _ask_for_voices(state, asker, console=console, interactive=interactive, home=home)
    if _ask_yes_no("read replies aloud by default?", default=state.auto_speak):
        state.auto_speak = True
    else:
        state.auto_speak = False
    if _ask_yes_no("test the voice now?", default=False):
        _test_voice(state, out)


def _one(choice: Any) -> str:
    """One screen answer as a string, whatever shape the asker returned."""
    if isinstance(choice, set):
        choice = next(iter(choice), "")
    return str(choice or "")


def _install_chosen(choice: Any) -> bool:
    """True when the answer was an ``install:`` row rather than a selection."""
    return audio_screen.install_target(_one(choice)) is not None


def _voice_detour(choice: Any) -> bool:
    """True for a row that does something and then shows the screen again."""
    return _install_chosen(choice) or audio_screen.is_choose(_one(choice))


def _run_voice_detour(
    state: WizardState,
    choice: Any,
    asker: Any,
    build_submenu: Any,
    home: Any,
    *,
    console: Console | None = None,
    interactive: bool = True,
    apply_choice: Any = None,
    direction: str = "stt",
) -> None:
    """Run what an Install row or the submenu asked for.

    Declining the submenu (Esc, or Skip) pins nothing and returns to the screen
    it came from, which is what makes Esc read as "back" rather than "never
    mind the whole question".
    """
    out = console.print if console is not None else print
    if _install_chosen(choice):
        # Installing is not choosing: it puts the engine on the machine and
        # sends the user back to a list where it is now active, one Enter from
        # being pinned.  Pinning it for them would decide a thing they came
        # here to decide.
        target = _one(choice)
        if audio_screen.run_install(target, home, out):
            out(f"{audio_screen.install_target(target)} installed — pick it to use it")
        return
    picked = asker(build_submenu(state), console=console, interactive=interactive)
    name = _one(picked)
    if not _picked(picked):
        return
    _follow_up(state, name, home, out, direction=direction, apply_choice=apply_choice)


def _follow_up(
    state: WizardState,
    name: str,
    home: Any,
    out: Any,
    *,
    direction: str,
    apply_choice: Any = None,
) -> None:
    """Do whatever picking engine ``name`` in the submenu means.

    Every row leads somewhere: an engine that is not installed is installed,
    a system package is explained and offered, a custom command is asked for
    and tested, a hosted one asks for its key.  Only then is anything pinned,
    and an install alone never pins.
    """
    from snowpea_core.setup import catalog as catalog_mod

    apply_it = apply_choice or audio_screen.apply
    items = (
        catalog_mod.tts_catalog(audio_screen.detected_tts(state))
        if direction == "tts"
        else catalog_mod.stt_catalog(audio_screen.detected_stt(state))
    )
    item = next((row for row in items if row.id == name), None)
    if item is None:
        return
    action = audio_screen.row_action(item)

    if action == audio_screen.ACTION_INSTALL:
        if audio_screen.run_install(f"{audio_screen.INSTALL_PREFIX}{name}", home, out):
            out(f"{name} installed — pick it to use it")
        return
    if action == audio_screen.ACTION_SYSTEM:
        _offer_system_install(name, item, out)
        return
    if action == audio_screen.ACTION_COMMAND:
        if not _ask_for_voice_command(state, direction, out):
            return
    if action == audio_screen.ACTION_KEY and not _ask_for_voice_key(state, out):
        return
    apply_it(state, name)


def _offer_system_install(name: str, item: Any, out: Any) -> None:
    """Show the platform's own command for a system package, and offer to run it.

    We will not run a package manager as root behind someone's back, but making
    them retype a command we already know is not help either.  So: print it,
    ask, and run it through the shell where sudo can prompt in the terminal it
    already owns.
    """
    hint = item.install_hint or f"install {name} with your system package manager"
    out(hint)
    if not _ask_yes_no(f"run `{hint}` now?", default=False):
        out(f"{name} is not installed; run it yourself and pick {name} again")
        return
    import shlex
    import subprocess

    try:
        result = subprocess.run(shlex.split(hint), check=False)  # noqa: S603
    except (OSError, ValueError) as exc:
        out(f"could not run it: {exc}")
        return
    import shutil

    if result.returncode == 0 and shutil.which(name):
        out(f"{name} installed — pick it to use it")
    else:
        out(f"{name} still is not on PATH; pick it again once it is")


def _ask_for_voice_command(state: WizardState, direction: str, out: Any) -> bool:
    """Ask for a custom template, validate it, and self-test it once."""
    placeholders = " and ".join(audio_screen.COMMAND_REQUIRED.get(direction, ()))
    current = state.tts_command if direction == "tts" else state.stt_command
    hint = "saved — Enter to keep" if current else f"must contain {placeholders}"
    entered = ui.ask_text(f"command [{hint}]: ").strip() or (current or "")
    problem = audio_screen.validate_command(entered, direction)
    if problem:
        out(f"not saved: {problem}")
        return False
    if direction == "tts":
        state.tts_command = entered
    else:
        state.stt_command = entered
    failure = _self_test_command(state, direction)
    if failure:
        # It parses and the program exists; that it did not produce audio here
        # is worth saying, not worth refusing over.
        out(f"warning: {failure}")
    return True


#: How long a custom command gets to prove itself.
SELF_TEST_SECONDS = 3.0


def _self_test_command(state: WizardState, direction: str) -> str | None:
    """Run the template once on a short phrase; a sentence when it did not work."""
    if direction != "tts":
        return None
    import tempfile

    from snowpea_core.audio import AudioError
    from snowpea_core.audio import tts as tts_backends

    provider = tts_backends.build_provider("command", command=state.tts_command)
    try:
        with tempfile.TemporaryDirectory(prefix="snowpea-tts-test-") as tmp:
            _run_sync(
                asyncio.wait_for(
                    provider.synthesize(TEST_PHRASE, out_dir=Path(tmp)),
                    timeout=SELF_TEST_SECONDS,
                )
            )
    except (AudioError, TimeoutError) as exc:
        return f"the command did not produce audio: {exc}"
    except Exception as exc:  # noqa: BLE001 - a self-test never fails setup
        return f"could not test the command: {type(exc).__name__}: {exc}"
    return None


def _ask_for_voice_key(state: WizardState, out: Any) -> bool:
    """The hosted engine needs the OpenAI key; it is the one already configured."""
    if state.api_key or state.has_saved_key:
        return True
    entered = ui.ask_text("OpenAI API key [Enter to use $OPENAI_API_KEY]: ", secret=True).strip()
    if entered:
        state.api_key = entered
        return True
    out("no key entered — OpenAI voice cannot run until one is set")
    return True


def _ask_for_voices(
    state: WizardState,
    asker: Any,
    *,
    console: Console | None = None,
    interactive: bool = True,
    home: Any = None,
) -> None:
    """After an engine is pinned, which voice it speaks each language with.

    One tab per language, because one engine can and should sound like a
    different person in Korean than in English; a single voice is stored under
    ``*`` and still works, which is what the legacy ``voice`` field becomes.
    """
    from snowpea_core.audio import voices as voice_catalog

    engine = (state.tts_provider or "").strip()
    if not engine or engine in {"command", "off"}:
        return
    out = console.print if console is not None else print
    languages = _voice_languages(state)
    try:
        found = _run_sync(
            voice_catalog.voices_for(
                engine,
                home=home,
                languages=languages,
                installed_engines=tuple(audio_screen.detected_tts(state)),
                openai_key=bool(state.api_key or state.has_saved_key),
            )
        )
    except Exception as exc:  # noqa: BLE001 - a voice list never fails setup
        out(f"could not list {engine} voices: {exc}")
        return
    if not found:
        return

    grouped = voice_catalog.by_language(found)
    for tag in languages:
        options = grouped.get(tag) or grouped.get(voice_catalog.ANY) or []
        if not options:
            out(f"{engine} has no voice for {tag}")
            continue
        pinned = state.tts_voices.get(tag) or state.tts_voices.get(voice_catalog.ANY)
        choice = asker(
            audio_screen.build_voices(options, tag, pinned),
            console=console,
            interactive=interactive,
        )
        name = _one(choice)
        if not _picked(choice):
            continue
        voice = next((item for item in options if item.id == name), None)
        if voice is None:
            continue
        if not voice.installed and not _install_voice(engine, voice, home, out):
            continue
        state.tts_voices[tag] = name
        if _ask_yes_no(f"preview {name} in {tag}?", default=False):
            _preview_voice(state, name, tag, out)
    # A single voice chosen for every language is the same as one under `*`,
    # and storing it that way keeps the settings file honest about the choice.
    values = set(state.tts_voices.values())
    if len(values) == 1 and set(state.tts_voices) >= set(languages):
        state.tts_voices = {voice_catalog.ANY: values.pop()}


def _configured_reply_language(settings: Any) -> str:
    """``agent.replyLanguage``, or ``""`` when it is left on auto."""
    tag = str(getattr(getattr(settings, "agent", None), "replyLanguage", "") or "").strip()
    return "" if tag.lower() in {"", "auto"} else tag


def _voice_languages(state: WizardState) -> tuple[str, ...]:
    """Korean and English, plus the transcription language when it is another."""
    from snowpea_core.audio import voices as voice_catalog

    tags = list(voice_catalog.DEFAULT_LANGUAGES)
    forced = (state.stt_language or "").strip().lower().partition("-")[0]
    if forced and forced != "auto" and forced not in tags:
        tags.append(forced)
    return tuple(tags)


def _install_voice(engine: str, voice: Any, home: Any, out: Any) -> bool:
    """Fetch a voice that is not on disk yet; ``True`` when it landed."""
    from snowpea_core.audio import install as audio_install

    async def progress(line: str) -> None:
        out(f"  {line}")

    try:
        result = _run_sync(
            audio_install.install_voice(engine, voice.id, home=Path(home), progress=progress)
        )
    except Exception as exc:  # noqa: BLE001 - a failed download is not a failed wizard
        out(f"could not install {voice.id}: {exc}")
        return False
    if not result.ok:
        out(result.hint or f"could not install {voice.id}")
    return bool(result.ok)


#: What a preview says, per language, so it is a sentence rather than a word.
PREVIEW_TEXT: dict[str, str] = {
    "ko": "안녕하세요, 스노우피가 준비되었습니다.",
    "ja": "こんにちは、スノーピーの準備ができました。",
    "zh": "你好，snowpea 已经准备好了。",
}
PREVIEW_DEFAULT = "Hello, snowpea is ready."


def _preview_voice(state: WizardState, voice: str, language: str, out: Any) -> None:
    """Synthesise a short sentence in ``language`` and play it."""
    import tempfile

    from snowpea_core.audio import AudioError, player
    from snowpea_core.audio import tts as tts_backends

    provider = tts_backends.resolve_provider(
        state.tts_provider or "", command=state.tts_command, language=language
    )
    if provider is None:
        out("the engine is not available here; nothing was played")
        return
    phrase = PREVIEW_TEXT.get(language, PREVIEW_DEFAULT)
    try:
        with tempfile.TemporaryDirectory(prefix="snowpea-voice-") as tmp:
            speech = _run_sync(
                provider.synthesize(phrase, out_dir=Path(tmp), voice=voice, language=language)
            )
            _run_sync(player.play(speech.path))
    except AudioError as exc:
        out(f"could not preview: {exc}")
    except Exception as exc:  # noqa: BLE001 - a preview never fails setup
        out(f"could not preview: {type(exc).__name__}: {exc}")


def _ask_for_stt_language(
    state: WizardState,
    asker: Any,
    *,
    console: Console | None = None,
    interactive: bool = True,
    reply_language: str = "",
) -> None:
    """Which language transcription expects, once an engine is pinned."""
    if not (state.stt_provider or "").strip() or state.stt_provider in {"off", "command"}:
        return
    choice = asker(
        audio_screen.build_language(state, reply_language=reply_language),
        console=console,
        interactive=interactive,
    )
    name = _one(choice)
    if not _picked(choice):
        return
    if name == audio_screen.LANGUAGE_OTHER:
        entered = ui.ask_text("language tag (e.g. fr, pt-BR): ").strip()
        if entered:
            state.stt_language = entered
        return
    state.stt_language = name


def _picked(choice: Any) -> bool:
    """True when a screen answer was a real choice rather than Skip."""
    from snowpea_core.setup.state import SKIP

    if isinstance(choice, set):
        choice = next(iter(choice), SKIP)
    return isinstance(choice, str) and choice not in {"", SKIP}


#: What the wizard says when it tests the configured voice.
TEST_PHRASE = "snowpea ready"


def _test_voice(state: WizardState, out: Any) -> None:
    """Synthesise a short phrase and play it, reporting why if it cannot.

    A failed test is information, not an error: setup carries on and the
    answer stays as chosen, because the backend may exist on the machine that
    ends up running the daemon.
    """
    import tempfile

    from snowpea_core.audio import AudioError, player
    from snowpea_core.audio import tts as tts_backends

    provider = tts_backends.resolve_provider(
        state.tts_provider or "auto", command=state.tts_command
    )
    if provider is None:
        out("no speech backend is available here; nothing was tested")
        return
    try:
        with tempfile.TemporaryDirectory(prefix="snowpea-voice-") as tmp:
            speech = _run_sync(
                provider.synthesize(
                    TEST_PHRASE, out_dir=Path(tmp), voice=state.tts_voice, stem="test"
                )
            )
            out(f"{provider.name} wrote {speech.path.name}")
            _run_sync(player.play(speech.path))
            out("played it")
    except AudioError as exc:
        out(f"could not play the test ({exc.code}): {exc}")
    except Exception as exc:  # noqa: BLE001 - a failed test must not stop setup
        out(f"could not test the voice: {exc}")


def _ask_for_registry_token(state: WizardState, *, interactive: bool) -> None:
    """Optional publisher token for ``snowpea skill publish``/``rate``.

    Skipping this leaves publishing to ``SNOWPEA_REGISTRY_TOKEN`` or a
    per-call ``--token``; it never blocks search or install, which need no
    token at all.
    """
    if not interactive:
        return
    hint = "saved — Enter to keep" if state.has_saved_registry_token else "optional, Enter to skip"
    entered = ui.ask_text(f"skill registry publisher token [{hint}]: ", secret=True)
    if entered:
        state.registry_token = entered


def _ask_for_search_key(
    state: WizardState, *, interactive: bool, console: Console | None = None
) -> None:
    """Ask for the search provider's API key or base URL when it needs one.

    Runs the same :class:`~snowpea_core.setup.credentials.CredentialPlan` the
    browser screen and ``/setup`` run, so what counts as "needs a key", what the
    hint says and where the answer is written are decided once.
    """
    from snowpea_core.setup import credentials as creds
    from snowpea_core.tools import search_providers

    pid = state.search_provider
    plan = creds.plan_for("search", pid, state.search_credentials)
    if plan is None:
        return
    if not interactive:
        _note_missing_search_key(state, search_providers)
        return
    out = console.print if console is not None else print
    if not plan.needed:
        return

    _ask_prompts(plan, console=console)
    for field_name, value in plan.answers.items():
        block = dict(state.search_credentials.get(pid) or {})
        block[field_name] = value
        state.search_credentials[pid] = block

    if not (state.search_credentials.get(pid) or {}).get("api_key"):
        message = creds.refusal(plan)
        if message:
            out(message)
    _note_missing_search_key(state, search_providers)


def _ask_prompts(plan: Any, *, console: Console | None = None) -> Any:
    """Run one :class:`~snowpea_core.setup.credentials.CredentialPlan` at a terminal.

    The plan says *what* to ask and whether each answer is a secret; this is
    only the terminal's way of asking it.  ``/setup`` runs the same plan through
    the question queue, which is what keeps the two from drifting apart.
    """
    for prompt in plan.prompts:
        try:
            answer = ui.ask_text(f"{prompt.text()}: ", secret=prompt.secret)
        except (KeyboardInterrupt, EOFError):
            break
        if answer:
            plan.answers[prompt.field] = answer.strip()
    return plan


def _probe_browser(provider_id: str, credentials: dict[str, str]) -> str | None:
    """Kept as a seam the tests patch; the check itself lives in ``credentials``."""
    from snowpea_core.setup import credentials as creds

    plan = creds.CredentialPlan(
        kind="browser", provider_id=provider_id, label=provider_id, prompts=()
    )
    plan.answers.update(credentials)
    return creds.probe(plan)


def _ask_for_browser_key(
    state: WizardState, *, interactive: bool, console: Console | None = None
) -> None:
    """Ask the browser provider for its credentials when it needs any.

    The gap this closes: picking Browserbase or Firecrawl used to write the
    provider name and nothing else, so the choice could not work and nothing
    said so.  Masked input, Enter keeps what is saved, and a provider that
    declares more than one credential (Browserbase wants a project id as well)
    is asked for all of them rather than half.
    """
    from snowpea_core.setup import credentials as creds

    pid = state.browser_provider
    plan = creds.plan_for("browser", pid, state.browser_credentials)
    if plan is None:
        return
    if not interactive:
        _note_missing_browser_key(state)
        return
    out = console.print if console is not None else print
    if not plan.needed:
        return

    _ask_prompts(plan, console=console)
    for field_name, value in plan.answers.items():
        state.set_browser_value(pid, field_name, value)

    block = state.browser_credentials.get(pid) or {}
    if block.get("api_key"):
        problem = _probe_browser(pid, {k: str(v) for k, v in block.items()})
        if problem:
            out(f"warning: {problem}")
    else:
        message = creds.refusal(plan)
        if message:
            out(message)
    _note_missing_browser_key(state)


def _note_missing_browser_key(state: WizardState) -> None:
    """Leave a summary note when the chosen browser provider cannot run."""
    from snowpea_core.tools import browser_providers

    pid = state.browser_provider
    if not browser_providers.needs_key(pid):
        return
    block = state.browser_credentials.get(pid) or {}
    missing = [
        name for name in browser_providers.extra_envs(pid) if not block.get(name.lower())
    ]
    if not block.get("api_key"):
        env = browser_providers.credential_env(pid)
        state.notes.append(
            f"{pid}: no API key — the browser tools refuse until one is set"
            + (f" (or ${env} is exported)" if env else "")
        )
    elif missing:
        state.notes.append(f"{pid}: missing {', '.join(missing)}; the first call will fail")


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

        if vendor not in PRESETS and not state.is_local_server(vendor):
            # A name nothing knows is a new OpenAI-compatible server when the
            # same run says where it lives, and a typo otherwise.
            if not flags.get("base_url"):
                raise SetupError(
                    f"unknown vendor: {vendor} (try `snowpea provider list`, or pass "
                    "--base-url to add it as a local OpenAI-compatible server)"
                )
            try:
                state.add_local_server(vendor)
            except ValueError as exc:
                raise SetupError(str(exc)) from None
        state.select_vendor(vendor)
        state.api_key = flags.get("key") or state.api_key
        state.model = flags.get("model") or state.model
        state.base_url = flags.get("base_url") or state.base_url
        if flags.get("model"):
            # An explicit CLI model is caller intent, not merely an additional
            # profile; make it the default just as the old single-model setup did.
            state.default_model = profile_id(vendor, str(state.model))
        if vendor == "local" and not state.base_url:
            from snowpea_core.providers.presets import LOCAL_VARIANTS

            variant = state.variant or "ollama"
            state.variant = variant
            state.base_url = LOCAL_VARIANTS[variant].base_url
        answered.add("providers")
    elif flags.get("key"):
        raise SetupError("--key needs --vendor")

    search_provider = flags.get("search_provider")
    search_key = flags.get("search_key")
    if search_provider:
        from snowpea_core.tools import search_providers

        if search_providers.get(search_provider) is None:
            raise SetupError(f"unknown search provider: {search_provider}")
        state.search_provider = search_provider
        if search_key:
            state.set_search_key(search_provider, search_key)
        _note_missing_search_key(state, search_providers)
        answered.add("search")
    elif search_key:
        raise SetupError("--search-key needs --search-provider")

    browser_provider = flags.get("browser_provider")
    browser_key = flags.get("browser_key")
    if browser_provider:
        from snowpea_core.tools import browser_providers

        if browser_providers.get(browser_provider) is None:
            raise SetupError(f"unknown browser provider: {browser_provider}")
        state.browser_provider = browser_provider
        if browser_key:
            state.set_browser_key(browser_provider, browser_key)
        _note_missing_browser_key(state)
        answered.add("browser")
    elif browser_key:
        raise SetupError("--browser-key needs --browser-provider")

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
