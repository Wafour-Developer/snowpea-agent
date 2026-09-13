# Headless runs

[English](../en/headless.md) · [한국어](../ko/headless.md) · [全部页面](../README.md)

`snowpea -c` 在没有 UI 的情况下跑一个回合，并以一个你可以据此分支的退出码结束。想把 snowpea 放进脚本、git hook 或 CI 任务里，靠的就是它。

```bash
snowpea -c "what does this repository do?"
snowpea -c "add a regression test for the parser" --mode auto
snowpea -c "summarize today's diff" --json --cwd ~/src/api --timeout 300
```

## 选项

| 选项 | 含义 |
|---|---|
| `-c`, `--prompt TEXT` | prompt；它的存在本身就让这次运行成为无界面运行 |
| `--mode plan\|accept\|auto` | 权限模式，默认取项目默认值 |
| `--json` | 输出 JSON Lines 而不是散文 |
| `--cwd DIR` | 会话的工作目录 |
| `--timeout SEC` | SEC 秒后中断并关闭会话 |
| `--provider VENDOR` | 这次运行使用的供应商 |
| `--resume SESSION_ID` | 接着一个已保存的会话继续，而不是新开一个 |
| `--approve-none` | 拒绝每一次审批，而不是询问 |
| `--home DIR` | 覆盖 `SNOWPEA_HOME` |

## 它做了什么

它确保守护进程在运行，在 `--cwd` 下创建一个会话，发送 prompt，把陆续到达的 `session.event` 通知渲染出来，并在回合结束时关闭会话。不会启动 TUI，所以不需要 Node。

斜杠命令本身就是一个合法的 prompt，因为命令注册表属于核心而不属于 UI：

```bash
snowpea -c "/ralph add a failing test then make it pass" --mode auto
snowpea -c "/deepinit"
```

## 接着一个已保存的会话继续

`-c` 通常会开一个全新的会话。`--resume` 接着你已经有的那个继续，它是 TUI 中 `/resume` 的无界面那一半：已保存的历史被重新载入，prompt 追加在它后面。

```bash
snowpea session list --include-closed
snowpea -c "and now write the tests" --resume s-4f2c9a1b7e30
```

与 `--resume` 一起使用时，`--mode` 和 `--provider` 会被忽略——已保存的会话保留它自己的。`--resume` 单独使用不会做任何事：在 TUI 里请用 `/resume` 以交互方式挑一个会话。

## 已保存的会话

会话关闭后仍留在 `$SNOWPEA_HOME/state.db` 中，它们的附件和语音则在 `$SNOWPEA_HOME/attachments/<id>/` 和 `$SNOWPEA_HOME/audio/<id>/` 下。删除一个已保存的会话会把这些一并删掉。

```bash
snowpea session list                                  # live sessions
snowpea session list --include-closed --json          # plus the saved ones
snowpea session list --include-closed --workdir ~/src/api
snowpea session delete s-4f2c9a1b7e30                 # one saved session
snowpea session clear --workdir ~/src/api             # every saved session of one project
snowpea session clear --all                           # every saved session, everywhere
```

活着的会话绝不会被删除；先关掉它。每一行都会打印会话 id、模式、创建时间、工作目录，以及它最后看到的那条 prompt。

## Team 与模型配置

项目 team 和按 agent 的模型路由都是设置项，所以不用 UI 也能配置。

```bash
snowpea team list                                     # global + project teams, * marks the active one
snowpea team create delivery architect executor verifier
snowpea team use delivery
snowpea team delete delivery

snowpea model profiles --json                         # profiles, the default, per-agent assignments
snowpea model default fast                            # models.default
snowpea model assign executor deep                    # agents.models.executor
```

`snowpea team create` 写的是 `<workdir>/.snowpea/settings.json`——和 `/team create` 写的是同一个文件——并且会拒绝它找不到的 agent 名字。`snowpea team delete` 只删除项目 team；全局 team 要在 `$SNOWPEA_HOME/settings.json` 里编辑。如果 profile id 不存在，`snowpea model assign` 会被守护进程拒绝。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 回合完成 |
| `1` | agent 以失败告终 |
| `2` | 用法或配置错误 |
| `3` | 无法连接到守护进程 |
| `4` | 一次审批被拒绝，或者模式挡住了这个动作 |
| `5` | `--timeout` 到点；会话被中断并关闭 |

```bash
if snowpea -c "does this repo have a failing test?" --mode plan; then
  echo "clean"
else
  echo "exit $?"
fi
```

值得琢磨的是 `4`。在 plan 模式下，一次写入尝试会产生 `mode_denied` 错误并以 `4` 退出，这让 plan 模式成为 CI 中一道可用的只读闸门。

## JSON 输出

加上 `--json` 后，每收到一个 `session.event` 就写出一行 JSON 对象，最后一行是一条结果记录：

```json
{"kind":"message.delta","sessionId":"…","seq":12,"payload":{"text":"Looking at "}}
{"kind":"tool.call","sessionId":"…","seq":13,"payload":{"callId":"c1","name":"grep","args":{"pattern":"def main"}}}
{"kind":"tool.result","sessionId":"…","seq":14,"payload":{"callId":"c1","name":"grep","ok":true,"output":"…"}}
{"kind":"turn.done","sessionId":"…","seq":20,"payload":{"turnId":"t1","reason":"complete"}}
{"kind":"result","exitCode":0,"sessionId":"…","usage":{"inputTokens":4120,"outputTokens":380}}
```

事件 kind 有 `message.delta`、`message.done`、`tool.call`、`tool.result`、`diff`、`subagent.spawn`、`subagent.update`、`subagent.done`、`team.task.update`、`mode.changed`、`usage`、`error` 和 `turn.done`。`turn.done` 的 reason 是 `complete`、`interrupted`、`error`、`denied` 或 `timeout` 之一。完整的 payload schema 在 [docs/protocol.md](../../protocol.md) 里。

```bash
snowpea -c "list the modules" --json | jq -r 'select(.kind=="message.delta") | .payload.text' | tr -d '\n'
```

## 没有人在的审批

有 TTY 时，一次审批请求会变成 stdin 上的一个 y/n 提示。没有 TTY 时——管道、CI runner——没人可问，于是请求会立刻被拒绝，进程以 `4` 退出。如果这正是你要的，就明说：

```bash
snowpea -c "run the linter" --approve-none
```

自动化场景下诚实的两种配置是：只需要读的事情用 plan 模式，以及在一个你愿意丢弃的容器里用 auto 模式。容器那一半见 [Backends](backends.md)。

## 不需要会话的子命令

有些子命令只调用一个 RPC 方法就退出，不创建会话，也不联系模型。它们快、确定、而且免费，这让它们成为安装后最合适的冒烟测试：

```bash
snowpea --version
snowpea tools list --json
snowpea commands list --json
snowpea daemon status --json
```

## 在 CI 里

```yaml
- run: curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh
- run: snowpea --version
- run: snowpea tools list --json
- run: snowpea -c "/deep-research whether this dependency has a known CVE" --mode plan --timeout 600 --json
```

把 `SNOWPEA_HOME` 设成任务本地的目录，这样各次运行不会共享状态；另外记住，你这一步结束之后守护进程仍在运行——如果 runner 是长期存活的，请在任务末尾执行 `snowpea daemon stop`。

## 下一步

[Protocol](protocol.md) —— CLI 实际上说的是什么语言。
