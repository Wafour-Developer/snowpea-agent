# Commands

There are two command surfaces. Slash commands run inside a session and are owned by the core, so the same `/ralph` behaves identically in the TUI, in `snowpea -c`, in a scheduled job and in a Telegram message. CLI subcommands inspect and configure the daemon without opening a session at all.

```bash
snowpea commands list
snowpea commands list --json
```

That prints the live registry, including commands contributed by installed plugins. `/help` inside the UI prints the same thing.

## Slash commands

### Session and mode

| Command | What it does |
|---|---|
| `/help` | list every available command |
| `/tools` | list registered tools with category, permission and state |
| `/plan`, `/accept`, `/auto` | switch mode |
| `/mode [plan\|accept\|auto\|save\|show]` | show, switch, or save the project default |
| `/approvals` | list unattended approvals waiting for an answer |
| `/allow <regex> [--global]` | promote a repeated prompt to a silent allow |
| `/allowlist [remove <id>]` | show or prune the allowlist |
| `/backend [local\|docker\|ssh] [json]` | show or change where tools execute |

### Work

| Command | What it does |
|---|---|
| `/ralph <task>` | PRD loop: stories with acceptance criteria, implement, verify, review until APPROVE |
| `/ultrawork <task>` | split into independent parts, run them on concurrent subagents, merge the reports |
| `/deepinit [path]` | walk the repository and write hierarchical `AGENTS.md` files |
| `/team <n> <task>` | n workers, one git worktree each, branches merged as tasks finish |
| `/deep-interview <idea>` | Socratic interview that scores ambiguity and refuses to hand off until the spec holds |
| `/deep-research <topic>` | multi-source web research fanned out over subagents, answered with citations |
| `/ralplan <task>` | consensus planning — planner, architect and critic argue before any code is written |

`/ralph` keeps its state in `<project>/.snowpea/ralph/` as `prd.json` and `progress.md`, so you can read what it thinks it is doing, and it stops at `ralph.max_iterations` (10) if it cannot converge. `/ultrawork` and `/deepinit` fan out under `agents.max_concurrent` (3 by default). The last three are `SKILL.md` files under `core/snowpea_core/builtin_skills/`, loaded by the same loader your own skills use — read them, copy them, change them.

### Generators

| Command | What it does |
|---|---|
| `/agent create "<description>"` | write an agent definition into `<project>/.snowpea/agents/<name>.md` |
| `/agent list` | list agent definitions |
| `/skill learn [name]` | turn the session you just finished into `<project>/.snowpea/skills/<name>/SKILL.md` |

A generated agent is a `delegate_task` target immediately, no reload needed.

### Scheduling

| Command | What it does |
|---|---|
| `/schedule "<spec>" "<task>" [--channel X] [--mode M]` | register a job |
| `/schedule` | list jobs, or cancel one |

## CLI subcommands

### Inspecting

```bash
snowpea --version
snowpea tools list --json
snowpea commands list --json
snowpea agents --json
snowpea daemon status --json
```

`tools list` and `commands list` call one RPC method each and exit. They create no session and call no model, which makes them the right smoke test after an install or in CI.

### Daemon

```bash
snowpea daemon status
snowpea daemon start
snowpea daemon stop
```

`status` prints the port, pid, uptime, the four keepalive counters — sessions, jobs, gateway bindings, named agents — and whether the daemon intends to exit, with the reason it will not.

### Providers

```bash
snowpea provider list
snowpea provider login openai
snowpea setup --vendor deepseek --key sk-...
```

### Skills and plugins

```bash
snowpea skill list
snowpea skill search "pdf"
snowpea skill install oh-my-claudecode
snowpea skill install ./my-plugin
snowpea skill remove my-plugin
```

### Jobs

```bash
snowpea job schedule --at "0 9 * * *" --task "summarize yesterday's commits" --channel telegram:123456
snowpea job schedule --in 10m --task "check the build" --mode plan
snowpea job list --json
snowpea job run <job-id>
snowpea job cancel <job-id>
```

`--at`, `--in`, `--every`, `--cron` and `--spec` are the same option under five names; use whichever reads best for the schedule you are writing.

### Gateway

```bash
snowpea gateway bind telegram TELEGRAM_BOT_TOKEN agent:scribe --user 987654
snowpea gateway list --json
snowpea gateway unbind <binding-id>
```

### Team and service

```bash
snowpea team status
snowpea service install
snowpea service status
snowpea service uninstall
```

`team status` reports each task's state and retry count for a running team. `service` registers the daemon to start at login — a systemd user unit on Linux, a launchd agent on macOS, a scheduled task on Windows. It is off by default, and you only need it if you want schedules and gateways to survive a reboot without anyone logging into a terminal.

### Global options

| Option | Meaning |
|---|---|
| `--version` | print the version and exit |
| `--home DIR` | override `SNOWPEA_HOME` for this invocation |
| `--mode plan\|accept\|auto` | mode for the session being started |
| `-c`, `--prompt TEXT` | run one headless turn and exit |
| `--json` | emit JSON Lines instead of prose |
| `--cwd DIR` | working directory of the session |
| `--timeout SEC` | abort the turn after SEC seconds |
| `--provider VENDOR` | vendor for this session |
| `--approve-none` | deny every approval instead of prompting |

## Running a slash command headlessly

Because the registry lives in the core, a slash command is a valid headless prompt:

```bash
snowpea -c "/ralph add a failing test then make it pass" --mode auto
snowpea -c "/deepinit" --json
```

The CLI does not parse it. It hands the text to the core, which dispatches it exactly as the TUI would.

## Next

[Plugins](plugins.md) — adding commands of your own.
