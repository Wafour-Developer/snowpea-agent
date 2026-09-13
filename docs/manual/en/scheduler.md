# Scheduler

The scheduler lives inside the daemon. Jobs run in the daemon process, in the mode you registered them with, and deliver their answer to a channel you name. Nothing runs in a separate cron daemon and nothing needs your terminal to be open.

## Registering a job

```bash
snowpea job schedule --at "0 9 * * *" --task "summarize yesterday's commits" --channel telegram:123456
snowpea job schedule --in 10m --task "check whether the build is green" --mode plan
snowpea job schedule --every 30m --task "watch the error log" --channel log --workdir ~/src/api
```

From inside a session:

```
/schedule "every day at 9am" "summarize yesterday's commits" --channel telegram:123456
/schedule
```

`--at`, `--in`, `--every`, `--cron` and `--spec` are five names for the same option. Use whichever makes the line read correctly.

## Specs

| Form | Example |
|---|---|
| cron, five fields | `0 9 * * *` |
| one-shot delay | `in 60s`, `in 10m`, `in 2h` |
| interval | `every 30m`, `every 6h` |
| natural language, English | `every day at 9am`, `in 10 minutes` |
| natural language, Korean | `매일 09:00`, `10분 뒤` |

A job is `cron`, `once` or `interval` depending on which it parsed as. `job list` shows the parsed kind and the next run time, which is the fastest way to check that a natural-language spec meant what you thought.

```bash
snowpea job list
snowpea job list --json
```

## Managing jobs

```bash
snowpea job run <job-id>
snowpea job cancel <job-id>
```

`job run` fires the job immediately in the daemon, exactly as the timer would. It is the right way to test a job before trusting it to a schedule.

Each job records `last_run` and `last_status`, which is `ok`, `error`, or `denied_by_timeout` when an approval expired unanswered.

## How a job executes

The scheduler ticks every 15 seconds. When a job is due it creates a session in the job's working directory and mode, marked unattended, prompts it with the job's task, and delivers the final assistant message to the channel. `job.event` notifications with kind `started`, `finished`, `failed` or `denied` go to every attached client, so a running TUI shows the job's progress.

Two things are worth knowing. First, an occurrence key of job id plus scheduled time is unique, so a job cannot fire twice for the same slot even if the daemon restarts mid-tick. Second, on start-up the scheduler catches up one-shot jobs it missed while the daemon was down, if they are less than an hour late; older ones are marked missed rather than run.

## Reminders come back to the session that asked

A job remembers where it came from. Whichever session registered it — a `/schedule` typed into a TUI session, or the `schedule` tool used inside a turn — is stamped on the job as `originSessionId`, and `job list --json` shows it. When the job fires, its answer is emitted into that session as a `message.done` event whose text is prefixed:

```text
⏰ Scheduled reminder (job_7f21c0)

Yesterday's commits: 14 across three repositories…
```

So the reminder lands in the thread you asked from, and it is persisted there like any other message: open that session later and the reminder is in its history.

The session does not have to still be open. A closed session is restored purely to take the delivery and is closed again immediately afterwards, so a firing job never leaves a session running behind your back. A live session is left alone.

An explicit `--channel` is additive rather than a replacement. The originating session gets the reminder either way, and the channel gets the same text as well. When there is no origin session — a job registered before this existed, or one whose session has been deleted — and the channel could not be delivered, the text falls back to the jobs log at `$SNOWPEA_HOME/logs/jobs.log`.

## Channels

| Channel | Goes to |
|---|---|
| `telegram:<chat_id>` | a Telegram chat |
| `discord:<channel_id>` | a Discord channel |
| `slack:<channel>` | a Slack channel |
| `log` | `$SNOWPEA_HOME/logs/daemon.log` only |

The platform must be bound first — see [Gateway](gateway.md). `log` needs nothing and is the right choice while you are still working out what a job should say.

## Modes and approvals

A job carries its own mode, independent of the session you registered it from. Registering a job at all needs a `send` permission, so in accept mode you will be asked to approve the registration itself; that is deliberate, because a scheduled job is a standing grant of whatever its mode allows.

When a job's turn hits something that needs approval, the request is unattended: it appears in the TUI approval queue and in the bound chat with allow/deny buttons at the same time. Whoever answers first wins, everyone else is told what happened, and if nobody answers within `approvals.timeoutSec` (300 seconds) the request is denied and the job's `last_status` becomes `denied_by_timeout`.

A job in plan mode can read and search but never write. A job in auto mode asks nothing at all. For anything unattended, plan is the safe default and auto deserves a container.

## Keeping the daemon alive

Enabled jobs are one of the four counters that suppress the idle shutdown, so a daemon with a schedule stays up on its own:

```bash
snowpea daemon status
```

That prints `will not exit` with the reason. Surviving a reboot is a separate question — for that, register the service:

```bash
snowpea service install
snowpea service status
```

## Next

[Gateway](gateway.md) — where the answers go.
