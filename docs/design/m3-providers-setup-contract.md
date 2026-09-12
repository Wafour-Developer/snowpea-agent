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
    auth_methods: tuple[str, ...]        # ("api_key",) | ("api_key","device_code") | ("api_key","oauth_pkce")
    env_keys: tuple[str, ...]            # e.g. ("OPENAI_API_KEY",)
    supports_parallel_tools: bool = True
    tool_call_style: Literal["openai","anthropic","gemini"] = "openai"
    stream_delta_shape: Literal["openai","anthropic","gemini"] = "openai"
    extra_headers: dict[str, str] = field(default_factory=dict)
PRESETS: dict[str, VendorPreset]   # exactly 11 ids above; "local" covers vLLM/Ollama/LM Studio via base_url sub-presets local-vllm/local-ollama/local-lmstudio (same id "local", `variant`)
```
Exactly `openai` has `device_code`, exactly `openrouter` has `oauth_pkce`. `provider.list` returns `[{vendor, label, authMethods, configured, defaultModel, models?}]` for all 11.

## 2. Adapters
- `providers/openai_compat.py` — `/v1/chat/completions` streaming (`stream: true`, `tools`, `tool_choice: auto`), handles both accumulated tool-call deltas (index-based) and whole-object tool calls; usage from `stream_options.include_usage` when supported else final chunk. All quirks go through `providers/normalize.py` (single point) using preset flags.
- `providers/gemini_native.py` — `generateContent` streaming (`streamGenerateContent?alt=sse`), function declarations, functionCall/functionResponse parts.
- `providers/anthropic_native.py` — already M1.
- `providers/registry.py.get(vendor, model)` resolves: `SNOWPEA_PROVIDER` (`fake:<script>` or `<vendor>[:model]`) → session/provider arg → `settings.providers.default` → first configured vendor. Credentials from `settings.providers[vendor].api_key` or `env_keys`.

## 3. Web token login (`providers/auth_web.py`)
- `openai` device code: POST device authorization endpoint, print `user_code` + `verification_uri`, poll token endpoint until granted/expired; store `settings.providers.openai.token` (+refresh if given). Endpoints/config constants in one dict so they can be corrected without code changes.
- `openrouter` OAuth PKCE: generate verifier/challenge, open `https://openrouter.ai/auth?callback_url=http://localhost:<port>/callback&code_challenge=...&code_challenge_method=S256`, local aiohttp callback server receives `code`, POST `https://openrouter.ai/api/v1/auth/keys` {code, code_verifier, code_challenge_method} → `key` saved as api_key.
- Any other vendor → `RpcError("login_unsupported", "...use API key: snowpea setup --vendor <v> --key ...")`.
- RPC `provider.loginWeb(vendor, method)`; CLI `snowpea setup --login <vendor>`.

## 4. Replay & fixtures (`providers/replay.py`, `tests/fixtures/providers/`)
- Fixture file: `tests/fixtures/providers/<vendor>/<case>.json` = `{"vendor","model","synthetic":bool,"exchanges":[{"request":{...scrubbed...},"response_stream":[<raw SSE/JSON chunks>]}]}`.
- `SNOWPEA_PROVIDER_MODE=replay` makes adapters' HTTP layer return recorded chunks by matching request order (not content). `record` mode (maintainers) writes fixtures with scrubbing (Authorization, cookies, `organization`, account ids → `***`). `scripts/scrub_fixtures.py --check <dir>` scans for `sk-`, `Bearer `, `key=` patterns.
- Because no real keys exist in this environment, generate **synthetic** golden fixtures for all 11 vendors from the documented wire formats (mark `"synthetic": true`); the matrix test asserts the normalized StreamEvent sequence `text_delta* → tool_call → usage → done(tool_use)` then a second exchange `text_delta* → done(end_turn)` for the golden scenario "call shell once → answer".
- `tests/fixtures/providers/fake/scripted.py` is the M1 `providers/fake.py` re-exported (keep one implementation).

## 5. Setup wizard (`setup/`)
- `setup/catalog.py`: `CatalogItem(id, label, tier: free|paid|subscription, key: "no key"|"key optional"|"key required"|"self-hosted", default: bool, description)`, catalogs for `search` (from `tools/search_providers` registry order), `browser`, `tools` (categories with default on/off: file/terminal/git/web/browser/delegate/schedule/memory/skills/todo/session-search/clarify/cron on; media(image/video/tts) on-but-inactive; vision, computer-use, x-search off), `gateway` (telegram/discord/slack, all off).
- `setup/screens/*.py`: each screen = pure function `(state, catalog) -> Screen(title, items, selected, multi: bool, help)` plus `apply(state, choice)`; rendering via `rich` prompts (single-select list with ↑↓/Enter, multi-select with Space, `Skip — keep defaults` always last). Non-interactive path: every screen has a CLI flag (`--vendor/--key/--model`, `--search-provider`, `--browser-provider`, `--tools a,b,-c`, `--gateway telegram --token ...`).
- `setup/wizard.py`: `quick` (vendor screen only + defaults), `full` (all screens in order providers → search → browser → **audio** → tools → gateway → done; the Audio section — speech-to-text and text-to-speech backends, voice, read-aloud — was added by CORE-multimodal and deliberately carries no circled numeral, so the ①–⑥ the other screens print did not have to be renumbered), `blank` (write default settings, nothing asked). `setup/detect.py` finds env keys, existing `~/.hermes` / Claude Code config for import hints, node/uv presence.
- Output: `$SNOWPEA_HOME/settings.json` with `providers`, `search.provider`, `browser.provider`, `tools.enabled_categories`, `gateway`. Ordering assertion (AC-02b): search list order = free·no-key first, then free·key/self-hosted, then paid; first item marked `★`.
