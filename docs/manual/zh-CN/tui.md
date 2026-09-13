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

只有当守护进程还为这个目录留着一个会话时，最近会话这一段才会出现——另一个终端里开的、headless 跑的、崩溃留下的。输入为空时按 `R` 会把它重放到当前窗口；`/resume` 打开的则是覆盖所有已保存会话的选择器。若守护进程没有这样的会话，这一段根本不显示：一个无法接受的提议比没有提议更糟。

横幅只打印一次。它会像其他输出一样向上滚走，不再重绘。

## 已保存的会话

启动画面上的提议只是最新的那一个。这台机器跑过的一切都还在磁盘上，`/sessions` 打开覆盖它们的选择器：

```text
╭──────────────────────────────────────────────────────────────────────╮
│ ❯  01J9F2… · 2026-03-14 09:41 · fix the flaky worktree test          │
│    01J9DR… · 2026-03-13 18:02 · add the scheduler reminder story     │
│    01J9C7… · 2026-03-13 11:26 · (no prompt)                          │
│    Cancel                                                            │
│ ↑↓ move · Enter resume · Esc cancel                                  │
╰──────────────────────────────────────────────────────────────────────╯
```

这份列表包含已关闭的会话，而不只是守护进程仍然开着的那些，并且每一行都带着那个会话最后看到的 prompt，所以不用记住 id 也能认出某一行。行按从新到旧排列，范围限定在当前目录，而你已经身处其中的那个会话不会出现在提议里。

| 你输入什么 | 它做什么 |
|---|---|
| `/sessions` | 打开选择器 |
| `/resume` | 同一个选择器 |
| `/resume <sessionId>` | 直接重开那个会话，不经过选择器 |
| 输入为空时按 `R` | 跳过选择器，直接取这个目录里最新的会话 |
| `/session delete <id>` | 删除一个已保存的会话 |
| `/session clear` | 删除这个目录的已保存会话 |
| `/session clear --all` | 删除这台机器上每一个已保存的会话 |

恢复会把会话的历史重放进当前窗口并继续下去——工作目录、模式、供应商、模型和 team 都会跟着回来，无论守护进程是否还开着它。

删除会把消息、事件和会话记录一并带走，连同那个会话拥有的附件和语音文件，因为一条为了其中粘贴过的东西而被删掉的线程，不该把那份粘贴留在身后。活着的会话绝不会被删除：先关掉它，在此之前 `clear` 会跳过它。反过来，恢复需要一个空闲的回合——有东西在跑时 `/resume` 和 `/sessions` 会拒绝，因为在一个活着的回合底下换掉记录会把它撕成两半。

## 界面布局

界面是内联绘制的，像 `git log` 那样，而不是一个全屏应用。已完成的输出交还给终端，所以回滚缓冲、鼠标、你的 `Ctrl+Shift+F` 都还是原来那一套。只有屏幕下方是活的。

```text
 › explain the retry logic                 ← scrollback: yours, never redrawn
 ⏺ Read 3 files (128 lines)
 ◆ The retry lives in `client.ts`…

 ✢ Pondering… (12s · ↓ 3.7k tokens)       ← the live region starts here
 > the next thing you type
 ⏵⏵ auto mode on · 1 shell · ← 2 agents
 snowpea v0.1.2 | ~/project | Model: anthropic/claude-sonnet-4-5 | Mode: ACCEPT | ctx 34% (68k/200k)
 [!!] context 85% — /compact to free space
 ● main
 ✳ executor         Implement story IDE-004               running · 27s · ↓ 159.1k tokens
 ◯ 4 idle agents
```

底部面板从上到下依次是：模式摘要行、状态行、（存在时的）上下文警告，然后是代理行。模式排在最前面是刻意的——它是决定下一回合被允许做什么的那一行，所以它离你正在输入的地方最近。输入行在它们上面，工作指示行又在输入行上面。输入行以下没有变化的行不会重绘。

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

`Shift+Tab` 按 accept → auto → plan → accept 循环。当前模式在状态行和摘要行里，每种模式允许什么见 [Modes](modes.md)。`Ctrl+P` 不走循环，只开关 plan 模式。

还有一个选择器，用于你想直接挑一个模式、而不是切到下一个的时候。`↓` 越过最新的历史条目会移到摘要行上；在那里按 `Enter` 就打开它：

```text
⏵⏵ auto mode on · 1 shell · ← 2 agents · Enter to choose mode
❯  accept mode
   auto mode
   plan mode
```

它从你当前所处的模式开始，`Enter` 取走高亮的那个，`Esc` 则不动模式。无论哪种方式，模式摘要行都画在状态行的上方，而不是下方。

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

当守护进程有什么要警告你的时候——一条够到工作目录之外的命令，一种光看参数看不出来的风险——请求会带上一个 `note`，它在 TUI 和无界面 CLI 中都以红色渲染在参数上方：

```text
shell risk=high timeout=300s
  ⚠ this deletes a directory outside the working tree
  command: rm -rf ../build
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

`/compact [instructions]` 把到目前为止的对话压缩成摘要并从摘要继续；后面可选的指示说明要保留什么。当某个回合将要超过 `context.autoCompactPercent` 时，守护进程也会自己压缩。无论哪种方式，记录里都会留下发生的位置：

```text
───────────── compacted (68.0k → 12.1k tokens) ─────────────
```

显示为 `ctx 12.3k used` 而没有百分比，说明守护进程不知道该模型的窗口大小。`snowpea session context` 和 `providers.<vendor>.context_window` 设置见 [Commands](commands.md)。

## 输入

`↑` 向前翻你之前发过的 prompt，`↓` 往回走到你正在写的那条。历史按机器保存，而不是按会话：它在 `$SNOWPEA_HOME/tui-history.jsonl` 里，保留最近 500 条，并且不会把同一条连续记录两次。

在最新一条之后再按 `↓` 做的是另一件事：光标离开输入行，走到下面那几行。先是摘要行，在那里按 `Enter` 会打开模式选择器：

```text
⏵⏵ auto mode on · 1 shell · ← 2 agents · Enter to choose mode
```

然后是代理行，一行一个。`Esc` 或 `↑` 回到输入行。

### 在它工作时发送

你不必等一个回合结束。在某个回合运行期间发出的 prompt 会被接受并排队，而不是被拒绝，队列按先进先出的顺序消化——一次只跑一个回合、对着同一份历史，所以两个供应商循环绝不会压在同一段对话上。附件在你按下 `Enter` 的那一刻就被捕获，因此此刻排队的一个 chip，到它那一回合开始时仍然是你当初指的那个文件。这个队列只在内存里；它活不过守护进程重启。

`Esc` 会连同正在运行的回合一起把队列丢掉。中断意味着「停下我刚才要的事」，而这必须包括还在等着的后续项，否则 Stop 之后队列照跑不误。每一条被丢掉的 prompt 都会以 reason 为 `dropped` 的 `turn.dequeued` 报告给客户端，随后是它自己的 `turn.done`，于是不会有任何等着那个 turn id 的东西被晾着。

客户端还会在一条 prompt 进入队列时看到 `turn.queued`，在一条出队时看到 reason 为 `started` 的 `turn.dequeued`。终端 UI 目前还没有画出队列指示（待办）——在它画出来之前，排队的 prompt 只是安静地等到自己那一回合开始。

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
> what is wrong with this layout?
```

它认得终端实际递过来的各种形态：一个路径、一次多个、空格被转义的路径、空格没转义的路径、`file://` URL、文件管理器加的引号。`Ctrl+V` 直接从系统剪贴板取图片——用 `wl-paste`、`xclip`、AppleScript 或 PowerShell 中这台机器上有的那个——并保存到 `$SNOWPEA_HOME/tmp/` 下。`/attach <path>` 是手动添加的方式。

输入为空时 `Backspace` 去掉最新的 chip，`Ctrl+X` 全部清空。发送 prompt 时文件一起发出去，记录里会写明发了什么：

```text
› what is wrong with this layout?
  📎 screenshot.png
```

文件是按路径发送的，所以不会产生副本。模型之后怎么处理它们——以及 20MB 上限、缩放、模型看不了图片时会发生什么——见 [Attachments and voice](voice.md)。

## 语音

语音需要后端，而持有后端的是守护进程。用 `snowpea setup audio` 配置，各后端需要什么见 [Attachments and voice](voice.md)。

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

## Markdown 表格

回复中的 Markdown 表格会以对齐的边框渲染。列宽计算考虑了韩文、CJK 和 emoji 在终端里的宽度，过长的路径或句子会在单元格内换行。当列数多到放不下时，各个值会改为堆叠在它们的列标签之下。内联视图和全屏视图用的是同一个渲染器；代码围栏里的表格源码保持原样。

## 内置与自定义代理

`/agent list` 会把打包好的角色（`architect`、`critic`、`executor`、`explorer`、`test-engineer`、`verifier`）和自定义定义一起列出。内置角色不需要用户创建任何文件，其 source 为 `builtin`。同名的自定义定义会覆盖内置的；项目定义的优先级高于全局定义。这些名字同样可以通过 `delegate_task` 的 `agent` 参数使用。

## 启动时的更新通知

每次启动都会在后台检查更新。`/update` 总是绕过更早的否定缓存并重新检查；当确实没有更新的版本时，它会报告这个构建已经是最新的，而不是显示一次安装失败。出现横幅时，在输入为空时按 `U` 或输入 `/update` 打开确认。选 `y` 安装并重启，选 `n`/Esc 推迟。正常输入中的小写 `u` 不是更新快捷键。

Git `main`/`master` 安装会比较已安装的 commit，因此不需要版本号变动也能检测到新的提交。检查失败、构建未变化以及降级都不会触发安装。PyPI/release 安装仍然沿用基于版本号的检查。

如果一次较早的自动更新导致启动失败并报 `Cannot read properties of undefined (reading 'rawCall')`，请重新安装当前的 `main`：

```sh
uv tool install --force --reinstall 'snowpea-agent[images] @ git+https://github.com/Wafour-Developer/snowpea-agent@main'
```

重新安装之后，等到没有工作在跑时，用 `snowpea daemon stop`，再启动 `snowpea` 以加载新的守护进程代码。

帮助面板不会超出终端高度。用 `↑`/`↓` 或 `PgUp`/`PgDn` 滚动；用 **Esc、F1、q 或 Enter** 关闭。帮助中的 Esc 不会中断正在运行的回合。

底部的输入行、连接/模型状态、模式摘要和代理列表之间，用跨越终端整个内容宽度的分隔线隔开，使活动区域一眼可辨。

## Team 与短委派

第一次 `snowpea setup` 会用内置角色创建一个 `default` team。`/team create delivery architect executor verifier` 会用已有的 agent 创建一个项目 team，立即激活它，并拒绝不认识的名字。用 `/team list`、`/team use <name>` 和 `/team delete <name>` 管理它。有 team 处于激活状态时，页脚只显示它的名字和成员，自动委派也被限制在这份名单之内。

输入 `$executor fix the tests` 就能直接委派，不用写长命令。不认识的名字或不在 team 内的名字会直接失败，而不是悄悄变成一个通用 agent。省略名字的内部委派会确定性地使用 `executor`（如果存在），否则用 team 的第一个成员。
