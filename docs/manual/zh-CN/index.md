# snowpea manual

[English](../en/index.md) · [한국어](../ko/index.md) · [全部页面](../README.md)

snowpea 是一个跑在你自己机器上的开源多供应商编码 agent。一个 Python 核心作为本地守护进程常驻，拥有所有有状态的东西——会话、工具、权限、记忆、调度、消息平台绑定。客户端通过一套有文档的 WebSocket JSON-RPC 协议连接到它：今天是一个 Ink 终端 UI，v0.2 会有一个 Electron IDE，以及你用 TypeScript SDK 做出来的任何东西。

## 从哪里开始

如果你刚装好 snowpea，先读 [Install](install.md)，再读 [Setup](setup.md)，然后是 [Modes](modes.md)。日常使用有这些就够了。之后的内容都是可选的表层。

| 文档 | 什么时候读它 |
|---|---|
| [Install](install.md) | 安装、升级，或者某一步安装失败时 |
| [Setup](setup.md) | 选供应商、填 API 密钥、用浏览器登录、挑搜索和浏览器供应商时 |
| [Modes](modes.md) | agent 问得太多，或者问得太少时 |
| [Terminal UI](tui.md) | 按键、面板、附件、语音，以及屏幕在告诉你什么 |
| [Commands](commands.md) | 想要斜杠命令和 CLI 子命令的完整列表时 |
| [Attachments and voice](voice.md) | 在 prompt 里发图片和文件，用说的下指令并让它说回来 |
| [Plugins](plugins.md) | 安装或编写 skill、agent、命令、hook 和 MCP server 时 |
| [Scheduler](scheduler.md) | 希望工作在你不在的时候照常发生时 |
| [Gateway](gateway.md) | 想从 Telegram、Discord 或 Slack 跟 agent 对话时 |
| [Backends](backends.md) | 代码在容器里或者在另一台主机上时 |
| [Headless](headless.md) | 用脚本驱动 snowpea，或者把它接进 CI 时 |
| [Protocol](protocol.md) | 要针对守护进程写一个客户端时 |

## 它长什么样

```text
snowpea              → starts the daemon if needed, attaches the terminal UI
snowpea -c "..."     → one headless turn, no UI, deterministic exit code
snowpea <subcommand> → inspect or configure without opening a session
```

守护进程是懒的。它在第一个客户端连上时启动，并在空闲一段时间后自行关闭——但只有在没有任何理由继续活着时才会：没有打开的会话，没有启用的任务，没有 gateway 绑定，没有已命名的 agent。是哪一项把它留着，`snowpea daemon status` 会告诉你。

```bash
snowpea daemon status
```

## 东西都放在哪里

| 路径 | 内容 |
|---|---|
| `$SNOWPEA_HOME`（默认 `~/.snowpea`） | 所有全局性的东西 |
| `$SNOWPEA_HOME/settings.json` | 供应商、搜索与浏览器选择、工具类别、超时 |
| `$SNOWPEA_HOME/credentials.json` | bot token 和密钥，权限 `0600` |
| `$SNOWPEA_HOME/daemon.json` | 端口、pid、token、启动时间、协议版本 |
| `$SNOWPEA_HOME/state.db` | 会话、事件、记忆、任务、team 任务、已命名的 agent |
| `$SNOWPEA_HOME/logs/` | `daemon.log`、`approvals.jsonl` |
| `$SNOWPEA_HOME/plugins/`、`skills/`、`agents/`、`commands/` | 安装的以及手写的扩展 |
| `<project>/.snowpea/settings.json` | 这个仓库的默认模式、allowlist、backend |
| `<project>/.snowpea/{skills,agents,commands}/` | 项目本地的扩展 |
| `<project>/.claude/{skills,agents,commands}/` | 为兼容 Claude Code 而读取的位置 |

`SNOWPEA_HOME` 在任何地方都被尊重，所以想要第二套互相隔离的安装，只差一个环境变量：

```bash
SNOWPEA_HOME=/tmp/snowpea-scratch snowpea daemon status
```

## 获取帮助

UI 里的 `/help` 会列出核心当前拥有的每一条命令，包括你的插件加进来的那些。在 shell 中，`snowpea commands list` 打印同一份注册表，而 `snowpea tools list` 打印各个工具连同它们的权限标签以及是否处于激活状态。

```bash
snowpea commands list
snowpea tools list --json
```

Bug 和问题请提到 [GitHub issues](https://github.com/Wafour-Developer/snowpea-agent/issues)。如果你想改代码，[Contributing](../../CONTRIBUTING.md) 和 [Architecture](../../ARCHITECTURE.md) 是最先要读的两份文档。
