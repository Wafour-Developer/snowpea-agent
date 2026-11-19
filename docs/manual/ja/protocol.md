# Protocol

[English](../en/protocol.md) · [한국어](../ko/protocol.md) · [すべてのページ](../README.md)

生成される完全なリファレンスは [docs/protocol.md](../../protocol.md) です。このページは、それを読む前に持っておきたい見取り図です。

コアにできることはすべて、UI の操作である前に JSON-RPC のメソッドです。デーモンとターミナル UI のあいだにプライベートな経路はありません。TUI はごく普通のクライアントであり、TUI にできることはあなたのクライアントにもできます。

## Source of truth

`core/snowpea_core/server/protocol.py` が `PROTOCOL_VERSION`、各メソッドのパラメータと結果のスキーマ、そしてすべてのイベントのペイロードを定義します。`scripts/gen_protocol.py` がそこから `docs/protocol.md` と `sdk/src/protocol.ts` を生成します。生成されたファイルを手で編集することは決してなく、ずれがあれば CI がビルドを失敗させます。

```bash
uv run python scripts/gen_protocol.py --check
```

## Transport

デーモンは起動時に選んだループバックのポートにバインドし、認証トークンとともに `$SNOWPEA_HOME/daemon.json` に記録します。

| Surface | Endpoint | Purpose |
|---|---|---|
| WebSocket | `ws://127.0.0.1:<port>/ws` | JSON-RPC 2.0、双方向 |
| HTTP GET | `/health` | 生存確認 |
| HTTP GET | `/version` | サーバーとプロトコルのバージョン |
| HTTP GET | `/protocol.json` | スキーマ全体を JSON で |

HTTP のエンドポイントは、プローブ、インストーラー、デバッグのための読み取り専用の便宜です。状態を変える呼び出しは WebSocket にしか存在しません。

## Handshake

接続した直後、クライアントは `$SNOWPEA_HOME/token` のトークン、自分自身のバージョン、そしてビルド時に想定したプロトコルバージョンを添えて `system.hello` を呼びます。それより前の他のメソッドはすべて `unauthorized` で失敗します。メジャーバージョンの不一致は `protocol_incompatible` で拒否されます。

```json
{"jsonrpc":"2.0","id":1,"method":"system.hello",
 "params":{"token":"…","clientVersion":"0.1.0","protocolVersion":"0.1.0"}}
```

結果は `protocolVersion`、`serverVersion`、そして `capabilities` のリストを持ちます。バージョンが凍結されたあと、機能はこれで折衝されます。

クライアントは `locale` も送ってください (プロトコル 1.65.0、capability `clientLocale`)。`ko`、`ko-KR`、`ja`、`es-ES`、`zh-CN` のような BCP 47 タグで利用者の言語を伝えると、検証の問題と拒否 (`settings.set` の問題、`security.get` の問題、ブラウザーが表示する本人専用の拒否) がその言語で返ります。英語、韓国語、日本語、スペイン語、簡体字中国語に対応し、地域付きのタグは言語に、それ以外は英語にフォールバックします。メソッド自身の `locale` パラメーターがあれば、その呼び出しではそちらが優先されます。タグは正規化され (`ko_kr` → `ko-KR`)、言語タグでない値は拒否されずに無視されるので、古いクライアントや変わったクライアントも接続できます。訳されるのは人が読む文だけで、`details.reason` と `problemItems[].reason`/`code` はどの言語でも同じです。TUI は `LC_ALL`、`LC_MESSAGES`、`LANG` を送り、SDK の `connect({locale})` は渡された値を送ります。

プロトコル 1.66.0 (capability `securityDefaultOff`) から、セキュリティチェックは既定でオフです。人がオンにするまで (`settings.set {scope: 'global', patch: {security: {enabled: true}}}`)、`security.get` は `enabled: false` と `byDefault: true` を返します。この状態はセキュリティ設定の画面でだけ、中立的に表示してください。チェックがオフの間は、デーモンのトークンや保存されたログインの読み取りもブロックされず、モードのルールに従います。1.65.0 以前のクライアントのために、`problems` は引き続き中立的な一行 (問題コード `security-disabled`) で始まります。

プロトコル 1.67.0 (capability `externalModelSwitch`) から、外部 CLI セッションのモデルを変更できます。`external.setModel {ref, model}` (その CLI の `external.clis` `models` のいずれか、または CLI の既定なら `null`) は `{ref, model, appliesNextTurn?}` を返し、次のターンから適用されます。実行中のターンは元のモデルのまま終わります。選択はデーモンを再起動しても保たれ、`ExternalSessionRow.model` と `external.state` イベント (新しいフィールド `model`) に表れます。`ExternalCliInfo.defaultModel` は CLI が示す場合 (Codex) に既定のモデルを示します。Claude Code は完全なモデル id も受け付けます。tmux ペインで動いているセッションは拒否されます (`external_unavailable`、`details.reason` `tmux_transport`)。そのペインで `/model` を使って変更してください。

プロトコル 1.68.0(capability `lspInstallReasons`)以降、`lsp.catalog` の行と `lsp.install` の結果は言語サーバーをインストールできない理由を示します。`reason` `needs_toolchain` は `toolchain`(`dotnet-sdk`、`go`、`rustup`、`node`、`python`)、`hint`、`url` と共に、`npm_failed` は `npmCode`(`ERESOLVE`、`EACCES`、`E404` など)と `detail`(npm の最後のエラー行)と共に返ります。.NET サーバーには SDK が必要で、ランタイムだけの `dotnet` は無いものとして扱います。capability `lspToolchainInstall` があれば .NET SDK の `needs_toolchain` 結果に `needsToolchain {name, sizeMb, source, version}` が付き、利用者の確認後 `lsp.install {ids, withToolchain: true}` が Microsoft から SDK を取得し(https、リリースメタデータの sha512 で検証)ユーザーごとの ディレクトリに入れてからサーバーをインストールします。`lsp.install.progress` に `phase`、`percent`、`toolchain` が加わり、`lsp.toolchain.cancel {name}` はダウンロードを止め、`lsp.toolchain.remove {name}` は SDK を削除します。ローカルの利用者だけが行え、ブラウザーは `actor: 'person'` と `personConfirm: 'window'` を送ります。

## Methods, by area

| Area | Methods |
|---|---|
| system | `system.hello`, `system.info`, `system.health`, `system.shutdown` |
| session | `session.create`, `session.list`, `session.resume`, `session.close`, `session.prompt`, `session.interrupt`, `session.compact`, `session.deleteSaved`, `session.setMode` |
| commands and tools | `command.list`, `command.run`, `tool.list` |
| approvals | `approval.list`, `approval.respond`, および `permission.allowlist.add` / `list` / `remove` |
| providers | `provider.list`, `provider.configure`, `provider.loginWeb` |
| agents | `agent.list`, `agent.create`, `agent.spawn`, `agent.bindChannel`, `agent.delete` |
| team | `team.start`, `team.status` |
| jobs | `job.schedule`, `job.list`, `job.cancel`, `job.runNow` |
| gateway | `gateway.bind`, `gateway.list`, `gateway.unbind` |
| memory | `memory.search`, `memory.write` |
| skills | `skill.search`, `skill.install`, `skill.list`, `skill.remove`, `skill.reload` |

1つだけ向きが逆のメソッドがあります。`approval.request` は通知ではなく、サーバーからクライアントへの *リクエスト* です。デーモンがクライアントに判断を求め、返事を待ちます。この双方向性こそ、このプロトコルが HTTP とストリームではなく WebSocket JSON-RPC である理由です。

## Events

通知はデーモンから購読中のクライアントへ流れます。

- `session.event(sessionId, seq, kind, payload, ts)` は、ターンの中で起きることをすべて運びます。kind は `message.delta`、`message.done`、`tool.call`、`tool.result`、`diff`、`subagent.spawn`、`subagent.update`、`subagent.done`、`team.task.update`、`mode.changed`、`backend.changed`、`usage`、`error`、`turn.done` です。
- 無人の承認待ち行列のための `approval.pending` と `approval.resolved`。
- `job.event` と `gateway.event`。
- スキルやプラグインの再読み込み後の `commands.changed`。

すべての `session.event` は、そのセッション内で単調増加する `seq` を持ちます。接続が切れたクライアントは再接続して `session.resume(sessionId, afterSeq)` を呼び、取り逃したぶんをちょうど再送してもらいます。イベントが単にブロードキャストされるのではなく永続化されているのはそのためです。

## Errors

エラーは JSON-RPC のエラーで、`error.data.code` に文字列のコードが入ります。`unauthorized`、`protocol_incompatible`、`not_found`、`invalid_params`、`mode_denied`、`approval_denied`、`approval_timeout`、`tool_inactive`、`not_implemented`、`login_unsupported`、`search_provider_unavailable`、`browser_provider_unavailable`、`internal` です。

## Using the SDK

```ts
import { connect } from "@snowpea/sdk";

const client = await connect({ port, token, clientVersion: "1.0.0" });
const { sessionId } = await client.call("session.create", { workdir: process.cwd(), mode: "accept" });

client.on("session.event", (e) => {
  if (e.kind === "message.delta") process.stdout.write(e.payload.text);
});

client.onRequest("approval.request", async (req) => ({ decision: "deny", scope: "once" }));

await client.call("session.prompt", { sessionId, text: "what does this repo do?" });
```

クライアントは自分で再接続し、最後に見た `seq` から再開します。`sdk/src/protocol.ts` はすべてのメソッドとイベントの型を提供します。生成物なので、デーモンが持たないメソッドを記述することはできません。

## Versioning and the freeze gate

`PROTOCOL_VERSION` は semver です。v0.2 のデスクトップ IDE が始まる前に、プロトコルは凍結ゲートを通過しなければなりません。バージョンは `1.0.0`、そして連続する3つのリリースタグにわたって `docs/protocol.md` と生成されたスキーマに変更がなく、その3つすべてで SDK の契約テストが通っていること。

```bash
uv run python scripts/gen_protocol.py --check
git log --oneline -- docs/protocol.md sdk/src/protocol.ts
```

凍結後、追加的な変更にはマイナーバージョンの引き上げが必要で、`system.hello` の `capabilities` による折衝を通ります。メジャーな変更は、すべてのクライアントを同時にリリースすることを意味します。それこそが、このゲートが可視化するために存在するコストです。
