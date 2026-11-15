# Plugins and skills

[English](../en/plugins.md) · [한국어](../ko/plugins.md) · [全部页面](../README.md)

snowpea 直接读取 Claude Code 和 Codex 的插件目录布局。为 Claude Code 或 Codex 写的插件不用改动就能在这里安装并运行，一个已经有 `.claude/` 目录的仓库完全不需要任何改动，而 Claude Code 和 Codex 在这台机器上安装的东西也会自动出现。

## 各是什么

本页涉及三种不同的东西。

| 种类 | 格式 | 位置 | 项目文件夹可否携带 | 代码是否在守护进程内运行 |
|---|---|---|---|---|
| **技能（Skill）** | `SKILL.md` —— Claude Code 与 Codex 共用的开放标准（agentskills.io） | 主目录或项目的 `skills/` | 可以 | 否，只是说明 |
| **插件（Claude Code 格式）** | 带 `plugin.json` 或 `.claude-plugin/plugin.json` 的技能、命令、agent、hook、MCP server 打包 | `$SNOWPEA_HOME/plugins/` | 不可以，仅主目录 | 否；其 hook 与 MCP server 启动你已批准的命令 |
| **插件（Codex 格式）** | 带 `.codex-plugin/plugin.json` 的同样打包（技能、`commands/`、`agents/*.md`、`hooks.json`、`.mcp.json`）；不支持应用连接器（`.app.json`） | `$SNOWPEA_HOME/plugins/` | 不可以，仅主目录 | 否；其 hook 与 MCP server 启动你已批准的命令 |
| **扩展**（snowpea 扩展格式，Python） | 定义 `apply(ctx)` 的 `plugin.py` | `$SNOWPEA_HOME/extensions/<名称>/` | 不可以，仅主目录 | 是，守护进程内的 Python；需要信任 |

扩展参照 DeepSeek Harness 的设计，但与 DeepSeek Harness 的插件不兼容。

每个技能、命令、agent 和插件都会记录它所写成的 `format`（`snowpea`、`claude` 或 `codex`），`skill.list`、`skill.read`、`command.list` 和 `agent.list` 会带上它（协议 1.52.0）。项目指令文件 `AGENTS.md` 与 `CLAUDE.md` 一并读取。

## 从 Claude Code 和 Codex 读取的内容

除了安装到 snowpea 里的东西，每次重新加载还会读取另外两个工具在这台机器上安装的东西。这些条目在这里是只读的：`skill.read` 回答 `readOnly: true`，`skill.write` 从不写进它们的文件夹，`skill.remove` 也碰不到它们——请用安装它们的工具删除。

| | Claude Code（`$CLAUDE_CONFIG_DIR`，否则 `~/.claude`） | Codex（`$CODEX_HOME`，否则 `~/.codex`） |
|---|---|---|
| 已安装插件 | `plugins/installed_plugins.json`，跳过 `settings.json` 中 `enabledPlugins` 标为 false 的 —— 来源 `claude-plugin` | `plugins/cache/<marketplace>/<plugin>/<version>/`（每个插件的最新版本），跳过 `config.toml` 中 `[plugins."<plugin>@<marketplace>"] enabled = false` 的 —— 来源 `codex-plugin` |
| 从插件读取 | 技能、命令、agent | 技能、命令、agent；带应用连接器（`.app.json`）的插件会附注 `needs Codex apps` 列出，其技能照常加载 |
| 用户级 | `skills/`、`agents/*.md`、`commands/*.md` —— 来源 `claude-global` | `skills/<name>/`（不含 Codex 自带的 `skills/.system`）、作为命令的 `prompts/*.md`、作为 agent 定义的 `agents/*.toml` —— 来源 `codex-global` |
| 项目级 | `<project>/.claude/{skills,agents,commands}/` —— 来源 `claude-project` | `<project>/.agents/skills/` —— 来源 `codex-project` |
| 不导入 | 已安装插件的 hook 与 MCP server | 已安装插件的 hook 与 MCP server、`hooks.json`、`config.toml` 的 `[mcp_servers]` |
| 开关 | 无：文件夹存在就读取 | `skills.importCodex`（`true`）；设为 `false` 则不从 Codex 读取任何东西 |

不导入 hook 和 MCP server，是因为它们会代你执行命令，而你批准的是 Claude Code 或 Codex，不是 snowpea。若想在这里也使用，请把插件安装到 snowpea。Codex 的 agent 角色（`agents/<name>.toml`）这样变成 agent 定义：`name`、`description`、`model` 原样保留，`developer_instructions` 作为提示词，`model_reasoning_effort` 变成 `thinking`（`none`、`minimal` 为 off，其他级别为 on），`sandbox_mode = "read-only"` 变成 `permission: plan`；`nickname_candidates`、`mcp_servers` 等没有对应项，会被忽略。导入的 agent 不会占用内置角色（`explore`、`architect` 等）的名字：来自插件的仍可用 `<plugin>:<name>` 调用，没有插件的则被略去。

名字冲突时，snowpea 自己的副本胜过 Claude Code 的，Claude Code 的胜过 Codex 的。保留一条旧规则：Claude Code 的全局技能（`~/.claude/skills`）会遮住同名的 snowpea 全局技能。在 Codex 内部，项目技能胜过全局技能，全局技能胜过插件的。

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

`.claude-plugin/plugin.json`（Claude Code 格式）或 `.codex-plugin/plugin.json`（Codex 格式）也可以替代 `plugin.json`；Codex 清单的 `interface.displayName` 显示为插件的 `title`。`${CLAUDE_PLUGIN_ROOT}`、`${SNOWPEA_PLUGIN_ROOT}` 和 Codex 的 `${PLUGIN_ROOT}` 都展开为插件目录。marketplace 仓库会在根目录（或 `.claude-plugin/`）放一个 `marketplace.json`，其中列出 `plugins: [{name, source}]`；Codex 的 marketplace 把目录放在 `.agents/plugins/marketplace.json`，`source` 写作 `{"source": "local", "path": "./plugins/<name>"}`。Codex 格式的文件夹同样可以用 `snowpea skill install ./<文件夹>` 安装到 `$SNOWPEA_HOME/plugins/<name>`，`github:openai/plugins@<plugin>` 会从 Codex 的目录中只安装那一个插件。

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

各个根目录按这个顺序扫描，名字冲突时后面的赢——但来自 Claude Code、Codex 已安装插件以及 Codex 文件夹的条目，从不替换已加载的条目：

1. 内置 —— `core/snowpea_core/builtin_skills/`
2. 全局 —— `$SNOWPEA_HOME/{skills,agents,commands}/`，然后是 Claude Code 的 `~/.claude/{skills,agents,commands}/`
3. 插件 —— `$SNOWPEA_HOME/plugins/*/`，然后是 Claude Code 已安装的插件
4. 项目 —— `<project>/.claude/{skills,agents,commands}/`，然后是 `<project>/.snowpea/{skills,agents,commands}/`
5. Codex —— `<project>/.agents/skills/`，然后是 `~/.codex/{skills,prompts,agents}`，然后是 Codex 已安装的插件

所以同名时项目 skill 覆盖插件 skill，插件覆盖内置，而来自 Codex 的东西不会覆盖 snowpea 或 Claude Code 的任何东西。加载器会记录每一项来自哪里，`skill list` 会显示出来。

## 扩展

扩展（snowpea 扩展格式，Python）是 `$SNOWPEA_HOME/extensions/<名称>/` 文件夹，其中 `plugin.py` 定义 `apply(ctx)`，可选 `plugin.json`（`name`、`description`、`version`、`engines.snowpea`、`configSchema`）。它可以添加工具、提示词段落、运行时上下文、对每个工具结果的钩子、回合结束时的检查、命令和 MCP 服务器；禁用、重新加载或删除时全部撤销，无需重启。扩展在守护进程内以其权限运行，请只安装可信代码。只扫描主目录，从不扫描项目文件夹。详情与示例见[英文手册](../en/plugins.md#extensions)。

## 下一步

[Scheduler](scheduler.md) —— 在你不在的时候把活干了。
