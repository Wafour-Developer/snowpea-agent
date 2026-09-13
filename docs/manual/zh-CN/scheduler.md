# Scheduler

[English](../en/scheduler.md) · [한국어](../ko/scheduler.md) · [全部页面](../README.md)

调度器就住在守护进程里。任务在守护进程的进程中运行，用的是你注册它时指定的模式，并把答案投递到你指定的频道。没有任何东西跑在单独的 cron 守护进程里，也不需要你的终端一直开着。

## 注册一个任务

```bash
snowpea job schedule --at "0 9 * * *" --task "summarize yesterday's commits" --channel telegram:123456
snowpea job schedule --in 10m --task "check whether the build is green" --mode plan
snowpea job schedule --every 30m --task "watch the error log" --channel log --workdir ~/src/api
```

在会话内部：

```
/schedule "every day at 9am" "summarize yesterday's commits" --channel telegram:123456
/schedule
```

`--at`、`--in`、`--every`、`--cron` 和 `--spec` 是同一个选项的五个名字。挑一个让这行读起来顺的就好。

## 调度式

| 形式 | 例子 |
|---|---|
| cron，五个字段 | `0 9 * * *` |
| 一次性延迟 | `in 60s`、`in 10m`、`in 2h` |
| 间隔 | `every 30m`、`every 6h` |
| 自然语言，英文 | `every day at 9am`、`in 10 minutes` |
| 自然语言，韩文 | `매일 09:00`、`10분 뒤` |

一个任务按解析结果被归为 `cron`、`once` 或 `interval`。`job list` 会显示解析出来的类型和下一次运行时间，这是检查自然语言调度式是否如你所想的最快方式。

```bash
snowpea job list
snowpea job list --json
```

## 管理任务

```bash
snowpea job run <job-id>
snowpea job cancel <job-id>
```

`job run` 会立刻在守护进程里触发这个任务，和定时器触发时完全一样。在把一个任务交给调度之前，这是测试它的正确方式。

每个任务都会记录 `last_run` 和 `last_status`，后者是 `ok`、`error`，或者当一次审批无人回答而过期时的 `denied_by_timeout`。

## 一个任务是怎么执行的

调度器每 15 秒走一拍。任务到点时，它会在这个任务的工作目录和模式下创建一个会话，标记为无人值守，用任务的内容作为 prompt，并把最终那条 assistant 消息投递到频道。kind 为 `started`、`finished`、`failed` 或 `denied` 的 `job.event` 通知会发给每一个连着的客户端，因此运行中的 TUI 能看到任务的进展。

有两件事值得知道。第一，由任务 id 加计划时间构成的一次性键（occurrence key）是唯一的，所以即便守护进程在一拍中途重启，同一个时间槽也不会触发两次。第二，启动时调度器会补跑守护进程停机期间错过的一次性任务，条件是迟到不足一小时；更早的会被标记为 missed 而不会运行。

## 提醒会回到提出它的那个会话

任务记得自己从哪儿来。注册它的那个会话——不管是在 TUI 会话里敲的 `/schedule`，还是某一回合中用到的 `schedule` 工具——都会以 `originSessionId` 的形式打在任务上，`job list --json` 会显示它。任务触发时，它的答案会作为一个 `message.done` 事件发进那个会话，文本带着前缀：

```text
⏰ Scheduled reminder (job_7f21c0)

Yesterday's commits: 14 across three repositories…
```

于是提醒落在你提问的那条线程里，并且像其他消息一样被持久化在那里：以后再打开那个会话，提醒就在它的历史中。

那个会话不必仍然开着。已关闭的会话会被单纯为了接收这次投递而恢复，紧接着又立刻关闭，所以触发的任务绝不会在你背后留下一个还开着的会话。活着的会话则不会被动到。

显式的 `--channel` 是叠加的，而不是替代。无论如何原会话都会收到提醒，频道也会收到同样的文本。当没有来源会话时——某个在这套机制存在之前注册的任务，或者它的会话已被删除——并且频道也投递不出去，文本会回落到 `$SNOWPEA_HOME/logs/jobs.log` 这份任务日志。

## 频道

| 频道 | 送到哪里 |
|---|---|
| `telegram:<chat_id>` | 一个 Telegram 聊天 |
| `discord:<channel_id>` | 一个 Discord 频道 |
| `slack:<channel>` | 一个 Slack 频道 |
| `log` | 只写 `$SNOWPEA_HOME/logs/daemon.log` |

平台必须先绑定——见 [Gateway](gateway.md)。`log` 什么都不需要，在你还在琢磨一个任务该说什么的阶段，它是正确的选择。

## 模式与审批

任务带着自己的模式，与你注册它时所在的会话无关。光是注册一个任务就需要 `send` 权限，所以在 accept 模式下，你会被要求批准注册这件事本身；这是刻意的，因为一个定时任务是对其模式所允许的一切开出的一张长期授权。

当一个任务的回合遇到需要审批的事情时，请求属于无人值守：它会同时出现在 TUI 的审批队列里，以及带着允许/拒绝按钮的绑定聊天里。谁先回答谁算数，其余人都会被告知发生了什么；如果在 `approvals.timeoutSec`（300 秒）内无人回答，请求会被拒绝，任务的 `last_status` 变为 `denied_by_timeout`。

plan 模式下的任务可以读和搜，但绝不会写。auto 模式下的任务什么都不问。对任何无人值守的东西来说，plan 是安全的默认值，而 auto 值得配一个容器。

## 让守护进程活着

已启用的任务是抑制空闲关闭的四个计数器之一，所以带着调度的守护进程会自己留在那儿：

```bash
snowpea daemon status
```

它会打印 `will not exit` 以及原因。活过一次重启是另一个问题——为此请把服务注册上：

```bash
snowpea service install
snowpea service status
```

## 下一步

[Gateway](gateway.md) —— 答案送到哪里去。
