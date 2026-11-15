# Plugins and skills

[English](../en/plugins.md) · [한국어](../ko/plugins.md) · [すべてのページ](../README.md)

snowpea は Claude Code と Codex のプラグイン構成をそのまま読みます。Claude Code 向けや Codex 向けに書かれたプラグインは修正なしでここにインストールされ動作し、すでに `.claude/` ディレクトリがあるリポジトリは何も変えずにそのまま動き、このマシンに Claude Code と Codex がインストールしたものも自動的に見えます。

## 何が何か

このページには別々の 3 種類が出てきます。

| 種類 | 形式 | 置き場所 | プロジェクトフォルダーに置けるか | デーモン内でコードが動くか |
|---|---|---|---|---|
| **スキル** | `SKILL.md` — Claude Code と Codex が共有するオープン標準（agentskills.io） | ホームまたはプロジェクトの `skills/` | はい | いいえ（指示のみ） |
| **プラグイン（Claude Code 形式）** | `plugin.json` または `.claude-plugin/plugin.json` を持つスキル・コマンド・エージェント・フック・MCP サーバーのバンドル | `$SNOWPEA_HOME/plugins/` | いいえ（ホームのみ） | いいえ。フックと MCP サーバーは承認したコマンドを起動 |
| **プラグイン（Codex 形式）** | `.codex-plugin/plugin.json` を持つ同じバンドル（スキル、`commands/`、`agents/*.md`、`hooks.json`、`.mcp.json`）。アプリコネクタ（`.app.json`）は非対応 | `$SNOWPEA_HOME/plugins/` | いいえ（ホームのみ） | いいえ。フックと MCP サーバーは承認したコマンドを起動 |
| **拡張**（snowpea 拡張形式、Python） | `apply(ctx)` を定義した `plugin.py` | `$SNOWPEA_HOME/extensions/<名前>/` | いいえ（ホームのみ） | はい、デーモン内の Python。信頼が必要 |

拡張は DeepSeek Harness の設計を手本にしていますが、DeepSeek Harness のプラグインとは互換性がありません。

すべてのスキル・コマンド・エージェント・プラグインは書かれた `format`（`snowpea`、`claude`、`codex`）を記録し、`skill.list`、`skill.read`、`command.list`、`agent.list` がそれを返します（プロトコル 1.52.0）。プロジェクトの指示ファイル `AGENTS.md` は `CLAUDE.md` と並んで読まれます。

## Claude Code と Codex から読み込むもの

snowpea にインストールしたもののほか、再読み込みのたびに、ほかの 2 つのツールがこのマシンにインストールしたものを読みます。これらはここでは読み取り専用です。`skill.read` は `readOnly: true` を返し、`skill.write` はそのフォルダーに書かず、`skill.remove` も届きません。削除はインストールしたツールで行ってください。

| | Claude Code（`$CLAUDE_CONFIG_DIR`、なければ `~/.claude`） | Codex（`$CODEX_HOME`、なければ `~/.codex`） |
|---|---|---|
| インストール済みプラグイン | `plugins/installed_plugins.json`。`settings.json` の `enabledPlugins` が false のものは除く — ソース `claude-plugin` | `plugins/cache/<marketplace>/<plugin>/<version>/`（各プラグインの最新版）。`config.toml` の `[plugins."<plugin>@<marketplace>"] enabled = false` は除く — ソース `codex-plugin` |
| プラグインから読むもの | スキル、コマンド、エージェント | スキル、コマンド、エージェント。アプリコネクタ（`.app.json`）を持つプラグインは `needs Codex apps` の注記付きで表示され、スキルはそのまま読み込まれる |
| ユーザーレベル | `skills/`、`agents/*.md`、`commands/*.md` — ソース `claude-global` | `skills/<name>/`（Codex 自身の `skills/.system` は除く）、コマンドとして `prompts/*.md`、エージェント定義として `agents/*.toml` — ソース `codex-global` |
| プロジェクトレベル | `<project>/.claude/{skills,agents,commands}/` — ソース `claude-project` | `<project>/.agents/skills/` — ソース `codex-project` |
| 取り込まないもの | インストール済みプラグインのフックと MCP サーバー | インストール済みプラグインのフックと MCP サーバー、`hooks.json`、`config.toml` の `[mcp_servers]` |
| スイッチ | なし（フォルダーがあれば常に読む） | `skills.importCodex`（`true`）。`false` なら Codex から何も読まない |

フックと MCP サーバーを取り込まないのは、それらがあなたの代わりにコマンドを実行するもので、あなたが承認したのは snowpea ではなく Claude Code や Codex に対してだからです。ここでも使いたい場合は、プラグインを snowpea にインストールしてください。Codex のエージェントロール（`agents/<name>.toml`）は次のようにエージェント定義になります。`name`・`description`・`model` はそのまま、`developer_instructions` はプロンプト、`model_reasoning_effort` は `thinking`（`none`・`minimal` は off、それ以外は on）、`sandbox_mode = "read-only"` は `permission: plan`。`nickname_candidates`、`mcp_servers` などは対応がなく無視されます。取り込んだエージェントが組み込みロール（`explore`、`architect` など）の名前を奪うことはありません。プラグイン由来のものは `<plugin>:<name>` として残り、プラグインのないものは除外されます。

名前が衝突した場合、snowpea 自身のものが Claude Code のものに、Claude Code のものが Codex のものに勝ちます。従来の規則が 1 つ残ります。Claude Code のグローバルスキル（`~/.claude/skills`）は同名の snowpea グローバルスキルを隠します。Codex の中では、プロジェクトのスキルがグローバルに、グローバルがプラグインのものに勝ちます。

## Layout

```
my-plugin/
  plugin.json            # {"name": "...", "version": "...", "description": "..."}
  skills/<name>/SKILL.md # a skill, which becomes a /<name> command
  agents/*.md            # agent definitions, usable as delegate_task targets
  commands/*.md          # plain markdown commands
  hooks/hooks.json       # PreToolUse / PostToolUse / Stop
  .mcp.json              # MCP servers this plugin brings
```

`plugin.json` の代わりに `.claude-plugin/plugin.json`（Claude Code 形式）や `.codex-plugin/plugin.json`（Codex 形式）も受け付けます。Codex のマニフェストの `interface.displayName` はプラグインの `title` として表示されます。`${CLAUDE_PLUGIN_ROOT}`、`${SNOWPEA_PLUGIN_ROOT}`、Codex の `${PLUGIN_ROOT}` はどれもプラグインのディレクトリに展開されます。マーケットプレイスのリポジトリは、ルート（または `.claude-plugin/`）に `plugins: [{name, source}]` を並べた `marketplace.json` を置きます。Codex のマーケットプレイスはカタログを `.agents/plugins/marketplace.json` に置き、`source` は `{"source": "local", "path": "./plugins/<name>"}` の形です。Codex 形式のフォルダーも `snowpea skill install ./<フォルダー>` で `$SNOWPEA_HOME/plugins/<name>` にインストールでき、`github:openai/plugins@<plugin>` は Codex のカタログからそのプラグインだけをインストールします。

## Installing

```bash
snowpea skill install ./my-plugin
snowpea skill install https://github.com/someone/their-plugin.git
snowpea skill install oh-my-claudecode
snowpea skill list --json
snowpea skill remove my-plugin
```

インストールはプラグインを `$SNOWPEA_HOME/plugins/<name>` にコピーし、レジストリを再読み込みします。再読み込みは `commands.changed` 通知を発行するので、TUI は再起動なしでパレットを更新します。

## Searching

```bash
snowpea skill search "pdf"
snowpea skill search "code review" --json
```

3つのソースがまとめて照会され、ヒットにはそれぞれ由来の `source` が付きます。`claude-marketplace`（登録済みの各マーケットプレイスリポジトリの `marketplace.json`）、`agentskills.io`、`hermes-hub` です。失敗したソースは検索全体を失敗させるのではなく、何も寄与しないだけです。ヒットのインストール指定は、そのまま `skill install` に渡せます。

登録済みのマーケットプレイスは `$SNOWPEA_HOME/marketplaces.json` にあり、oh-my-claudecode のマーケットプレイスが初期値として入っています。

## SKILL.md

フロントマターは [agentskills.io](https://agentskills.io) の標準に従います。

```markdown
---
name: changelog
description: Write a release changelog from the commits since the last tag.
argument-hint: "<tag>"
allowed-tools: [git_log, git_diff, read_file, write_file]
---

Collect the commits since $ARGUMENTS. Group them by type, drop noise,
and write CHANGELOG.md with the newest release on top.
```

本文が `/changelog` コマンドになります。`$ARGUMENTS` はコマンドの後ろに続いた文字列に置換され、本文は指示として注入され、ターンが始まります。`allowed-tools` はそのターンのあいだ強制されます。読み取り系のツールだけを列挙したスキルは、モードが何を許していようと書き込めません。省略すると、スキルはセッション通常のツールセットを得ます。`user-invocable: false` は、スキルをコマンド一覧から外しつつ、エージェントからは使えるままにします。

## Agent definitions

```markdown
---
name: reviewer
description: Reviews a diff for correctness and missing tests.
model: inherit
tools: [read_file, grep, git_diff]
permission: plan
max_turns: 12
---

You review changes. Be specific, cite file and line, and say APPROVE or
list what must change. Never edit files yourself.
```

これを `<project>/.snowpea/agents/reviewer.md` に置くか、`/agent create "reviews diffs for missing tests"` に書かせてください。どちらでも `delegate_task` の対象になり、`snowpea agents --json` に並びます。

## Hooks

`hooks/hooks.json` は Claude Code の形を使います。

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "shell|write_file", "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/guard.sh"}]}
    ]
  }
}
```

コマンドは標準入力から JSON として `{event, tool_name, tool_input, session_id, cwd}` を受け取り、環境変数には `SNOWPEA_HOME` と `SNOWPEA_TOOL_NAME` が入ります。`PreToolUse` は権限の判定のあと、ツールの直前に走ります。終了ステータス 2 は呼び出しをブロックし、その標準エラー出力がモデルの見るエラーになるので、ターンは死なずに拒否として続きます。`PostToolUse` はツールが戻った直後、`Stop` はそれ以上ツール呼び出しがないままターンが終わったときに走ります。それ以外のフックイベントは解析されたうえで無視されます。

`${CLAUDE_PLUGIN_ROOT}`、`${SNOWPEA_PLUGIN_ROOT}`、`${SNOWPEA_PYTHON}` は、波括弧の有無にかかわらず、フックと MCP のコマンドの中で展開されます。

## MCP servers

`.mcp.json` は Claude Code の形式を使い、`$SNOWPEA_HOME`、プロジェクトのディレクトリ、インストール済みの各プラグインから読み込まれます。

```json
{
  "mcpServers": {
    "notes": {"command": "${SNOWPEA_PYTHON}", "args": ["-m", "my_notes_server"]},
    "remote": {"url": "https://example.internal/mcp"}
  }
}
```

サーバーは遅延起動し、デーモンが生きているあいだキャッシュされ、そのツールは `mcp__<server>__<tool>` として現れます。

```bash
snowpea tools list --json
```

権限はサーバーごとにデフォルトで `network` となり、設定の `mcp.permissions` で上書きできます。

## Where things are found, and who wins

ルートは次の順にスキャンされ、名前が衝突した場合は後のものが勝ちます。ただし Claude Code・Codex のインストール済みプラグインと Codex のフォルダーから来たものは、すでに読み込まれた項目を置き換えません。

1. built-ins — `core/snowpea_core/builtin_skills/`
2. global — `$SNOWPEA_HOME/{skills,agents,commands}/`、続いて Claude Code の `~/.claude/{skills,agents,commands}/`
3. plugins — `$SNOWPEA_HOME/plugins/*/`、続いて Claude Code のインストール済みプラグイン
4. project — `<project>/.claude/{skills,agents,commands}/`、続いて `<project>/.snowpea/{skills,agents,commands}/`
5. Codex — `<project>/.agents/skills/`、続いて `~/.codex/{skills,prompts,agents}`、続いて Codex のインストール済みプラグイン

つまり、プロジェクトのスキルは同名のプラグインのスキルを上書きし、プラグインはビルトインを上書きし、Codex 由来のものは snowpea や Claude Code のものを上書きしません。ローダーはそれぞれの出どころを記録し、`skill list` がそれを表示します。

## 拡張

拡張（snowpea 拡張形式、Python）は `$SNOWPEA_HOME/extensions/<名前>/` フォルダーで、`apply(ctx)` を定義した `plugin.py` と、任意で `plugin.json` を置きます。ツール、プロンプトのセクション、実行時コンテキスト、すべてのツール結果へのフック、ターン終了時のチェック、コマンド、MCP サーバーを追加でき、無効化・再読み込み・削除で再起動なしにすべて元に戻ります。読み込みに失敗した拡張はファイルと行番号付きのエラーを示し、何も残しません。デーモン内でその権限で動くため、信頼できるコードだけを入れてください。スキャンするのはホームフォルダーのみです。`snowpea extension new|list|info|enable|disable|reload` で管理します。書き方は開発者ガイド [拡張を書く](extensions.md) を参照してください。

## Next

[Scheduler](scheduler.md) — 席を外しているあいだに作業を走らせる。
