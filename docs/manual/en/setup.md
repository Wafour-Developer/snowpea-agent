# Setup

`snowpea setup` writes `$SNOWPEA_HOME/settings.json`. It has three shapes.

```bash
snowpea setup            # quick: configure LLM models; defaults for other sections
snowpea setup --full     # every screen, in order
snowpea setup --blank    # asks nothing, writes the defaults
```

Quick is the right answer the first time. Full is worth one pass once you know what you want to change. Blank exists for scripted installs and for CI.

## The screens

`--full` walks five screens and a summary. Every screen ends with **Skip — keep defaults**, and every screen has a command-line flag so you never have to be interactive.

| Screen | Choice | Flag |
|---|---|---|
| Providers | LLM vendor, key, model | `--vendor`, `--key`, `--model`, `--base-url` |
| Search | one web-search provider | `--search-provider` |
| Browser | one browser provider | `--browser-provider` |
| Tools | which tool categories are on | `--tools` |
| Gateway | Telegram / Discord / Slack | `--gateway`, `--token`, `--user-id` |
| Done | summary of what was written | — |

Lists are ordered free-and-keyless first, then free-but-needs-a-key or self-hosted, then paid. The default in each list is marked with a star. Nothing in the default configuration requires a paid account beyond your LLM vendor: web search and the browser both work with no key at all.

## Multiple models and agent assignments

Run `snowpea setup providers` to register multiple models, choose a default, and assign registered models to built-in or custom agents. Multiple models from the same provider are supported. Clear an assignment to return an agent to the default.

A model profile pairs a provider with a model ID. Credentials and base URLs remain shared in `providers.<provider>`; profiles do not duplicate API keys. This configuration illustrates the structure; replace the example model IDs with real ones.

```json
{
  "models": {
    "default": "daily",
    "profiles": {
      "daily": {"provider": "openai", "model": "your-default-model-id"},
      "reasoning": {"provider": "anthropic", "model": "your-reasoning-model-id"}
    }
  },
  "agents": {
    "max_concurrent": 3,
    "models": {"architect": "reasoning", "critic": "reasoning"}
  }
}
```

Agent routing precedence is **agent assignment → explicit model in the agent definition → default model**. Agents without an assignment or explicit definition model use `models.default`. New ordinary sessions also start with the default; explicit session provider/model overrides are preserved. Existing sessions are not automatically changed. Installations without model profiles retain their legacy behavior.

## Vendors

Eleven vendors ship in v0.1.

| Vendor id | Label | Adapter | Auth |
|---|---|---|---|
| `anthropic` | Anthropic | native Messages API | API key |
| `openai` | OpenAI | OpenAI-compatible | API key, device-code login |
| `openrouter` | OpenRouter | OpenAI-compatible | API key, OAuth PKCE login |
| `gemini` | Google Gemini | native | API key, Google OAuth (ADC via `gcloud`), access token |
| `xai` | xAI Grok | OpenAI-compatible | API key |
| `glm` | Zhipu GLM | OpenAI-compatible | API key |
| `minimax` | MiniMax | OpenAI-compatible | API key |
| `kimi` | Moonshot Kimi | OpenAI-compatible | API key |
| `deepseek` | DeepSeek | OpenAI-compatible | API key |
| `qwen` | Qwen | OpenAI-compatible | API key |
| `local` | OpenAI-compatible local (vLLM, Ollama, LM Studio) | OpenAI-compatible | base URL, key optional |

```bash
snowpea provider list
snowpea provider list --json
```

`provider list` shows each vendor's auth methods, default model, and whether it is configured.

On a desktop, `snowpea provider login gemini` opens Google's ADC login. On a
remote/headless machine, run `snowpea provider login gemini --token` and paste
the OAuth access token at the hidden prompt. OpenAI supports the same `--token`
form. Omit the value so the token does not appear in shell history.

### Adding a key

```bash
snowpea setup --vendor deepseek --key sk-your-key-here
snowpea setup --vendor anthropic --key sk-ant-... --model claude-sonnet-4-5
```

Environment variables are picked up too — if `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is already exported, setup offers it rather than asking you to paste.

### Browser login

Two vendors support logging in through a browser instead of pasting a key.

```bash
snowpea provider login openai        # device code: a code appears, you approve it in the browser
snowpea provider login openrouter    # OAuth PKCE: a local callback receives the code
```

`snowpea setup --login openai` is an alias of the same thing. Any other vendor answers with `login_unsupported` and tells you the `--vendor`/`--key` command to run instead:

```bash
snowpea provider login deepseek
```

### A local model

```bash
snowpea setup --vendor local --base-url http://localhost:11434/v1 --model qwen3:8b
```

Anything that speaks `/v1/chat/completions` works — vLLM, Ollama, LM Studio, llama.cpp's server. Tool calling has to be supported by the model you load, or the agent will be able to talk but not act.

### Which vendor gets used

In order of precedence: the `SNOWPEA_PROVIDER` environment variable, then `--provider` on the command line or the `provider` argument of the session, then `providers.default` in settings, then the first configured vendor.

```bash
snowpea -c "summarize README.md" --provider deepseek
```

## Search providers

`web_search` and `web_extract` sit on a provider registry. The default, `ddgs`, needs no key and no account.

```bash
snowpea setup --search-provider ddgs
snowpea setup --search-provider exa --search-key sk-your-exa-key
snowpea setup search
```

`ddgs` is the only provider that needs nothing at all. The `*_free` ids are free *tiers* of keyed products, not keyless endpoints: Exa answers `402` without a key, Parallel and Keenable answer `401`, and so does Tavily. They are tagged `key required` and cannot answer a search until a key is configured.

| id | tag | needs |
| --- | --- | --- |
| `ddgs` | free, no key | nothing |
| `firecrawl` | paid, key optional | nothing; the cloud search endpoint answers keyless but rate-limited |
| `brave_free` | free, key required | `BRAVE_API_KEY` |
| `exa_free` | no key | Anonymous, rate-limited hosted MCP at `https://mcp.exa.ai/mcp` |
| `exa` | key required | `EXA_API_KEY` (direct REST API) |
| `keenable_free`, `keenable` | key required | `KEENABLE_API_KEY` |
| `parallel_free`, `parallel` | key required | `PARALLEL_API_KEY` |
| `tavily` | free, key required | `TAVILY_API_KEY` |
| `xai_grok` | paid, key required | `XAI_API_KEY` |
| `searxng` | free, self-hosted | `SEARXNG_URL` |
| `firecrawl_selfhost` | free, self-hosted | `FIRECRAWL_URL` |

Choosing a key-required provider in `snowpea setup search` prompts for the key (masked) and stores it under `search.credentials.<id>.api_key`; leaving it empty prints a warning, because a provider without its key cannot answer.

`exa_free` uses Exa's official hosted MCP tools (`web_search_exa` and `web_fetch_exa`) anonymously; no API key prompt is shown. Anonymous rate limits still apply. Choose `exa` instead when you want the direct API with `EXA_API_KEY` and paid account limits.

When the configured provider cannot run, `web_search` falls back and says so rather than pretending. The session gets one `error{code:"search_provider_unavailable"}` event, and the assistant is instructed to repeat the reason to you.

Check which provider actually answers:

```bash
snowpea search test "snowpea agent github"
snowpea search test "snowpea agent github" --json
snowpea tools list --json
```

`snowpea search test` runs one real query with your configured provider and prints the provider that answered, plus the reason each skipped provider dropped out. `snowpea tools list` shows `web_search` with its provider, written as `exa_free → ddgs` when the configured id cannot run.

`web_extract` refuses private, loopback and link-local addresses, and truncates fetched pages to `tools.max_output_chars` (20000 by default).

## Browser providers

`local_chromium` is the default and runs a headless Chromium through Playwright on your own machine. The first run may ask you to download the browser binary. The other ids — `camoufox`, `browser_use_local`, `browserbase`, `firecrawl_cloud` — are registered so you can see and select them, and answer with `browser_provider_unavailable` until they are configured.

```bash
snowpea setup --browser-provider local_chromium
```

## Tool categories

Categories switch whole groups of tools on and off. Pass a comma-separated list; a leading `-` turns one off.

```bash
snowpea setup --tools media,-browser
snowpea tools list
```

Categories are `file`, `terminal`, `git`, `web`, `browser`, `delegate`, `schedule`, `memory`, `media`, and `mcp` for anything a `.mcp.json` server contributed. Media tools (`image_generate`, `video_generate`, `music_generate`, `text_to_speech`) are always registered but stay `inactive` until credentials exist; they flip to `active` without a restart once configured, and calling one before that returns `tool_inactive` with a hint.

## Gateway

```bash
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token --user-id 987654
```

The interactive screen asks for both: the bot token, then your own account id on that platform (Telegram tells you yours if you send `/start` to [@userinfobot](https://t.me/userinfobot)). The id matters because it is the only account allowed to answer an approval from chat.

The token is stored in `$SNOWPEA_HOME/credentials.json` (mode `0600`), and the messenger starts listening with the daemon — no binding step needed. Binding a bot to a *particular* agent, session or chat is still a separate step, covered in [Gateway](gateway.md).

## What ends up on disk

`$SNOWPEA_HOME/settings.json` holds `providers`, `search.provider`, `browser.provider`, `tools.enabled_categories`, `gateway`, `agents.max_concurrent` (3), `team.max_conflict_retries` (2), `approvals.timeoutSec` (300) and `memory.enabled` (true). Per-project overrides for mode, allowlist and backend live in `<project>/.snowpea/settings.json` and win over the global file. Secrets are never written into `settings.json`, and never logged.

## Next

[Modes](modes.md) — decide how much the agent may do without asking.
