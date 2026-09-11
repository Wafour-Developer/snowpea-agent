# Setup

`snowpea setup` writes `$SNOWPEA_HOME/settings.json`. It has three shapes.

```bash
snowpea setup            # quick: asks for one LLM vendor, defaults everything else
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

## Vendors

Eleven vendors ship in v0.1.

| Vendor id | Label | Adapter | Auth |
|---|---|---|---|
| `anthropic` | Anthropic | native Messages API | API key |
| `openai` | OpenAI | OpenAI-compatible | API key, device-code login |
| `openrouter` | OpenRouter | OpenAI-compatible | API key, OAuth PKCE login |
| `gemini` | Google Gemini | native | API key |
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
snowpea setup --search-provider tavily
```

Free and keyless: `ddgs` (default), `exa_free`, `keenable_free`, `parallel_free`. Free with a key or self-hosted: `brave_free`, `tavily`, `searxng` (set `SEARXNG_URL`), `firecrawl_selfhost`. Paid: `exa`, `keenable`, `parallel`, `firecrawl`, `xai_grok`. If the configured provider fails, `web_search` falls back down the free chain and logs which one answered.

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
