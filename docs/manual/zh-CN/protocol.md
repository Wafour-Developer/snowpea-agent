# Protocol

[English](../en/protocol.md) · [한국어](../ko/protocol.md) · [全部页面](../README.md)

完整的、自动生成的参考文档是 [docs/protocol.md](../../protocol.md)。本页是你在读它之前想要的那份导览。

核心能做的每一件事，首先是一个 JSON-RPC 方法，然后才是一个 UI 上的可用功能。守护进程和终端 UI 之间没有私有通道——TUI 只是一个普通客户端，它能做的事，你的客户端也能做。

## 事实来源

`core/snowpea_core/server/protocol.py` 定义了 `PROTOCOL_VERSION`、每个方法的参数与结果 schema，以及每一个事件的 payload。`scripts/gen_protocol.py` 由它生成 `docs/protocol.md` 和 `sdk/src/protocol.ts`。这两份生成的文件从不手工编辑，一旦漂移，CI 会让构建失败：

```bash
uv run python scripts/gen_protocol.py --check
```

## 传输

守护进程在启动时绑定一个选定的回环端口，并把它连同一个认证 token 记录在 `$SNOWPEA_HOME/daemon.json` 中。

| 表面 | 端点 | 用途 |
|---|---|---|
| WebSocket | `ws://127.0.0.1:<port>/ws` | JSON-RPC 2.0，双向 |
| HTTP GET | `/health` | 存活检查 |
| HTTP GET | `/version` | 服务端与协议版本 |
| HTTP GET | `/protocol.json` | 整份 schema 的 JSON |

这些 HTTP 端点是为探针、安装器和调试准备的只读便利接口。每一个会改变状态的调用都只存在于 WebSocket 上。

## 握手

连接建立后，客户端立即用 `$SNOWPEA_HOME/token` 中的 token、自己的版本，以及自己编译时所针对的协议版本调用 `system.hello`。在此之前的任何其他方法都会以 `unauthorized` 失败。主版本不匹配会以 `protocol_incompatible` 被拒绝。

```json
{"jsonrpc":"2.0","id":1,"method":"system.hello",
 "params":{"token":"…","clientVersion":"0.1.0","protocolVersion":"0.1.0"}}
```

结果里带着 `protocolVersion`、`serverVersion` 和一份 `capabilities` 列表，版本冻结之后就是靠它来协商功能的。

## 方法，按领域分

| 领域 | 方法 |
|---|---|
| system | `system.hello`, `system.info`, `system.health`, `system.shutdown` |
| session | `session.create`, `session.list`, `session.resume`, `session.close`, `session.prompt`, `session.interrupt`, `session.compact`, `session.deleteSaved`, `session.setMode` |
| 命令与工具 | `command.list`, `command.run`, `tool.list` |
| 审批 | `approval.list`, `approval.respond`，以及 `permission.allowlist.add` / `list` / `remove` |
| 供应商 | `provider.list`, `provider.configure`, `provider.loginWeb` |
| agent | `agent.list`, `agent.create`, `agent.spawn`, `agent.bindChannel`, `agent.delete` |
| team | `team.start`, `team.status` |
| 任务 | `job.schedule`, `job.list`, `job.cancel`, `job.runNow` |
| gateway | `gateway.bind`, `gateway.list`, `gateway.unbind` |
| 记忆 | `memory.search`, `memory.write` |
| skill | `skill.search`, `skill.install`, `skill.list`, `skill.remove`, `skill.reload` |
| backend | `backend.set` |

有一个方法是反方向走的。`approval.request` 是一个服务端到客户端的*请求*，而不是通知：守护进程向客户端要一个决定，并等待回复。正是这种双向性，才让这套协议是 WebSocket JSON-RPC，而不是 HTTP 加一条流。

## 事件

通知由守护进程流向已订阅的客户端。

- `session.event(sessionId, seq, kind, payload, ts)` 承载一个回合中发生的一切。kind 有：`message.delta`、`message.done`、`tool.call`、`tool.result`、`diff`、`subagent.spawn`、`subagent.update`、`subagent.done`、`team.task.update`、`mode.changed`、`backend.changed`、`usage`、`error`、`turn.done`。
- `approval.pending` 和 `approval.resolved`，用于无人值守的审批队列。
- `job.event` 和 `gateway.event`。
- skill 或插件重新加载之后的 `commands.changed`。

每一个 `session.event` 都带着一个在其会话内单调递增的 `seq`。断线的客户端重连后调用 `session.resume(sessionId, afterSeq)`，就能被精确地重发它错过的部分。这正是事件要被持久化、而不只是广播出去的原因。

## 错误

错误是 JSON-RPC 错误，`error.data.code` 里带一个字符串代码：`unauthorized`、`protocol_incompatible`、`not_found`、`invalid_params`、`mode_denied`、`approval_denied`、`approval_timeout`、`tool_inactive`、`not_implemented`、`login_unsupported`、`search_provider_unavailable`、`browser_provider_unavailable`、`internal`。

## 使用 SDK

```ts
import { connect } from "@snowpea/sdk";

const client = await connect({ port, token, clientVersion: "1.0.0" });
const { sessionId } = await client.call("session.create", { workdir: process.cwd(), mode: "accept" });

client.on("session.event", (e) => {
  if (e.kind === "message.delta") process.stdout.write(e.payload.text);
});

client.onRequest("approval.request", async (req) => ({ decision: "deny", scope: "once" }));

await client.call("session.prompt", { sessionId, text: "what does this repo do?" });
```

这个客户端会自行重连，并从它看到的最后一个 `seq` 处恢复。`sdk/src/protocol.ts` 给你每个方法和事件的类型；它是生成出来的，所以不可能描述出守护进程没有的方法。

## 版本管理与冻结闸门

`PROTOCOL_VERSION` 遵循语义化版本。在 v0.2 桌面 IDE 开工之前，这套协议必须通过一道冻结闸门：版本达到 `1.0.0`，并且连续三个发布 tag 之间 `docs/protocol.md` 和生成的 schema 都没有变化，同时 SDK 的契约测试在这三次上都通过。

```bash
uv run python scripts/gen_protocol.py --check
git log --oneline -- docs/protocol.md sdk/src/protocol.ts
```

冻结之后，增量式的改动需要升一个次版本号，并通过 `system.hello` 中的 `capabilities` 协商。一次主版本变更意味着所有客户端要同时发版，而这正是这道闸门要让人看见的代价。
