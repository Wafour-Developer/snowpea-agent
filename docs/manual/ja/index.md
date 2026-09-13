# snowpea manual

[English](../en/index.md) · [한국어](../ko/index.md) · [すべてのページ](../README.md)

snowpea は、あなた自身のマシンで動くオープンソースのマルチベンダー・コーディングエージェントです。Python のコアがローカルデーモンとして常駐し、状態を持つものはすべて — セッション、ツール、権限、メモリ、スケジュール、メッセンジャーのバインディング — そのデーモンが所有します。クライアントは文書化された WebSocket JSON-RPC プロトコル経由でデーモンに接続します。いまは Ink のターミナル UI、v0.2 では Electron の IDE、そして TypeScript SDK の上にあなたが作るものすべてです。

## Where to start

snowpea をインストールしたばかりなら、[Install](install.md)、続けて [Setup](setup.md)、[Modes](modes.md) の順に読んでください。日々使うにはそれで十分です。その先はすべて、必要になったときに見ればよい追加の面です。

| Page | Read it when |
|---|---|
| [Install](install.md) | インストール、アップグレード、あるいはインストールの手順が失敗したとき |
| [Setup](setup.md) | ベンダーを選ぶ、API キーを入れる、ブラウザでログインする、検索・ブラウザのプロバイダーを選ぶとき |
| [Modes](modes.md) | エージェントが尋ねすぎるとき、または尋ねなさすぎるとき |
| [Terminal UI](tui.md) | キー、パネル、添付、音声 — 画面が何を伝えているか |
| [Commands](commands.md) | スラッシュコマンドと CLI サブコマンドの全一覧がほしいとき |
| [Attachments and voice](voice.md) | プロンプトで画像やファイルを送るとき、話しかけて話し返してもらうとき |
| [Plugins](plugins.md) | スキル、エージェント、コマンド、フック、MCP サーバーをインストールまたは自作するとき |
| [Scheduler](scheduler.md) | 席を外しているあいだに作業を進めてほしいとき |
| [Gateway](gateway.md) | Telegram、Discord、Slack からエージェントと話したいとき |
| [Backends](backends.md) | コードがコンテナの中や別のホストにあるとき |
| [Headless](headless.md) | snowpea をスクリプト化する、または CI に組み込むとき |
| [Protocol](protocol.md) | デーモンを相手にクライアントを作るとき |

## The shape of the thing

```text
snowpea              → starts the daemon if needed, attaches the terminal UI
snowpea -c "..."     → one headless turn, no UI, deterministic exit code
snowpea <subcommand> → inspect or configure without opening a session
```

デーモンは遅延起動です。最初のクライアントで起動し、アイドル時間が過ぎると自分で終了します — ただし生かしておく理由が何もないときだけです。開いているセッション、有効なジョブ、ゲートウェイのバインディング、名前付きエージェントのいずれもないとき、という意味です。そのどれがデーモンを生かしているかは `snowpea daemon status` が教えてくれます。

```bash
snowpea daemon status
```

## Where things live

| Path | What |
|---|---|
| `$SNOWPEA_HOME`（デフォルトは `~/.snowpea`） | グローバルなものすべて |
| `$SNOWPEA_HOME/settings.json` | プロバイダー、検索とブラウザの選択、ツールのカテゴリ、タイムアウト |
| `$SNOWPEA_HOME/credentials.json` | ボットトークンとシークレット、モード `0600` |
| `$SNOWPEA_HOME/daemon.json` | ポート、pid、トークン、起動時刻、プロトコルバージョン |
| `$SNOWPEA_HOME/state.db` | セッション、イベント、メモリ、ジョブ、チームのタスク、名前付きエージェント |
| `$SNOWPEA_HOME/logs/` | `daemon.log`、`approvals.jsonl` |
| `$SNOWPEA_HOME/plugins/`, `skills/`, `agents/`, `commands/` | インストール済みおよび手書きの拡張 |
| `<project>/.snowpea/settings.json` | このリポジトリのデフォルトモード、allowlist、バックエンド |
| `<project>/.snowpea/{skills,agents,commands}/` | プロジェクトローカルの拡張 |
| `<project>/.claude/{skills,agents,commands}/` | Claude Code 互換のために読み込まれます |

`SNOWPEA_HOME` はどこでも尊重されるので、独立した2つ目のインストールは環境変数ひとつぶんの距離にあります。

```bash
SNOWPEA_HOME=/tmp/snowpea-scratch snowpea daemon status
```

## Getting help

UI 内の `/help` は、プラグインが追加したものも含めて、コアがいま持っているコマンドをすべて一覧します。シェルからは `snowpea commands list` が同じレジストリを表示し、`snowpea tools list` が各ツールを権限タグとアクティブかどうかつきで表示します。

```bash
snowpea commands list
snowpea tools list --json
```

バグや質問は [GitHub issues](https://github.com/Wafour-Developer/snowpea-agent/issues) へどうぞ。コードを変更したい場合は、[Contributing](../../CONTRIBUTING.md) と [Architecture](../../ARCHITECTURE.md) がまず読むべき2つの文書です。
