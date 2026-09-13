# Plugins and skills

[English](../en/plugins.md) · [한국어](../ko/plugins.md) · [すべてのページ](../README.md)

snowpea は Claude Code のプラグイン構成をそのまま読みます。Claude Code 向けに書かれたプラグインは修正なしでここにインストールされ動作しますし、すでに `.claude/` ディレクトリがあるリポジトリは何も変えずにそのまま動きます。

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

`plugin.json` の代わりに `.claude-plugin/plugin.json` も受け付けます。マーケットプレイスのリポジトリは、ルートに `plugins: [{name, source}]` を並べた `marketplace.json` を置きます。

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

ルートは次の順にスキャンされ、名前が衝突した場合は後のものが勝ちます。

1. built-ins — `core/snowpea_core/builtin_skills/`
2. global — `$SNOWPEA_HOME/{skills,agents,commands}/`
3. plugins — `$SNOWPEA_HOME/plugins/*/`
4. project — `<project>/.claude/{skills,agents,commands}/`、続いて `<project>/.snowpea/{skills,agents,commands}/`

つまり、プロジェクトのスキルは同名のプラグインのスキルを上書きし、プラグインはビルトインを上書きします。ローダーはそれぞれの出どころを記録し、`skill list` がそれを表示します。

## Next

[Scheduler](scheduler.md) — 席を外しているあいだに作業を走らせる。
