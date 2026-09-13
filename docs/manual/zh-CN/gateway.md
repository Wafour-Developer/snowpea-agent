# Messenger gateway

[English](../en/gateway.md) · [한국어](../ko/gateway.md) · [全部页面](../README.md)

gateway 把一个聊天平台连到一个会话或一个已命名的 agent 上。那个聊天里的消息会变成 prompt，agent 的回答会以消息回来，审批则以按钮的形式到达。在 v0.1 中获得完整支持的平台是 Telegram。Discord 和 Slack 实现了同一套接口、消息投递可用，只是背后的实战检验少一些。

## 最短的路

```bash
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token --user-id 987654
snowpea            # or: snowpea daemon start
```

配置到此为止。向导把这个消息平台记进 `settings.json`，守护进程启动时会把它变成一条*通配*绑定：任何向 bot 发消息的聊天都会拿到属于自己的会话，工作目录是 `$HOME`，除非你设置了 `gateway.telegram.workdir`。起来之后 `snowpea daemon status` 会显示 `messengers   telegram (listening)`。token 本身会以 `0600` 权限复制进 `$SNOWPEA_HOME/credentials.json`，其他地方一律只按名字引用它，因此它绝不会出现在 `state.db` 里、RPC 结果里，或者某一行日志里。

`--user-id` 是你自己在那个平台上的数字账号 id，也是唯一被允许从聊天里回答审批的账号。给 [@userinfobot](https://t.me/userinfobot) 发 `/start`，Telegram 就会告诉你你的 id。不填它，消息平台照样能对话，但每一次审批按钮点击都会被拒绝，包括你自己点的。

在向导里（或用 `settings.set`）把这个消息平台关掉，那条绑定也会随之移除。你手工建立的绑定不会被它碰到。

## 手工绑定

当你想要的不是一个通配会话时就用这种方式：绑到一个已命名的 agent、某一个特定的聊天，或者同一平台上的两个账号。

```bash
snowpea gateway bind telegram TELEGRAM_BOT_TOKEN agent:scribe --user 987654
snowpea gateway bind telegram TELEGRAM_BOT_TOKEN new:~/src/api --channel 123456 --user 987654
snowpea gateway list --json
snowpea gateway unbind <binding-id>
```

三个位置参数依次是平台、凭据引用（`credentials.json` 中的一个键，或者一个环境变量的名字）、以及目标：

| 目标 | 含义 |
|---|---|
| `agent:<name>` | 一个已命名的常驻 agent，拥有自己的记忆命名空间 |
| `session:<id>` | 一个已存在的会话 |
| `new:<workdir>` | 收到第一条消息时在那个目录下新建一个会话 |

`--channel` 把绑定限制到一个聊天 id。`--user` 指定被允许批准事情的平台用户。两个都要设。没有 `--user` 就没有任何人有权回答审批，而 gateway 会无视所有人的按钮点击。

## 弄一个 bot

**Telegram。** 找 [@BotFather](https://t.me/BotFather) 说 `/newbot`，把 token 留好。你的聊天 id 就是 bot 在你给它发消息时看到的那个数字，第一条入站消息到达时会出现在守护进程日志里。这里用的是 long polling，所以不需要公网 URL，也不需要 webhook。

**Discord。** 创建一个应用，添加一个 bot 用户，打开 message content intent，把它邀请进你的服务器，把 bot token 留好。频道 id 是频道 URL 的最后一段路径。

**Slack。** 创建一个 app，加上 `chat:write` 以及你的工作区需要的事件，安装它，把 bot token 留好。目标处使用频道名或 id。

## 跟它对话

给 bot 发一条消息，你就在一个会话里了。斜杠命令的行为和在终端里完全一样，因为命令注册表位于核心中：

```
/mode
/ralph fix the failing integration test
/schedule "매일 09:00" "어제 커밋 요약" --channel telegram:123456
```

由 gateway 创建的会话会带着它们的来源出现在 `session.list` 中，所以连到同一个守护进程的 TUI 也能看到那个聊天正在做什么。

## 无人值守的审批

由 gateway 或调度器会话发起的审批属于无人值守，它和你自己敲键盘触发的那一种行为不同：

- 它会作为 `approval.pending` 通知广播给每一个连着的客户端，因此 TUI 的审批队列会显示它。
- 它会带着允许和拒绝按钮发送到绑定的聊天里。
- 两边之中先给出的回答获胜。其余所有人都会收到 `approval.resolved`，其中写明决定内容和做决定的人。
- 只有被绑定的 `--user` 可以从聊天里回答。其他人的点击会被忽略并记入日志。
- 经过 `approvals.timeoutSec`（默认 300 秒）仍无人回答，它会被拒绝。

你在 TUI 里敲键盘触发的审批只留在那个界面上，绝不会进入共享队列。这条边界正是重点所在：你自己终端里的提示不会漏进群聊，而聊天里的审批也不会被陌生人代答。

每一个已决定的条目都会连同请求 id、工具、决定、决定者、作用域，以及是否属于无人值守，一起追加到 `$SNOWPEA_HOME/logs/approvals.jsonl`。

## 已命名的 agent

一个已命名的 agent 会带着自己的会话、自己的记忆命名空间、自己的频道和自己的任务活过守护进程重启。两个已命名的 agent 读不到彼此的记忆，正因如此，把一个交给工作频道、另一个交给私人频道才是合理的。

```bash
snowpea agents --json
```

用 `/agent create "<description>"` 建一个，用 `gateway bind ... agent:<name>` 给它绑一个频道，下次守护进程启动时它会连同绑定一起被恢复。已命名的 agent 是第四个 keepalive 计数器，所以有一个这样的 agent 的守护进程永远不会因空闲而退出。

## 安全

gateway 是一条远程执行路径。请按这个标准对待它。

- 绑定 `--user`。永远要绑。
- 对于从你无法完全掌控的聊天里够得着的一切，优先用 plan 模式。
- 审批请求是一次性的，并且绑在一个请求 id 上；重放一次按钮点击什么都不会发生。
- 凭据放在一个 `0600` 的文件里，条件允许时会使用操作系统的钥匙串。
- 偶尔读一读 `approvals.jsonl`。它记录着你没看着的时候都允许过什么。

## 下一步

[Backends](backends.md) —— 让工具在别处运行。
