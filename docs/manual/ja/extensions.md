# 拡張を書く

拡張は、snowpea デーモンが自分の中に読み込む Python コードです。モデルに新しいツールを与え、システムプロンプトに内容を加え、変わる事実をモデルに伝え続け、すべてのツール結果を確認し、ターンが早く終わりすぎないようにし、スラッシュコマンドを加え、MCP サーバーを動かせます。このページだけで、snowpea のソースを読まずに拡張を書けます。

他の言語: [English](../en/extensions.md) · [한국어](../ko/extensions.md) · [简体中文](../zh-CN/extensions.md) · [Español](../es/extensions.md)

このページのファイルはすべて、リポジトリの [`examples/extensions/`](../../../examples/extensions/README.md) にある、そのまま動く完全な拡張で、テストスイートが一つずつ読み込みます。

## 拡張とは何か、いつ書くか

snowpea のアドオンは三種類です（[プラグイン](plugins.md#what-is-what) を参照）。

| やりたいこと | 書くもの |
|---|---|
| 必要なときにモデルが読む手順・チェックリスト・チームの流儀を教える | **スキル**（`SKILL.md`）— コードなし |
| スキル・エージェント・コマンド・フック・MCP サーバーを Claude Code や Codex の形式でまとめて配る | **プラグイン** |
| エージェントの働き方そのものを変える: Python のツール、プロンプトのセクション、変わったときに再送する事実、すべてのツール結果の検査、ターンを続ける理由 | **拡張** |

役目を果たせる一番軽いものを選んでください。スキルは信頼する必要がありませんが、拡張はデーモンの中でコードを実行します。

## 信頼モデル

- 拡張は**デーモンのプロセス内で、デーモンの権限で**動きます。あなたが読み書きできるすべてのファイルを読み書きでき、ネットワークに接続でき、すべてのセッションを見られます。シェルスクリプトと同じく、信頼できるコードだけを入れてください。
- スキャンするのは `$SNOWPEA_HOME/extensions/`（既定は `~/.snowpea/extensions/`）だけです。プロジェクトのフォルダーは**決して**スキャンしないので、クローンしたリポジトリを開いてもそのコードは動きません。
- モデルが拡張のツールで行うことは、引き続きモードと承認のルールを通ります。ツールは自分がどんな操作か（`read`、`write`、`exec`、`network`、`send`）を宣言し、人に尋ねるかどうかはモードが決めます。承認のプロンプトにはツールを登録した拡張の名前が出ます。
- デーモンの環境で `SNOWPEA_EXTENSIONS=0` にすると、拡張なしで起動します。

## クイックスタート

スターターテンプレートから拡張を作ります。

```bash
snowpea extension new hello
```

`~/.snowpea/extensions/hello/` に二つのファイルができます。`plugin.py`:

<!-- example: examples/extensions/hello/plugin.py -->
```python
"""hello: a snowpea extension.

Created by `snowpea extension new hello`. Edit this file, then run
`snowpea extension reload hello` and look for the tool in
`snowpea tools list`. The developer guide is docs/manual/en/extensions.md.
"""

from __future__ import annotations

from typing import Any

# Import only from snowpea_core.extensions: it is the stable API.
from snowpea_core.extensions import PluginContext, Tool, ToolContext, ToolResult


async def greet(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Runs when the model calls the tool; ``args`` follows ``input_schema``."""
    who = str(args.get("name") or "world").strip()
    if len(who) > 100:
        # ok=False: the model reads ``error`` and can call again with better args.
        return ToolResult(ok=False, error="name is longer than 100 characters")
    return ToolResult(ok=True, output=f"Hello, {who}!")


def apply(ctx: PluginContext) -> None:
    """Called once when the extension loads.

    Everything registered through ``ctx`` is undone when the extension is
    disabled, reloaded or removed. If this function raises, nothing stays
    registered and `snowpea extension list` shows the error.
    """
    ctx.tools.register(
        Tool(
            name="hello_greet",
            category="hello",
            description="Greet someone by name. Use it when the user asks to be greeted.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Who to greet."}},
            },
            # read: runs without asking in every mode. Use write, exec, network
            # or send for a tool that changes files, runs programs, goes online
            # or sends messages; the mode then decides whether to ask.
            permission="read",
            run=greet,
        )
    )
    ctx.log.info("ready")  # goes to $SNOWPEA_HOME/logs/daemon.log
```

そして `plugin.json`:

<!-- example: examples/extensions/hello/plugin.json -->
```json
{
  "name": "hello",
  "description": "Greets people: the starting point made by snowpea extension new.",
  "version": "0.1.0",
  "engines": {"snowpea": "^1"}
}
```

動いているデーモンに読み込み（動いていなければ起動します）、ツールを確認します。

```bash
snowpea extension reload hello
snowpea tools list
```

`hello_greet` が `[extension:hello]` 付きで表示されます。エージェントに誰かへの挨拶を頼むと、このツールを呼びます。`plugin.py` の挨拶を変えて `snowpea extension reload hello` をもう一度実行すれば、デーモンを再起動しなくても次のモデル呼び出しから新しいコードが使われます。間違いがあれば `reload` は終了コード 1 で終わり、ファイルと行番号付きのエラーを表示します。

```bash
snowpea extension list
snowpea extension info hello
```

## フォルダー構成と `plugin.json`

拡張は `$SNOWPEA_HOME/extensions/` 内のフォルダー一つです。フォルダー名が拡張の名前で、英字・数字・`-`・`_` で 64 文字まで使えます。

| パス | 必須 | 内容 |
|---|---|---|
| `plugin.py` | はい | `apply(ctx)` を定義します。これがないフォルダーは無視されます |
| `plugin.json` | いいえ | 下のマニフェスト |
| `*.py`、サブパッケージ | いいえ | 自分のモジュール。相対インポートします: `from . import helpers`、`from .lib import client`（サブフォルダーには `__init__.py` が必要） |
| `tests/` | いいえ | テスト。デーモンは無視します |
| その他 | いいえ | データファイル。`Path(__file__).parent` で見つけます |

自分のモジュールの絶対インポート（`import helpers`）は意図的に使えません。拡張はそれぞれ独立したパッケージとして読み込まれるので、二つの拡張に `helpers.py` があってもお互いのものは見えません。サードパーティのパッケージはデーモンの Python（`snowpea` を動かしている Python）に入っている必要があります。小さな純 Python の依存なら、サブパッケージとしてコピーして相対インポートしてください。

`plugin.json` のフィールド（すべて任意。未知のフィールドは警告として記録されます）:

| フィールド | 型 | 意味 |
|---|---|---|
| `name` | 文字列 | 書くならフォルダー名と同じであること |
| `title` | 文字列 | マーケットのインデックスでの表示名 |
| `description` | 文字列 | 一行の説明。`snowpea extension list` に出ます |
| `version` | 文字列 | 拡張のバージョン。`list` と `info` に出ます（semver 推奨） |
| `engines.snowpea` | 文字列 | 前提とする拡張 API のメジャー: `"^1"`、`"1"`、`"1.x"`、`"~1.0"`、`">=1"`（[バージョン管理](#バージョン管理) を参照） |
| `configSchema` | オブジェクト | 設定の JSON Schema（[設定と configSchema](#設定と-configschema) を参照） |

JSON として正しくない、フィールドの型が違う、`name` がフォルダー名と違う、`configSchema` が正しい JSON Schema でない `plugin.json` は拡張を読み込ませず、理由が `snowpea extension list` に出ます。

## ライフサイクル

拡張の読み込みは、**発見**（`plugin.py` のあるフォルダー）→ **検証**（`plugin.json`、`engines.snowpea`、`configSchema` による設定）→ `plugin.py` の**インポート** → **適用**（`apply(ctx)` がすべてを登録）→ **実行**（登録したものをデーモンが呼ぶ）→ **破棄**（登録したものを新しい順にすべて元に戻す）です。

- **デーモン起動時**に、有効な拡張をすべて読み込みます。組み込み機能とスキルの後なので、それらが使っている名前を求めると、黙って置き換えるのではなく分かりやすいエラーで拒否します。
- `apply(ctx)` は普通の関数でも `async def` でもかまいません。非同期なら最大 30 秒待ちます。
- どこかの段階で失敗すると拡張は `error`（または `incompatible`）になり、理由は `snowpea extension list` に、トレースバックはデーモンのログに残り、**登録したものは何も残りません**。`apply` が例外を出す前に登録したものも元に戻します。
- **再読み込み**（`snowpea extension reload <名前>`）は、すべてを破棄し、フォルダー・`plugin.json`・設定を読み直し、拡張のすべてのモジュールをディスク上のソースから読み込み直して（キャッシュされた `.pyc` は使いません）、もう一度適用します。無効な拡張は無効のままです。
- **無効化**と**有効化**は破棄と適用を行い、選択は `settings.extensions.<名前>.enabled` に保存されます。
- フォルダーを**削除**すると、次の `snowpea extension list`（またはどの `extension.*` 呼び出しでも）でアンロードされます。
- `settings.extensions` の**編集**（手作業、`settings_set`、別のクライアント）は、デーモンが変わったファイルを読んだ時点（次のターンの前、または `extension.*` 呼び出し時）で反映されます。`enabled` が変われば読み込み・アンロード、`config` が変われば再読み込みです。
- **デーモン停止時**にすべての拡張を破棄するので、`on_dispose` の関数は開いたものを閉じられます。

動いているセッションから見えること: ツール、プロンプトのセクション、ランタイムコンテキストのプロバイダー、フック、コマンドは毎回その場で参照されます。変更はすべてのセッションの**次のモデル呼び出しから**効き、ターンの途中でも同じです。実行中のツール呼び出しは開始時のコードで終わります。削除されたばかりのツールをモデルが呼ぶと「unknown tool」エラーを受け取り、自分で対応します。

## API

すべて `snowpea_core.extensions` からインポートします。これらの名前が安定した API で、定義されているモジュールはリリースごとに移ることがあります。

| 名前 | 何か |
|---|---|
| `PluginContext` | `apply(ctx)` が受け取るもの |
| `Tool`、`ToolResult`、`ToolContext`、`PermissionTag` | ツール |
| `PromptSection`、`PromptInputs`、`SECTION_ORDERS` | システムプロンプトのセクション |
| `ContextProvider`、`CONTEXT_ORDERS` | ランタイムコンテキストのプロバイダー |
| `PostDecision` | `post_execute` フックの戻り値 |
| `StopDecision`、`StopContext`、`STOP_ORDERS` | `turn_stopping` リスナー |
| `Command`、`CommandContext` | スラッシュコマンド |
| `ExtensionError` | できない登録が出す例外 |
| `API_VERSION`、`TOOL_PERMISSIONS`、`DISPOSE_TIMEOUT` | 定数 |

登録メソッドはどれも **disposer**、つまりその登録一つを先に取り除く関数を返します。拡張が外れるときにどのみちすべて取り除かれるので、使うことはまれです。登録できないとき — オブジェクトの形が違う、組み込み・スキル・別の拡張がすでに使っている名前 — はどの登録も `ExtensionError` を出し、`apply` が失敗して拡張全体が読み込まれないままになります。

### `ctx.tools.register(tool, *, timeout=None) -> Disposer`

モデルにツールを提供します。デーモンのツールレジストリに登録され、`snowpea tools list` と `tool.list` に `source` = `extension:<名前>` で出ます。

- `tool`: `Tool(name, category, description, input_schema, permission, run, parallel_safe=False)`。`name` は `[A-Za-z][A-Za-z0-9_-]{0,63}` で未使用のもの、`input_schema` は `"type": "object"` の JSON Schema、`permission` は `read`、`write`、`exec`、`network`、`send` のどれか、`run` は `async def run(tool_ctx, args) -> ToolResult` です。
- `timeout`: 秒。これより長い呼び出しはタイムアウトのエラーで失敗します。

<!-- example: examples/extensions/hello/plugin.py -->
```python
    ctx.tools.register(
        Tool(
            name="hello_greet",
            category="hello",
            description="Greet someone by name. Use it when the user asks to be greeted.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Who to greet."}},
            },
            # read: runs without asking in every mode. Use write, exec, network
            # or send for a tool that changes files, runs programs, goes online
            # or sends messages; the mode then decides whether to ask.
            permission="read",
            run=greet,
        )
    )
```

[ツールを書く](#ツールを書く) を参照してください。

### `ctx.prompt.section(section) -> Disposer`

システムプロンプトに一片を加えます。プロンプトセクションのレジストリに登録されます。

- `section`: `PromptSection(name, order, render, requires_tool=None)`。`render(inputs: PromptInputs) -> str` はモデル呼び出しごとに実行され、テキストを返すか、`""` を返してセクションを外します。位置は `order` が決めます（[プロンプトのセクション](#プロンプトのセクション) を参照）。`requires_tool` を指定すると、そのツールが提供されている間だけ出ます。

<!-- example: examples/extensions/house-style/plugin.py -->
```python
    ctx.prompt.section(PromptSection(name="house-style:rules", order=650, render=render))
```

### `ctx.prompt.context(provider) -> Disposer`

ランタイムの事実を加えます。ランタイムコンテキストのレジストリに登録されます。

- `provider`: `ContextProvider(name, order, render, required=False)`。`render(core, session) -> str` はモデル呼び出しの前に毎回実行され、テキストが変わったときだけモデルに送られます。`required=True` なら軽量の委任エージェントにも送られます。

<!-- example: examples/extensions/git-branch/plugin.py -->
```python
    ctx.prompt.context(ContextProvider(name="git-branch:branch", order=500, render=render))
```

### `ctx.hooks.post_execute(name, listener, *, order=0) -> Disposer`

モデルが結果を読む前に、終わったツール呼び出しをすべて見ます。post-execute チェーンに `<拡張>:<name>` で登録されます。

- `listener(tool_ctx, tool_name, args, result)` は `None`、`PostDecision.accept(output=None, *notes)`、`PostDecision.block(feedback, *notes)` のどれかを返します。`async` でもかまいません。
- `order`: リスナーは `order` 順、同じなら登録順に動きます。

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
    ctx.hooks.post_execute("redact", redact, order=-100)
```

### `ctx.hooks.turn_stopping(name, listener, *, order=150) -> Disposer`

ターンが終わろうとするときに動きます。turn-stopping チェーンに登録されます。

- `listener(core, session, stop: StopContext)` は `None` か `StopDecision(text, producer, ...)` を返します。`async` でもかまいません。`order` 順で最初の決定が勝ち、その `text` が次の指示になります。

<!-- example: examples/extensions/finish-the-job/plugin.py -->
```python
    ctx.hooks.turn_stopping("placeholders", check, order=120)
```

### `ctx.commands.register(command) -> Disposer`

スラッシュコマンドを加えます。コマンド表（`snowpea commands list`、`command.list`、TUI のパレット）に登録され、クライアントには `commands.changed` で知らされます。

- `command`: `Command(name, summary, run, args_schema={})`。`name` に先頭の `/` は付けません。`run` は `async def run(cmd_ctx: CommandContext, args: str)` です。

<!-- example: examples/extensions/scratchpad/plugin.py -->
```python
    ctx.commands.register(
        Command(
            name="scratch",
            summary="keep, list or clear notes that last across sessions",
            run=scratch,
            args_schema={
                "type": "object",
                "properties": {
                    "arguments": {"type": "string", "description": "add <text> | clear"}
                },
            },
        )
    )
```

### `ctx.mcp.register(name, entry) -> Disposer`

拡張が読み込まれている間、MCP サーバーを動かします。MCP サーバーの一覧（`snowpea mcp list` では拡張がプラグインとして表示されます）に、サーバーが起動すればそのツールがツールレジストリに登録されます。

- `name`: サーバー名。ツールは `mcp__<name>__<tool>` になります。
- `entry`: `.mcp.json` の項目一つ。stdio なら `command`、`args`、`env`、`cwd`、http/sse なら `url`、`headers`、加えて `timeoutSec`、`toolTimeoutSec`、`tools.include` / `tools.exclude`。

<!-- example: examples/extensions/glossary-mcp/plugin.py -->
```python
    ctx.mcp.register(
        "glossary",
        {
            "command": sys.executable,  # the daemon's Python
            "args": [str(server)],
            "timeoutSec": 20,
        },
    )
```

### `ctx.on_dispose(fn) -> fn`

拡張がアンロードされるとき、ほかのすべてと一緒に、登録の逆順で `fn()` を実行します。`fn` は `async` でもよく、最大 `DISPOSE_TIMEOUT`（10）秒待ちます。失敗したり止まったりしたものは記録され、残りはそのまま実行されます。

<!-- example: examples/extensions/ticket-workflow/plugin.py -->
```python
    client = tracker.Tracker(ctx.config["baseUrl"], ctx.config.get("token", ""))
    # Registered first, so it runs last: after everything that uses the client is gone.
    ctx.on_dispose(client.close)
```

### `ctx.config`、`ctx.data_dir`、`ctx.log`、`ctx.name`、`ctx.path`

- `ctx.config`（dict）: `settings.extensions.<名前>.config`。`configSchema` で検証済みで、トップレベルの `default` で補われています。コピーなので、新しい設定は再読み込みで届きます。

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
    patterns = [re.compile(text) for text in [*BUILT_IN, *ctx.config["patterns"]]]
    marker = ctx.config["replacement"]
```

- `ctx.data_dir`（Path）: `$SNOWPEA_HOME/extension-data/<名前>/`。初めて使うときに作られます。ファイルは拡張のフォルダーではなくここに置けば、拡張を更新しても残ります。

<!-- example: examples/extensions/scratchpad/plugin.py -->
```python
    store: Path = ctx.data_dir / "notes.json"
```

- `ctx.log`（logging.Logger）: 名前は `snowpea.extensions.<名前>` で、デーモンのログに書きます。
- `ctx.name`（str）と `ctx.path`（Path）: 拡張の名前とフォルダー。
- `ctx.core`: デーモンのコア。安定した API には**含まれません**。上のもので足りないときだけ読み、変わりうると考えてください。

## ツールを書く

<!-- example: examples/extensions/file-stats/plugin.py -->
```python
"""file-stats: a tool in depth.

Shows an argument schema with required fields, a permission tag, errors the
model can act on, the session's backend for file access (so the tool works in
SSH and Docker sessions too), progress for a long call, work moved off the
event loop, a per-call timeout, and facts for surfaces in ``meta``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import posixpath
from typing import Any

from snowpea_core.extensions import PluginContext, Tool, ToolContext, ToolResult

MAX_FILES = 500

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "A file or folder, relative to the project root.",
        },
        "pattern": {
            "type": "string",
            "description": "For a folder: a glob such as '*.py'. Default: every file.",
            "default": "*",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _count(blobs: list[bytes]) -> tuple[int, int]:
    """Lines and bytes; runs in a worker thread."""
    return sum(blob.count(b"\n") for blob in blobs), sum(len(blob) for blob in blobs)


async def file_stats(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    # The session's files: on this machine, in a Docker container or over SSH.
    backend = tool_ctx.backend
    raw = str(args.get("path") or "").strip()
    if not raw:
        return ToolResult(ok=False, error="path is required")
    root = posixpath.normpath(str(backend.cwd).replace("\\", "/"))
    target = posixpath.normpath(str(backend.resolve(raw)).replace("\\", "/"))
    # A read tool still stays inside the project: say so instead of reading.
    if target != root and not target.startswith(root.rstrip("/") + "/"):
        return ToolResult(ok=False, error=f"{raw} is outside the project; give a path inside it")
    if not await backend.exists(target):
        return ToolResult(ok=False, error=f"{raw} does not exist")
    parent, name = posixpath.split(target)
    if f"{name}/" in await backend.list_dir(parent):  # list_dir marks folders with "/"
        pattern = str(args.get("pattern") or "*")
        names = await backend.list_dir(target)
        files = [
            posixpath.join(target, entry)
            for entry in names
            if not entry.endswith("/") and fnmatch.fnmatch(entry, pattern)
        ]
    else:
        files = [target]
    if len(files) > MAX_FILES:
        return ToolResult(ok=False, error=f"{len(files)} files match; narrow the pattern")
    if tool_ctx.progress is not None:  # None when nobody is watching (tests, headless)
        await tool_ctx.progress.emit("stdout", f"reading {len(files)} files\n")
    blobs = [await backend.read_bytes(file) for file in files]
    # Counting is CPU work: keep it off the daemon's event loop.
    lines, size = await asyncio.to_thread(_count, blobs)
    return ToolResult(
        ok=True,
        output=f"{len(files)} files, {lines} lines, {size} bytes",
        meta={"files": len(files), "lines": lines, "bytes": size},  # for surfaces, not the model
    )


def apply(ctx: PluginContext) -> None:
    ctx.tools.register(
        Tool(
            name="file_stats",
            category="files",
            description=(
                "Count lines and bytes of a file, or of the files directly in a folder. "
                "Use it to size up files before reading them."
            ),
            input_schema=SCHEMA,
            permission="read",  # reads only: no approval needed in any mode
            run=file_stats,
            parallel_safe=True,  # two calls in one response may run at once
        ),
        timeout=30,  # a call that takes longer fails with a timeout error
    )
```

**スキーマと説明**が、モデルがツールについて知るすべてです。説明には何をするか、いつ使うかを書き、引数ごとに説明を付け、必須のものを示してください。モデルはたいていスキーマに従いますが、いつもではありません。頼りにする値は確かめてください。

**`ToolContext`** は `tool_ctx.session`（その `workdir`、`id`、`mode`）、`tool_ctx.backend`、`tool_ctx.core`、`tool_ctx.call_id`、`tool_ctx.progress` を持ちます。`tool_ctx.backend` はセッションのファイルとシェルがある場所 — このマシン、Docker コンテナー、SSH ホスト — です。プロジェクトには `open()` や `subprocess` ではなくこれを通してアクセスしてください: `cwd`、`resolve(path)`、`exists`、`list_dir`（フォルダーは `/` で終わる）、`read_file`、`read_bytes`、`write_file`、`write_bytes`、`remove_file`、`run(command, cwd=, timeout=)`。そうしないと、セッションが別の場所で動いているとき、ツールはデーモンのマシン上で動作します。

**`ToolResult`** のフィールド:

| フィールド | 読む側 | 用途 |
|---|---|---|
| `ok` | 全員 | `False` なら呼び出しは失敗 |
| `output` | モデル | `ok` のときの結果テキスト |
| `error` | モデル | 何が悪かったか。モデルが呼び出しを直せるように書きます |
| `meta` | クライアント（`tool.result`）、モデルではない | UI 向けの構造化された事実 |
| `contexts` | モデル、次の呼び出しの前 | 結果に添えるメモ |
| `path`、`diff` | クライアント | 変更したファイルと、表示する unified diff |

ただの `str` を返すと `ToolResult(ok=True, output=...)` とみなされます。`tools.maxResultChars`（既定 100 000）より長い出力は先頭と末尾を残して切られ、全文はモデルが読める場所に保存されます。

**権限タグ**は、モードごとに人に尋ねるかどうかを決めます。

| タグ | 意味 | plan | accept | auto |
|---|---|---|---|---|
| `read` | 読むだけで何も変えない | 許可 | 許可 | 許可 |
| `write` | プロジェクトのファイルを変える | 拒否 | 許可 | 許可 |
| `exec` | プログラムを実行する | 確認 | 確認 | 許可 |
| `network` | ネットワークに出る | 許可 | 確認 | 許可 |
| `send` | ユーザーの代わりにメッセージを送る | 拒否 | 確認 | 許可 |

ツールができる一番危険なことのタグを選んでください。`write` ツールでは、引数の `path`、`paths`、`edits[].path` にあるファイルを呼び出し前にチェックポイントに残すので、人が変更を元に戻せます。承認のプロンプトには `from extension <名前>` が付きます。

**エラー。** モデルが対処できることには `ToolResult(ok=False, error=...)` を返してください。例外は代わりに捕まえます。呼び出しは `<ツール> (extension <名前>) failed: <型>: <メッセージ> (at plugin.py:<行>)` で失敗し、トレースバックはデーモンのログへ行き、ターンもデーモンも続きます。それでもエラーの文面は自分で書いてください。トレースバックはモデルにとって意味がありません。

**進捗。** `await tool_ctx.progress.emit("stdout", text)` は、呼び出しの実行中にクライアントへ一行流します。見ている人がいなければ `progress` は `None` です。

**タイムアウトと中断。** `register` に `timeout=` を渡すか自分で作業を制限（`asyncio.wait_for`）しない限り、タイムアウトはありません。人がターンを中断すると、ツールを動かしていたタスクがキャンセルされ、待っていた `await` で `asyncio.CancelledError` が起きます。`finally` で後始末し、例外はそのまま伝えてください。

**並行性。** `run` はすべてのセッションが共有するデーモンのイベントループで動きます。決してブロックしないでください。ファイルや CPU の処理は `asyncio.to_thread` へ、ネットワークは非同期クライアントで。複数のセッションが同時にツールを呼ぶことがあるので、共有状態は守ってください。一つのモデル応答の呼び出しは順に実行され、ツールが `parallel_safe=True`（`read` と `network` のツールだけ可）なら同時に動きます。

## プロンプトのセクション

<!-- example: examples/extensions/house-style/plugin.py -->
```python
"""house-style: a system-prompt section.

The section sits at order 650, in the stable band (below 1000): between the
persona (600) and the reply-language rule (700). Its text depends only on the
mode, so it does not change from one model call to the next and the provider's
prompt cache stays warm.
"""

from __future__ import annotations

from snowpea_core.extensions import PluginContext, PromptInputs, PromptSection

RULES = """House style for this team:
- Commit messages: imperative mood, at most 72 characters in the subject.
- Never add a dependency without saying why in the same answer."""

PLAN_NOTE = "In plan mode, list the commits you would make, one line each."


def render(inputs: PromptInputs) -> str:
    """Runs for every model call; return "" to leave the section out."""
    if inputs.subagent:
        return ""  # delegated agents get a brief, not the house rules
    if inputs.mode == "plan":
        return f"{RULES}\n{PLAN_NOTE}"
    return RULES


def apply(ctx: PluginContext) -> None:
    ctx.prompt.section(PromptSection(name="house-style:rules", order=650, render=render))
```

セクションは `order` 順、同じなら名前順に並びます。order は**帯**も決め、これがコストに効きます。プロバイダーはプロンプトの先頭をキャッシュするので、内容が変わるセクションはその位置から後ろのキャッシュを無効にします。

| 帯 | order | 変わるとき | 組み込みのセクション（order） |
|---|---|---|---|
| stable | 1000 未満 | モード・モデル・役割・応答言語が変わるときだけ | identity (−1000), execution (−900), plan-continuity (−800), vendor (0), config-rule (100), search-honesty (200), memory-guidance (300), mode (400), role-preamble (500), role (510), persona (600), reply-language (700) |
| context | 1000–1999 | セッションごとに一度、圧縮の後 | environment (1000), context-files (1100), skills (1200), tools (1300) |
| volatile | 2000 以上 | ターンごと | memory (2000), plan-line (2100), context-pressure (2200) |

変わらないテキストは stable 帯に置き、`render` が毎回同じテキストを返すようにしてください。セッション中に変わる事実ならランタイムコンテキストのプロバイダーを使います。`PromptInputs` には `mode`、`role`、`subagent`、`tools`（と `tool_names`）、`reply_language`、`root`（プロジェクトのフォルダー）などがあります。例外を出したセクションはその呼び出しで外され、一度だけ記録されます。セクション名の頭には拡張の名前を付けてください。

## ランタイムコンテキストのプロバイダー

<!-- example: examples/extensions/git-branch/plugin.py -->
```python
"""git-branch: a runtime-context provider.

Tells the model which git branch the session's project is on. The provider
runs before every model call, so it reads .git/HEAD directly instead of
starting a git process. The model is sent the text again only when it changes:
switch branches mid-session and the next model call says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from snowpea_core.extensions import ContextProvider, PluginContext


def current_branch(workdir: Path) -> str:
    """The checked-out branch of the repository holding ``workdir``, or ""."""
    for folder in (workdir, *workdir.parents):
        head = folder / ".git" / "HEAD"
        if head.is_file():
            text = head.read_text(encoding="utf-8").strip()
            prefix = "ref: refs/heads/"
            return text[len(prefix) :] if text.startswith(prefix) else f"(detached at {text[:12]})"
    return ""


def render(core: Any, session: Any) -> str:
    branch = current_branch(Path(session.workdir))
    return f"Current git branch: {branch}" if branch else ""


def apply(ctx: PluginContext) -> None:
    ctx.prompt.context(ContextProvider(name="git-branch:branch", order=500, render=render))
```

すべてのプロバイダーはモデル呼び出しの前に毎回描画され、一つの並んだリストになります。このリストは前のものを置き換える一つの `<system-reminder>` としてモデルに送られます — ターンの開始時、あるいはターンの途中なら次のモデル呼び出しの前に — が、**最後に送ったものと違うときだけ**送り、圧縮が前のものを要約して消したときにまた送ります。テキストが同じ間、プロバイダーにコストはかかりません。`render` は速く（プロセスもネットワークも使わない: モデル呼び出しごとに動きます）、決定的にしてください。組み込みの order: goal (50)、plan (100)、plan-auto (110)、context-pressure (900)。

## フック

### `post_execute`

<!-- example: examples/extensions/redact-secrets/plugin.py -->
```python
"""redact-secrets: a post_execute hook with a validated config.

Every tool result passes through the hook before the model reads it. Text that
looks like a credential is replaced with [redacted]. Extra patterns come from
settings.extensions.redact-secrets.config.patterns, which plugin.json's
configSchema checks before apply() runs.
"""

from __future__ import annotations

import re
from typing import Any

from snowpea_core.extensions import PluginContext, PostDecision, ToolContext, ToolResult

BUILT_IN = [
    r"sk-[A-Za-z0-9_-]{20,}",  # OpenAI-style API keys
    r"ghp_[A-Za-z0-9]{36}",  # GitHub personal access tokens
    r"AKIA[0-9A-Z]{16}",  # AWS access key ids
]


def apply(ctx: PluginContext) -> None:
    # The schema guarantees a list of strings; compiling can still fail.
    patterns = [re.compile(text) for text in [*BUILT_IN, *ctx.config["patterns"]]]
    marker = ctx.config["replacement"]

    def redact(
        tool_ctx: ToolContext, name: str, args: dict[str, Any], result: ToolResult
    ) -> PostDecision | None:
        text = result.output if result.ok else (result.error or "")
        cleaned = text
        for pattern in patterns:
            cleaned = pattern.sub(marker, cleaned)
        if cleaned == text:
            return None  # no change: the next listener sees the result as it was
        ctx.log.info("redacted a secret from %s", name)
        if not result.ok:
            # accept() replaces the output only; for a failed call, block with the clean text.
            return PostDecision.block(cleaned)
        return PostDecision.accept(cleaned, "A credential in this result was redacted.")

    # Order -100: before listeners at the default 0, so they never see the secret.
    ctx.hooks.post_execute("redact", redact, order=-100)
```

すべてのツール呼び出し — 組み込み、MCP、ホスト、拡張 — は、実行された後、出力が切り詰められモデルに見せられる前に、このチェーンを通ります。リスナーは次のどれかを返します。

| 戻り値 | 効果 |
|---|---|
| `None` | 変更なし |
| `PostDecision.accept(output=None, *notes)` | モデルが読むテキストを置き換え（`output` を渡した場合）、モデルの次の呼び出しの前に送るメモを加えます |
| `PostDecision.block(feedback, *notes)` | 呼び出しを失敗にし、エラーを `feedback` にします。後のリスナーは動きません |

リスナーは `order` 順、同じなら登録順に動き、組み込みのリスナーはありません。例外を出したリスナーは記録して飛ばします。

### `turn_stopping`

<!-- example: examples/extensions/finish-the-job/plugin.py -->
```python
"""finish-the-job: a turn_stopping listener.

When the model is about to end its turn with an answer that still holds a
placeholder ("TODO", "FIXME", "..."), the turn goes on with one more
instruction. At most twice per turn, so a model that cannot finish is never
kept in a loop.

Order 120: after the built-in verify gate (100), which asks for a passing
check first, and before unfinished-work (150) and the Stop hooks (200). The
first listener that returns a decision wins.
"""

from __future__ import annotations

import re
from typing import Any

from snowpea_core.extensions import PluginContext, StopContext, StopDecision

PLACEHOLDER = re.compile(r"\b(TODO|FIXME)\b|<placeholder>")
MAX_PER_TURN = 2


def apply(ctx: PluginContext) -> None:
    sent: dict[tuple[str, Any], int] = {}  # (session id, turn id) -> nudges so far

    def check(core: Any, session: Any, stop: StopContext) -> StopDecision | None:
        if stop.at_budget or not PLACEHOLDER.search(stop.last_message):
            return None
        key = (session.id, session.current_turn)
        count = sent.get(key, 0)
        if count >= MAX_PER_TURN:
            return None
        sent[key] = count + 1
        if len(sent) > 1000:  # forget old turns
            sent.pop(next(iter(sent)))
        return StopDecision(
            text=(
                "Your answer still has a TODO, FIXME or placeholder. Finish that part "
                "now, or say plainly why it cannot be done."
            ),
            producer="finish-the-job",
            shown="finish-the-job: the answer still had a placeholder",
            count=count + 1,
            limit=MAX_PER_TURN,
        )

    ctx.hooks.turn_stopping("placeholders", check, order=120)
```

モデルがツールを呼ばずに答えると、ターンは終わろうとします。その前にリスナーが `order` 順に動き、最初に `StopDecision` を返したものが勝ち、その `text` が次の指示として送られます。組み込み:

| order | リスナー | ターンを続けるとき |
|---|---|---|
| 100 | `verify-gate` | コードを編集した後に通ったチェックがない、UI を一度も見ていない、TODO が残っている |
| 150 | `unfinished-work` | 答えが今すぐできる作業を並べている |
| 200 | `stop-hooks` | Claude Code の `Stop` フックが求める |

`StopContext` は `last_message`、`stop_continuations`、`at_budget`（ラウンド予算の確認への答えなので、終わらせること）を持ちます。`StopDecision(text, producer, via="message", shown="", count=None, limit=None)`: `producer` はクライアントが表示するイベントにあなたの名前を残し、`count`/`limit` で「3 回中 2 回目」のような表示ができます。一つのターンを続ける回数には**必ず上限を**設けてください。モデルが要求を満たせないこともあり、その繰り返しを止めるものはほかにありません。

## コマンド

<!-- example: examples/extensions/scratchpad/plugin.py -->
```python
"""scratchpad: a slash command that keeps notes across sessions.

    /scratch add <text>   keep a note
    /scratch              list the notes
    /scratch clear        forget them

A command runs without the model: it answers with cmd_ctx.say(). The notes
live in ctx.data_dir, outside the extension's folder, so updating the
extension keeps them.
"""

from __future__ import annotations

import json
from pathlib import Path

from snowpea_core.extensions import Command, CommandContext, PluginContext


def apply(ctx: PluginContext) -> None:
    store: Path = ctx.data_dir / "notes.json"

    def read() -> list[str]:
        try:
            return list(json.loads(store.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return []

    def write(notes: list[str]) -> None:
        store.write_text(json.dumps(notes, ensure_ascii=False), encoding="utf-8")

    async def scratch(cmd_ctx: CommandContext, args: str) -> None:
        verb, _, rest = args.strip().partition(" ")
        notes = read()
        if verb == "add" and rest.strip():
            write([*notes, rest.strip()])
            await cmd_ctx.say(f"Noted ({len(notes) + 1} in the scratchpad).")
        elif verb == "clear":
            write([])
            await cmd_ctx.say("Scratchpad cleared.")
        elif verb:
            await cmd_ctx.say("Usage: /scratch [add <text> | clear]")
        elif notes:
            await cmd_ctx.say("\n".join(f"{n}. {note}" for n, note in enumerate(notes, 1)))
        else:
            await cmd_ctx.say("The scratchpad is empty. Add a note with /scratch add <text>.")

    ctx.commands.register(
        Command(
            name="scratch",
            summary="keep, list or clear notes that last across sessions",
            run=scratch,
            args_schema={
                "type": "object",
                "properties": {
                    "arguments": {"type": "string", "description": "add <text> | clear"}
                },
            },
        )
    )
```

コマンドはモデルを呼ばない独立したターンです。`run(cmd_ctx, args)` は名前の後ろ全体を一つの文字列で受け取ります。`cmd_ctx.say(text)` は会話に答えを書き（そのテキストは会話に残り、次のターンでモデルも知ります）、`cmd_ctx.session` と `cmd_ctx.core` もあります。例外が起きるとコマンドのターンはユーザーに見えるエラーで終わります。組み込みコマンドの名前は使えず、スキルや別の拡張が使っている名前も使えません。後から同じ名前のスキルを入れるとスキルが名前を取り、拡張の次の再読み込みは名前の衝突で失敗します。

## MCP サーバー

<!-- example: examples/extensions/glossary-mcp/plugin.py -->
```python
"""glossary-mcp: an MCP server that lives as long as the extension.

The entry has the shape of one .mcp.json entry. The server starts in the
background when the extension loads; its tools appear as
mcp__glossary__define once it is up, carry the MCP permission (network unless
settings.mcp says otherwise), and go away when the extension is disabled,
reloaded or removed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from snowpea_core.extensions import PluginContext


def apply(ctx: PluginContext) -> None:
    server = Path(__file__).with_name("server.py")
    ctx.mcp.register(
        "glossary",
        {
            "command": sys.executable,  # the daemon's Python
            "args": [str(server)],
            "timeoutSec": 20,
        },
    )
```

サーバーはバックグラウンドで起動するので、`apply` は待ちません。起動に失敗したサーバーは記録され、拡張は読み込まれたままです。名前はほかのどこにも（`.mcp.json`、設定、プラグイン）宣言されていてはいけません。`snowpea mcp list` はサーバーを拡張の所有として表示し、アンロードするとツールを外してプロセスを止めます。機能がすでに MCP サーバーとしてあるならこれを、Python 数行で済むならツールを使ってください。

## 設定と `configSchema`

<!-- example: examples/extensions/redact-secrets/plugin.json -->
```json
{
  "name": "redact-secrets",
  "description": "Replaces credentials in tool output before the model reads it.",
  "version": "1.0.0",
  "engines": {"snowpea": "^1"},
  "configSchema": {
    "type": "object",
    "properties": {
      "patterns": {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "default": [],
        "description": "More regular expressions to redact."
      },
      "replacement": {
        "type": "string",
        "default": "[redacted]",
        "description": "What a match is replaced with."
      }
    },
    "additionalProperties": false
  }
}
```

設定は `settings.json` の `extensions.<名前>.config`、`enabled` の隣にあります。`apply` の前に、設定を `configSchema` で検証します（`jsonschema` パッケージが知る JSON Schema のドラフトならどれでも。`$schema` で選びます）。合わない設定では拡張は読み込まれず、エラーは `settings.extensions.redact-secrets.config.patterns[0]: '' should be non-empty` のように失敗したパスを示します。ないトップレベルのプロパティは `default` で補います。クライアントは `extension.configure` で設定を変え（検証して保存し、拡張を再読み込みします）、`extension.list` がスキーマを返すので、そこからフォームを描けます。トークンのような秘密は、そのマシンの `settings.json` が十分に非公開なときだけ設定に置いてください。

## ログとデバッグ

- `ctx.log` と `snowpea.extensions` 以下で記録したものは `$SNOWPEA_HOME/logs/daemon.log` へ行きます。拡張についてのデーモン自身の記録 — 読み込み時に何を登録したか、なぜ失敗したか（トレースバック全体付き）、例外を出したツール・セクション・プロバイダー・フック — もそこに残ります。
- `snowpea extension list` は拡張ごとの状態 — `loaded`、`unloaded`、`disabled`、`error`、`incompatible` — と、失敗ならあなたのコードを指す `Type: message (at file:line)` を表示します。
- `snowpea extension info <名前>` はフォルダー、エラー、設定、スキーマ、登録したツール・コマンド・MCP サーバーを表示します。
- `snowpea extension reload <名前>` は拡張が読み込めなかったとき終了コード 1 で終わるので、スクリプトの編集・再読み込みのループは間違いで止まります。

よくある間違い:

| 症状 | 原因 |
|---|---|
| ツールが一度も呼ばれない | 説明にいつ使うかがない、または現在のモードで権限が拒否される |
| `run must be an async function` | `async def run` ではなく `def run` |
| ツールの実行中にデーモンが止まる | イベントループ上のブロッキング処理。`asyncio.to_thread` を使ってください |
| 自分のモジュールで `ModuleNotFoundError` | `from . import helpers` ではなく `import helpers` |
| パッケージで `ModuleNotFoundError` | デーモンの Python に入っていない |
| 再読み込みしても前の動き | コードが拡張の外（コアのモジュール、グローバルなレジストリ）に状態を持っている。状態はクロージャーか自分のモジュールに |
| プロンプトのキャッシュが効かなくなった | テキストが変わる stable 帯のセクション。その事実はランタイムコンテキストのプロバイダーへ |

## 拡張をテストする

`snowpea_core.extensions.testing.load_extension` は一時フォルダーに専用のデーモンを起動し、拡張をそこへコピーし、本物のデーモンと同じマネージャーで読み込み、セッションを一つ開きます。ブロックを抜けると拡張をアンロードし、何か登録が残っていればテストを失敗させます。モデルは呼ばず、ネットワークにも何も出ません。`hello` の例に付いているテスト:

<!-- example: examples/extensions/hello/tests/test_hello.py -->
```python
"""Tests for the hello extension; run with `python -m pytest tests` in its folder.

Needs pytest and pytest-asyncio next to snowpea in the same Python.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.extensions.testing import load_extension

EXTENSION = Path(__file__).resolve().parents[1]  # the folder holding plugin.py


@pytest.mark.asyncio
async def test_greets_by_name(tmp_path: Path) -> None:
    # A private daemon in tmp_path; the extension is loaded as the real one would be.
    async with load_extension(EXTENSION, home=tmp_path) as ext:
        assert ext.registrations == ["tool:hello_greet"]
        result = await ext.call_tool("hello_greet", {"name": "Ada"})
        assert result.ok and result.output == "Hello, Ada!"
        too_long = await ext.call_tool("hello_greet", {"name": "x" * 101})
        assert not too_long.ok and "longer than 100" in too_long.error
    # Leaving the block unloads it and fails the test if anything stayed registered.
```

`load_extension(folder, *, config=None, home=None, check=True)` が渡すオブジェクト:

| メンバー | すること |
|---|---|
| `call_tool(name, args)` | エージェントループと同じようにツールを呼び、`post_execute` フックを通して `ToolResult` を返します |
| `system_prompt()` | セッションの次のモデル呼び出しが受け取るシステムプロンプト |
| `runtime_context()` | 今の `{プロバイダー名: テキスト}` |
| `stop_turn(last_message)` | `turn_stopping` チェーンの決定 |
| `run_command(name, args)` | スラッシュコマンドを実行し、言ったことを返します |
| `reload(config=None)` | 再読み込み。新しい設定も渡せます |
| `registrations`、`extension`、`session`、`core` | 登録したもの、状態とエラー、セッション、コア |

`check=True` なら読み込みの失敗で拡張のエラーを持つ `ExtensionLoadError` を出すので、悪い設定のテストは `pytest.raises(ExtensionLoadError, match=...)` になります。

## バージョン管理

`snowpea_core.extensions.API_VERSION` はこの API のメジャーバージョン（今は `1`）です。拡張が頼っているものがなくなるか意味が変わるときだけ上がります: `ctx` のメンバーや引数、`snowpea_core.extensions` が公開する名前のフィールドやメソッド、`run`・`render`・リスナーの呼び出し形、このページが約束する順序。メンバー・任意の引数・フィールド・新しい組み込みセクションの追加は該当しません。`ctx.core` と `snowpea_core.extensions` 以外のモジュールはこの約束の外です。

前提としたメジャーを `plugin.json` に `"engines": {"snowpea": "^1"}` と書いてください。API のメジャーが違うデーモンは拡張を `incompatible` と表示し（理由に両方のバージョンが出ます）、読み込みません。拡張自身のバージョンは `version` に書きます。

## 配布

拡張のマーケットプレイスはまだありません。共有するにはフォルダーを公開します — ルートに `plugin.py` がある git リポジトリ、またはフォルダーの zip — そして使う人に `$SNOWPEA_HOME/extensions/<名前>/` へ置いてもらい（例: `git clone <リポジトリ> ~/.snowpea/extensions/<名前>`）、`snowpea extension reload <名前>` を実行してもらいます。更新は `git pull` と再読み込みで、`ctx.data_dir` のデータは残ります。拡張はデーモンの権限で動くので、何をするのか、何をどこへ送るのか、各ツールがどの権限を宣言するのかを利用者に伝えてください。リポジトリの `scripts/gen_extension_index.py` は拡張フォルダーの一覧を静的な `index.json` に書き出し、一覧をホストしたい人が使えます。

## CLI と RPC のリファレンス

```bash
snowpea extension list
snowpea extension info hello --json
snowpea extension enable hello
snowpea extension disable hello
snowpea extension reload hello
snowpea extension reload
snowpea extension new hello
```

| コマンド | すること | 終了コード |
|---|---|---|
| `snowpea extension list [--json]` | すべての拡張とその状態・エラー | 0 |
| `snowpea extension info <名前> [--json]` | 拡張一つと、そのツール・コマンド・MCP サーバー・設定・スキーマ | `error` か `incompatible` なら 1 |
| `snowpea extension enable <名前>` | 有効にして読み込む | 読み込めなければ 1 |
| `snowpea extension disable <名前>` | アンロードして無効のままにする | 0 |
| `snowpea extension reload [<名前>]` | 一つ、または有効なものすべてを再読み込み | 一つでも読み込めなければ 1 |
| `snowpea extension new <名前>` | スターターテンプレートを書く。デーモン不要 | フォルダーがあれば 2 |

`new` 以外は次のメソッドでデーモンと話します（capability `extensions`）。

| メソッド | パラメーター | 結果 |
|---|---|---|
| `extension.list` | `{}` | `{extensions: ExtensionInfo[]}`。フォルダーを再スキャンします |
| `extension.enable` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.disable` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.reload` | `{name}` | `{extension: ExtensionInfo}` |
| `extension.configure` | `{name, config}` | `{extension: ExtensionInfo}`。設定が `configSchema` に合わなければ、失敗したパスを示す `invalid_params` エラー |

`ExtensionInfo`: `name`、`path`、`description`、`version`、`enabled`、`state`（`loaded`、`unloaded`、`disabled`、`error`、`incompatible`）、`error`（または null）、`effects`（持っている登録の数）、`configSchema`（または null）、`config`。完全なスキーマは [プロトコルリファレンス](../../protocol.md) にあります。

## トラブルシューティング

| 見えること | すること |
|---|---|
| `snowpea extension list` に拡張がない | フォルダーは `$SNOWPEA_HOME/extensions/` の直下にあり、`plugin.py` を含むこと。`SNOWPEA_HOME` を確認 |
| 状態が `unloaded` | デーモン起動後に追加された: `snowpea extension reload <名前>` |
| 状態が `error`、`plugin.json:…` | メッセージどおりにマニフェストを直す |
| 状態が `error`、`settings.extensions.<名前>.config…` | そのパスの設定を直すか、`extension.configure` で設定 |
| 状態が `incompatible` | 別の API メジャー向けの拡張です。合うバージョンを入手 |
| `… is already registered by builtin` | 別の名前に。組み込みのツールとコマンドは置き換えられません |
| `… is already registered by extension:<別のもの>` | 二つの拡張が同じ名前を求めています。片方の名前を変えるか、もう片方を無効に |
| ツールが `(extension <名前>) failed:` で失敗 | ツール内の例外。場所とデーモンログのトレースバックがどこかを示します |
| ツールが `timed out after` で失敗 | `register` に渡した `timeout` に達しました |
| MCP サーバーのツールが現れない | サーバーが起動しなかった理由はデーモンのログに。項目を `snowpea mcp test` で試す |
| 何も起きず、エラーもない | デーモンが `SNOWPEA_EXTENSIONS=0` で起動したか、`settings.json` で拡張が無効 |

## 次へ

[プラグイン](plugins.md) — スキル、Claude Code と Codex のプラグイン、フック、MCP サーバー。
