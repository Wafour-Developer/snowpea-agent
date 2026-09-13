# Scheduler

[English](../en/scheduler.md) · [한국어](../ko/scheduler.md) · [すべてのページ](../README.md)

スケジューラはデーモンの中にいます。ジョブはデーモンのプロセスの中で、登録したときのモードで実行され、指定したチャンネルに答えを届けます。別の cron デーモンで動くものは何もなく、あなたのターミナルが開いている必要もありません。

## Registering a job

```bash
snowpea job schedule --at "0 9 * * *" --task "summarize yesterday's commits" --channel telegram:123456
snowpea job schedule --in 10m --task "check whether the build is green" --mode plan
snowpea job schedule --every 30m --task "watch the error log" --channel log --workdir ~/src/api
```

セッションの中からは、

```
/schedule "every day at 9am" "summarize yesterday's commits" --channel telegram:123456
/schedule
```

`--at`、`--in`、`--every`、`--cron`、`--spec` は同じオプションの5つの呼び名です。行として自然に読めるものを使ってください。

## Specs

| Form | Example |
|---|---|
| cron、5フィールド | `0 9 * * *` |
| 一度きりの遅延 | `in 60s`, `in 10m`, `in 2h` |
| 間隔 | `every 30m`, `every 6h` |
| 自然言語、英語 | `every day at 9am`, `in 10 minutes` |
| 自然言語、韓国語 | `매일 09:00`, `10분 뒤` |

ジョブは、どう解釈されたかに応じて `cron`、`once`、`interval` のいずれかになります。`job list` は解釈された種別と次の実行時刻を表示するので、自然言語の指定が思った通りの意味になったかを確かめる最短の方法になります。

```bash
snowpea job list
snowpea job list --json
```

## Managing jobs

```bash
snowpea job run <job-id>
snowpea job cancel <job-id>
```

`job run` は、タイマーが起こすのとまったく同じようにデーモン内でジョブを即座に発火させます。スケジュールに任せる前にジョブを試す正しい方法です。

各ジョブは `last_run` と `last_status` を記録します。`last_status` は `ok`、`error`、または承認が答えられないまま期限切れになった場合の `denied_by_timeout` です。

## How a job executes

スケジューラは15秒ごとに時を刻みます。ジョブの時間が来ると、ジョブの作業ディレクトリとモードでセッションを作り、無人の印を付け、ジョブのタスクをプロンプトとして与え、最終的なアシスタントメッセージをチャンネルに届けます。`started`、`finished`、`failed`、`denied` の種別を持つ `job.event` 通知が接続中のすべてのクライアントに届くので、動いている TUI にはジョブの進捗が見えます。

知っておく価値のあることが2つあります。1つ目は、ジョブ ID と予定時刻からなる occurrence key が一意なので、デーモンが tick の途中で再起動しても同じ枠で二度発火することはない、ということ。2つ目は、起動時にスケジューラは、デーモンが落ちているあいだに取り逃した一度きりのジョブを、1時間以内の遅れであれば取り返して実行する、ということです。それより古いものは実行されず missed の印が付きます。

## Reminders come back to the session that asked

ジョブは自分がどこから来たかを覚えています。それを登録したセッション — TUI のセッションで打った `/schedule`、あるいはターンの中で使われた `schedule` ツール — が `originSessionId` としてジョブに刻まれ、`job list --json` に表示されます。ジョブが発火すると、その答えはそのセッションに `message.done` イベントとして流れ込み、テキストには接頭辞が付きます。

```text
⏰ Scheduled reminder (job_7f21c0)

Yesterday's commits: 14 across three repositories…
```

つまりリマインダーは、あなたが頼んだスレッドに届き、他のメッセージと同じようにそこに永続化されます。あとでそのセッションを開けば、リマインダーは履歴の中にあります。

そのセッションが開いたままである必要はありません。閉じたセッションは配信を受け取るためだけに復元され、直後にまた閉じられるので、発火したジョブがあなたの知らないところでセッションを走らせたままにすることはありません。生きているセッションはそのまま放置されます。

明示的な `--channel` は置き換えではなく追加です。元のセッションはどちらにせよリマインダーを受け取り、チャンネルにも同じテキストが届きます。元のセッションがない場合 — この仕組みができる前に登録されたジョブや、セッションが削除されたジョブ — で、かつチャンネルにも配信できなかったときは、テキストは `$SNOWPEA_HOME/logs/jobs.log` のジョブログにフォールバックします。

## Channels

| Channel | Goes to |
|---|---|
| `telegram:<chat_id>` | Telegram のチャット |
| `discord:<channel_id>` | Discord のチャンネル |
| `slack:<channel>` | Slack のチャンネル |
| `log` | `$SNOWPEA_HOME/logs/daemon.log` だけ |

プラットフォームは先にバインドされている必要があります — [Gateway](gateway.md) を参照してください。`log` は何も必要とせず、ジョブに何を言わせるかをまだ詰めているあいだの正しい選択です。

## Modes and approvals

ジョブは、登録元のセッションとは独立に自分のモードを持ちます。ジョブの登録自体に `send` 権限が必要なので、accept モードでは登録そのものの承認を求められます。これは意図的です。スケジュールされたジョブは、そのモードが許すこと全部に対する常時の許可だからです。

ジョブのターンが承認を要するものに当たると、そのリクエストは無人になります。TUI の承認待ち行列と、バインドされたチャットの allow/deny ボタンに同時に現れます。最初に答えた人が勝ち、他の全員には何が起きたかが伝えられ、`approvals.timeoutSec`（300秒）以内に誰も答えなければリクエストは拒否され、ジョブの `last_status` は `denied_by_timeout` になります。

plan モードのジョブは読むことと検索はできますが、決して書きません。auto モードのジョブは何も尋ねません。無人で動くものには plan が安全なデフォルトで、auto はコンテナに値します。

## Keeping the daemon alive

有効なジョブは、アイドルシャットダウンを抑える4つのカウンターの1つなので、スケジュールを持つデーモンは自分で起き続けます。

```bash
snowpea daemon status
```

これは `will not exit` と、その理由を表示します。再起動を越えて生き延びるかどうかは別の問題です。そのためにはサービスとして登録してください。

```bash
snowpea service install
snowpea service status
```

## Next

[Gateway](gateway.md) — 答えがどこへ行くか。
