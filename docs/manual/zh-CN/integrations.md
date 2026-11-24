# CLI 钩子集成（beta）

当 Claude Code、Codex、Cursor agent 或 Antigravity（agy）在 snowpea 终端中运行时，snowpea 需要知道它在做什么：是哪一个会话，是在工作、已完成，还是在等你回答。这些 CLI 在相应事件发生时可以调用钩子（hook）。集成会把一个很小的 snowpea 钩子脚本注册到每个 CLI 自己的钩子配置中，让 CLI 把状态告诉 snowpea。

在 snowpea 终端之外，钩子什么也不做。安装集成不会改变你自己直接运行 CLI 时的行为。

本功能处于 beta 阶段，[终端会话](terminals.md)同样如此。

其他语言: [English](../en/integrations.md) · [한국어](../ko/integrations.md) · [全部页面](../README.md)

相关: [Terminal sessions（beta）](terminals.md) · [External sessions（beta）](../en/external-sessions.md)

## 命令

```bash
snowpea integration status                  # 四个 CLI 全部
snowpea integration install claude          # 一个 CLI
snowpea integration install all             # 所有存在配置文件夹的 CLI
snowpea integration uninstall codex
snowpea integration status agy --json
```

`install`、`uninstall`、`status` 接受 `claude`、`codex`、`cursor`、`agy` 或 `all`（默认）。它们直接操作文件，因此不需要守护进程正在运行。

## 每个 CLI 会修改的文件

| CLI | 配置文件夹（可用环境变量覆盖，默认值） | 文件 | 事件，以及 snowpea 获知的内容 |
|---|---|---|---|
| Claude Code | `$CLAUDE_CONFIG_DIR`，否则 `~/.claude` | `settings.json` | `SessionStart`（会话 id；在 startup、resume、clear、compact、fork 时）· `UserPromptSubmit` → working · `Stop` → done · `Notification` 为 `permission_prompt` 或 `elicitation_dialog` 时 → blocked |
| Codex | `$CODEX_HOME`，否则 `~/.codex` | `hooks.json`、`config.toml` | `SessionStart`（会话 id）· `UserPromptSubmit` → working · `Stop` → done。若 `config.toml` 中 `[features]` 的 `hooks` 标志未开启，则将其开启 |
| Cursor agent | `$CURSOR_CONFIG_DIR`，否则 `~/.cursor` | `hooks.json` | `sessionStart`（会话 id）· `stop` → done |
| Antigravity（agy） | `$ANTIGRAVITY_CLI_CONFIG_DIR`，否则 `~/.gemini/config` | `hooks.json` 中的 `snowpea` 块 | `PreInvocation`（会话 id） |

每次安装还会修改两个 snowpea 文件：

- `$SNOWPEA_HOME/hooks/snowpea-agent-state.sh`（Windows 上为 `.ps1`）：四个 CLI 共用的钩子脚本。只有设置了 `SNOWPEA_TERMINAL_ID` 时它才会工作，此时把 CLI 的事件传给 snowpea 核心。
- `$SNOWPEA_HOME/integrations/state.json`：记录 snowpea 对每个 CLI 具体添加了什么，卸载时据此还原。

升级 snowpea 后重新安装，钩子脚本会被覆盖。请把你自己的钩子放在其他文件中，不要放在这个文件里。

只有 Claude Code 会通过钩子报告 blocked 状态。对于 Codex、Cursor agent 和 agy，snowpea 会从屏幕上读取权限确认或提问，使用各 CLI 的屏幕规则。

snowpea 只通过它写入的命令字符串精确匹配来查找钩子。你自己的其他钩子保持不变。

## 安装会做什么

- 只向上述文件中添加 snowpea 的条目，不改动其他内容。格式、注释以及你自己条目的顺序都会保留。
- 以原子方式写入文件（先写临时文件，再重命名），保留文件权限，并重新读取写入的结果进行检查。
- 拒绝硬链接文件，因为写入可能会改动你没有预料到的第二个文件。
- 如果集成已经是最新的，不会改动任何字节。重复运行安装是安全的。
- 对已有文件的第一次修改之前，会保留一份备份：同一文件夹中的 `<文件>.snowpea-backup-<日期>`。卸载时不会删除备份。
- 要求 CLI 的配置文件夹已存在。若不存在，`install <cli>` 会提示你先运行一次该 CLI。`install all` 会跳过文件夹不存在的 CLI，并说明跳过了。

## 卸载会做什么

卸载只精确移除安装时添加的内容，不触碰其他部分：

- 只删除 snowpea 的条目，以及仅包含这些条目的分组。你自己的钩子保留。
- 安装时新建的文件，若其中已无内容，则删除。原本就存在的空文件，恢复为原来的空内容。
- Codex 的 `[features]` `hooks` 标志，只有在安装时是由安装将其开启的情况下，才会被再次关闭。如果你原本就开着，则保持开启。
- 当没有任何 CLI 仍保留集成时，删除钩子脚本。

## 状态值

`status` 为每个 CLI 输出一行：状态、配置文件夹，以及（如有）原因。出现 `[CLI not found on PATH]` 表示 CLI 本身未安装，因此即使有集成，它也不会产生任何作用。

| 状态 | 含义 | 处理方式 |
|---|---|---|
| `not-installed` | CLI 配置中没有 snowpea 的条目 | `snowpea integration install <cli>` |
| `current` | 全部就位且为最新 | 无需操作 |
| `outdated` | 由旧版 snowpea 安装，或已注册的事件与当前版本不同 | `snowpea integration install <cli>` |
| `modified` | snowpea 的某个条目被手动编辑或删除，或者 Codex 的标志被关闭 | 卸载后重新安装，或把文件改回原样 |

## 桌面应用与 web

桌面应用和 snowpea web 设置中的 Integrations 页面，会显示同样的各行、每个 CLI 会修改的文件，并提供相同的安装与卸载操作。该页面及其操作仅限在本机上的用户使用：远程登录无法查看或修改它们。

## 环境变量

| 变量 | 用途 |
|---|---|
| `SNOWPEA_HOME` | 钩子脚本与 `integrations/state.json` 所在位置（默认 `~/.snowpea`） |
| `CLAUDE_CONFIG_DIR` | Claude Code 的配置文件夹 |
| `CODEX_HOME` | Codex 的配置文件夹 |
| `CURSOR_CONFIG_DIR` | Cursor agent 的配置文件夹 |
| `ANTIGRAVITY_CLI_CONFIG_DIR` | agy 的配置文件夹 |

如果你启动 CLI 时设置了其中某个变量，请在设置同样变量的情况下安装集成，这样它才会写入 CLI 所读取的文件夹。
