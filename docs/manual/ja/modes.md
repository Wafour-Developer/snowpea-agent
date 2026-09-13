# Modes, permissions and the allowlist

[English](../en/modes.md) · [한국어](../ko/modes.md) · [すべてのページ](../README.md)

すべてのツールは権限タグを1つ持ちます。モードは、それぞれのタグをどう扱うかを決めます。

| tag | tools |
|---|---|
| `read` | `read_file`, `list_dir`, `glob`, `grep`, `git_status`, `git_diff`, `git_log`, `process_list`, `memory_search`, `transcribe_audio` |
| `write` | `write_file`, `edit_file`, `git_commit`, `memory_write` |
| `exec` | `shell`, `process_kill`, `delegate_task` |
| `network` | `web_search`, `web_extract`, `browser_*`, メディアツール, `text_to_speech`, デフォルトでは MCP サーバー |
| `send` | `schedule_create`, `schedule_list`, `schedule_cancel` |

## The matrix

| mode | read | write | exec | network | send |
|---|---|---|---|---|---|
| **plan** | allow | deny | deny | allow | deny |
| **accept**（デフォルト） | allow | allow | ask | ask | ask |
| **auto** | allow | allow | allow | allow | allow |

**plan** は考えるためのモードです。エージェントはリポジトリを読み、ウェブを検索できますが、何も変更できません。拒否された呼び出しは `mode_denied` コードの `error` イベントを生んでターンを終わらせ、ヘッドレス実行は `4` で終了します。

**accept** は実作業のデフォルトで、Claude Code の acceptEdits に相当します。ファイルの読み取りと編集は確認なしで行われ、シェルコマンド、ネットワーク呼び出し、そして何かを送信するものは先に尋ねます。

**auto** は何も尋ねません。あなたが見ているとき、使い捨てのコンテナの中、あるいは影響範囲を考え抜いたスケジュールジョブで使ってください。

## Switching

```
/plan
/accept
/auto
/mode
/mode show
/mode save
```

```bash
snowpea --mode plan
snowpea -c "draft a migration plan" --mode plan
```

`/mode save` は `<project>/.snowpea/settings.json` に `"defaultMode"` を書き込むので、このリポジトリでの次のセッションはそこから始まります。現在のモードは常にステータス行に出ています。

## Approvals

ポリシーが *ask* と言うとき、コアは承認リクエストを作り、そのツール呼び出しをブロックします。対話的なリクエストは、そのターンが来た面にだけ届きます — あなたが打ち込んだ TUI のウィンドウ、または `snowpea -c` を走らせているターミナルです。他の誰かの待ち行列に現れることはありません。

回答にはスコープが付きます。

| scope | meaning |
|---|---|
| `once` | この呼び出しだけ |
| `session` | このセッションのあいだ、一致するものすべて |
| `project` | `<project>/.snowpea/settings.json` に保存されます |
| `always` | `$SNOWPEA_HOME/settings.json` に保存されます |

誰も答えないことも1つの答えです。`approvals.timeoutSec`（デフォルト300秒）を過ぎるとリクエストは拒否され、ターンが終わります。タイムアウトを含むすべての判断は `$SNOWPEA_HOME/logs/approvals.jsonl` に追記されます。

スケジュールされたジョブや届いたチャットメッセージが上げたリクエストは *無人* であり、振る舞いが異なります — [Gateway](gateway.md) を参照してください。

## The allowlist

allowlist の項目は、一致するコマンドについて *ask* を *allow* に格上げします。*deny* を格上げすることは決してできないので、ここに何を追加しても plan モードが弱くなることはありません。

```
/allow ^ls( .*)?$
/allow ^git (status|diff|log)\b
/allow ^npm (run )?test$ --global
/allow tool:web_search
/allowlist
/allowlist remove 3
```

パターンはシェルコマンドに対して照合される正規表現です。`tool:<name>` の形はツール1つ丸ごとを許可します。`--global` なしの場合はプロジェクトの設定に、付けた場合は `$SNOWPEA_HOME/settings.json` に入ります。

パターンはアンカーを付けて狭く書いてください。`^git ` は `git push --force` も許してしまいます。`^git (status|diff|log)\b` はそうなりません。

## Headless and unattended

`snowpea -c` は TTY があるときは標準入力で尋ねます。ない場合 — CI ジョブやパイプ — 尋ねる相手がいないので、承認リクエストは即座に拒否となり、プロセスは `4` で終了します。そのつもりなら明示してください。

```bash
snowpea -c "run the linter" --approve-none
```

CI では、読むだけのものには plan モード、失っても構わないコンテナの中では auto モード、というのが正直な組み合わせです。確認を黙らせるために開発マシンで auto に手を伸ばさないでください。そのためにあるのが allowlist です。

## Next

[Commands](commands.md) — コマンドの全面。
