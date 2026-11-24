# Terminal sessions（beta）

ターミナルセッションとは、snowpea の中で実際のターミナル上で動くプログラムのことです。シェル、snowpea CLI、または外部の CLI（Claude Code、Codex、agy、Cursor agent）のいずれかになります。その画面を見て、そこに入力でき、snowpea は各ターミナルが作業中か、あなたの応答を待っているか、完了したかを追跡します。

この機能は beta です。各 CLI の画面を読むルールは CLI の出力に合わせているため、新しい CLI のバージョンでは snowpea が見る内容が変わることがあります。ステータスがおかしいと感じたら、ターミナルの画面そのものが正です。

他の言語: [English](../en/terminals.md) · [한국어](../ko/terminals.md) · [すべてのページ](../README.md)

関連: [External sessions（beta）](../en/external-sessions.md) · [Jarvis mode（beta）](../en/jarvis.md) · [CLI hook integrations（beta）](integrations.md) · [snowpea web](web.md) · [Terminal UI](tui.md)

## ターミナルセッションとは

- デーモンはターミナルごとに小さな holder プロセスを一つ起動します。holder はプログラムの擬似端末を保持し、直近 1 MiB の出力を残し、終了コードを記録します。
- ターミナルは、デスクトップアプリ、snowpea web、または Jarvis から開きます。`snowpea term` コマンドは、既に存在するターミナルの一覧表示、読み取り、入力、接続に使います。
- 同時に動かせるのは `terminals.max` 個までです（既定 16、1 から 64 まで指定可能）。
- プロファイル: `shell`、`snowpea`、`claude`、`codex`、`agy`、`cursor`。

## 外部 CLI セッションをターミナルで開く

`jarvis.external.view` が `terminal`（既定）のとき、外部セッションはターミナルとして開きます。CLI はチャット画面の代わりにペインで動きます。以前の構造化されたチャット表示を使いたい場合は `chat` に設定してください。表示方法を指定しないクライアントは、従来どおりチャット表示になります。

- 新しいセッションは、選んだプロジェクトのフォルダで CLI を起動します。
- 既存のセッションは、CLI 自身の再開コマンドで続きから始まります（`claude --resume <id>`、`codex resume <id>`、agy と Cursor も各自のコマンド）。セッション id は hook 連携から得るので、ターミナルから再開できるようにするには、先に [`snowpea integration install`](integrations.md) を実行してください。
- 別のターミナルで既に動いているセッションは、これまでどおり閲覧のみです。

CLI はあなたの対話シェルの中で動くので、PATH や nvm などの設定がそのまま効きます。CLI が終了すると、`terminals.keepShell` を無効にしていない限り、ペインはシェルに戻ります。

## ターミナルで動かす snowpea セッション

snowpea のセッションは、チャットとして見ることも、同じセッションに接続した端末で snowpea CLI を動かして見ることもできます。両方とも同じセッションのクライアントなので、同時に開いても構いません。切り替えはセッションのヘッダーにあります。

- 既定の表示は `ui.sessionView` で決まります。`chat`（既定）か `terminal` です。これはあなた自身の画面の設定で、他の人の表示は変わりません。
- シェルから、ローカルのデーモンに既にあるセッションへ接続するには `snowpea --session <id>` を実行します。TUI は新しいセッションを作らず、そのセッションを再開します。

## 設定

| キー | 既定値 | 役割 |
|---|---|---|
| `ui.sessionView` | `chat` | snowpea セッションの既定の表示 |
| `jarvis.external.view` | `terminal` | 外部 CLI セッションの開き方: `terminal` または `chat` |
| `terminals.max` | `16` | 同時に動かせるターミナルの数（1 から 64） |
| `terminals.keepShell` | `true` | CLI が終了したら、ペインを閉じずにシェルへ戻す |
| `terminals.headlessCols` / `terminals.headlessRows` | `160` / `48` | 接続している画面がないときにターミナルが使うサイズ |
| `terminals.sounds.blocked` | `true` | ターミナルがあなたの応答を待つとき、音と通知を出す |
| `terminals.sounds.done` | `true` | ターミナルが完了し、あなたがそれを見ていないとき、音と通知を出す |

`terminals.*` と `ui.*` の設定は、ローカルマシンにいる本人だけが変更できます。リモートやトンネル経由の接続からは変更できません。

## ステータス: ドットの意味

各ターミナルは次の 5 つのいずれかの状態にあり、アプリはそれをドットで表示します。

| ドット | 状態 | 意味 |
|---|---|---|
| needs you | `blocked` または `error` | プログラムが応答（権限の確認や質問）を待っているか、失敗しました |
| done | `done` | ターンが終わったが、まだ誰もそのターミナルを見ていません（`seen` が false） |
| working | `working` | ターンが実行中です |
| idle | `idle` | 何も実行していません |

blocked には種類もあります: `permission`、`question`、`auth`。サイドバーでは、プロジェクトのドットを needs you、done、working、idle の順にまとめて表示します。

snowpea は、次の順で優先度の高いものから状態を決めます。

1. プログラム自身（snowpea TUI は自分の状態を直接報告します）;
2. 画面に表示された権限の確認や質問（その CLI の画面ルールが読み取る）;
3. CLI の hook からの報告（[hook 連携](integrations.md)）;
4. 画面ルールだけによる状態。

## 権限のオプション

CLI をターミナルで起動するとき、権限モードを選べます。選択肢は、各 CLI 自身のモードから来ます。

| CLI | 提供されるオプション | 危険なオプション（赤、先に確認） |
|---|---|---|
| Claude Code | Accept edits、Plan、Auto、Don't ask | Bypass permissions（`--dangerously-skip-permissions`） |
| Codex | Read only、Default | Full access（`--dangerously-bypass-approvals-and-sandbox`） |
| agy | Accept edits、Auto、Plan | Skip permissions（`--dangerously-skip-permissions`） |
| Cursor agent | Ask、Plan | Run everything（`--yolo`） |

危険なオプションは次のように扱われます。

- 表示されるすべての場所で赤いバッジが付き、アプリは開始前にリスクの確認を求めます。
- Jarvis やほかのツールが要求した場合、設定に関係なく拒否されます。
- `web.remote.maxMode` が `auto` より低いときは拒否されます。
- Web トンネル経由の接続では、ターミナルのロックを解除していない限り拒否されます（[snowpea web](web.md#トンネルで公開アドレスを得る)）。

Jarvis は、危険なオプションで起動したターミナルの権限の確認に応答しません。Accept edits や Don't ask などの危険でないモードも、ローカルのアプリでのあなたの操作だけで許可され、Jarvis が代わりに選ぶことはありません。Jarvis が `terminal_create` で起動する CLI ターミナルは、常に CLI の既定の権限で起動します。

## デーモンを再起動してもターミナルは残る

holder は独立して動くため、デーモンを再起動またはアップグレードしても、ターミナルは動き続けます。デーモンが再び起動すると、生きている holder すべてに再接続し、holder が持つ直近の出力から画面を描き直します。holder が死んでいるターミナル（例えば再起動後）は「失われた」として一覧に出るので、CLI セッションを再開できます。

```bash
snowpea daemon stop                    # デーモンだけ止まる。ターミナルは動き続ける
snowpea daemon stop --and-terminals    # デーモンとすべてのターミナルセッションを終了する
```

## `snowpea term` コマンド

```bash
snowpea term ls                        # 生きているターミナルの一覧
snowpea term ls --all                  # 終了したものも含める
snowpea term read <id> --lines 100     # 画面をプレーンテキストで、履歴 100 行を付けて表示
snowpea term send <id> "テストを実行して"   # テキストを入力して Enter を押す
snowpea term send <id> --no-submit "下書き"   # Enter を押さずに入力だけする
snowpea term attach <id>               # このウィンドウでそのターミナルを操作する
snowpea term attach <id> --observe     # 見るだけ: 入力もサイズ変更もしない
```

- `attach` は今のターミナルを raw モードにし、セッションを表示します。**Ctrl+\\** で切り離しでき、セッションは動き続けます。`attach` は対話的なターミナルが必要で、Windows ではまだ対応していません。Windows では `read` と `send` を使うか、アプリでターミナルを開いてください。
- `send` は、そのターミナルへの入力をあなたが止めてから入力します。権限の確認などで止まっているターミナルには送らず、`--force` を付けたときだけ送ります。`--force` は、画面を読んで何を聞かれているか確かめたあとにだけ使ってください。
- `read` は画面を表示するだけで、何も変更しません。

## Jarvis とターミナル（beta）

Jarvis モードを有効にすると、Jarvis は自分が見られるプロジェクトのターミナルを見ることができ、次のツールを使います。

| ツール | 役割 |
|---|---|
| `terminal_list` | ターミナルの一覧、ステータス、誰が起動したか |
| `terminal_read` | 一つのターミナルの画面テキスト、ステータス、タイトル |
| `terminal_send` | CLI ターミナルにメッセージを入力して送信する |
| `terminal_keys` | 名前付きのキーを押す（enter、escape、up、y、1 …） |
| `terminal_interrupt` | CLI が実行中の処理を中断する |
| `terminal_wait` | ターミナルが作業を止める、ブロックする、終了する、または一致するテキストを出力するまで待つ |
| `terminal_create` | プロジェクトで Claude Code、Codex、agy、Cursor のいずれかを既定の権限で起動する。最初のプロンプトを付けることもできる |

ルールは次のとおりです。

- Jarvis は CLI ターミナルにしか入力しません。通常のシェルには入力せず、`force` は使わず、危険なオプションで起動したターミナルにも入力しません。
- Jarvis が入力するのは、あなたの依頼か、あなたが委任した作業のためだけです。読み取りと指示はすべて、アクセス記録（`jarvis.access`）と決定台帳（ledger）に残ります。
- `terminal_create` は危険なオプションを決して使わず、作成したターミナルには Jarvis が起動したという印を付けます。

Jarvis が起きるとき:

- 委任されたターミナルが `blocked` や `error` になる、または `done` に達すると、Jarvis はすぐに起きます（通常のまとめ処理と予算の範囲内）。
- それ以外のターミナルは、30 秒ごとのダイジェストに入ります。
- Jarvis が起動したターミナルが blocked、error、exit になると、Jarvis はそのターミナルの最後の 20 行を含むメモを受け取ります。メモは Jarvis に渡されるイベントで、ターミナルに入力されることはありません。

ターミナル内の権限の確認への応答: Jarvis が権限の確認に答えるのは、次のすべてが成り立つときだけです。ターミナルが Jarvis に完全に委任されていて、あなたが保持していないこと。ターミナルが危険なものでないこと。画面ルールが確認の対象になっているコマンドを読み取れていること。委任エンジンがそのコマンドを `safe` または `risky` に分類していること。`guarded` や `never` のコマンド、そのほかすべての場合は、確認はあなたのものです。Jarvis のすべての応答と拒否は決定台帳に記録されます。委任のレベルについては [Jarvis mode](../en/jarvis.md#delegation) で説明しています。
