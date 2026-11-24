# 终端会话（beta）

终端会话是在 snowpea 内、真实终端中运行的程序：可以是 shell、snowpea CLI，或某个外部 CLI（Claude Code、Codex、agy、Cursor agent）。你可以看到它的画面、向它输入，snowpea 会跟踪它是在工作、在等你，还是已经完成。

本功能处于 beta 阶段。每个 CLI 的屏幕读取规则都依据该 CLI 的输出，因此新版本的 CLI 可能改变 snowpea 看到的内容。如果状态看起来不对，终端本身才是准绳。

其他语言: [English](../en/terminals.md) · [한국어](../ko/terminals.md) · [全部页面](../README.md)

相关: [External sessions（beta）](../en/external-sessions.md) · [Jarvis mode（beta）](../en/jarvis.md) · [CLI 钩子集成（beta）](integrations.md) · [snowpea web](web.md) · [Terminal UI](tui.md)

## 什么是终端会话

- 守护进程为每个终端启动一个小型 holder 进程。holder 持有程序的伪终端，保留最近 1 MiB 的输出，并记录退出码。
- 终端可以从桌面应用、snowpea web 或 Jarvis 打开。`snowpea term` 命令用于列出、读取、输入以及连接已经存在的终端。
- 同时运行的终端最多 `terminals.max` 个（默认 16，可设为 1 到 64）。
- 配置档（profile）：`shell`、`snowpea`、`claude`、`codex`、`agy`、`cursor`。

## 把外部 CLI 会话放进终端

当 `jarvis.external.view` 为 `terminal`（默认）时，外部会话会以终端方式打开：CLI 在一个窗格中运行，取代聊天视图。若想保留之前的结构化聊天视图，把它设为 `chat`。未指定视图的客户端仍使用聊天视图。

- 新会话会在你选择的项目文件夹中启动 CLI。
- 已有会话使用 CLI 自身的恢复命令继续（`claude --resume <id>`、`codex resume <id>`，agy 与 Cursor 也有对应命令）。会话 id 来自钩子集成，所以如果想从终端恢复，请先运行 [`snowpea integration install`](integrations.md)。
- 已在另一个终端中运行的会话仍然只能查看。

CLI 在你的交互式 shell 中运行，因此 PATH 和 nvm 等设置照常生效。CLI 退出后，除非关闭了 `terminals.keepShell`，窗格会回到 shell。

## 在终端中使用 snowpea 会话

每个 snowpea 会话都可以两种方式查看：聊天，或者在连接到同一会话的终端里运行 snowpea CLI。两种视图都是同一会话的客户端，可以同时打开。切换按钮位于会话标题栏。

- 默认视图由 `ui.sessionView` 决定：`chat`（默认）或 `terminal`。这是你自己界面的设置，不会改变别人的视图。
- 若要从 shell 连接到本地守护进程中已有的会话，运行 `snowpea --session <id>`。TUI 会恢复该会话，而不是新建一个。

## 设置

| 键 | 默认值 | 作用 |
|---|---|---|
| `ui.sessionView` | `chat` | snowpea 会话的默认视图 |
| `jarvis.external.view` | `terminal` | 外部 CLI 会话的打开方式：`terminal` 或 `chat` |
| `terminals.max` | `16` | 同时运行的终端上限（1 到 64） |
| `terminals.keepShell` | `true` | CLI 退出后回到 shell，而不是关闭窗格 |
| `terminals.headlessCols` / `terminals.headlessRows` | `160` / `48` | 没有任何视图连接时终端使用的尺寸 |
| `terminals.sounds.blocked` | `true` | 终端需要你处理时发出声音并弹出提示 |
| `terminals.sounds.done` | `true` | 终端完成而你没有在看时发出声音并弹出提示 |

`terminals.*` 和 `ui.*` 设置只能由本机的用户修改。远程或隧道连接无法修改它们。

## 状态：圆点代表什么

每个终端有以下五种状态之一，应用会以圆点显示：

| 圆点 | 状态 | 含义 |
|---|---|---|
| needs you | `blocked` 或 `error` | 程序在等待回答（权限确认或提问），或者已经失败 |
| done | `done` | 一轮已结束，但还没有人看过该终端（`seen` 为 false） |
| working | `working` | 一轮正在运行 |
| idle | `idle` | 没有在运行任何东西 |

阻塞状态还有类型：`permission`、`question`、`auth`。侧边栏会按 needs you、done、working、idle 的顺序汇总项目的圆点。

snowpea 按以下优先级（从高到低）决定状态来源：

1. 程序本身（snowpea TUI 会直接报告自己的状态）；
2. 屏幕上可见的权限确认或提问（由该 CLI 的屏幕规则读取）；
3. CLI 的钩子报告（[钩子集成](integrations.md)）；
4. 仅由屏幕规则得出的状态。

## 权限选项

以终端方式启动 CLI 时，可以选择它的权限模式。选项来自各 CLI 自身的模式：

| CLI | 可选项 | 危险选项（红色，需先确认） |
|---|---|---|
| Claude Code | Accept edits、Plan、Auto、Don't ask | Bypass permissions（`--dangerously-skip-permissions`） |
| Codex | Read only、Default | Full access（`--dangerously-bypass-approvals-and-sandbox`） |
| agy | Accept edits、Auto、Plan | Skip permissions（`--dangerously-skip-permissions`） |
| Cursor agent | Ask、Plan | Run everything（`--yolo`） |

危险选项的规则如下：

- 在所有出现的地方都显示红色标记，应用会在启动前要求你确认风险。
- Jarvis 或任何工具请求时，无论设置如何都会被拒绝。
- `web.remote.maxMode` 低于 `auto` 时被拒绝。
- 通过 Web 隧道连接时，若未解锁终端，同样被拒绝（[snowpea web](web.md#用隧道获得公网地址)）。

Jarvis 不会回答危险选项启动的终端中的权限确认。Accept edits、Don't ask 等非危险模式同样只能由本地应用中的用户自己选择，Jarvis 不会替你选择。Jarvis 通过 `terminal_create` 启动的 CLI 终端，始终使用 CLI 的默认权限。

## 守护进程重启后终端仍然保留

holder 独立运行，因此守护进程重启或升级时，终端会继续运行。守护进程再次启动时，会重新连接每个仍在运行的 holder，并用 holder 保存的最近输出重绘屏幕。holder 已经死亡的终端（例如重启机器之后）会以"丢失"的形式列出，你可以恢复其 CLI 会话。

```bash
snowpea daemon stop                    # 只停止守护进程；终端继续运行
snowpea daemon stop --and-terminals    # 守护进程和所有终端会话一并结束
```

## `snowpea term` 命令

```bash
snowpea term ls                        # 正在运行的终端
snowpea term ls --all                  # 同时列出已结束的
snowpea term read <id> --lines 100     # 以纯文本显示屏幕，附带 100 行历史
snowpea term send <id> "运行测试"        # 输入文字并按 Enter
snowpea term send <id> --no-submit "草稿"  # 只输入，不按 Enter
snowpea term attach <id>               # 在当前窗口中接管该终端
snowpea term attach <id> --observe     # 仅观看：从不输入，从不调整尺寸
```

- `attach` 会把你的终端切换为 raw 模式并显示会话。按 **Ctrl+\\** 可以分离，会话继续在后台运行。`attach` 需要交互式终端，目前不支持 Windows；Windows 上请使用 `read` 和 `send`，或在应用中打开该终端。
- `send` 会等你停止在该终端中输入后再发送文字。遇到因权限确认而阻塞的终端会拒绝发送，除非加上 `--force`。只有在读过屏幕并确定提示的内容后，才使用 `--force`。
- `read` 只显示屏幕内容，不会改变任何东西。

## Jarvis 与终端（beta）

开启 Jarvis 模式后，Jarvis 能看到其可访问项目中的终端，并拥有以下工具：

| 工具 | 作用 |
|---|---|
| `terminal_list` | 列出终端、状态以及由谁启动 |
| `terminal_read` | 某个终端的屏幕文字、状态和标题 |
| `terminal_send` | 向 CLI 终端输入一条消息并提交 |
| `terminal_keys` | 按下命名按键（enter、escape、up、y、1 …） |
| `terminal_interrupt` | 中断 CLI 正在进行的操作 |
| `terminal_wait` | 等待终端停止工作、阻塞、退出，或输出匹配的文字 |
| `terminal_create` | 在某个项目中以默认权限启动 Claude Code、Codex、agy 或 Cursor，可附带第一条提示 |

规则如下：

- Jarvis 只向 CLI 终端输入。它从不向普通 shell 输入，从不使用 `force`，也从不向以危险选项启动的终端输入。
- Jarvis 只为你的请求或你委派的工作而输入。每一次读取和每一条指令都会记录在访问日志（`jarvis.access`）和决策账本（ledger）中。
- `terminal_create` 从不启用危险选项，并会在终端上标注为由 Jarvis 启动。

以下情况会唤醒 Jarvis：

- 被委派的终端变为 `blocked` 或 `error`，或到达 `done`，Jarvis 会立即被唤醒（遵循平常的合并处理与预算规则）；
- 其他终端进入 30 秒的摘要（digest）；
- 由 Jarvis 启动的终端变为阻塞、出错或退出时，Jarvis 会收到一条便笺，内含该终端最后 20 行。便笺是给 Jarvis 的事件，不会被输入到终端中。

关于终端内的权限确认：只有当以下条件全部成立时，Jarvis 才会回答权限确认：终端已完全委派给 Jarvis 且未被你占用；终端不是危险终端；屏幕规则能读出该提示所询问的命令；委派引擎将该命令归类为 `safe` 或 `risky`。归类为 `guarded` 或 `never` 的命令，以及其他所有情况，都由你来回答。Jarvis 的每一次回答和每一次拒绝都会写入决策账本。委派级别的说明见 [Jarvis 模式](../en/jarvis.md#delegation)。
