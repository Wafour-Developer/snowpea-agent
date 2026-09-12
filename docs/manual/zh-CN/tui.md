# The terminal UI

[English](../en/tui.md) · [한국어](../ko/tui.md) · [全部页面](../README.md)

不带参数运行 `snowpea` 会打开终端界面。它是一个瘦客户端：哪个工具可以运行、某条命令是什么意思、什么时候压缩上下文——这些判断都归守护进程所有，本页讲的是这些判断呈现出来的那一层界面。

## 启动画面

会话最先画出的是按终端宽度缩放的字标，然后是三行告诉你身在何处的信息：

```text
██████████  ██      ██  ██████████  ██      ██  ██████████  ██████████  ██████████
██          ████    ██  ██      ██  ██      ██  ██      ██  ██          ██      ██
██████████  ██  ██  ██  ██      ██  ██      ██  ██████████  ██████████  ██████████
        ██  ██    ████  ██      ██  ██  ██  ██  ██          ██          ██      ██
        ██  ██      ██  ██      ██  ████  ████  ██          ██          ██      ██
██████████  ██      ██  ██████████  ██      ██  ██          ██████████  ██      ██
🌱 snowpea v0.1.2

          Open-source multi-vendor coding agent and personal AI assistant
        v0.1.2 · anthropic/claude-sonnet-4-5 · /home/you/project · ACCEPT

Last session: 26m ago · "add the worktree parallel session story to the IDE plan"
Press R or type /resume to continue it
```

只有当守护进程还为这个目录留着一个会话时，最近会话这一段才会出现——另一个终端里开的、headless 跑的、崩溃留下的。输入为空时按 `R`，或者输入 `/resume`，就会把它重放到当前窗口。若守护进程没有这样的会话，这一段根本不显示：一个无法接受的提议比没有提议更糟。

横幅只打印一次。它会像其他输出一样向上滚走，不再重绘。

## 界面布局

界面是内联绘制的，像 `git log` 那样，而不是一个全屏应用。已完成的输出交还给终端，所以回滚缓冲、鼠标、你的 `Ctrl+Shift+F` 都还是原来那一套。只有屏幕下方是活的。

```text
 › 解释一下重试逻辑                          ← 回滚缓冲：属于你，不会重绘
 ⏺ Read 3 files (128 lines)
 ◆ 重试逻辑在 `client.ts` 里…

 ✢ Pondering… (12s · ↓ 3.7k tokens)       ← 从这里开始是活动区域
 > 你接下来要输入的内容
 snowpea v0.1.2 | ~/project | Model: anthropic/claude-sonnet-4-5 | Mode: ACCEPT | ctx 34% (68k/200k)
 [!!] context 85% — /compact to free space
 ⏵⏵ auto mode on · 1 shell · ← 2 agents
 ● main
 ✳ executor         Implement story IDE-004               running · 27s · ↓ 159.1k tokens
 ◯ 4 idle agents
```

底部面板从上到下依次是：状态行、（存在时的）上下文警告、摘要行、代理行。输入行在它们上面，工作指示行又在输入行上面。没有变化的行不会重绘。

## 正在工作时

一个回合进行时，输入行上方会出现一行，这一行说明此刻真正在发生什么：

| 显示 | 含义 |
|---|---|
| `✢ Pondering… (12s · ↓ 3.7k tokens)` | 模型在思考；这个动词每隔几秒换一次 |
| `✳ Running shell: npm test… (4s · …)` | 某个工具正在执行，带着名字和参数 |
| `✶ 3 agents working… (1m 2s · …)` | 这个回合已经委派出去了 |
| `✳ /ralph… (3m 10s · …)` | 一个命令工作流占着这个回合 |
| `⏸ Waiting for approval` | 它被你挡住了 |

计时和 token 数是本回合的，不是整个会话的；会话总量在状态行里。`Esc` 中断。

一连串工具调用结束后，会折叠成回滚缓冲里的一行，而不是每次一张卡片：

```text
⏺ Ran 2 shell commands (34 lines)
⏺ Read 3 files (128 lines)
```

只有失败的调用会保留自己的卡片和输出，因为那才是需要读的。`Ctrl+O` 展开仍在活动区域中的最近一次工具调用或 diff。

## 模式

`Shift+Tab` 按 accept → auto → plan → accept 循环。当前模式在状态行和摘要行里，每种模式允许什么见 [Modes](../en/modes.md)。`Ctrl+P` 不走循环，只开关 plan 模式。

## 审批

守护进程要问的时候，用菜单来问。`↑`/`↓` 移动，`Enter` 选中当前行，`Esc` 拒绝：

```text
╭──────────────────────────────────────────────────────╮
│ Approval required                                    │
│ shell risk=high timeout=300s                         │
│   command: rm -rf build                              │
│                                                      │
│ ❯  Yes   (y)                                         │
│    Yes, and don't ask again this session   (a)       │
│    Yes for this project   (p)                        │
│      adds an allowlist rule the daemon keeps         │
│    No   (n)                                          │
│ ↑↓ move · Enter confirm · Esc cancel                 │
╰──────────────────────────────────────────────────────╯
```

光标从 `Yes` 开始，所以 `Enter` 就是同意。`y`、`a`、`p`、`n` 依然可以直接用。对话框打开时键盘归它所有：你输入的字符不会漏进后面的草稿，`Shift+Tab` 也不会改模式。

无人值守的回合发起的审批——定时任务、Telegram 消息——会进入队列。`Ctrl+R` 把键盘交给那个队列，`/approvals` 列出它。

## 差异

文件改动出现在它发生的地方，也就是对话里：

```text
✎ Edited README.md  (+4 −2)
--- a/README.md
+++ b/README.md
@@ -1,5 +1,7 @@
 # snowpea
-an agent
+an open-source multi-vendor coding agent
… 3 more lines (Ctrl+O)
```

新建文件显示为 `✚ Created notes.md (7 lines)`。长补丁截断到十二行；只要还在活动区域，`Ctrl+O` 就能展开最近的那一个。

## 上下文

状态行带着模型上下文窗口的占用情况：

```text
ctx 34% (68k/200k)
```

70% 以下是暗的，从那里开始变黄，85% 起变红；超过 80% 时，摘要行上方会出现一行告诉你该怎么办：

```text
[!!] context 85% — /compact to free space
```

`/compact [指示]` 把到目前为止的对话压缩成摘要并从摘要继续；后面可选的指示说明要保留什么。当某个回合将要超过 `context.autoCompactPercent` 时，守护进程也会自己压缩。无论哪种方式，记录里都会留下发生的位置：

```text
───────────── compacted (68.0k → 12.1k tokens) ─────────────
```

显示为 `ctx 12.3k used` 而没有百分比，说明守护进程不知道该模型的窗口大小。`snowpea session context` 和 `providers.<vendor>.context_window` 设置见 [Commands](commands.md)。

## 输入

`↑` 向前翻你之前发过的 prompt，`↓` 往回走到你正在写的那条。历史按机器保存，而不是按会话：它在 `$SNOWPEA_HOME/tui-history.jsonl` 里，保留最近 500 条，并且不会把同一条连续记录两次。

在最新一条之后再按 `↓` 做的是另一件事：光标离开输入行，走到下面那几行。先是摘要行，在那里按 `Enter` 会列出正在跑的东西：

```text
⏵⏵ auto mode on · 1 shell · ← 2 agents · Enter to list them
    ◦ Ran shell: npm -w tui test · 12s
```

然后是代理行，一行一个。`Esc` 或 `↑` 回到输入行。

### 看进一个代理内部

在代理行上按 `Enter`，会把上面的记录换成那个代理自己的对话——它拿到的任务、它的工具调用、它的回答：

```text
╭────────────────────────────────────────────────────────────────────╮
│ ◯ executor · Implement story IDE-004      running · 28s · ↓ 159.1k │
│ › Implement story IDE-004 (Worktree parallel sessions)             │
│ ✓ read_file path=docs/stories/IDE-004.md (1 lines)                 │
│ ◆ Added the worktree manager and its tests; 3 files changed.       │
│ ↑↓ PgUp/PgDn scroll · Esc back to the main transcript              │
╰────────────────────────────────────────────────────────────────────╯
```

每个被委派的代理都跑在自己的会话里，这个视图就是那个会话：先重放已经发生的部分，然后继续实时跟进。`Esc`，或在 `● main` 上按 `Enter`，就回到主记录。`Ctrl+A` 解除折叠规则、把面板完全展开，空闲的代理和藏在 `↓ N more` 后面的行都会列出来。

## 更新

有新版本时状态行会说，输入为空时按 `U`——或者输入 `/update`——打开确认。升级执行、守护进程重启，界面以新版本回来。

```text
snowpea v0.1.2 → v0.1.3 (U to update)
```

## 附件

把文件路径粘贴或拖进输入行，它会变成一个 chip，而不是一段文字：

```text
[📎 screenshot.png 1.2MB] (backspace removes the last · Ctrl+X clears)
> 这个布局哪里有问题？
```

它认得终端实际递过来的各种形态：一个路径、一次多个、空格被转义的路径、空格没转义的路径、`file://` URL、文件管理器加的引号。`Ctrl+V` 直接从系统剪贴板取图片——用 `wl-paste`、`xclip`、AppleScript 或 PowerShell 中这台机器上有的那个——并保存到 `$SNOWPEA_HOME/tmp/` 下。`/attach <路径>` 是手动添加的方式。

输入为空时 `Backspace` 去掉最新的 chip，`Ctrl+X` 全部清空。发送 prompt 时文件一起发出去，记录里会写明发了什么：

```text
› 这个布局哪里有问题？
  📎 screenshot.png
```

文件是按路径发送的，所以不会产生副本。模型之后怎么处理它们——以及 20MB 上限、缩放、模型看不了图片时会发生什么——见 [Attachments and voice](../en/voice.md)。

## 语音

语音需要后端，而持有后端的是守护进程。用 `snowpea setup audio` 配置，各后端需要什么见 [Attachments and voice](../en/voice.md)。

| 按键或命令 | 作用 |
|---|---|
| `/voice` | 启用语音输入 |
| `Ctrl+Space` 或 `/rec` | 开始录音；再按一次停止 |
| `/tts on`、`/tts off` | 每条回复结束时朗读出来 |
| `Esc` | 停止正在朗读的回复 |

录音时，工作指示的位置会计时：

```text
● REC 00:07
```

停止后会转写，并把文字放进草稿而不是直接发送，因为语音识别出错的频率高到值得先读一遍。守护进程有麦克风时在守护进程那边录，没有时在这台机器上录。

状态行里的 `🔊` 表示正在朗读回复。请求一项不存在的能力时，命令不会悄无声息，而是用守护进程自己的话说明原因：

```text
voice input needs speech-to-text: no transcription backend: install the whisper CLI, set an OpenAI API key, or …
```

## 全屏

`--fullscreen` 切换到备用屏幕缓冲区布局：记录变成由界面自己滚动的窗口，用 `PgUp`/`PgDn` 和 `Ctrl+U`/`Ctrl+D` 翻动，退出时不会在回滚缓冲里留下任何东西。

```bash
snowpea --fullscreen
```

它在慢速连接上更省带宽，因为只重绘变化的行。代价是这个会话期间拿走了终端的回滚缓冲，这也是它不是默认值的原因。

## 按键

| 按键 | 作用 |
|---|---|
| `Enter` | 发送，或确认选中的选项 |
| `Shift+Tab` | 循环切换模式 |
| `Ctrl+P` | 开关 plan 模式 |
| `↑` / `↓` | 之前的 prompt；在最新一条之后按 `↓` 进入面板 |
| `Esc` | 中断回合、停止朗读、退出代理视图 |
| `Ctrl+O` | 展开最近的工具调用或 diff |
| `Ctrl+A` | 完全展开代理面板 |
| `Ctrl+R` | 切到待处理的审批队列 |
| `Ctrl+V` | 附加剪贴板中的图片 |
| `Ctrl+X` | 清空附件 |
| `Ctrl+Space` | 开始或停止录音 |
| `U` | 接受提示的更新 |
| `R` | 继续启动画面提议的会话 |
| `F1` | 帮助 |
| `Ctrl+C` | 退出 |

`/help` 列出守护进程拥有的全部命令，包括插件添加的那些，并且会一并输出这张表。
