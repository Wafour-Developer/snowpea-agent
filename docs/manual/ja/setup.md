# Setup

[English](../en/setup.md) · [한국어](../ko/setup.md) · [すべてのページ](../README.md)

`snowpea setup` は `$SNOWPEA_HOME/settings.json` を書き込みます。これには3つの形があります。

```bash
snowpea setup            # quick: asks for one LLM vendor, defaults everything else
snowpea setup --full     # every screen, in order
snowpea setup --blank    # asks nothing, writes the defaults
```

初回に使うべきなのは quick です。full は、何を変えたいかが分かってから一度通す価値があります。blank はスクリプトによるインストールや CI のために存在します。

## The screens

`--full` は5つの画面とサマリーを順に進みます。どの画面も最後は **Skip — keep defaults** で終わり、どの画面にもコマンドラインフラグが用意されているため、対話式にする必要は一切ありません。

| Screen | Choice | Flag |
|---|---|---|
| Providers | LLM vendor, key, model | `--vendor`, `--key`, `--model`, `--base-url` |
| Search | one web-search provider | `--search-provider` |
| Browser | one browser provider | `--browser-provider` |
| Tools | which tool categories are on | `--tools` |
| Gateway | Telegram / Discord / Slack | `--gateway`, `--token` |
| Done | summary of what was written | — |

一覧はまず無料でキー不要のもの、次に無料だがキーが必要またはセルフホストのもの、最後に有料のものという順に並んでいます。各リストのデフォルトには星印が付いています。デフォルト構成で有料アカウントが必要になるのは LLM vendor だけです。web search とブラウザはどちらもキーなしで動作します。

## Vendors

v0.1 では11個のベンダーが同梱されています。

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

`provider list` は各ベンダーの認証方式、デフォルトモデル、設定済みかどうかを表示します。

### Adding a key

```bash
snowpea setup --vendor deepseek --key sk-your-key-here
snowpea setup --vendor anthropic --key sk-ant-... --model claude-sonnet-4-5
```

環境変数も認識されます。`OPENAI_API_KEY` や `ANTHROPIC_API_KEY` がすでにエクスポートされていれば、setup は貼り付けを求める代わりにそれを提示します。

### Browser login

2つのベンダーは、キーを貼り付ける代わりにブラウザからログインする方式をサポートしています。

```bash
snowpea provider login openai        # device code: a code appears, you approve it in the browser
snowpea provider login openrouter    # OAuth PKCE: a local callback receives the code
```

`snowpea setup --login openai` はこれと同じことのエイリアスです。それ以外のベンダーは `login_unsupported` で応答し、代わりに実行すべき `--vendor`/`--key` コマンドを教えてくれます。

```bash
snowpea provider login deepseek
```

### A local model

```bash
snowpea setup --vendor local --base-url http://localhost:11434/v1 --model qwen3:8b
```

`/v1/chat/completions` を話すものなら何でも動作します。vLLM、Ollama、LM Studio、llama.cpp のサーバーなどです。ツール呼び出しはロードしたモデル自身がサポートしている必要があり、そうでなければエージェントは会話はできても行動できなくなります。

### Which vendor gets used

優先順位は、`SNOWPEA_PROVIDER` 環境変数、次にコマンドラインの `--provider` またはセッションの `provider` 引数、次に settings の `providers.default`、最後に設定済みの最初のベンダーの順です。

```bash
snowpea -c "READMEを要約して" --provider deepseek
```

## Search providers

`web_search` と `web_extract` はプロバイダーのレジストリの上に成り立っています。デフォルトの `ddgs` はキーもアカウントも必要ありません。

```bash
snowpea setup --search-provider ddgs
snowpea setup --search-provider tavily
```

無料でキー不要: `ddgs`（デフォルト）、`exa_free`、`keenable_free`、`parallel_free`。無料だがキーが必要、またはセルフホスト: `brave_free`、`tavily`、`searxng`（`SEARXNG_URL` を設定）、`firecrawl_selfhost`。有料: `exa`、`keenable`、`parallel`、`firecrawl`、`xai_grok`。設定したプロバイダーが失敗した場合、`web_search` は無料のチェーンを順に下ってフォールバックし、どれが応答したかをログに記録します。

`web_extract` はプライベート、ループバック、リンクローカルのアドレスを拒否し、取得したページを `tools.max_output_chars`（デフォルト20000）まで切り詰めます。

## Browser providers

`local_chromium` がデフォルトで、Playwright 経由であなた自身のマシン上でヘッドレス Chromium を実行します。初回実行時にはブラウザバイナリのダウンロードを求められることがあります。それ以外の ID — `camoufox`、`browser_use_local`、`browserbase`、`firecrawl_cloud` — は確認・選択できるよう登録されていますが、設定が完了するまでは `browser_provider_unavailable` で応答します。

```bash
snowpea setup --browser-provider local_chromium
```

## Tool categories

カテゴリはツールのグループ全体をまとめてオン・オフします。カンマ区切りのリストを渡してください。先頭に `-` を付けるとそのカテゴリをオフにします。

```bash
snowpea setup --tools media,-browser
snowpea tools list
```

カテゴリは `file`、`terminal`、`git`、`web`、`browser`、`delegate`、`schedule`、`memory`、`media`、そして `.mcp.json` サーバーが提供するもの全般のための `mcp` です。メディアツール（`image_generate`、`video_generate`、`music_generate`、`text_to_speech`）は常に登録されていますが、認証情報が存在するまでは `inactive` のままです。設定が完了すると再起動なしで `active` に切り替わり、それより前に呼び出すとヒント付きで `tool_inactive` が返ります。

## Gateway

```bash
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token
```

これはトークンを `$SNOWPEA_HOME/credentials.json`（モード `0600`）に保存するだけで、それ以上のことはしません。ボットをエージェントやセッションに紐づけるのは別の手順で、[Gateway](../en/gateway.md) で扱います。

## What ends up on disk

`$SNOWPEA_HOME/settings.json` には `providers`、`search.provider`、`browser.provider`、`tools.enabled_categories`、`gateway`、`agents.max_concurrent`（3）、`team.max_conflict_retries`（2）、`approvals.timeoutSec`（300）、`memory.enabled`（true）が保持されます。mode、allowlist、backend のプロジェクトごとの上書きは `<project>/.snowpea/settings.json` にあり、グローバルファイルより優先されます。シークレットが `settings.json` に書き込まれることはなく、ログに残ることもありません。

## Next

[Modes](../en/modes.md) — decide how much the agent may do without asking.
