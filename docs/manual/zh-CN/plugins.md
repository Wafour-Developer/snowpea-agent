# Plugins and skills

[English](../en/plugins.md) · [한국어](../ko/plugins.md) · [全部页面](../README.md)

snowpea 直接读取 Claude Code 的插件目录布局。为 Claude Code 写的插件不用改动就能在这里安装并运行，而一个已经有 `.claude/` 目录的仓库则完全不需要任何改动。

## 布局

```
my-plugin/
  plugin.json            # {"name": "...", "version": "...", "description": "..."}
  skills/<name>/SKILL.md # a skill, which becomes a /<name> command
  agents/*.md            # agent definitions, usable as delegate_task targets
  commands/*.md          # plain markdown commands
  hooks/hooks.json       # PreToolUse / PostToolUse / Stop
  .mcp.json              # MCP servers this plugin brings
```

`.claude-plugin/plugin.json` 也可以替代 `plugin.json`。marketplace 仓库会在根目录放一个 `marketplace.json`，其中列出 `plugins: [{name, source}]`。

## 安装

```bash
snowpea skill install ./my-plugin
snowpea skill install https://github.com/someone/their-plugin.git
snowpea skill install oh-my-claudecode
snowpea skill list --json
snowpea skill remove my-plugin
```

安装会把插件复制到 `$SNOWPEA_HOME/plugins/<name>` 并重新加载注册表。一次重新加载会发出 `commands.changed` 通知，因此 TUI 不用重启就能刷新它的命令面板。

## 搜索

```bash
snowpea skill search "pdf"
snowpea skill search "code review" --json
```

三个来源会被同时查询，每一条命中都带着它来自哪个 `source`：`claude-marketplace`（每个已注册 marketplace 仓库的 `marketplace.json`）、`agentskills.io` 和 `hermes-hub`。某个来源失败时只是不贡献结果，而不会让整次搜索失败。把命中项的安装标识直接喂回 `skill install` 即可。

已注册的 marketplace 存放在 `$SNOWPEA_HOME/marketplaces.json` 中，初始已经写入了 oh-my-claudecode 这个 marketplace。

## SKILL.md

前置元数据遵循 [agentskills.io](https://agentskills.io) 标准：

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

正文会成为一条 `/changelog` 命令。`$ARGUMENTS` 会被替换成命令后面跟着的内容，正文作为一条指令注入，然后一个回合开始。`allowed-tools` 在那一回合内是强制生效的——一个只列了读工具的 skill 就写不了东西，无论模式允许什么。不写它的话，skill 拿到的是会话正常的工具集。`user-invocable: false` 让一个 skill 不出现在命令列表里，但仍可供 agent 使用。

## Agent 定义

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

把它放进 `<project>/.snowpea/agents/reviewer.md`，或者让 `/agent create "reviews diffs for missing tests"` 帮你写一个。无论哪种方式，它都会成为一个 `delegate_task` 目标，并且 `snowpea agents --json` 会列出它。

## Hook

`hooks/hooks.json` 用的是 Claude Code 的形状：

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "shell|write_file", "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/guard.sh"}]}
    ]
  }
}
```

这个命令会从 stdin 收到 JSON 形式的 `{event, tool_name, tool_input, session_id, cwd}`，环境变量中还有 `SNOWPEA_HOME` 和 `SNOWPEA_TOOL_NAME`。`PreToolUse` 在权限裁定之后、工具执行之前运行；退出状态 2 会阻止这次调用，它的 stderr 会变成模型看到的错误，于是这一回合会带着一次拒绝继续下去，而不是直接死掉。`PostToolUse` 在工具返回后立即运行，`Stop` 则在一个回合结束且不再有工具调用时运行。其他 hook 事件会被解析并忽略。

`${CLAUDE_PLUGIN_ROOT}`、`${SNOWPEA_PLUGIN_ROOT}` 和 `${SNOWPEA_PYTHON}` 会在 hook 和 MCP 命令中展开，带不带花括号都行。

## MCP server

`.mcp.json` 使用 Claude Code 的格式，并且会从 `$SNOWPEA_HOME`、项目目录，以及每一个已安装的插件中读取：

```json
{
  "mcpServers": {
    "notes": {"command": "${SNOWPEA_PYTHON}", "args": ["-m", "my_notes_server"]},
    "remote": {"url": "https://example.internal/mcp"}
  }
}
```

server 会惰性启动，在守护进程的生命周期内被缓存，它们的工具以 `mcp__<server>__<tool>` 的形式出现：

```bash
snowpea tools list --json
```

权限默认按 server 为 `network`，可以在设置的 `mcp.permissions` 下覆盖。

## 东西在哪里被找到，以及谁赢

各个根目录按这个顺序扫描，名字冲突时后面的赢：

1. 内置 —— `core/snowpea_core/builtin_skills/`
2. 全局 —— `$SNOWPEA_HOME/{skills,agents,commands}/`
3. 插件 —— `$SNOWPEA_HOME/plugins/*/`
4. 项目 —— `<project>/.claude/{skills,agents,commands}/`，然后是 `<project>/.snowpea/{skills,agents,commands}/`

所以同名时项目 skill 覆盖插件 skill，插件覆盖内置。加载器会记录每一项来自哪里，`skill list` 会显示出来。

## 下一步

[Scheduler](scheduler.md) —— 在你不在的时候把活干了。
