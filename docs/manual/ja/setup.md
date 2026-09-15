# Setup

[English](../en/setup.md) · [한국어](../ko/setup.md) · [すべてのページ](../README.md)

`snowpea setup` は `$SNOWPEA_HOME/settings.json` を書き込みます。これには3つの形があります。

```bash
snowpea setup            # quick: configure LLM models; defaults for other sections
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
| Gateway | Telegram / Discord / Slack | `--gateway`, `--token`, `--user-id` |
| Done | summary of what was written | — |

一覧はまず無料でキー不要のもの、次に無料だがキーが必要またはセルフホストのもの、最後に有料のものという順に並んでいます。各リストのデフォルトには星印が付いています。デフォルト構成で有料アカウントが必要になるのは LLM vendor だけです。web search とブラウザはどちらもキーなしで動作します。

## Multiple models and agent assignments

`snowpea setup providers` を実行すると、複数のモデルを登録し、デフォルトを選び、登録したモデルを標準またはカスタムのエージェントに割り当てられます。同じプロバイダーの複数モデルもサポートされます。割り当てを解除すると、そのエージェントはデフォルトに戻ります。

モデルプロファイルは、プロバイダーとモデル ID の組です。認証情報とベース URL は `providers.<provider>` で共有されたままで、プロファイルが API キーを重複して持つことはありません。次の設定は構造を示す例です。例のモデル ID は実在のものに置き換えてください。

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

エージェントの振り分けの優先順位は **エージェントへの割り当て → エージェント定義に書かれた明示的なモデル → デフォルトのモデル** です。割り当ても定義上の明示的なモデルも持たないエージェントは `models.default` を使います。新しい通常のセッションもデフォルトで始まります。セッションに明示的に指定されたプロバイダー/モデルの上書きはそのまま保たれます。既存のセッションが自動的に変更されることはありません。モデルプロファイルのないインストールは、従来どおりの挙動を保ちます。

## Vendors

v0.1 では11個のベンダーが同梱されています。

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
| `local` | Local / OpenAI-compatible servers (vLLM, Ollama, LM Studio) | OpenAI-compatible | base URL, key optional |

```bash
snowpea provider list
snowpea provider list --json
```

`provider list` は各ベンダーの認証方式、デフォルトモデル、設定済みかどうかを表示します。

デスクトップでは、`snowpea provider login gemini` が Google の ADC ログインを開きます。リモートやヘッドレスのマシンでは `snowpea provider login gemini --token` を実行し、伏せ字のプロンプトに OAuth のアクセストークンを貼り付けてください。OpenAI も同じ `--token` の形をサポートします。値を書かずに省略すれば、トークンがシェルの履歴に残りません。

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

**トラブルシューティング:** デバイスコードによるログインは、同じリクエストが他所では通るのに、ネットワークやアカウントによっては `device authorization failed (HTTP 403)` で失敗することがあります。ウィザードはベンダー自身のエラーテキストを表示し、終了せずに認証方式の選択をもう一度尋ねるので、「1=API key」か「3=OAuth token」を選んで続けてください。

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
snowpea -c "summarize README.md" --provider deepseek
```

## Search providers

`web_search` と `web_extract` はプロバイダーのレジストリの上に成り立っています。デフォルトの `ddgs` はキーもアカウントも必要ありません。

```bash
snowpea setup --search-provider ddgs
snowpea setup --search-provider exa --search-key sk-your-exa-key
snowpea setup search
```

まったく何も必要としないプロバイダーは `ddgs` だけです。`*_free` の ID は、キーの要らないエンドポイントではなく、キーが必要な製品の無料 *ティア* です。Exa はキーなしでは `402` を返し、Parallel と Keenable は `401`、Tavily も同様です。これらには `key required` のタグが付いており、キーが設定されるまで検索に答えられません。

| id | tag | needs |
| --- | --- | --- |
| `ddgs` | free, no key | 何も要りません |
| `firecrawl` | paid, key optional | 何も要りません。クラウドの検索エンドポイントはキーなしでも応答しますが、レート制限があります |
| `brave_free` | free, key required | `BRAVE_API_KEY` |
| `exa_free` | no key | `https://mcp.exa.ai/mcp` の、匿名でレート制限つきのホスト型 MCP |
| `exa` | key required | `EXA_API_KEY`（直接の REST API） |
| `keenable_free`, `keenable` | key required | `KEENABLE_API_KEY` |
| `parallel_free`, `parallel` | key required | `PARALLEL_API_KEY` |
| `tavily` | free, key required | `TAVILY_API_KEY` |
| `xai_grok` | paid, key required | `XAI_API_KEY` |
| `searxng` | free, self-hosted | `SEARXNG_URL` |
| `firecrawl_selfhost` | free, self-hosted | `FIRECRAWL_URL` |

`snowpea setup search` でキーが必要なプロバイダーを選ぶと、キーの入力を（伏せ字で）求められ、`search.credentials.<id>.api_key` に保存されます。空のままにすると警告が出ます。キーのないプロバイダーは検索に答えられないからです。

`exa_free` は Exa 公式のホスト型 MCP ツール（`web_search_exa` と `web_fetch_exa`）を匿名で使い、API キーの入力は求められません。匿名のレート制限は適用されます。`EXA_API_KEY` と有料アカウントの制限で直接 API を使いたい場合は、代わりに `exa` を選んでください。

設定したプロバイダーが動かせないとき、`web_search` はそれを取り繕わず、フォールバックしたうえでそう伝えます。セッションには `error{code:"search_provider_unavailable"}` イベントが1つ届き、アシスタントはその理由をあなたに伝えるよう指示されます。

実際にどのプロバイダーが答えるかを確認するには、

```bash
snowpea search test "snowpea agent github"
snowpea search test "snowpea agent github" --json
snowpea tools list --json
```

`snowpea search test` は設定済みのプロバイダーで実際のクエリを1回実行し、答えたプロバイダーと、飛ばされた各プロバイダーが脱落した理由を表示します。`snowpea tools list` は `web_search` をそのプロバイダーつきで表示し、設定された ID が動かせないときは `exa_free → ddgs` のように書かれます。

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
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token --user-id 987654
```

対話式の画面は両方を尋ねます。ボットのトークン、そしてそのプラットフォーム上のあなた自身のアカウント ID です（Telegram では [@userinfobot](https://t.me/userinfobot) に `/start` を送れば自分の ID が分かります）。この ID が重要なのは、チャットから承認に答えられる唯一のアカウントだからです。

トークンは `$SNOWPEA_HOME/credentials.json`（モード `0600`）に保存され、メッセンジャーはデーモンと一緒に待ち受けを始めます。バインドの手順は要りません。ボットを *特定の* エージェント、セッション、チャットに紐づけるのは依然として別の手順で、[Gateway](gateway.md) で扱います。

## What ends up on disk

`$SNOWPEA_HOME/settings.json` には `providers`、`search.provider`、`browser.provider`、`tools.enabled_categories`、`gateway`、`agents.max_concurrent`（3）、`team.max_conflict_retries`（2）、`approvals.timeoutSec`（300）、`memory.enabled`（true）が保持されます。mode、allowlist、backend のプロジェクトごとの上書きは `<project>/.snowpea/settings.json` にあり、グローバルファイルより優先されます。シークレットが `settings.json` に書き込まれることはなく、ログに残ることもありません。

## Next

[Modes](modes.md) — decide how much the agent may do without asking.
