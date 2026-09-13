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

## Where the model list comes from

Every surface that lists models — the setup wizard, `snowpea provider models`, the `provider.models` RPC and the TUI `/model` picker — asks one function, which tries four things in order and tells you which one answered:

| Rung | Source | When it answers |
| --- | --- | --- |
| 1 | **live** — the vendor's own endpoint | whenever the vendor can be reached with your credential |
| 2 | **settings** — `providers.<vendor>.models`, or `.oauth_models` for an OAuth account | when the live listing fails or you have pinned a list |
| 3 | **cache** — the last good listing, under `$SNOWPEA_HOME/cache/models-<vendor>-<auth>.json` | when the vendor is unreachable and nothing is pinned |
| 4 | **curated** — this build's list, merged with the public [models.dev](https://models.dev) catalog | last resort, and the normal answer for backends that publish no listing |

The live endpoint differs by vendor *and* by how you signed in: `/v1/models` for the OpenAI-compatible vendors, `/v1beta/models` for a Gemini API key, Anthropic's `/v1/models`, Ollama's `/api/tags` for a local server that has no OpenAI-compatible listing, and — for a ChatGPT subscription — the Codex backend's own per-account catalog at `chatgpt.com/backend-api/codex/models`, which is what the Codex CLI shows. A Google sign-in runs on Code Assist, which publishes no model listing at all, so those accounts always show the curated list; that is the correct answer there, not a degraded one.

To pin a list yourself — an account with early access to a model nothing advertises yet, or a server whose listing lies:

```json
{
  "providers": {
    "local": {"base_url": "http://localhost:8000/v1", "models": ["Qwen/Qwen3-32B"]},
    "openai": {"auth_method": "chatgpt", "oauth_models": ["gpt-5.1-codex"]}
  }
}
```

`models` applies to any account; `oauth_models` applies only when that provider is signed in with OAuth, so one block can pin the Codex catalog without also pinning what an API key would see. Both are read on the next listing — no restart. Set `SNOWPEA_MODELS_DEV=0` to keep the curated rung off the network entirely.

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

### Which model a turn actually uses

Five rungs, highest first. The first one that resolves wins; anything left unresolved falls through to the vendor's own default.

| # | Rung | Set it with |
|---|---|---|
| 1 | a one-off override for this delegation | `delegate_task(model=…)`, `agent.spawn(model=…)` |
| 2 | the agent's assignment | `snowpea model assign <agent> <profile>`, or `agents.models` / project `models.agents` |
| 3 | the session pin | `/model <profile>`, `session.setModel` |
| 4 | the project default | project `models.default` |
| 5 | the global default | `models.default`, or `snowpea model default <profile>` |

Rung 2 reads the agent's assignment first and the agent definition's own `model:` field second. A reference is a profile id, a `vendor:model` pair, or a bare vendor name; `inherit` means "no opinion, keep going".

New ordinary sessions start at rung 4/5. Existing sessions are not automatically changed when you edit settings, except that a session you pinned keeps its pin — the pin is stored with the session and survives a restart. Installations with no model profiles at all retain their legacy behaviour.

### Per-project models

A repository can carry its own `models` block in `<workdir>/.snowpea/settings.json`, with the same three keys plus `agents`. Each merges over the global one key by key, so you only state what differs:

```json
{
  "models": {
    "default": "reasoning",
    "agents": {"executor": "daily"},
    "profiles": {"local": {"provider": "ollama", "model": "your-local-model-id"}}
  }
}
```

From the shell:

```bash
snowpea model profiles                       # the merged view, each row tagged global or project
snowpea model default reasoning --project    # this repository's default
snowpea model assign executor daily --project
snowpea model assign executor                # omit the id to clear the assignment
```

### Reply language

`agent.replyLanguage` decides what language answers come back in. `"auto"`, the default, follows whatever language you wrote in; a tag such as `"ko"` or `"ja"` pins it whatever you write.

```json
{"agent": {"replyLanguage": "ko"}}
```

It reaches delegations too. A subagent sees none of your conversation, so `delegate_task` appends one short English line to every brief naming the output language — the setting when it names one, otherwise the language of your own last message (Hangul → Korean, kana → Japanese, Han → Chinese, Cyrillic → Russian, anything else → English). The brief itself may stay in English, which models read most precisely; what comes back is in your language. You never read the child's report directly: the main agent relays what it found, in your language, in its own words.

The terminal UI follows the same setting for its own wording, so a Korean session reads `파일 3개 읽음` rather than `Read 3 files`. Korean, Japanese and Chinese have wording of their own; every other language keeps the English chrome.

### Deleting a profile

`settings.set` merges, and a merge cannot express a removal — so `null` deletes the key:

```json
{"models": {"profiles": {"daily": null}}}
```

Move `models.default` off a profile before deleting it; the settings validator refuses a document whose default names a profile that no longer exists.

## Vendors

Eleven vendors ship in v0.1.

| Vendor id | Label | Adapter | Auth |
|---|---|---|---|
| `anthropic` | Anthropic | native Messages API | API key |
| `openai` | OpenAI | OpenAI-compatible, or the Codex backend for a ChatGPT login | API key, browser login (ChatGPT), device code |
| `openrouter` | OpenRouter | OpenAI-compatible | API key, OAuth PKCE login |
| `gemini` | Google Gemini | native, or the Code Assist API for a Google login | API key, browser login (Google), ADC via `gcloud`, access token |
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

`provider list --json` also carries `authStatus` per vendor: `active`,
`expired` (an OAuth session past its expiry), or `unconfigured`.

### Adding a key

```bash
snowpea setup --vendor deepseek --key sk-your-key-here
snowpea setup --vendor anthropic --key sk-ant-... --model claude-sonnet-4-5
```

Environment variables are picked up too — if `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is already exported, setup offers it rather than asking you to paste.

### Browser login

Three vendors can be signed into through a browser instead of a pasted key.

```bash
snowpea provider login openai        # ChatGPT: consent page, callback on localhost:1455
snowpea provider login gemini        # Google: consent page, callback on a free localhost port
snowpea provider login openrouter    # OAuth PKCE: a local callback receives the code
```

`snowpea setup --login openai` is an alias of the same thing. Any other vendor answers with `login_unsupported` and tells you the `--vendor`/`--key` command to run instead:

```bash
snowpea provider login deepseek
```

**A ChatGPT or Google login is not an API key.** It signs in to your
*subscription*, which the vendors' API-key endpoints refuse, so snowpea sends
those turns to the backend each vendor's own CLI uses — `chatgpt.com`'s Codex
API for OpenAI, the Code Assist API for Gemini. That switch is automatic; what
you notice is the model list, which becomes the set those backends serve
(`gpt-5-codex`, `gpt-5`, … / `gemini-2.5-pro`, `gemini-2.5-flash`). Nothing
changes for API-key users.

**On a machine without a usable browser** — over SSH, or a Linux session with no
display — the browser step is skipped automatically in favour of the headless
flow: a device code for OpenAI, `gcloud auth application-default login` for
Gemini. Force it anywhere with `--device-code`, or by exporting
`SNOWPEA_HEADLESS_LOGIN=1`:

```bash
snowpea provider login openai --device-code
snowpea provider login gemini --token        # or paste an OAuth access token
```

Omit the value after `--token` so it does not appear in shell history. A pasted
token is checked with one authenticated request before it is stored; a failure
is a warning, not a refusal, since the check itself can fail offline.

**Logins expire, and snowpea renews them.** The refresh token is stored next to
the access token and used automatically — before a turn when the token is about
to expire, and once more if the backend rejects it anyway. Only when the
refresh itself fails do you have to act, and the error says so in as many
words:

```
your ChatGPT login expired and could not be renewed — run `snowpea provider login openai` to sign in again
```

Until then the vendor shows as `expired` rather than `active` in
`snowpea provider list` and on the setup screen.

**Troubleshooting.**

| What you see | What it means |
|---|---|
| `device authorization failed (HTTP 403)` | Some networks and accounts refuse the device-code request. Use the browser login instead, or an API key. |
| `port 1455 is already in use` | OpenAI accepts only `http://localhost:1455/auth/callback`, so the port cannot be moved. Close the other sign-in holding it (another Codex or snowpea login), or use `--device-code`. |
| The browser opens and nothing happens | The callback never arrived — check that the browser is on *this* machine. Over SSH use `--device-code`. |
| `the callback state did not match` | The page that answered was not the sign-in snowpea started. Run the login again. |
| `gemini OAuth login needs the Google Cloud CLI` | Only the `gcloud` ADC route needs it; the browser login does not. |

The setup wizard prints the vendor's own error text and re-asks the
authentication choice instead of exiting, so a failed login never ends the run.

### A local model

```bash
snowpea setup --vendor local --base-url http://localhost:11434/v1 --model qwen3:8b
```

Anything that speaks `/v1/chat/completions` works — vLLM, Ollama, LM Studio, llama.cpp's server. Tool calling has to be supported by the model you load, or the agent will be able to talk but not act.

### Output budget and thinking

A reasoning model — Qwen3, DeepSeek-R1, GLM's thinking variants — streams its
thinking *before* it writes anything, and that thinking is charged to the same
`max_tokens` as the answer. Ask for too little and the model spends the whole
budget on thinking and answers nothing.

```json
{
  "agent": { "max_tokens": 16384, "thinking": "auto" },
  "providers": {
    "local": { "max_tokens": 32768, "thinking": "off" }
  }
}
```

`agent.max_tokens` (16384) is what one call to the model may produce, and a
vendor block overrides it for that vendor. Either way it is clamped to what
the model actually accepts, so a ceiling like `gpt-4`'s 8192 is respected
rather than sent and rejected.

`agent.thinking` is `on`, `off` or `auto`. `auto` — the default — thinks in the
session you are watching and stays quiet in a delegated one, where the report
*is* the output and hidden reasoning only eats the budget. `off` sends
`chat_template_kwargs: {"enable_thinking": false}`, which Qwen-style servers
(vLLM, SGLang) honour and others ignore. One agent definition can overrule
both with `thinking: on` in its frontmatter.

The daemon also reacts when a turn stops at the limit: an answer that is empty
but burned reasoning tokens is asked again once with thinking off (or twice
the budget, for a vendor with no switch), and an answer cut off mid-sentence is
resumed up to twice and the pieces joined. The TUI says which happened —
`response hit the output limit; continued`, or `… and is incomplete` when even
that was not enough. A `delegate_task` summary that is still cut off ends with
`[truncated at max_tokens after 2 continuations]`.

**Empty or truncated answers**

| What you see | What it means |
|---|---|
| The model answers with nothing at all | The whole budget went on hidden reasoning. Raise `agent.max_tokens`, or set the vendor's `thinking` to `off`. |
| The answer stops mid-sentence | The budget is too small for this kind of task; the daemon resumes twice and then says so. Raise `agent.max_tokens`. |
| `HTTP 400 … max_tokens` | The model's ceiling is below the budget and is not in snowpea's table — set `providers.<vendor>.max_tokens` to the published limit. |

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

`$SNOWPEA_HOME/settings.json` holds `providers`, `search.provider`, `browser.provider`, `tools.enabled_categories`, `gateway`, `agents.max_concurrent` (3), `team.max_conflict_retries` (2), `approvals.timeoutSec` (300), `agent.max_tokens` (16384), `agent.thinking` (`auto`) and `memory.enabled` (true). Per-project overrides for mode, allowlist and backend live in `<project>/.snowpea/settings.json` and win over the global file. Secrets are never written into `settings.json`, and never logged.

## Next

[Modes](modes.md) — decide how much the agent may do without asking.
