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
from snowpea_core.setup.screens import audio as audio_screen
from snowpea_core.setup.screens import browser as browser_screen
from snowpea_core.setup.screens import done as done_screen
from snowpea_core.setup.screens import gateway as gateway_screen
from snowpea_core.setup.screens import providers as providers_screen
from snowpea_core.setup.screens import search as search_screen
from snowpea_core.setup.screens import tools as tools_screen
from snowpea_core.setup.state import WizardState, profile_id

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
            _configure_models(state, interactive=interactive, console=console, home=paths.home)
        if name == "search":
            _ask_for_search_key(state, interactive=interactive, console=console)
        if name == "audio":
            _ask_for_audio(state, asker, interactive=interactive, console=console)
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
                api_key=state.api_key
                or str((state.provider_configs.get(state.vendor) or {}).get("api_key") or "")
                or None,
                base_url=state.base_url or None,
                refresh=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a down server must not stop setup
        out(f"could not list models ({exc})")
        entered = ui.ask_text(f"model id [{state.model or 'required'}]: ")
        if entered:
            state.model = entered
        return
    if not available:
        out("the server listed no models")
        entered = ui.ask_text(f"model id [{state.model or 'required'}]: ")
        if entered:
            state.model = entered
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
    while True:
        vendor = ui.ask_text("add another model — provider id (Enter to finish): ").strip()
        if not vendor:
            break
        state.select_vendor(vendor)
        _ask_for_key(state, interactive=True)
        _ask_for_model(state, interactive=True, console=console)
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
    prompt = f"default model (Enter={default_idx}: {profiles[default_idx - 1]}): "
    picked = ui.ask_text(prompt).strip()
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
        agent = ui.ask_text("assign model to agent (Enter to finish): ").strip()
        if not agent:
            break
        current = state.agent_models.get(agent)
        hint = f"; Enter uses default{f' (currently {current})' if current else ''}"
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
    voice_hint = state.tts_voice or "Enter for the backend's default"
    voice = ui.ask_text(f"voice [{voice_hint}]: ")
    if voice:
        state.tts_voice = voice
    current = "Y/n" if state.auto_speak else "y/N"
    spoken = ui.ask_text(f"read replies aloud by default? [{current}]: ").strip().lower()
    if spoken:
        state.auto_speak = spoken.startswith("y")
    if ui.ask_text("test the voice now? [y/N]: ").strip().lower().startswith("y"):
        _test_voice(state, out)


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


def _ask_for_search_key(
    state: WizardState, *, interactive: bool, console: Console | None = None
) -> None:
    """Ask for the search provider's API key when it needs one.

    ``ddgs`` and the self-hosted providers skip this; the keyed ones ask, and
    an empty answer warns rather than silently leaving the provider unusable.
    """
    from snowpea_core.tools import search_providers

    pid = state.search_provider
    provider = search_providers.get(pid)
    if provider is None:
        return
    if not interactive:
        _note_missing_search_key(state, search_providers)
        return
    out = console.print if console is not None else print
    if provider.meta.key == "self-hosted":
        env = next((name for name in provider.meta.env if name.endswith("_URL")), "")
        current = (state.search_credentials.get(pid) or {}).get("url") or ""
        hint = "saved — Enter to keep" if current else f"Enter to use ${env}" if env else ""
        entered = ui.ask_text(f"{provider.meta.label} base URL [{hint}]: ")
        if entered:
            block = dict(state.search_credentials.get(pid) or {})
            block["url"] = entered
            state.search_credentials[pid] = block
        return
    if provider.meta.key == "no key":
        return
    env = search_providers.credential_env(pid)
    saved = state.has_search_key(pid)
    hint = "saved — Enter to keep" if saved else (f"Enter to use ${env}" if env else "optional")
    entered = ui.ask_text(f"{provider.meta.label} API key [{hint}]: ", secret=True)
    if entered:
        state.set_search_key(pid, entered)
    elif not saved and provider.meta.key == "key required":
        out(f"no key entered — {pid} cannot answer searches until it has one")
    _note_missing_search_key(state, search_providers)


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
