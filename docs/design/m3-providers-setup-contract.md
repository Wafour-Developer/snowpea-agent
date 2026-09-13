# M3 Providers & Setup Contract (binding for US-011, US-012)

Builds on `providers/base.py` (ToolSpec/ChatMessage/StreamEvent/ChatProvider), `providers/registry.py`, `providers/anthropic_native.py`, `providers/fake.py`. Plan: §2.8, §3.1 providers/setup, §4 M3, AC-02, AC-02b, §6 risk 3.

## 1. Vendor presets (`providers/presets.py`)
```python
@dataclass(frozen=True)
class VendorPreset:
    id: str                      # anthropic|openai|openrouter|gemini|xai|glm|minimax|kimi|deepseek|qwen|local
    label: str
    adapter: Literal["anthropic_native","gemini_native","openai_compat"]
    base_url: str | None         # openai_compat only; local → user supplied (default http://localhost:11434/v1)
    default_model: str
    auth_methods: tuple[str, ...]        # ("api_key",) | ("api_key","device_code","oauth_token")
                                         # | ("api_key","oauth_pkce") | ("api_key","google_adc","oauth_token")
    env_keys: tuple[str, ...]            # e.g. ("OPENAI_API_KEY",)
    supports_parallel_tools: bool = True
    tool_call_style: Literal["openai","anthropic","gemini"] = "openai"
    stream_delta_shape: Literal["openai","anthropic","gemini"] = "openai"
    extra_headers: dict[str, str] = field(default_factory=dict)
PRESETS: dict[str, VendorPreset]   # exactly 11 ids above; "local" covers vLLM/Ollama/LM Studio via base_url sub-presets local-vllm/local-ollama/local-lmstudio (same id "local", `variant`)
```
Auth methods per preset (`providers/presets.py`): `openai` = `("api_key","device_code","oauth_token")`, `gemini` = `("api_key","google_adc","oauth_token")`, `openrouter` = `("api_key","oauth_pkce")`; every other vendor is `("api_key",)`. `gemini`'s two OAuth methods and `openai`'s `oauth_token` were added in v0.1.x — the original "exactly `openai` has `device_code`, exactly `openrouter` has `oauth_pkce`" no longer holds. `provider.list` returns `[{vendor, label, authMethods, configured, defaultModel, models?}]` for all 11.

## 2. Adapters
- `providers/openai_compat.py` — `/v1/chat/completions` streaming (`stream: true`, `tools`, `tool_choice: auto`), handles both accumulated tool-call deltas (index-based) and whole-object tool calls; usage from `stream_options.include_usage` when supported else final chunk. All quirks go through `providers/normalize.py` (single point) using preset flags.
- `providers/gemini_native.py` — `generateContent` streaming (`streamGenerateContent?alt=sse`), function declarations, functionCall/functionResponse parts.
- `providers/anthropic_native.py` — already M1.
- `providers/registry.py.get(vendor, model)` resolves: `SNOWPEA_PROVIDER` (`fake:<script>` or `<vendor>[:model]`) → session/provider arg → `settings.providers.default` → first configured vendor. Credentials from `settings.providers[vendor].api_key` or `env_keys`; for `openai` a stored `oauth_token` is used as the bearer credential when no API key is set, and `gemini` passes `auth_method`/`oauth_token` straight to `gemini_native`.
- The **model profile layer sits above this** (v0.1.x, §6): `SessionManager.create` calls `config/model_routing.route_for()` — explicit override → the agent's assignment (project over global) → the definition's `model:` → the session pin → the project default → `models.default` — and hands the resolved `(provider, model)` to `ProviderRegistry.get()`. `route_for` returning `ModelRoute(None, None)` means "no multi-model settings apply", and the registry's own resolution above is then used unchanged.

## 3. Web token login (`providers/auth_web.py`)

Each flow is split into a `*_start` (talk to the vendor / open the callback server, return the code and URL) and a `*_finish` (poll or wait, exchange for a token), joined by a `LoginStart` dataclass whose `finish()` resumes to a `LoginResult` (CORE-login-progress, added in v0.1.x).

- **`openai` device code** — POST the device authorization endpoint, surface `user_code` + `verification_uri`, poll the token endpoint until granted/expired; store under `settings.providers.openai`. Endpoints live in one `ENDPOINTS` dict so they can be corrected without code changes. This authenticates a **ChatGPT/Codex subscription**; direct OpenAI API billing remains a separate API-key path.
- **`openrouter` OAuth PKCE** — verifier/challenge, `https://openrouter.ai/auth?callback_url=http://localhost:<port>/callback&code_challenge=…&code_challenge_method=S256`, local aiohttp callback receives `code`, POST `https://openrouter.ai/api/v1/auth/keys` → `key` saved as `api_key`.
- **`gemini` Google ADC** — `google_adc_start` shells `gcloud auth application-default login`. It MUST fail with a named prerequisite when `shutil.which("gcloud")` is empty ("gemini OAuth login needs the Google Cloud CLI (`gcloud`); install it or configure a Gemini API key") rather than a generic error. On success it persists `{"auth_method": "google_adc"}` — no token is copied into `settings.json`; `gemini_native` shells `gcloud` per request for a fresh access token and sends it as `Authorization: Bearer …`.
- **`gemini` / `openai` remote token** — for a headless or remote machine with no browser and no `gcloud`, an access token obtained elsewhere is stored as `settings.providers.<vendor>.oauth_token` with `auth_method: "oauth_token"` and sent as a bearer credential (`providers/gemini_native.py`; `providers/registry.py`). `ProviderRegistry.is_configured` counts a vendor with `auth_method` in `("google_adc","oauth_token")`, or a non-empty `oauth_token`, as configured even with no API key.
- Any other vendor → `RpcError("login_unsupported", "…use API key: snowpea setup --vendor <v> --key …")`.

RPC `provider.loginWeb(vendor, method)` answers `status: "await_user"` with `userCode` / `verificationUri` / `verificationUriComplete` / `expiresInSec` **as soon as the code or URL is known**, and runs the rest as a connection-tracked background task reporting `provider.loginProgress` (phases `started|await_user|polling|done|failed`, broadcast like `system.updateProgress`). A failed background login is reported as `loginProgress{phase:"failed"}` and a log line — the RPC response is already gone, so nothing is attached to it, and nothing is persisted on that path.

`provider.configure` accepts exactly these keys and silently drops the rest (`server/app_server.py`, `provider_configure_handler`): `api_key`, `base_url`, `model`, `models`, `variant`, `token`, `oauth_token`, `refresh_token`, `auth_method`. An empty result after filtering is `invalid_params`.

CLI:
- `snowpea setup --login <vendor>` (unchanged; an alias for the command below).
- `snowpea provider login <vendor>` → `provider.loginWeb`, 900s timeout (`cli/commands.py`, `provider_login`).
- `snowpea provider login <vendor> --token [TOKEN]` stores an OAuth access token directly via `provider.configure`; **`openai` and `gemini` only**, every other vendor exits `EXIT_USAGE` with "does not expose an OAuth access-token login; use its API key". A bare `--token` prompts with `getpass`; an empty answer is a usage error.

## 4. Replay & fixtures (`providers/replay.py`, `tests/fixtures/providers/`)
- Fixture file: `tests/fixtures/providers/<vendor>/<case>.json` = `{"vendor","model","synthetic":bool,"exchanges":[{"request":{...scrubbed...},"response_stream":[<raw SSE/JSON chunks>]}]}`.
- `SNOWPEA_PROVIDER_MODE=replay` makes adapters' HTTP layer return recorded chunks by matching request order (not content). `record` mode (maintainers) writes fixtures with scrubbing (Authorization, cookies, `organization`, account ids → `***`). `scripts/scrub_fixtures.py --check <dir>` scans for `sk-`, `Bearer `, `key=` patterns.
- Because no real keys exist in this environment, generate **synthetic** golden fixtures for all 11 vendors from the documented wire formats (mark `"synthetic": true`); the matrix test asserts the normalized StreamEvent sequence `text_delta* → tool_call → usage → done(tool_use)` then a second exchange `text_delta* → done(end_turn)` for the golden scenario "call shell once → answer".
- `tests/fixtures/providers/fake/scripted.py` is the M1 `providers/fake.py` re-exported (keep one implementation).

## 5. Setup wizard (`setup/`)
- `setup/catalog.py`: `CatalogItem(id, label, tier: free|paid|subscription, key: "no key"|"key optional"|"key required"|"self-hosted", default: bool, description)`, catalogs for `search` (from `tools/search_providers` registry order), `browser`, `tools` (categories with default on/off: file/terminal/git/web/browser/delegate/schedule/memory/skills/todo/session-search/clarify/cron on; media(image/video/tts) on-but-inactive; vision, computer-use, x-search off), `gateway` (telegram/discord/slack, all off).
- `setup/screens/*.py`: each screen = pure function `(state, catalog) -> Screen(title, items, selected, multi: bool, help)` plus `apply(state, choice)`; rendering via `rich` prompts (single-select list with ↑↓/Enter, multi-select with Space, `Skip — keep defaults` always last). Non-interactive path: every screen has a CLI flag (`--vendor/--key/--model`, `--search-provider`, `--browser-provider`, `--tools a,b,-c`, `--gateway telegram --token ...`).
- `setup/wizard.py`: `quick` (vendor screen only + defaults), `full` (all screens in order providers → search → browser → **audio** → tools → gateway → done; the Audio section — speech-to-text and text-to-speech backends, voice, read-aloud — was added by CORE-multimodal and deliberately carries no circled numeral, so the ①–⑥ the other screens print did not have to be renumbered), `blank` (write default settings, nothing asked). `setup/detect.py` finds env keys, existing `~/.hermes` / Claude Code config for import hints, node/uv presence.

  **Authentication prompt (`setup/wizard.py`, `_ask_for_key`).** When the picked vendor's preset lists more than one auth method the wizard asks `authentication [1=API key, 2=browser login, 3=OAuth token (remote/headless)] (Enter=1):`. Option 3 appears only when `"oauth_token"` is in the preset's `auth_methods`. `2` runs `auth_web.login(vendor)` synchronously and, on success, merges the returned credentials into the vendor block while clearing any stale `api_key`/`oauth_token`; a failure prints the vendor's reason (with a hint on HTTP 403) and re-prompts instead of ending the wizard, and a cancellation is a `state.notes` line. `3` prompts for the token with `secret=True` and sets `auth_method = "oauth_token"`. Anything else falls through to the API-key prompt, so Enter still means "key" as before, and a vendor with one auth method is asked nothing new. (v0.1.x에서 추가)

  **Gateway follow-ups (CORE-gateway-autostart).** `setup/screens/gateway.py` stays a pure build/apply pair with no I/O; `wizard._ask_for_gateway` asks for the token and then the approver's account id (`settings.gateway.<platform>.allowed_user_id`) the way `_ask_for_key` follows the providers screen. Blank is allowed and is reported in the summary as `telegram (no approver)` with a note that chat approvals stay blocked — a read-only messenger is a legitimate setup. `--gateway/--token/--user-id` and the non-interactive path are unaffected.
- Output: `$SNOWPEA_HOME/settings.json` with `providers`, `search.provider`, `browser.provider`, `tools.enabled_categories`, `gateway`, and (v0.1.x) `audio`, `models` (§6), `agents.teams` / `agents.default_team` (`config/settings.py`). Ordering assertion (AC-02b): search list order = free·no-key first, then free·key/self-hosted, then paid; first item marked `★`.

## 6. Model profiles and per-agent routing (`config/model_routing.py`)

Added in v0.1.x. `settings.json` carries two related blocks (`config/settings.py`):

```jsonc
{
  "models": {
    "default": "sonnet",                                   // profile id used by new sessions
    "profiles": {                                          // ModelProfile{provider, model}
      "sonnet": {"provider": "anthropic", "model": "claude-sonnet-4-5"},
      "cheap":  {"provider": "openai",    "model": "gpt-5-mini"}
    }
  },
  "agents": {
    "models": {"executor": "cheap", "architect": "sonnet"} // agent name -> profile id
  }
}
```

A project may carry the same block, plus `agents`, in `<workdir>/.snowpea/settings.json` (`ProjectModelsSettings`, `config/project.py`). Each key merges over the global one key by key — project wins — exactly as `team_config.teams_for` merges teams. `ModelProfile` lives in `config/project.py` and is re-exported from `config/settings.py`. (CORE-model-assignment)

`route_for(settings, *, provider, model, agent, definition_model, workdir, session_pin) -> ModelRoute{provider, model}` resolves in strict precedence order, first non-empty wins:

1. explicit `provider` / `model` arguments (already-resolved caller intent — `session.create`, `delegate_task(model=…)`, `agent.spawn(model=…)`, an RPC arg);
2. the agent's assignment: project `models.agents[agent]` over global `agents.models[agent]`;
3. `definition_model` — the `model:` field of the agent's `.md` definition;
4. `session_pin` — what `/model` or `session.setModel` fixed on this session, and what a subagent inherits from its parent;
5. project `models.default`;
6. global `settings.models.default`.

Returning `ModelRoute(None, None)` is meaningful: it means "no multi-model settings apply", and the caller MUST keep the old `ProviderRegistry` default or parent inheritance rather than substituting anything. The only caller in core is `SessionManager.create`, which also derives `definition_model` itself through the `definition_model_for` hook `wire_core` injects — callers MUST NOT be required to pass it.

A reference is resolved by `resolve_reference`: a known profile id wins (project profiles included); `"inherit"` and the empty string resolve to nothing; `vendor:model` is accepted as the legacy spelling; a bare word is accepted **only when it names a vendor in `PRESETS`**, otherwise it logs a warning and resolves to nothing so the next rung applies.

There MUST be exactly one implementation of this precedence. `agent/subagent.py` previously held a second one (`_split_model`) that never consulted `models.profiles`, so a valid profile id in an agent `.md` was read as a vendor name; it is deleted.

The settings validator MUST reject unknown profile references — both `models.default` and every value in `agents.models` — on the `settings.set` path, so a typo is `invalid_params` rather than a silent fall-through. On the **load** path it MUST NOT be fatal: `Settings.load` drops the unresolvable references, logs them at ERROR and boots degraded, because a daemon that refuses to start leaves no `settings.set` to repair it with. Any other validation failure still raises.

`settings.set` merges, and a merge cannot express a removal, so **`null` in a patch deletes the key** (`config/patch.deep_merge`, shared with `server/settings_handlers.py`). That is the only way to delete a profile, an agent assignment or a team over RPC. It is safe for scalars because every optional field in `Settings`/`ProjectSettings` defaults to `None`, so deleting a key and setting it to `null` validate to the same document. The validator still runs afterwards: deleting a profile that `models.default` still names is `invalid_params`.

`ProviderRegistry.default_vendor()` MUST ignore a default profile whose provider is not in `PRESETS` and fall through to `providers.default`; a bogus default profile must break at most the sessions that route through it, never every session.

`/model` lists the configured profiles (project merged over global, effective default marked) above the vendor's models and accepts a profile id, `vendor:model`, a bare vendor, a model name, a **row number** (`/model 2`), `inherit` to clear the pin, or `default <id>` to write `models.default`. A pin is persisted on the **session row** (`Store.update_model`) so it survives a restart and `session.resume`, and emits `model.changed`; a plain model id of the current vendor additionally persists to `settings.providers.<vendor>.model` (`commands/model_cmd.py`). `provider.models(vendor?)` asks one vendor's endpoint what it actually serves, defaulting to `ProviderRegistry.default_vendor()`. The `local` preset's `default_model` is a placeholder: an `openai_compat` adapter built with a placeholder and no `model_resolver` MUST raise `model_not_configured` rather than calling the server (`providers/openai_compat.py`, `providers/registry.py`), and a listing made only of placeholders counts as no listing.
