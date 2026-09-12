# The terminal UI

[English](../en/tui.md) · [한국어](../ko/tui.md) · [すべてのページ](../README.md)

引数なしで `snowpea` を実行するとターミナル UI が開きます。これは薄いクライアントです。どのツールを実行してよいか、コマンドが何を意味するか、いつ圧縮するか — そうした判断はすべてデーモンのものです。このページは、その判断が現れる画面についての説明です。

## 起動画面

セッションが最初に描くのは、ターミナル幅に合わせたワードマークと、いまどこにいるかを伝える3行です。

```text
██████████  ██      ██  ██████████  ██      ██  ██████████  ██████████  ██████████
██          ████    ██  ██      ██  ██      ██  ██      ██  ██          ██      ██
██████████  ██  ██  ██  ██      ██  ██      ██  ██████████  ██████████  ██████████
        ██  ██    ████  ██      ██  ██  ██  ██  ██          ██          ██      ██
        ██  ██      ██  ██      ██  ████  ████  ██          ██          ██      ██
██████████  ██      ██  ██████████  ██      ██  ██          ██████████  ██      ██
🌱 snowpea v0.1.2

          Open-source multi-vendor coding agent and personal AI assistant
        v0.1.2 · anthropic/claude-sonnet-4-5 · /home/you/project · ACCEPT

Last session: 26m ago · "add the worktree parallel session story to the IDE plan"
Press R or type /resume to continue it
```

最後のセッションの案内は、このディレクトリのセッションをデーモンがまだ保持しているときにだけ出ます — 別のターミナルで開いたもの、ヘッドレス実行、クラッシュが残していったもの。入力が空の状態で `R` を押すか `/resume` と打つと、それをこのウィンドウに再生します。該当するセッションがなければ案内自体を出しません。受け取れない申し出は、申し出がないより悪いからです。

バナーは一度だけ出力されます。ほかの出力と同じように上へ流れていき、二度と描き直されません。

## 画面の構成

UI は `git log` のようにインラインで描きます。全画面アプリではありません。終わった出力はターミナルに渡されるので、スクロールバックも、マウスも、`Ctrl+Shift+F` もいつも通りです。生きているのは画面の下だけです。

```text
 › リトライ処理を説明して                   ← スクロールバック: あなたのもの、描き直さない
 ⏺ Read 3 files (128 lines)
 ◆ リトライは `client.ts` にあります…

 ✢ Pondering… (12s · ↓ 3.7k tokens)       ← ここから下が生きている領域
 > 次に打つもの
 snowpea v0.1.2 | ~/project | Model: anthropic/claude-sonnet-4-5 | Mode: ACCEPT | ctx 34% (68k/200k)
 [!!] context 85% — /compact to free space
 ⏵⏵ auto mode on · 1 shell · ← 2 agents
 ● main
 ✳ executor         Implement story IDE-004               running · 27s · ↓ 159.1k tokens
 ◯ 4 idle agents
```

下のパネルは上から順に、ステータス行、（あれば）コンテキスト警告、サマリー行、エージェント行です。入力行はその上にあり、作業インジケータはさらにその上です。変わっていない行は描き直しません。

## 動いているあいだ

ターンが動いているあいだ、入力行の上に1行出ます。その行は実際に何が起きているかを言います。

| 表示 | 意味 |
|---|---|
| `✢ Pondering… (12s · ↓ 3.7k tokens)` | モデルが考えている。動詞は数秒ごとに変わります |
| `✳ Running shell: npm test… (4s · …)` | ツールが実行中。名前と引数つき |
| `✶ 3 agents working… (1m 2s · …)` | ターンが委譲しました |
| `✳ /ralph… (3m 10s · …)` | コマンドワークフローがターンを握っています |
| `⏸ Waiting for approval` | あなたの返事待ちです |

時計とトークン数はセッション全体ではなく、このターンのものです。セッションの合計はステータス行にあります。`Esc` で中断します。

続けて走ったツール呼び出しは、終わるとカードの山ではなくスクロールバックの1行にたたまれます。

```text
⏺ Ran 2 shell commands (34 lines)
⏺ Read 3 files (128 lines)
```

失敗した呼び出しだけは出力ごと自分のカードを保ちます。読む必要があるのはそれだからです。`Ctrl+O` は、まだ生きている領域にある最新のツール呼び出しか diff を展開します。

## モード

`Shift+Tab` が accept → auto → plan → accept の順にモードを回します。現在のモードはステータス行とサマリー行にあり、それぞれが何を許すかは [Modes](../en/modes.md) にあります。`Ctrl+P` は巡回せずに plan モードだけを切り替えます。

## 承認

デーモンが尋ねるときはメニューで尋ねます。`↑`/`↓` で移動、`Enter` で選択中の行を確定、`Esc` は拒否です。

```text
╭──────────────────────────────────────────────────────╮
│ Approval required                                    │
│ shell risk=high timeout=300s                         │
│   command: rm -rf build                              │
│                                                      │
│ ❯  Yes   (y)                                         │
│    Yes, and don't ask again this session   (a)       │
│    Yes for this project   (p)                        │
│      adds an allowlist rule the daemon keeps         │
│    No   (n)                                          │
│ ↑↓ move · Enter confirm · Esc cancel                 │
╰──────────────────────────────────────────────────────╯
```

カーソルは `Yes` から始まるので、`Enter` はそのまま「はい」です。`y`、`a`、`p`、`n` も直接使えます。プロンプトが出ているあいだキーボードはプロンプトのものです。打った文字は後ろの入力行に漏れませんし、`Shift+Tab` もモードを変えません。

誰も見ていないターンが上げた承認 — スケジュールされたジョブ、Telegram のメッセージ — は待ち行列に入ります。`Ctrl+R` でその待ち行列にキーボードを渡し、`/approvals` が一覧を出します。

## 差分

ファイルの変更は、それが起きた場所、つまり会話の中に描かれます。

```text
✎ Edited README.md  (+4 −2)
--- a/README.md
+++ b/README.md
@@ -1,5 +1,7 @@
 # snowpea
-an agent
+an open-source multi-vendor coding agent
… 3 more lines (Ctrl+O)
```

新しいファイルは `✚ Created notes.md (7 lines)` と出ます。長いパッチは12行で切られ、まだ生きている領域にあるものは `Ctrl+O` で開けます。

## コンテキスト

ステータス行は、モデルのコンテキストウィンドウがどれだけ埋まっているかを持ち歩きます。

```text
ctx 34% (68k/200k)
```

70% までは淡く、そこから黄色、85% から赤、そして 80% を超えるとサマリー行の上に、どうすればよいかが1行出ます。

```text
[!!] context 85% — /compact to free space
```

`/compact [指示]` はここまでの会話を要約し、その要約から続けます。後ろに付ける指示は、何を残すかを伝えます。デーモンも、ターンが `context.autoCompactPercent` を超えそうなときは自分で圧縮します。どちらの場合も、どこで起きたかは記録に残ります。

```text
───────────── compacted (68.0k → 12.1k tokens) ─────────────
```

パーセントなしで `ctx 12.3k used` と出るときは、そのモデルのウィンドウ幅をデーモンが知らないという意味です。`snowpea session context` と `providers.<vendor>.context_window` の設定は [Commands](commands.md) にあります。

## 入力

`↑` は前に送ったプロンプトをさかのぼり、`↓` は書きかけに戻ります。履歴はセッション単位ではなくマシン単位です。`$SNOWPEA_HOME/tui-history.jsonl` に最新500件まで残り、同じプロンプトが続けて二度記録されることはありません。

最新の項目からもう一度押した `↓` は別のことをします。カーソルが入力行を抜け、下の行へ移ります。まずサマリー行で、そこで `Enter` を押すといま走っているものが開きます。

```text
⏵⏵ auto mode on · 1 shell · ← 2 agents · Enter to list them
    ◦ Ran shell: npm -w tui test · 12s
```

その次はエージェント行が1行ずつです。`Esc` か `↑` で入力行に戻ります。

### エージェントの中を見る

エージェント行で `Enter` を押すと、画面の記録がそのエージェント自身の会話に入れ替わります — 渡された指示、実行したツール、返した答えまで。

```text
╭────────────────────────────────────────────────────────────────────╮
│ ◯ executor · Implement story IDE-004      running · 28s · ↓ 159.1k │
│ › Implement story IDE-004 (Worktree parallel sessions)             │
│ ✓ read_file path=docs/stories/IDE-004.md (1 lines)                 │
│ ◆ Added the worktree manager and its tests; 3 files changed.       │
│ ↑↓ PgUp/PgDn scroll · Esc back to the main transcript              │
╰────────────────────────────────────────────────────────────────────╯
```

委譲されたエージェントはそれぞれ自分のセッションで動きます。この画面がそのセッションそのもので、過ぎた分を再生してから、続きを追いかけます。`Esc`、または `● main` で `Enter` を押すと戻ります。`Ctrl+A` はたたみ込みの規則を外してパネルを全部開くので、待機中のエージェントや `↓ N more` の裏に隠れていた行まで見えます。

## アップデート

新しいバージョンがあるとステータス行が知らせ、入力が空の状態で `U` を押すか `/update` と打つと確認メニューが開きます。アップグレードが走り、デーモンが再起動し、UI が新しいバージョンで戻ってきます。

```text
snowpea v0.1.2 → v0.1.3 (U to update)
```

## 添付

ファイルのパスを入力行に貼るか、ドラッグして落とすと、文字ではなくチップになります。

```text
[📎 screenshot.png 1.2MB] (backspace removes the last · Ctrl+X clears)
> このレイアウトの何が悪い？
```

ターミナルが実際に渡してくる形をそのまま理解します — パス1つ、複数、空白をエスケープしたパス、していないパス、`file://` URL、ファイルマネージャが付けた引用符。`Ctrl+V` はシステムのクリップボードから画像を直接取り、`$SNOWPEA_HOME/tmp/` の下に保存します。使うのは `wl-paste`、`xclip`、AppleScript、PowerShell のうちこのマシンにあるものです。`/attach <path>` は手で付ける方法です。

入力が空のときの `Backspace` は最新のチップを外し、`Ctrl+X` は全部消します。プロンプトを送るとファイルも一緒に行き、記録には何が行ったかが残ります。

```text
› このレイアウトの何が悪い？
  📎 screenshot.png
```

ファイルはパスとして送るので、コピーは作られません。そのファイルをモデルがどう扱うか — 20MB の上限、縮小、画像を見られないモデルで何が起きるか — は [Attachments and voice](../en/voice.md) にあります。

## 音声

音声はバックエンドがあって初めて動き、バックエンドを持っているのはデーモンです。`snowpea setup audio` で設定し、何が必要かは [Attachments and voice](../en/voice.md) にあります。

| キー・コマンド | 何をするか |
|---|---|
| `/voice` | 音声入力を有効にします |
| `Ctrl+Space` または `/rec` | 録音開始。もう一度で停止 |
| `/tts on`, `/tts off` | 返答が終わるたびに読み上げます |
| `Esc` | 読み上げ中の返答を止めます |

録音中は、作業インジケータの場所が時間を数えます。

```text
● REC 00:07
```

停止すると文字起こしして、送らずに入力行へ入れます。音声認識は十分な頻度で間違えるので、一度読む必要があるからです。録音はデーモンにマイクがあればデーモンで、なければこのマシンで行います。

ステータス行の `🔊` は、返答を読み上げているという意味です。ない機能を頼むと、何も起きない代わりにデーモン自身の言葉で理由が返ります。

```text
voice input needs speech-to-text: no transcription backend: install the whisper CLI, set an OpenAI API key, or …
```

## 全画面

`--fullscreen` は代替スクリーンバッファのレイアウトに切り替えます。記録は UI 自身がスクロールする窓になり、`PgUp`/`PgDn` と `Ctrl+U`/`Ctrl+D` で動かせます。終了してもスクロールバックには何も残りません。

```bash
snowpea --fullscreen
```

変わった行だけを描き直すので、遅い回線では帯域を食いません。その代わりセッションのあいだターミナルのスクロールバックを取り上げます。既定でない理由はそれです。

## キー

| キー | 何をするか |
|---|---|
| `Enter` | 送信、または選択中の項目を確定 |
| `Shift+Tab` | モードを回す |
| `Ctrl+P` | plan モードの切り替え |
| `↑` / `↓` | 前のプロンプト。最新からさらに `↓` でパネルへ |
| `Esc` | ターンの中断、読み上げの停止、エージェント画面から戻る |
| `Ctrl+O` | 最新のツール呼び出しか diff を展開 |
| `Ctrl+A` | エージェントパネルを全部開く |
| `Ctrl+R` | 保留中の承認待ち行列へ |
| `Ctrl+V` | クリップボードの画像を添付 |
| `Ctrl+X` | 添付をすべて消す |
| `Ctrl+Space` | 録音の開始・停止 |
| `U` | 提示されたアップデートを受ける |
| `R` | 起動画面が提示したセッションを続ける |
| `F1` | ヘルプ |
| `Ctrl+C` | 終了 |

`/help` はプラグインが足したものも含めてデーモンが持つコマンドをすべて出し、この表も一緒に出します。
