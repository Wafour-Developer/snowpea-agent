# Setup

[English](../en/setup.md) · [한국어](../ko/setup.md) · [全部页面](../README.md)

`snowpea setup` 会写入 `$SNOWPEA_HOME/settings.json`。它有三种形态。

```bash
snowpea setup            # quick: asks for one LLM vendor, defaults everything else
snowpea setup --full     # every screen, in order
snowpea setup --blank    # asks nothing, writes the defaults
```

第一次使用时，Quick 是正确的选择。一旦你清楚自己想改什么，就值得跑一遍 Full。Blank 是为脚本化安装和 CI 准备的。

## 各个界面

`--full` 会依次走完五个界面和一个汇总页。每个界面都以 **Skip —— keep defaults** 结束，并且每个界面都有对应的命令行参数，因此你完全不需要以交互方式操作。

| Screen | Choice | Flag |
|---|---|---|
| Providers | LLM 供应商、密钥、模型 | `--vendor`, `--key`, `--model`, `--base-url` |
| Search | 一个网页搜索供应商 | `--search-provider` |
| Browser | 一个浏览器供应商 | `--browser-provider` |
| Tools | 启用哪些工具类别 | `--tools` |
| Gateway | Telegram / Discord / Slack | `--gateway`, `--token` |
| Done | 已写入内容的汇总 | — |

列表的排序是：免费且无需密钥的排在最前，然后是免费但需要密钥或需自建的，最后是付费的。每份列表中的默认项都用星号标出。默认配置里除了你的 LLM 供应商外，没有任何一项需要付费账户：网页搜索和浏览器都可以完全不用密钥使用。

## 供应商

v0.1 中共内置了十一个供应商。

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

`provider list` 会显示每个供应商的认证方式、默认模型，以及是否已配置。

### 添加密钥

```bash
snowpea setup --vendor deepseek --key sk-your-key-here
snowpea setup --vendor anthropic --key sk-ant-... --model claude-sonnet-4-5
```

环境变量也会被识别——如果 `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY` 已经导出，setup 会直接提供它，而不是要求你粘贴。

### 浏览器登录

有两个供应商支持通过浏览器登录，而不必粘贴密钥。

```bash
snowpea provider login openai        # device code: a code appears, you approve it in the browser
snowpea provider login openrouter    # OAuth PKCE: a local callback receives the code
```

`snowpea setup --login openai` 是同一操作的别名。其他任何供应商都会返回 `login_unsupported`，并告诉你应该运行哪个 `--vendor`/`--key` 命令：

```bash
snowpea provider login deepseek
```

### 使用本地模型

```bash
snowpea setup --vendor local --base-url http://localhost:11434/v1 --model qwen3:8b
```

任何支持 `/v1/chat/completions` 的服务都可以用——vLLM、Ollama、LM Studio、llama.cpp 的 server。工具调用（tool calling）必须由你加载的模型本身支持，否则 agent 只能对话而无法执行操作。

### 使用哪个供应商

优先级顺序为：`SNOWPEA_PROVIDER` 环境变量，其次是命令行上的 `--provider` 或会话的 `provider` 参数，再次是配置文件中的 `providers.default`，最后是第一个已配置的供应商。

```bash
snowpea -c "summarize README.md" --provider deepseek
```

## 搜索供应商

`web_search` 和 `web_extract` 建立在一个供应商注册表之上。默认的 `ddgs` 不需要密钥，也不需要账户。

```bash
snowpea setup --search-provider ddgs
snowpea setup --search-provider tavily
```

免费且无需密钥：`ddgs`（默认）、`exa_free`、`keenable_free`、`parallel_free`。免费但需要密钥或需自建：`brave_free`、`tavily`、`searxng`（设置 `SEARXNG_URL`）、`firecrawl_selfhost`。付费：`exa`、`keenable`、`parallel`、`firecrawl`、`xai_grok`。如果配置的供应商失败，`web_search` 会沿着免费链路依次回退，并记录是哪一个最终应答的。

`web_extract` 会拒绝私有地址、回环地址和链路本地地址，并把抓取到的页面截断至 `tools.max_output_chars`（默认 20000）。

## 浏览器供应商

`local_chromium` 是默认项，会在你自己的机器上通过 Playwright 运行一个无头 Chromium。首次运行时可能会要求你下载浏览器二进制文件。其他几个 id——`camoufox`、`browser_use_local`、`browserbase`、`firecrawl_cloud`——已注册，因此你可以看到并选择它们，但在配置完成之前都会返回 `browser_provider_unavailable`。

```bash
snowpea setup --browser-provider local_chromium
```

## 工具类别

类别用于整组地开关工具。传入一个逗号分隔的列表；前面加 `-` 表示关闭该类别。

```bash
snowpea setup --tools media,-browser
snowpea tools list
```

类别包括 `file`、`terminal`、`git`、`web`、`browser`、`delegate`、`schedule`、`memory`、`media`，以及代表 `.mcp.json` server 贡献内容的 `mcp`。媒体类工具（`image_generate`、`video_generate`、`music_generate`、`text_to_speech`）始终会被注册，但在凭据配置好之前处于 `inactive` 状态；一旦配置完成，无需重启即可切换为 `active`，而在此之前调用其中任何一个都会返回带提示的 `tool_inactive`。

## Gateway

```bash
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token
```

这只会把 token 存入 `$SNOWPEA_HOME/credentials.json`（权限 `0600`），仅此而已——把一个 bot 绑定到某个 agent 或会话是另外一个步骤，详见 [Gateway](../en/gateway.md)。

## 最终会写入磁盘的内容

`$SNOWPEA_HOME/settings.json` 保存了 `providers`、`search.provider`、`browser.provider`、`tools.enabled_categories`、`gateway`、`agents.max_concurrent`（3）、`team.max_conflict_retries`（2）、`approvals.timeoutSec`（300）以及 `memory.enabled`（true）。针对单个项目的 mode、allowlist 和 backend 覆盖项保存在 `<project>/.snowpea/settings.json` 中，其优先级高于全局文件。密钥永远不会写入 `settings.json`，也永远不会被记录到日志中。

## 下一步

[Modes](../en/modes.md) —— 决定 agent 在多大程度上可以不经询问就自行执行。
