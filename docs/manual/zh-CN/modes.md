# Modes, permissions and the allowlist

[English](../en/modes.md) · [한국어](../ko/modes.md) · [全部页面](../README.md)

每个工具都带着一个权限标签。模式决定每个标签会被怎么处理。

| 标签 | 工具 |
|---|---|
| `read` | `read_file`, `list_dir`, `glob`, `grep`, `git_status`, `git_diff`, `git_log`, `process_list`, `memory_search`, `transcribe_audio` |
| `write` | `write_file`, `edit_file`, `git_commit`, `memory_write` |
| `exec` | `shell`, `process_kill`, `delegate_task` |
| `network` | `web_search`, `web_extract`, `browser_*`, 媒体工具, `text_to_speech`, 默认还有 MCP server |
| `send` | `schedule_create`, `schedule_list`, `schedule_cancel` |

## 矩阵

| 模式 | read | write | exec | network | send |
|---|---|---|---|---|---|
| **plan** | 允许 | 拒绝 | 拒绝 | 允许 | 拒绝 |
| **accept**（默认） | 允许 | 允许 | 询问 | 询问 | 询问 |
| **auto** | 允许 | 允许 | 允许 | 允许 | 允许 |

**plan** 是用来思考的。agent 可以读你的仓库、可以搜索网页，但改不了任何东西。被拒绝的调用会产生一个带 `mode_denied` 代码的 `error` 事件并结束这一回合；无界面运行则以 `4` 退出。

**accept** 是干活时的默认值，对应 Claude Code 的 acceptEdits：文件读取和编辑不经询问直接进行，而 shell 命令、网络调用以及任何会发送消息的动作都会先问一句。

**auto** 什么都不问。在你盯着看的时候用它，在一个用完就扔的容器里用它，或者用在一个你已经想清楚影响范围的定时任务上。

## 切换

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

`/mode save` 会把 `"defaultMode"` 写进 `<project>/.snowpea/settings.json`，于是这个仓库里的下一个会话就从那个模式开始。状态行始终显示当前模式。

## 审批

当策略说*询问*时，核心会创建一个审批请求并挡住那次工具调用。交互式请求只会送到发起这一回合的那个界面——你正在输入的那个 TUI 窗口，或者正在跑 `snowpea -c` 的那个终端。它们不会出现在别人的队列里。

回答会带一个作用域：

| 作用域 | 含义 |
|---|---|
| `once` | 仅这一次调用 |
| `session` | 本次会话剩下的时间里，所有匹配的调用 |
| `project` | 存进 `<project>/.snowpea/settings.json` |
| `always` | 存进 `$SNOWPEA_HOME/settings.json` |

没有人回答本身也是一种回答。超过 `approvals.timeoutSec`（默认 300 秒）之后，请求会被拒绝，回合结束。包括超时在内的每一个决定，都会追加到 `$SNOWPEA_HOME/logs/approvals.jsonl`。

由定时任务或进来的聊天消息发起的请求属于*无人值守*，行为有所不同——见 [Gateway](gateway.md)。

## allowlist

一条 allowlist 条目会把匹配命令的*询问*提升为*允许*。它永远不能提升*拒绝*，所以你在这里加的任何东西都不会削弱 plan 模式。

```
/allow ^ls( .*)?$
/allow ^git (status|diff|log)\b
/allow ^npm (run )?test$ --global
/allow tool:web_search
/allowlist
/allowlist remove 3
```

模式是一个与 shell 命令相匹配的正则表达式。`tool:<name>` 这种形式把整个工具加进 allowlist。不带 `--global` 时条目落在项目设置里；带上它则落在 `$SNOWPEA_HOME/settings.json`。

写模式时要加锚点、要写窄。`^git ` 连 `git push --force` 也一并允许了，而 `^git (status|diff|log)\b` 不会。

## 无界面与无人值守

`snowpea -c` 在有 TTY 时会在 stdin 上询问。没有 TTY 时——CI 任务、管道——没有人可问，于是审批请求会立即被拒绝，进程以 `4` 退出。如果你本来就是这个意思，就把它写明白：

```bash
snowpea -c "run the linter" --approve-none
```

对 CI 来说，诚实的组合是：只读的事情用 plan 模式，或者在一个你丢得起的容器里用 auto 模式。不要为了让开发机上的提示安静下来而伸手去拿 auto；那正是 allowlist 存在的意义。

## 下一步

[Commands](commands.md) —— 完整的命令表面。
