# Commands

[English](../en/commands.md) · [한국어](../ko/commands.md) · [全部页面](../README.md)

命令分为两个层面。斜杠命令在会话内运行，由核心（core）负责实现，因此同一个 `/ralph` 在 TUI、`snowpea -c`、定时任务，以及 Telegram 消息中的行为完全一致。CLI 子命令则用于在不开启会话的情况下查看和配置守护进程。

```bash
snowpea commands list
snowpea commands list --json
```

这会打印实时的命令注册表，包括已安装插件贡献的命令。UI 中的 `/help` 打印的是同一份内容。

## 斜杠命令

### 会话与模式

| Command | What it does |
|---|---|
| `/help` | list every available command |
| `/tools` | list registered tools with category, permission and state |
| `/compact [instructions]` | summarise the conversation so far and continue with the summary |
| `/plan`, `/accept`, `/auto` | switch mode |
| `/mode [plan\|accept\|auto\|save\|show]` | show, switch, or save the project default |
| `/approvals` | list unattended approvals waiting for an answer |
| `/allow <regex> [--global]` | promote a repeated prompt to a silent allow |
| `/allowlist [remove <id>]` | show or prune the allowlist |
| `/backend [local\|docker\|ssh] [json]` | show or change where tools execute |

### Terminal UI

下面这些由终端界面自己处理，而不是核心，因此它们不会出现在 `snowpea commands list` 里，也只在有人坐在前面的会话中有效。屏幕上会发生什么见 [Terminal UI](tui.md)。

| Command | What it does |
|---|---|
| `/resume` | reopen the session this directory was last in, and replay it |
| `/model` | pick a model or profile from a list; `/model <ref>` switches directly |
| `$<agent> <prompt>` | hand one prompt to a named agent |
| `/attach <path>` | attach a file to the next prompt |
| `/voice` | arm voice input; `Ctrl+Space` then records |
| `/rec` | start or stop recording, same as `Ctrl+Space` |
| `/tts on\|off` | speak each reply as it finishes |
| `/update` | take the offered upgrade, same as `U` |

### 工作类

| Command | What it does |
|---|---|
| `/ralph <task>` | PRD loop: stories with acceptance criteria, implement, verify, review until APPROVE |
| `/ultrawork <task>` | split into independent parts, run them on concurrent subagents, merge the reports |
| `/deepinit [path]` | walk the repository and write hierarchical `AGENTS.md` files |
| `/team <n> <task>` | n workers, one git worktree each, branches merged as tasks finish |
| `/team create <name> <agent...>` | create a project team from existing agents and activate it |
| `/team use <name>` / `/team list` | switch the active project team or list teams |
| `/deep-interview <idea>` | Socratic interview that scores ambiguity and refuses to hand off until the spec holds |
| `/deep-research <topic>` | multi-source web research fanned out over subagents, answered with citations |
| `/ralplan <task>` | consensus planning — planner, architect and critic argue before any code is written |

`/ralph` 把状态保存在 `<project>/.snowpea/ralph/` 下的 `prd.json` 和 `progress.md` 中，因此你可以读到它自认为正在做什么，并且在无法收敛时会在 `ralph.max_iterations`（10）处停止。`/ultrawork` 和 `/deepinit` 会在 `agents.max_concurrent`（默认 3）的限制下并发展开。后三个是位于 `core/snowpea_core/builtin_skills/` 下的 `SKILL.md` 文件，使用的是与你自己的 skill 相同的加载器——你可以阅读它们、复制它们、修改它们。

### 生成器

| Command | What it does |
|---|---|
| `/agent create "<description>"` | write an agent definition into `<project>/.snowpea/agents/<name>.md` |
| `/agent list` | list agent definitions |
| `/skill learn [name]` | turn the session you just finished into `<project>/.snowpea/skills/<name>/SKILL.md` |

生成出来的 agent 会立即成为一个可用的 `delegate_task` 目标，无需重新加载。

### 调度

| Command | What it does |
|---|---|
| `/schedule "<spec>" "<task>" [--channel X] [--mode M]` | register a job |
| `/schedule` | list jobs, or cancel one |

## CLI 子命令

### 查看信息

```bash
snowpea --version
snowpea tools list --json
snowpea commands list --json
snowpea agents --json
snowpea daemon status --json
```

`tools list` 和 `commands list` 各自只调用一个 RPC 方法然后退出。它们不会创建会话，也不会调用模型，这使它们成为安装完成后或 CI 中最合适的冒烟测试。`tools list` 还会为那些有后端供应商的工具打印出它们的供应商，因此 `web_search` 会显示实际会来应答的那个搜索供应商。

### 上下文

```bash
snowpea session context --json
snowpea session compact s-abc123 "keep the API design decisions"
```

`session context` 为每个活着的会话打印一行：已用 token、模型的上下文窗口，以及两者之间的百分比。守护进程无法确定的窗口会打印成 `?` 而不是一个猜测——所有托管供应商都在一张静态表里，本地的 vLLM 或 Ollama server 会被问一次然后缓存，而 `settings.json` 中的 `providers.<vendor>.context_window` 比两者都优先。

压缩让长会话留在那个窗口之内。`/compact` 把到目前为止的一切总结成一条 “Session summary” 系统消息，原样保留最后几条消息，并从那里继续；`session compact` 是在 shell 里做同一件事。当某个回合将要超过窗口的 `context.autoCompactPercent`（默认 85）时，它也会自行发生，时机是在回合之间，绝不会在一个工具循环的中途。把 `context.autoCompact` 设为 `false` 就只留给 `/compact` 来做。

### 会话

```bash
snowpea session list
snowpea session list --include-closed --workdir ~/src/api --json
snowpea session delete s-abc123
snowpea session clear --all
```

`session list` 打印活着的会话，加上 `--include-closed` 则连已保存的一起打印——id、模式、创建时间、工作目录，以及每个会话最后看到的那条 prompt。`session delete` 和 `session clear` 会连同附件和语音文件一起删除已保存的会话；活着的会话绝不会被删除，所以请先关掉它。从 `session list` 里挑一个 id，用 `snowpea -c "…" --resume <id>` 接着它继续。

### 模型 profile

```bash
snowpea model profiles --json
snowpea model default fast
snowpea model assign executor deep
```

`model profiles` 打印 `models.profiles`，其中默认项被标出，另外还有 `agents.models` 里按 agent 的分配。`model assign` 把一个 agent 路由到一个 profile；如果 profile id 不存在，守护进程会拒绝。

### 搜索

```bash
snowpea search test "snowpea agent github"
snowpea search test "snowpea agent github" --json
```

`search test` 用配置好的供应商跑一次真实查询，并打印出是哪个供应商应答的，以及每个被跳过的供应商退出的原因——缺少 API 密钥、实例 URL 没设置、HTTP 错误。它不需要守护进程：它直接读 `$SNOWPEA_HOME/settings.json`。有供应商应答时退出码为 0，一个都不行时为 2。

### 守护进程

```bash
snowpea daemon status
snowpea daemon start
snowpea daemon stop
```

`status` 会打印端口、pid、运行时长、四个保活计数器——会话数、任务数、gateway 绑定数、已命名的 agent 数——以及守护进程是否打算退出，并给出它不退出的原因。

### 供应商

```bash
snowpea provider list
snowpea provider login openai
snowpea setup --vendor deepseek --key sk-...
```

### Skill 与插件

```bash
snowpea skill list
snowpea skill search "pdf"
snowpea skill install oh-my-claudecode
snowpea skill install ./my-plugin
snowpea skill remove my-plugin
```

### 任务

```bash
snowpea job schedule --at "0 9 * * *" --task "summarize yesterday's commits" --channel telegram:123456
snowpea job schedule --in 10m --task "check the build" --mode plan
snowpea job list --json
snowpea job run <job-id>
snowpea job cancel <job-id>
```

`--at`、`--in`、`--every`、`--cron` 和 `--spec` 是同一个选项的五种叫法；挑一个对你要写的调度来说读起来最顺的即可。

### Gateway

```bash
snowpea gateway bind telegram TELEGRAM_BOT_TOKEN agent:scribe --user 987654
snowpea gateway list --json
snowpea gateway unbind <binding-id>
```

### Team 与服务

```bash
snowpea team status
snowpea team list
snowpea team create delivery architect executor verifier
snowpea team use delivery
snowpea team delete delivery
snowpea service install
snowpea service status
snowpea service uninstall
```

`team status` 会报告运行中的 team 里每个任务的状态和重试次数。`team list`、`create`、`use` 和 `delete` 管的则是可复用的 agent 名单：就是 `/team create` 写进 `<workdir>/.snowpea/settings.json` 的那些项目 team，它们会和全局 team 一起列出，并标出当前激活的那一个。`team delete` 只删除项目 team。`service` 会把守护进程注册为开机自启——在 Linux 上是 systemd 用户单元，在 macOS 上是 launchd agent，在 Windows 上是计划任务。它默认是关闭的，只有当你希望调度和 gateway 在无人登录终端的情况下也能挺过重启时才需要它。

### 全局选项

| Option | Meaning |
|---|---|
| `--version` | print the version and exit |
| `--home DIR` | override `SNOWPEA_HOME` for this invocation |
| `--mode plan\|accept\|auto` | mode for the session being started |
| `-c`, `--prompt TEXT` | run one headless turn and exit |
| `--json` | emit JSON Lines instead of prose |
| `--cwd DIR` | working directory of the session |
| `--timeout SEC` | abort the turn after SEC seconds |
| `--provider VENDOR` | vendor for this session |
| `--resume SESSION_ID` | continue a saved session instead of opening a new one |
| `--approve-none` | deny every approval instead of prompting |

## 以无界面方式运行斜杠命令

由于命令注册表存在于核心中，斜杠命令本身就是一个合法的无界面 prompt：

```bash
snowpea -c "/ralph add a failing test then make it pass" --mode auto
snowpea -c "/deepinit" --json
```

CLI 不会解析它，而是把文本原样交给核心，由核心按照 TUI 会做的方式去分发。

## 下一步

[Plugins](plugins.md) —— 添加属于你自己的命令。
