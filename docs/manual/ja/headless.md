# Headless runs

[English](../en/headless.md) · [한국어](../ko/headless.md) · [すべてのページ](../README.md)

`snowpea -c` は UI なしでターンを1つ実行し、分岐に使える終了コードとともに終わります。snowpea をスクリプト、git フック、CI ジョブに組み込む方法がこれです。

```bash
snowpea -c "what does this repository do?"
snowpea -c "add a regression test for the parser" --mode auto
snowpea -c "summarize today's diff" --json --cwd ~/src/api --timeout 300
```

## Options

| Option | Meaning |
|---|---|
| `-c`, `--prompt TEXT` | プロンプト。これがあることで実行がヘッドレスになります |
| `--mode plan\|accept\|auto` | 権限モード。デフォルトはプロジェクトのデフォルト |
| `--json` | 散文ではなく JSON Lines を出力します |
| `--cwd DIR` | セッションの作業ディレクトリ |
| `--timeout SEC` | SEC 秒後にセッションを中断して閉じます |
| `--provider VENDOR` | この実行で使うベンダー |
| `--resume SESSION_ID` | 新しいセッションを開く代わりに、保存されたセッションを続けます |
| `--approve-none` | 確認する代わりにすべての承認を拒否します |
| `--home DIR` | `SNOWPEA_HOME` を上書きします |

## What it does

デーモンが動いていることを確かめ、`--cwd` にセッションを作り、プロンプトを送り、届いた `session.event` 通知をそのまま描画し、ターンが終わるとセッションを閉じます。TUI は起動しないので、Node は要りません。

コマンドのレジストリは UI ではなくコアのものなので、スラッシュコマンドもそのまま有効なプロンプトです。

```bash
snowpea -c "/ralph add a failing test then make it pass" --mode auto
snowpea -c "/deepinit"
```

## Continuing a saved session

`-c` は通常、新しいセッションを開きます。`--resume` はすでにあるセッションを続けるもので、TUI の `/resume` のヘッドレス版です。保存された履歴が読み込まれ、そこにプロンプトが追加されます。

```bash
snowpea session list --include-closed
snowpea -c "and now write the tests" --resume s-4f2c9a1b7e30
```

`--resume` と一緒に指定した `--mode` と `--provider` は無視されます。保存されたセッションは自分のものを保ちます。`--resume` を単体で使っても何も起きません。TUI の中では `/resume` で対話的にセッションを選んでください。

## Saved sessions

セッションは閉じたあとも `$SNOWPEA_HOME/state.db` に残り、その添付と音声は `$SNOWPEA_HOME/attachments/<id>/` と `$SNOWPEA_HOME/audio/<id>/` に残ります。保存されたセッションを削除すると、それらもすべて消えます。

```bash
snowpea session list                                  # live sessions
snowpea session list --include-closed --json          # plus the saved ones
snowpea session list --include-closed --workdir ~/src/api
snowpea session delete s-4f2c9a1b7e30                 # one saved session
snowpea session clear --workdir ~/src/api             # every saved session of one project
snowpea session clear --all                           # every saved session, everywhere
```

生きているセッションが削除されることはありません。先に閉じてください。各行には、セッション ID、モード、作成時刻、作業ディレクトリ、そしてそのセッションが最後に見たプロンプトが表示されます。

## Teams and model profiles

プロジェクトのチームとエージェントごとのモデル振り分けは設定なので、UI なしでも構成できます。

```bash
snowpea team list                                     # global + project teams, * marks the active one
snowpea team create delivery architect executor verifier
snowpea team use delivery
snowpea team delete delivery

snowpea model profiles --json                         # profiles, the default, per-agent assignments
snowpea model default fast                            # models.default
snowpea model assign executor deep                    # agents.models.executor
```

`snowpea team create` は `<workdir>/.snowpea/settings.json` — `/team create` が書くのと同じファイル — に書き込み、見つからないエージェント名は拒否します。`snowpea team delete` が消せるのはプロジェクトのチームだけで、グローバルのチームは `$SNOWPEA_HOME/settings.json` で編集します。`snowpea model assign` は、プロファイル ID が存在しなければデーモンに拒否されます。

## Exit codes

| Code | Meaning |
|---|---|
| `0` | ターンが完了しました |
| `1` | エージェントが失敗で終わりました |
| `2` | 使い方または設定の誤りです |
| `3` | デーモンに接続できませんでした |
| `4` | 承認が拒否されたか、モードが動作をブロックしました |
| `5` | `--timeout` を過ぎました。セッションは中断され、閉じられました |

```bash
if snowpea -c "does this repo have a failing test?" --mode plan; then
  echo "clean"
else
  echo "exit $?"
fi
```

考えておくべきはコード `4` です。plan モードでは書き込みの試みが `mode_denied` エラーと終了コード `4` を生むので、plan モードは CI で使える読み取り専用のゲートになります。

## JSON output

`--json` を付けると、受信した `session.event` が1行1 JSON オブジェクトとして書き出され、最後の行が結果のレコードになります。

```json
{"kind":"message.delta","sessionId":"…","seq":12,"payload":{"text":"Looking at "}}
{"kind":"tool.call","sessionId":"…","seq":13,"payload":{"callId":"c1","name":"grep","args":{"pattern":"def main"}}}
{"kind":"tool.result","sessionId":"…","seq":14,"payload":{"callId":"c1","name":"grep","ok":true,"output":"…"}}
{"kind":"turn.done","sessionId":"…","seq":20,"payload":{"turnId":"t1","reason":"complete"}}
{"kind":"result","exitCode":0,"sessionId":"…","usage":{"inputTokens":4120,"outputTokens":380}}
```

イベントの kind は `message.delta`、`message.done`、`tool.call`、`tool.result`、`diff`、`subagent.spawn`、`subagent.update`、`subagent.done`、`team.task.update`、`mode.changed`、`usage`、`error`、`turn.done` です。`turn.done` の reason は `complete`、`interrupted`、`error`、`denied`、`timeout` のいずれかです。ペイロードの完全なスキーマは [docs/protocol.md](../../protocol.md) にあります。

```bash
snowpea -c "list the modules" --json | jq -r 'select(.kind=="message.delta") | .payload.text' | tr -d '\n'
```

## Approvals without a person

TTY があれば、承認リクエストは標準入力の y/n の確認になります。ない場合 — パイプ、CI ランナー — 尋ねる相手がいないので、リクエストは即座に拒否され、プロセスは `4` で終了します。それが望みなら、明示してください。

```bash
snowpea -c "run the linter" --approve-none
```

自動化における正直な構成は2つです。読むだけで済むものには plan モード、そして捨ててよいコンテナの中での auto モード。コンテナの部分は [Backends](backends.md) を参照してください。

## Session-free subcommands

一部のサブコマンドは RPC メソッドを1つ呼んで終了し、セッションを作ることもモデルに接触することもありません。速く、決定的で、無料なので、インストール直後のスモークテストとして最適です。

```bash
snowpea --version
snowpea tools list --json
snowpea commands list --json
snowpea daemon status --json
```

## In CI

```yaml
- run: curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh
- run: snowpea --version
- run: snowpea tools list --json
- run: snowpea -c "/deep-research whether this dependency has a known CVE" --mode plan --timeout 600 --json
```

`SNOWPEA_HOME` はジョブごとのディレクトリに設定して、実行どうしが状態を共有しないようにしてください。また、ステップが終わってもデーモンは動き続けることを覚えておいてください。ランナーが長命なら、ジョブの最後に `snowpea daemon stop` を。

## Next

[Protocol](protocol.md) — CLI が実際に話しているもの。
