# Messenger gateway

The gateway connects a chat platform to a session or a named agent. Messages in that chat become prompts; the agent's answers come back as messages; approvals arrive as buttons. Telegram is the fully supported platform in v0.1. Discord and Slack implement the same interface and work for delivery, with less field time behind them.

## Credentials first

```bash
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token
```

The token goes into `$SNOWPEA_HOME/credentials.json` with mode `0600`. Alternatively export it and refer to the environment variable by name when binding. Either way the token is never written to `settings.json` and never appears in a log line.

## Binding

```bash
snowpea gateway bind telegram TELEGRAM_BOT_TOKEN agent:scribe --user 987654
snowpea gateway bind telegram TELEGRAM_BOT_TOKEN new:~/src/api --channel 123456 --user 987654
snowpea gateway list --json
snowpea gateway unbind <binding-id>
```

The three positional arguments are the platform, the credentials reference (a key in `credentials.json` or the name of an environment variable), and the target:

| Target | Meaning |
|---|---|
| `agent:<name>` | a named persistent agent, with its own memory namespace |
| `session:<id>` | an existing session |
| `new:<workdir>` | create a session in that directory on the first message |

`--channel` restricts the binding to one chat id. `--user` names the platform user allowed to approve things. Set both. Without `--user` there is nobody authorised to answer an approval, and the gateway will ignore button presses from anyone.

## Getting a bot

**Telegram.** Talk to [@BotFather](https://t.me/BotFather), `/newbot`, keep the token. Your chat id is the number the bot sees when you message it, visible in the daemon log on the first inbound message. Long polling is used, so no public URL and no webhook.

**Discord.** Create an application, add a bot user, enable the message content intent, invite it to your server, keep the bot token. The channel id is the last path segment of a channel URL.

**Slack.** Create an app, add `chat:write` and the events your workspace needs, install it, keep the bot token. Use the channel name or id as the target.

## Talking to it

Send the bot a message and you are in a session. Slash commands work exactly as they do in the terminal, because the command registry lives in the core:

```
/mode
/ralph fix the failing integration test
/schedule "매일 09:00" "어제 커밋 요약" --channel telegram:123456
```

Sessions created by a gateway show up in `session.list` with their origin, so a TUI attached to the same daemon can see what the chat is doing.

## Unattended approvals

An approval raised by a gateway or scheduler session is unattended, and behaves differently from one you triggered by typing:

- It is broadcast as an `approval.pending` notification to every attached client, so the TUI approval queue shows it.
- It is sent to the bound chat with allow and deny buttons.
- The first answer from either side wins. Everyone else receives `approval.resolved` naming the decision and who made it.
- Only the bound `--user` may answer from chat. Presses from anyone else are ignored and logged.
- After `approvals.timeoutSec` (300 seconds by default) with no answer, it is denied.

Approvals you raise by typing in the TUI stay on that surface and never enter the shared queue. That boundary is the point: your own terminal prompts do not leak into a group chat, and a chat approval cannot be answered by a stranger.

Everything decided is appended to `$SNOWPEA_HOME/logs/approvals.jsonl` with the request id, tool, decision, who decided, scope, and whether it was unattended.

## Named agents

A named agent survives daemon restarts with its own session, its own memory namespace, its own channels and its own jobs. Two named agents cannot read each other's memories, which is what makes it reasonable to give one a work channel and another a personal one.

```bash
snowpea agents --json
```

Create one with `/agent create "<description>"`, bind a channel to it with `gateway bind ... agent:<name>`, and it will be restored on the next daemon start along with its bindings. Named agents are the fourth keepalive counter, so a daemon with one never idles out.

## Security

The gateway is a remote execution path. Treat it that way.

- Bind `--user`. Always.
- Prefer plan mode for anything reachable from a chat you do not fully control.
- Approval requests are single-use and tied to one request id; a replayed button press does nothing.
- Credentials live in a `0600` file, and the OS keychain is used where available.
- Read `approvals.jsonl` occasionally. It is the record of what was allowed while you were not looking.

## Next

[Backends](backends.md) — running the tools somewhere else.
