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

## Where a vendor's default model comes from

Each vendor row in `snowpea setup` shows the model that vendor would start a session with, as `default: glm-5.3 (from models.dev)`. That id is resolved when the screen is drawn, not frozen into the build, so a release you installed months ago still offers what the vendor ships today. Three rungs, highest first: the **account's own listing**, when this machine holds a credential for that vendor; the newest chat-capable model the public [models.dev](https://models.dev) catalog lists for it, preferring a family's flagship over a cheaper sibling of the same generation and skipping preview and deprecated ids; and last the string built into this release. The row always says which rung answered, so a fallback never passes for your account's own answer. Live discovery is capped at two seconds and runs for every vendor at once, so a vendor that hangs is skipped rather than waited on, and `SNOWPEA_MODELS_DEV=0` keeps the middle rung off the network as it does everywhere else. The same three fields reach the IDE as `defaultModel` and `defaultModelSource` on `setup.catalog`.

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
| `local` | Local / OpenAI-compatible servers (vLLM, Ollama, LM Studio) | OpenAI-compatible | base URL, key optional |
| *your own name* | any number of extra OpenAI-compatible servers, declared with `"preset": "local"` | OpenAI-compatible | base URL, key optional |

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

### Several local servers

One `local` entry is one server. To run more than one — a vLLM box and an
Ollama laptop, say — give each its own name under `providers`. A block that
carries `"preset": "local"` is a local OpenAI-compatible server whose key is
its name:

```json
{
  "providers": {
    "local": {"base_url": "http://localhost:11434/v1", "model": "qwen3:8b"},
    "hon2": {
      "preset": "local",
      "label": "hon2 vLLM",
      "variant": "vllm",
      "base_url": "http://hon2.example.com:8000/v1",
      "model": "flash-next-mtp"
    }
  },
  "models": {
    "profiles": {"hon2:flash-next-mtp": {"provider": "hon2", "model": "flash-next-mtp"}},
    "default": "hon2:flash-next-mtp"
  }
}
```

The name is the vendor id, so `hon2:flash-next-mtp` works anywhere a model
reference does: `models.default`, a project's models, `agents.models`, and
`/model` in a session. Names are lower-case, start with a letter, may contain
digits, `-` and `_`, and must not be one of the built-in vendor ids.

`variant` is `vllm`, `ollama`, `lmstudio` or `generic`; it picks the URL the
wizard suggests and enables Ollama's `/api/tags` listing for a server with no
`/v1/models`. `api_key` is optional, `label` is what pickers show, and
`context_window` pins a window the server does not report.

From the command line:

```bash
snowpea provider add-local hon2 --url http://hon2.example.com:8000/v1 --type vllm
snowpea provider add-local hon2 --url http://hon2.example.com:8000/v1 --key sk-local --model flash-next-mtp
snowpea provider models hon2
snowpea provider remove hon2
```

`remove` forgets the block, the model profiles that named it, and the agent
assignments that used those profiles.

In `snowpea setup`, the **Local / OpenAI-compatible servers** row lists the
servers already configured, with "Add another server…" and "Remove a server…"
below them. Adding one asks for a name, the server type, the URL and an
optional key, then lists `/v1/models` so you can pick its default model.

An existing configuration with only `providers.local` keeps working exactly as
it did: that entry is a local server implicitly and needs no `preset` marker.

### Vision on local servers

Whether a model can be sent an image is decided in four steps, strongest first:

1. `providers.<vendor>.vision`, or `providers.<vendor>.models.<model>.vision`
   for one model of that server;
2. the public models.dev card, when one is already in the cache;
3. the model name, against a built-in list (`gpt-4o`, `claude`, `qwen2.5-vl`,
   `llava`, anything ending `-vl`, and so on);
4. for a local or named OpenAI-compatible server only: **try once**. The images
   go out, and if the server refuses the request, snowpea remembers that, says
   so once in the log, and immediately retries the same turn with the text
   description so the answer still arrives.

Step 4 is what makes an unknown local model usable. A vision model loaded under
a name no list recognises — `flash-next-mtp`, say — used to have every image
replaced by "(this model cannot see images)". Now it is simply sent the image.
The cost of a server that really is text-only is one refused request per model,
remembered for a week in `<SNOWPEA_HOME>/cache/vision.json`, so it is not paid
again on the next prompt or the next restart.

A hosted vendor is never probed this way: its catalog is knowable, and a
refused request there is a charge for nothing.

To settle it yourself rather than let the probe find out:

```json
{
  "providers": {
    "hon2": {
      "preset": "local",
      "base_url": "http://hon2:8000/v1",
      "vision": true,
      "models": {"flash-next-mtp": {"vision": true}, "qwen3-8b": {"vision": false}}
    }
  }
}
```

The per-model rule wins over the per-server one. `models` may still be a plain
list of ids when you are only pinning a catalog; the object form is for when
you want to say something about each model.

From the command line:

```bash
snowpea provider add-local hon2 --url http://hon2:8000/v1 --vision
snowpea provider add-local hon2 --url http://hon2:8000/v1 --no-vision
snowpea provider models hon2
```

`provider models` marks each model it knows about: 👁 for one that takes
images, `(text only)` for one that does not, and nothing at all for one nobody
has established yet. The setup wizard's model list shows the same eye.

An override also works on a hosted vendor, which is the way to describe a proxy
in front of it that strips images.

### Reasoning effort

`thinking` is a switch; effort is a dial. One scale — `low`, `medium`, `high`,
`max` — reaches every vendor, and each adapter maps it to whatever that vendor
actually accepts:

| Vendor | Wire field | low | medium | high | max |
|---|---|---|---|---|---|
| `openai` (API key) | `reasoning_effort` | `low` | `medium` | `high` | `high` |
| `openai` (ChatGPT login, Codex backend) | `reasoning.effort` | `low` | `medium` | `high` | `xhigh` |
| `anthropic` | `thinking.budget_tokens` | 2 048 | 8 192 | 32 768 | 65 536 |
| `gemini` | `thinkingConfig.thinkingBudget` | 2 048 | 8 192 | 32 768 | 65 536 |
| `openrouter`, `xai` | `reasoning_effort` | `low` | `medium` | `high` | `high` |
| `glm`, `minimax`, `kimi`, `deepseek`, `qwen` | — | nothing is sent | | | |
| `local` and named servers | `reasoning_effort`, opt-in | `low` | `medium` | `high` | `high` |

A token budget is capped at three quarters of the call's `max_tokens`, so a
"think hard" turn always keeps room to answer. `thinking: "off"` still wins
over every tier: a user who turned thinking off gets no hidden reasoning, and
an effort setting does not put it back.

The OpenAI field only goes to models that accept it — the `o`-series, `gpt-5*`
and `codex*`. If a model refuses it anyway (`HTTP 400: Unsupported parameter`),
the call is retried once without the field and that model is never sent it
again for the life of the daemon.

Self-hosted servers are opt-in, because vLLM either ignores `reasoning_effort`
or rejects it depending on the loaded model's chat template:

```json
{
  "providers": {
    "hon2": {"preset": "local", "base_url": "http://hon2:8000/v1", "effort_param": true}
  }
}
```

Settings, weakest rule first:

```json
{
  "agent": {
    "effort": "medium",
    "effortBy": {"openai": "high", "anthropic:claude-opus-4-1": "max"}
  }
}
```

`agent.effort` is the default for everything. `agent.effortBy` overrides it per
vendor (`"openai"`) or per model (`"openai:o3"`), and the model rule wins over
the vendor rule. Above both sits the session pin:

```text
/effort            show what is in force and which rule decided it
/effort high       pin this session
/effort auto       clear the pin
```

The pin is persisted with the session, so resuming a thread keeps the tier you
chose. `snowpea model profiles` shows the effort each profile resolves to, and the
TUI draws it next to the model (`⚙ high`); the `/model` picker has a row that
cycles through the tiers.

`providers.openai.reasoning_effort` from an older configuration is still read
for the Codex backend, so upgrading does not change how hard your sessions
think. Nothing writes it any more — `/effort` writes the unified keys.

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

`agent.max_tool_rounds` (200) is how many tool calls one turn may make before
the agent checks in with a picker — continue for another budget, or stop here.
It is a checkpoint for a long implementing turn, not a limit on the work.

Whatever happens next, **the turn writes a report first**: one more call to the
model with the tools switched off, asking what it did, what it found, what
remains and which files it changed. You always get that account, and a turn
that ends at the budget ends with `turn.done{reason:"budget"}` rather than an
error. A headless (`-c`) turn and a delegated child have nobody to ask, so they
report and stop; a session you are watching is asked after the report.

`agents.toolRounds` sets the budget for delegated children — either a number
for all of them or, like `agents.models`, a mapping keyed by agent name with
`default` as the catch-all:

```json
{
  "agents": { "toolRounds": { "default": 80, "explorer": 150 } }
}
```

An agent definition's own `tool_rounds:` frontmatter outranks both. With
nothing configured, a child gets `agent.max_tool_rounds` but never fewer than
80: a worker reads far more than the session that delegated to it, and all you
see of it is its final report.

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

Two more `tools` settings shape how the agent works with files and long output:

| setting | default | what it does |
|---|---|---|
| `tools.readBeforeWrite` | `true` | `edit_file` and `write_file` refuse a file this session has not read in full, or that a sibling subagent wrote after the read. The refusal is a `stale_file` error naming the reason. Creating a new file is always allowed. Set it to `false` to drop the guard. |
| `tools.maxResultLines` | `400` | Past this many lines, a `shell`, `grep`, `glob` or `list_dir` result keeps a head and a tail; the middle goes to `$SNOWPEA_HOME/cache/tool-output/` and the result carries a `read_file` pointer with the offset and limit that would read it back. |

`read_file` takes an optional `offset` (first line, 1-based) and `limit` (line count). A windowed read does not satisfy `readBeforeWrite`: the whole file has to be read before it is written.

Three `skills` settings shape the skills index the agent reads on every turn (see [Plugins and skills](plugins.md)):

| setting | default | what it does |
|---|---|---|
| `skills.indexInPrompt` | `true` | List every installed skill in the system prompt, grouped by origin, so the agent can load one with `skill_view` without calling a tool to discover it first. Set it to `false` to spend no context on the list. |
| `skills.indexMaxEntries` | `60` | How many skills the index names before it stops and says "… and K more — skill_list shows all". |
| `skills.protectRecentViews` | `2` | Turns of `skill_view` results a compaction leaves whole. Older bodies over 5000 characters become a `[SKILL_PRUNED: …]` pointer the agent is told to reload. |

## Browser providers

`local_chromium` is the default and runs a headless Chromium through Playwright on your own machine. The first run may ask you to download the browser binary. The other ids — `camoufox`, `browser_use_local`, `browserbase`, `firecrawl_cloud` — are registered so you can see and select them, and answer with `browser_provider_unavailable` until they are configured.

```bash
snowpea setup --browser-provider local_chromium
snowpea setup --browser-provider firecrawl_cloud --browser-key fc-your-key
snowpea setup browser
```

**A provider tagged `key required` asks for its credentials.** Picking `browserbase` or `firecrawl_cloud` prompts for the key straight after the list, masked, with Enter keeping whatever is already saved. Browserbase needs two values and is asked for both: the API key, then the project id, which is an identifier rather than a secret and so is not masked.

| id | tag | needs |
| --- | --- | --- |
| `local_chromium` ★ | free · no key | Playwright's Chromium, downloaded on first use |
| `camoufox` | free · no key | nothing yet |
| `browser_use_local` | free · no key | nothing yet |
| `browserbase` | paid · key required | `BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID` |
| `firecrawl_cloud` | paid · key required | `FIRECRAWL_API_KEY` |

Credentials land in `browser.credentials.<id>`, the same shape search uses:

```json
{ "browser": { "provider": "browserbase",
               "credentials": { "browserbase": { "api_key": "...", "browserbase_project_id": "..." } } } }
```

Settings win over the environment, so a key you type into the wizard beats an exported variable. An empty answer with nothing saved is refused in words rather than silently: the provider is written, the summary says it has no key, and the browser tools keep refusing until one exists.

When a key is entered, setup makes one cheap call to check it — Browserbase's session list, a `HEAD` against Firecrawl — with a three-second budget. A failure is a **warning, not a block**: an offline machine, a proxy or a provider having a bad afternoon must not cost you the key you just pasted, so it is saved either way.

## Tool categories

Categories switch whole groups of tools on and off. Pass a comma-separated list; a leading `-` turns one off.

```bash
snowpea setup --tools media,-browser
snowpea tools list
```

Categories are `file`, `terminal`, `git`, `web`, `browser`, `delegate`, `schedule`, `memory`, `media`, and `mcp` for anything a `.mcp.json` server contributed. Adding, testing and removing those `.mcp.json` servers is `/mcp` and `snowpea mcp`, covered in [MCP servers](plugins.md#mcp-servers). Media tools (`image_generate`, `video_generate`, `music_generate`, `text_to_speech`) are always registered but stay `inactive` until credentials exist; they flip to `active` without a restart once configured, and calling one before that returns `tool_inactive` with a hint.

## Voice in and out

Two lists, one decision: can you talk to it, and does it talk back. Both are shown whether or not the engine is installed here, because "why can't I use piper" should be answered on screen rather than by its absence.

Voice has **two states, per direction**: nothing pinned, which means off, or one engine pinned, which is the one used. There is no "automatic" that tries several — a chain that silently tried five backends could only ever report silence, and never which of them it had tried.

- **Not set** — `audio.stt.provider` / `audio.tts.provider` is absent. `audio.capabilities` reports that direction false with `no engine set — install or pick one in setup`.
- **Pinned** — one engine id. If it is not installed, the capability is false with `engine <id> is not installed`, not a quiet switch to a different one.

An older `settings.json` carrying `"auto"` reads as **unset**, so voice is off until you pick something. That is deliberate: it re-enables on purpose rather than by accident.

**Installing and choosing are separate steps.** `snowpea audio install <engine>` puts an engine on the machine and changes no setting. Picking it is what pins it. The wizard says which of the two is outstanding: *Not set*, *X is installed, not selected — pick it to use it*, or *Pinned: X*.

The two recommended engines are local and CPU-only, so voice works without an account and without a GPU.

**Voice in** — the recommended default is **SenseVoiceSmall**.

| Row | What it is | What picking it does |
|---|---|---|
| `sherpa-onnx-sensevoice` ★ | SenseVoiceSmall, zh/en/ja/ko/yue, ~17-20x real time on CPU, comes with its own VAD so it splits long recordings itself | installs it (package + ~230MB model), then pin it |
| `sherpa-onnx-zipformer-ko` | Korean streaming Zipformer INT8, ~10-38x real time on CPU | installs it, then pin it |
| `sherpa-onnx-zipformer-en` | English streaming Zipformer INT8 | installs it, then pin it |
| `local-whisper` | the whisper CLI you may already have | installs `faster-whisper`, then pin it |
| `openai` | hosted transcription | asks for the key (masked), then pins it |
| `command` | your own template | asks for the template, validates it, self-tests, pins |

**Voice out** — the recommended default is **Supertonic**.

| Row | What it is | What picking it does |
|---|---|---|
| `supertonic` ★ | Supertone's on-device neural TTS, 31 languages including Korean and English, runs on CPU | installs it (it fetches its own ONNX voices), then pin it |
| `piper` | local neural voices | installs it with a default voice, then pin it |
| `edge-tts` | Microsoft neural voices | installs it, then pin it |
| `espeak-ng` | small and robotic, everywhere | shows the platform command and offers to run it |
| `say` / `powershell` | built into macOS / Windows | pins it; **not listed** on other platforms |
| `openai` | hosted speech | asks for the key (masked), then pins it |
| `command` | your own template | asks for the template, validates it, self-tests, pins |

**Every row leads somewhere.** Picking an engine that is not installed installs it rather than setting a value that produces silence. A system package shows its own command and offers to run it. A custom command is asked for, checked for its placeholders, and self-tested for three seconds before it is pinned. An engine that could never work here — macOS `say` on Linux — is not listed at all.

An install reports **stages**, not just a log: `[download 3/6] 63% ▇▇▇▇▇▁▁▁ sherpa-onnx-sensevoice.tar.bz2`, with the log tail below it.

With speech on, the agent also says its **opening acknowledgement** — the line it writes before its first tool call — so a spoken request is not answered by a silent minute. Turn it off with `audio.tts.speakAck: false`.


Speech models come from the official sherpa-onnx release assets and land under `$SNOWPEA_HOME/models/sherpa-onnx/`. Downloads resume if they are interrupted, and a model only counts as installed once it has unpacked completely, so a cancelled download never leaves an engine looking ready.

Set `audio.stt.language` to a tag like `ko` to tell SenseVoice what to expect and to pick the matching Zipformer; leave it empty and SenseVoice detects the language itself.

**Installing an engine.** For the three that are ordinary user-space packages the daemon installs them for you:

```bash
snowpea audio install sherpa-onnx-sensevoice
snowpea audio install supertonic
snowpea audio install faster-whisper
snowpea audio install piper
snowpea audio install edge-tts
```

Engines that are command line tools — `piper`, `edge-tts`, `faster-whisper` — go in with `uv tool install` if `uv` is on PATH, then `pipx`, then `pip install --user`, printing the log as it goes.

`sherpa-onnx` and `supertonic` are Python libraries with no usable command line, so they go into an interpreter snowpea owns: **`$SNOWPEA_HOME/audio-runtime`**, created on first use with `uv venv` or `python -m venv`. The engines run their documented Python API in a child of that interpreter, nothing is written to your system or user site-packages, and "is it installed?" is a question about that directory rather than about PATH. Delete the directory to start over; the next install recreates it. A sherpa-onnx row installs the package and then downloads its model as well.

Installing `piper` also downloads one default voice into `$SNOWPEA_HOME/voices/` and records it in `audio.tts.voice`, because a piper binary with no voice cannot say anything. Detection re-runs at the end, so the engine is usable immediately and nothing needs restarting.

The setup wizard's two voice screens are **action-first**: the rows are things to do, not settings to believe in.

```text
Recommended: SenseVoiceSmall (CPU) — Install     ← only while nothing is installed
Install Piper…
Choose a specific engine…
Skip — keep defaults
```

With nothing installed, the recommended engine leads and is pre-selected, because installing it is the one move that helps. Once you have an engine the recommendation gives way to a status line that says something truer: **Automatic will use espeak-ng.**

Automatic is still the setting and still the default. It is no longer a *row*, because it is not an action: with nothing installed it is a promise the machine cannot keep, and with something installed the screen can simply tell you what it will use.

`Choose a specific engine…` opens a submenu that pins one on purpose — every engine, the installed ones first and the rest listed and marked, then Off, a custom command, and the one that needs an account. Esc goes back rather than abandoning the question, and the row then says what is pinned. Install rows run the install and show the screen again, so you land where you were with the engine now working.

`espeak-ng`, `say` and `powershell` are system packages, and the daemon will not run a package manager as root for you. Asking for one prints the command for your platform instead:

```
$ snowpea audio install espeak-ng
could not install espeak-ng: sudo apt install espeak-ng
```

**snowpea-studio is no longer a voice choice.** The `text_to_speech` media tool still forwards to a configured studio MCP server, and Automatic still falls back to it when nothing else is available. It is no longer offered in the voice list and no longer leads the chain: it needs an MCP server configured before it can say a word, so leading with it made "Automatic" resolve to a backend most machines do not have.

### Voices

Picking an engine is half a decision. The wizard follows it with a **voice step**: one tab per language, because one engine can and should sound like a different person in Korean than in English. A row that is not on disk downloads first; Preview synthesises a sentence in that language and plays it; Skip keeps the engine's own default, which is a perfectly good answer.

```json
{ "audio": { "tts": { "voices": { "ko": "F2", "en": "M1", "*": "M1" } } } }
```

A reply is spoken with its own language's entry, then `*`, then the engine's default. A settings file written before this was a mapping carries `voice: "M1"`, which reads as `"*"` and still works.

**Supertonic is one multilingual model.** Its ten preset styles (M1-M5, F1-F5) work in all 31 languages, so choosing a different one per language costs no extra download. **Piper is the opposite**: a voice *is* a download, and a language with no voice file is a language it cannot speak, so those rows say so and install on selection. `edge-tts`, `espeak-ng`, macOS `say` and Windows SAPI are asked at run time what they have, filtered to Korean, English and your reply language.

`audio.voices {engine}` returns the same list over RPC, and `audio.install {engine, voice}` fetches one with the same staged progress. Installing a voice does not select it, for the same reason installing an engine does not pin it.

### The transcription language

```json
{ "audio": { "stt": { "language": "auto" } } }
```

`auto` is the default and does not mean "no language". It means nobody forced one, so an engine that detects for itself does that, and one that cannot takes the language you are replying in. A BCP-47 tag forces it.

For **SenseVoice**, whisper and OpenAI this is a hint. For a **sherpa Zipformer it picks the model**: those are single-language, so asking a Korean model for English is asking for a different model. When the one you need is missing the reason says which: `sherpa-onnx-zipformer-en does not speak ko; install sherpa-onnx-zipformer-ko`. `audio.capabilities` reports `sttLanguage` and `sttLanguageSource` (`setting`, `reply` or `detect`) so a surface can say who decided.

## Gateway

```bash
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token --user-id 987654
```

The interactive screen asks for both: the bot token, then your own account id on that platform (Telegram tells you yours if you send `/start` to [@userinfobot](https://t.me/userinfobot)). The id matters because it is the only account allowed to answer an approval from chat.

The token is stored in `$SNOWPEA_HOME/credentials.json` (mode `0600`), and the messenger starts listening with the daemon — no binding step needed. Binding a bot to a *particular* agent, session or chat is still a separate step, covered in [Gateway](gateway.md).

## Project instruction files

A project tells the agent its own rules in a file the agent reads before every turn. Discovery follows the same order Hermes uses, and **the first type that matches wins** — a repository that carries two conventions does not pay for both:

1. `.snowpea/instructions.md` or `SNOWPEA.md`, nearest first, walking up to the git root.
2. The `AGENTS.md` chain, from the git root down to the session's directory. Each directory contributes the first of `AGENTS.override.md`, `AGENTS.md`, `agents.md` — the `.override.` name is meant to be gitignored, so you can keep personal instructions beside the committed ones. Identical content further down the chain is loaded once.
3. `CLAUDE.md` or `claude.md`, in the session's directory.
4. `.cursorrules` plus `.cursor/rules/*.mdc`, in the session's directory.

Without a `.git` ancestor the chain is the session's own directory alone. A file left in `/tmp` or in your home directory never gains prompt authority.

Sizes. One file reaches the prompt up to `clamp(context window × 4 × 0.06, 20 000, 500 000)` characters, which is 20 000 on a small local model and far more on a large one, and the merged block is capped by the same number. A file that is cut keeps its head and its tail with a marker between them naming the file to `read_file`, and the block says in words that it was cut. Pin the cap with `agent.contextFileMaxChars`, or skip project files entirely with `agent.ignoreContextFiles`.

Nested files. `/deepinit` writes an `AGENTS.md` per package directory, and a session sits at the repository root for its whole life, so the chain alone would never reach them. They are loaded up front as their own sections while the budget allows — a brand-new thread in the project already carries the whole hierarchy, as do a resumed session and a subagent in the same directory. The search goes four levels deep, takes at most 40 files, and never walks `.git`, `node_modules`, `.venv`, `dist`, `build`, `__pycache__` or any hidden directory.

Whatever does not fit is named instead:

```text
Nested instructions not loaded (read_file when you work there): src/AGENTS.md, test/AGENTS.md
```

and the nearest one is attached to the first tool result that touches its directory — reading, writing or editing a file there, listing it, globbing or grepping it, or a shell command that starts by changing into it. Once per session, and only for a file the prompt does not already quote.

Changes are picked up immediately. Writing or editing any of these files, at any depth, drops the cached prompt, as does finishing `/init`, `/deepinit` or `/skill create` — so the turn right after one of them already sees what it wrote.

## What ends up on disk

`$SNOWPEA_HOME/settings.json` holds `providers`, `search.provider`, `browser.provider`, `tools.enabled_categories`, `tools.readBeforeWrite` (true), `tools.maxResultLines` (400), `gateway`, `agents.max_concurrent` (3), `team.max_conflict_retries` (2), `approvals.timeoutSec` (300), `agent.max_tokens` (16384), `agent.thinking` (`auto`), `memory.enabled` (true), `memory.askScope` (true), `memory.digestEntries` (30), `memory.digestChars` (6000), `skills.indexInPrompt` (true), `skills.indexMaxEntries` (60) and `skills.protectRecentViews` (2). Per-project overrides for mode, allowlist and backend live in `<project>/.snowpea/settings.json` and win over the global file. Secrets are never written into `settings.json`, and never logged.

## Next

[Modes](modes.md) — decide how much the agent may do without asking.
