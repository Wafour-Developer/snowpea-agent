# CLI hook integrations（beta）

Claude Code、Codex、Cursor agent、Antigravity（agy）が snowpea のターミナル内で動くとき、snowpea はその CLI が今何をしているかを知る必要があります。どのセッションか、作業中か、完了したか、応答を待っているか。これらの CLI は、そうしたことが起きたときに hook を呼び出せます。連携は、snowpea の小さな hook スクリプトを各 CLI 自身の hook 設定に登録し、CLI が snowpea に知らせるようにします。

snowpea のターミナルの外では、hook は何もしません。連携をインストールしても、あなたが自分で CLI を実行するときの動作は変わりません。

この機能は beta で、[ターミナルセッション](terminals.md)も同じく beta です。

他の言語: [English](../en/integrations.md) · [한국어](../ko/integrations.md) · [すべてのページ](../README.md)

関連: [Terminal sessions（beta）](terminals.md) · [External sessions（beta）](../en/external-sessions.md)

## コマンド

```bash
snowpea integration status                  # 4 つの CLI すべて
snowpea integration install claude          # 1 つの CLI
snowpea integration install all             # 設定フォルダがある CLI すべて
snowpea integration uninstall codex
snowpea integration status agy --json
```

`install`、`uninstall`、`status` は `claude`、`codex`、`cursor`、`agy`、`all`（既定）のいずれかを受け取ります。ファイルを直接扱うので、デーモンが動いている必要はありません。

## 各 CLI が触るファイル

| CLI | 設定フォルダ（環境変数で上書き可、既定値） | ファイル | イベントと snowpea が知ること |
|---|---|---|---|
| Claude Code | `$CLAUDE_CONFIG_DIR`、なければ `~/.claude` | `settings.json` | `SessionStart`（セッション id。startup、resume、clear、compact、fork のとき）· `UserPromptSubmit` → working · `Stop` → done · `Notification` が `permission_prompt` または `elicitation_dialog` → blocked |
| Codex | `$CODEX_HOME`、なければ `~/.codex` | `hooks.json`、`config.toml` | `SessionStart`（セッション id）· `UserPromptSubmit` → working · `Stop` → done。`config.toml` の `[features]` の `hooks` フラグがオフならオンにする |
| Cursor agent | `$CURSOR_CONFIG_DIR`、なければ `~/.cursor` | `hooks.json` | `sessionStart`（セッション id）· `stop` → done |
| Antigravity（agy） | `$ANTIGRAVITY_CLI_CONFIG_DIR`、なければ `~/.gemini/config` | `hooks.json` の `snowpea` ブロック | `PreInvocation`（セッション id） |

インストールのたびに、snowpea のファイルも二つ触ります。

- `$SNOWPEA_HOME/hooks/snowpea-agent-state.sh`（Windows では `.ps1`）: 四つの CLI が共有する hook スクリプトです。`SNOWPEA_TERMINAL_ID` が設定されているときだけ動作し、その場合は CLI のイベントを snowpea のコアに渡します。
- `$SNOWPEA_HOME/integrations/state.json`: snowpea が各 CLI に何を追加したかの記録です。アンインストールはこれを見て元に戻します。

snowpea をアップデートして再インストールすると、hook スクリプトは上書きされます。自分で作った hook はこのファイルではなく、別のファイルに置いてください。

hook で blocked 状態を報告するのは Claude Code だけです。Codex、Cursor agent、agy では、権限の確認や質問を画面から読み取ります。その際は、各 CLI の画面ルールを使います。

snowpea が hook を見つけるのは、snowpea が書いたコマンド文字列が完全に一致するものだけです。あなたのほかの hook はそのまま残ります。

## インストールが行うこと

- 上のファイルに snowpea の項目を追加し、それ以外は変更しません。書式、コメント、あなた自身の項目の順序はそのまま残ります。
- ファイルは原子的に書き込みます（一時ファイルを作ってから名前を変える）。権限は保ち、書いた結果を読み直して確認します。
- ハードリンクのファイルは拒否します。書き込むと、意図していない二つ目のファイルが変わるおそれがあるためです。
- 連携が既に最新なら、バイトを一つも変えません。インストールを二回実行しても安全です。
- 既存のファイルを初めて変更する前に、バックアップを一つ残します: 同じフォルダの `<ファイル>.snowpea-backup-<日付>`。アンインストールしてもバックアップは削除しません。
- 設定フォルダが存在しなければ、`install <cli>` は先にその CLI を一度実行するよう伝えます。`install all` は、フォルダのない CLI を飛ばし、そのことを表示します。

## アンインストールが行うこと

アンインストールは、インストールが追加したものだけを正確に取り除き、それ以外には触れません。

- snowpea の項目と、それだけを含んでいたグループだけを削除します。あなたの hook は残ります。
- インストールが作ったファイルは、中身がなくなっていれば削除します。もともと空だったファイルは、元の空の状態に戻します。
- Codex の `[features]` の `hooks` フラグは、インストールがオンにしたときだけオフに戻します。もともとオンだったなら、そのままにします。
- snowpea の連携が残っている CLI がなくなると、hook スクリプトを削除します。

## ステータスの値

`status` は CLI ごとに、ステータス、設定フォルダ、理由（ある場合）を一行で表示します。`[CLI not found on PATH]` と出る場合は、CLI 自体がインストールされていないので、連携があっても何も動きません。

| ステータス | 意味 | 対処 |
|---|---|---|
| `not-installed` | CLI の設定に snowpea の項目がありません | `snowpea integration install <cli>` |
| `current` | すべて揃っていて最新です | なし |
| `outdated` | 古い snowpea が入れたか、登録されたイベントがこのバージョンと違います | `snowpea integration install <cli>` |
| `modified` | snowpea の項目のどれかを手で編集または削除した、または Codex のフラグをオフにした | 削除してから再インストールするか、ファイルを元に戻してください |

## デスクトップアプリと web

デスクトップアプリと snowpea web の設定にある Integrations ページでは、同じ行が表示され、各 CLI が触るファイルも示され、同じインストールとアンインストールの操作ができます。このページと操作は、ローカルマシンにいるあなた本人だけが使えます。リモートでサインインした接続からは、見ることも変更することもできません。

## 環境変数

| 変数 | 用途 |
|---|---|
| `SNOWPEA_HOME` | hook スクリプトと `integrations/state.json` の場所（既定 `~/.snowpea`） |
| `CLAUDE_CONFIG_DIR` | Claude Code の設定フォルダ |
| `CODEX_HOME` | Codex の設定フォルダ |
| `CURSOR_CONFIG_DIR` | Cursor agent の設定フォルダ |
| `ANTIGRAVITY_CLI_CONFIG_DIR` | agy の設定フォルダ |

これらの変数のどれかを設定した状態で CLI を起動するなら、連携も同じ変数を設定した状態でインストールしてください。そうすれば、CLI が読むフォルダに書き込まれます。
