# Setup

[English](../en/setup.md) · [한국어](../ko/setup.md) · [全部页面](../README.md)

`snowpea setup` 会写入 `$SNOWPEA_HOME/settings.json`。它有三种形态。

```bash
snowpea setup            # quick: configure LLM models; defaults for other sections
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
| Gateway | Telegram / Discord / Slack | `--gateway`, `--token`, `--user-id` |
| Done | 已写入内容的汇总 | — |

列表的排序是：免费且无需密钥的排在最前，然后是免费但需要密钥或需自建的，最后是付费的。每份列表中的默认项都用星号标出。默认配置里除了你的 LLM 供应商外，没有任何一项需要付费账户：网页搜索和浏览器都可以完全不用密钥使用。

## 多个模型与 agent 分配

运行 `snowpea setup providers` 可以注册多个模型、选一个默认项，并把已注册的模型分配给内置或自定义的 agent。支持来自同一供应商的多个模型。清除一项分配即可让该 agent 回到默认模型。

一个模型 profile 把一个供应商和一个模型 ID 配成一对。凭据和 base URL 仍然共享在 `providers.<provider>` 之下；profile 不会重复保存 API 密钥。下面这份配置用来说明结构；请把示例中的模型 ID 换成真实的。

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

agent 的路由优先级是 **agent 分配 → agent 定义中显式指定的模型 → 默认模型**。既没有分配、定义里也没有显式模型的 agent 使用 `models.default`。新的普通会话同样从默认模型开始；会话级别显式指定的供应商/模型覆盖项会被保留。已存在的会话不会被自动改变。没有配置模型 profile 的安装保持原有行为。

## 供应商

v0.1 中共内置了十一个供应商。

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

`provider list` 会显示每个供应商的认证方式、默认模型，以及是否已配置。

在桌面环境下，`snowpea provider login gemini` 会打开 Google 的 ADC 登录。在远程或无界面的机器上，运行 `snowpea provider login gemini --token`，并在隐藏的提示处粘贴 OAuth access token。OpenAI 也支持同样的 `--token` 形式。请省略值本身，这样 token 不会出现在 shell 历史里。

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

**排障：** 在某些网络或账号下，device-code 登录可能以 `device authorization failed (HTTP 403)` 失败，而同样的请求在别处却能成功——向导会打印供应商自己的错误文本并重新询问认证方式，而不是直接退出，所以选 “1=API key” 或 “3=OAuth token” 继续即可。

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
snowpea setup --search-provider exa --search-key sk-your-exa-key
snowpea setup search
```

`ddgs` 是唯一一个什么都不需要的供应商。带 `*_free` 的 id 是有密钥产品的免费*档位*，而不是不需要密钥的端点：没有密钥时 Exa 回 `402`，Parallel 和 Keenable 回 `401`，Tavily 也一样。它们被标记为 `key required`，在密钥配置好之前无法回答搜索。

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

在 `snowpea setup search` 中选一个需要密钥的供应商时，会提示输入密钥（输入内容被遮掩），并把它存进 `search.credentials.<id>.api_key`；留空则打印一条警告，因为没有密钥的供应商无法回答。

`exa_free` 以匿名方式使用 Exa 官方托管的 MCP 工具（`web_search_exa` 和 `web_fetch_exa`），不会提示输入 API 密钥。匿名的速率限制依然适用。如果你想要带 `EXA_API_KEY` 的直连 API 和付费账户的额度，请改选 `exa`。

当配置的供应商跑不起来时，`web_search` 会回退并如实说明，而不是假装无事。会话会收到一个 `error{code:"search_provider_unavailable"}` 事件，并且 assistant 会被要求把原因复述给你。

检查实际是哪个供应商在应答：

```bash
snowpea search test "snowpea agent github"
snowpea search test "snowpea agent github" --json
snowpea tools list --json
```

`snowpea search test` 会用你配置的供应商跑一次真实查询，并打印出应答的那个供应商，以及每个被跳过的供应商退出的原因。`snowpea tools list` 会显示 `web_search` 及其供应商；当配置的 id 跑不起来时，写作 `exa_free → ddgs`。

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
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token --user-id 987654
```

交互界面会问两样东西：bot token，然后是你自己在那个平台上的账号 id（给 [@userinfobot](https://t.me/userinfobot) 发 `/start`，Telegram 就会告诉你你的 id）。这个 id 很重要，因为它是唯一被允许从聊天里回答审批的账号。

token 存在 `$SNOWPEA_HOME/credentials.json`（权限 `0600`），消息平台会随守护进程一起开始监听——不需要额外的绑定步骤。把一个 bot 绑定到*某个特定的* agent、会话或聊天仍然是另外一个步骤，详见 [Gateway](gateway.md)。

## 最终会写入磁盘的内容

`$SNOWPEA_HOME/settings.json` 保存了 `providers`、`search.provider`、`browser.provider`、`tools.enabled_categories`、`gateway`、`agents.max_concurrent`（3）、`team.max_conflict_retries`（2）、`approvals.timeoutSec`（300）以及 `memory.enabled`（true）。针对单个项目的 mode、allowlist 和 backend 覆盖项保存在 `<project>/.snowpea/settings.json` 中，其优先级高于全局文件。密钥永远不会写入 `settings.json`，也永远不会被记录到日志中。

## 下一步

[Modes](modes.md) —— 决定 agent 在多大程度上可以不经询问就自行执行。
