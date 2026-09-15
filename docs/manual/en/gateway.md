# Messenger gateway

The gateway connects a chat platform to a session or a named agent. Messages in that chat become prompts; the agent's answers come back as messages; approvals arrive as buttons. Telegram is the fully supported platform in v0.1. Discord and Slack implement the same interface and work for delivery, with less field time behind them.

## The short way

```bash
snowpea setup --gateway telegram --token 123456:ABC-your-bot-token --user-id 987654
snowpea            # or: snowpea daemon start
```

That is the whole setup. The wizard records the messenger in `settings.json`, and the daemon turns it into a *catch-all* binding when it starts: any chat that messages the bot gets its own session, in `$HOME` unless you set `gateway.telegram.workdir`. `snowpea daemon status` says `messengers   telegram (listening)` once it is up. The token itself is copied into `$SNOWPEA_HOME/credentials.json` with mode `0600` and referred to by name everywhere else, so it never appears in `state.db`, in an RPC result, or in a log line.

`--user-id` is your own numeric account id on that platform, and it is the only account allowed to answer an approval from chat. Telegram tells you yours if you send `/start` to [@userinfobot](https://t.me/userinfobot). Leave it out and the messenger still talks, but every approval button press is refused, including your own.

Turning the messenger off in the wizard (or with `settings.set`) removes that binding again. Bindings you made by hand are never touched by this.

## Binding by hand

Use this when you want something other than one catch-all session: a named agent, one specific chat, or two accounts on one platform.

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

**Discord.** Create an application, add a bot user, enable the message content intent, invite it to your server, keep the bot token. Invite it with both the `bot` and the `applications.commands` scopes — the OAuth2 URL generator in the developer portal builds that link — because the second scope is what lets the chat commands appear in Discord's `/` picker. Snowpea registers them when the binding starts, so `/sessions`, `/resume`, `/new`, `/projects`, `/status`, `/stop`, `/help`, `/mode`, `/model` and `/effort` are listed there with a free-text `args` field; a command answered this way replies inside the command itself. Without that scope nothing breaks: typing `/sessions` as ordinary text still works as long as the message content intent is on. The channel id is the last path segment of a channel URL.

**Slack.** Create an app, add `chat:write` and the events your workspace needs, install it, keep the bot token. Use the channel name or id as the target.

## Talking to it

Send the bot a message and you are in a session. Slash commands work exactly as they do in the terminal, because the command registry lives in the core:

```
/mode
/ralph fix the failing integration test
/schedule "매일 09:00" "어제 커밋 요약" --channel telegram:123456
```

Sessions created by a gateway show up in `session.list` with their origin, so a TUI attached to the same daemon can see what the chat is doing.

## Chat commands

A terminal answers "which conversation am I in, what else is open, put me in another one" with its own window. A chat has no window, so seven commands answer it instead. They are handled by the gateway itself, before the command registry, and they never reach the model.

| Command | What it does |
|---|---|
| `/sessions` | The ten most recent conversations, `★` on the one this chat is in. Each row is also a button; a bare number right after the list picks that row. |
| `/resume <n \| id \| prefix>` | Point this chat at another session. A closed one is reopened. |
| `/new [path \| project \| n]` | Start a session: in a path you type, in a project from `/projects`, or — with no argument — in the binding's own workdir. |
| `/projects` | Known projects: the IDE's `ide.projects` list merged with the workdirs of recent sessions, pinned first. Each row starts a session there. |
| `/status` | Session id, workdir, mode, model, effort, whether a turn is running, what is queued, context used. |
| `/stop` | Interrupt the running turn and drop whatever was queued behind it. The same Stop the TUI has. |
| `/help` | These, plus the session commands worth typing on a phone. |

Telegram publishes them as its `/` menu when the bot starts, so they are offered rather than memorised.

`/resume`, `/new`, `/stop` and the row buttons are accepted only from the bound `--user`, for the same reason approvals are: they choose what the next message will run and where. `/sessions`, `/projects`, `/status` and `/help` are open to anyone in the conversation.

Which session a chat is in survives a daemon restart. The choice is one line in `$SNOWPEA_HOME/gateway-chats.json`; delete the file and every chat falls back to its binding's own target.

## Typing and progress

While a turn runs the chat shows the platform's "typing…" hint, refreshed every few seconds and paused whenever an approval or a question is waiting on you — the agent is not working while you decide. On the turn's first tool call one message goes out (`⏳ shell npm test`), and every later tool call *edits* that same message rather than sending another. At the end it settles on `✓ 4 tool calls · 1m 12s`, or `✗` when the turn failed or was stopped.

A platform that cannot edit a message gets no progress message at all, because without editing the feature is a stream of chat spam. Slack shows no typing hint either: bot tokens cannot send one.

Two settings turn them off:

```json
{ "gateway": { "typing": false, "progress": false } }
```

Both default to `true`. They sit beside the per-platform blocks in `settings.gateway` and are switches, not messengers.

## Unattended approvals

An approval raised by a gateway or scheduler session is unattended, and behaves differently from one you triggered by typing:

- It is broadcast as an `approval.pending` notification to every attached client, so the TUI approval queue shows it.
- It is sent to the bound chat with allow and deny buttons.
- The first answer from either side wins. Everyone else receives `approval.resolved` naming the decision and who made it.
- Only the bound `--user` may answer from chat. Presses from anyone else are ignored and logged.
- After `approvals.timeoutSec` (300 seconds by default) with no answer, it is denied.

Approvals you raise by typing in the TUI stay on that surface and never enter the shared queue. That boundary is the point: your own terminal prompts do not leak into a group chat, and a chat approval cannot be answered by a stranger.

Everything decided is appended to `$SNOWPEA_HOME/logs/approvals.jsonl` with the request id, tool, decision, who decided, scope, and whether it was unattended.

## Questions from the agent

The `ask_user` tool is the agent asking *you* something, and a bound chat gets it as a message with one button per option:

```text
❓ Renderer
What should the renderer be? This decides how much engine control you keep.
1. three.js (recommended) — fast start, little control over the engine
2. Raw WebGL2 — more work, complete control
(reply with the number, or type your own answer)
[1. three.js (recommended)] [2. Raw WebGL2] [✏️ 기타 / Other]
```

Unlike an approval, anyone in the conversation may answer: a question grants no permission, so there is nothing for a stranger to abuse. Press a button, or type the number — `2`, or `1,3` when the question takes several answers. Anything that is not a number is taken as a free-text answer, which is also what the "Other" button asks you for.

When one `ask_user` call carries several questions, the terminal shows them as tabs; a chat has no tabs, so the gateway posts them one at a time — the header line counts them (`❓ Storage (2/3)`) and the next question only appears once the current one is answered. All the answers go back together when the last one is in. After `questions.timeoutSec` (600 seconds by default) the agent is told nobody answered.

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
