# snowpea manual

snowpea is an open-source, multi-vendor coding agent that runs on your own machine. A Python core runs as a local daemon and owns everything stateful — sessions, tools, permissions, memory, schedules, messenger bindings. Clients attach to it over a documented WebSocket JSON-RPC protocol: an Ink terminal UI today, an Electron IDE in v0.2, and whatever you build on the TypeScript SDK.

Other languages: [한국어](../ko/index.md) · [日本語](../ja/index.md) · [简体中文](../zh-CN/index.md) · [Español](../es/index.md) · [all pages](../README.md)

## Where to start

If you have just installed snowpea, read [Install](install.md), then [Setup](setup.md), then [Modes](modes.md). That is enough to work with it every day. Everything after that is optional surface.

| Page | Read it when |
|---|---|
| [Install](install.md) | installing, upgrading, or an install step failed |
| [Setup](setup.md) | choosing a vendor, adding an API key, logging in through the browser, picking search and browser providers |
| [Modes](modes.md) | the agent asks too much, or not enough |
| [Terminal UI](tui.md) | the keys, the panels, attachments, voice, and what the screen is telling you |
| [Commands](commands.md) | you want the full list of slash commands and CLI subcommands |
| [Attachments and voice](voice.md) | sending images and files in a prompt, speaking to the agent and having it speak back |
| [Plugins](plugins.md) | installing or writing skills, agents, commands, hooks and MCP servers |
| [Language servers](lsp.md) | diagnostics after an edit, and the lsp_* tools |
| [Scheduler](scheduler.md) | you want work to happen while you are away |
| [Gateway](gateway.md) | you want to talk to the agent from Telegram, Discord or Slack |
| [Backends](backends.md) | the code lives in a container or on another host |
| [Headless](headless.md) | scripting snowpea, or wiring it into CI |
| [Protocol](protocol.md) | building a client against the daemon |

## The shape of the thing

```text
snowpea              → starts the daemon if needed, attaches the terminal UI
snowpea -c "..."     → one headless turn, no UI, deterministic exit code
snowpea <subcommand> → inspect or configure without opening a session
```

The daemon is lazy. It starts on the first client, and shuts itself down after an idle period — but only when there is nothing to keep alive: no open sessions, no enabled jobs, no gateway bindings, no named agents. `snowpea daemon status` tells you which of those is holding it open.

```bash
snowpea daemon status
```

## Where things live

| Path | What |
|---|---|
| `$SNOWPEA_HOME` (default `~/.snowpea`) | everything global |
| `$SNOWPEA_HOME/settings.json` | providers, search and browser choice, tool categories, timeouts |
| `$SNOWPEA_HOME/credentials.json` | bot tokens and secrets, mode `0600` |
| `$SNOWPEA_HOME/daemon.json` | port, pid, token, start time, protocol version |
| `$SNOWPEA_HOME/state.db` | sessions, events, memory, jobs, team tasks, named agents |
| `$SNOWPEA_HOME/logs/` | `daemon.log`, `approvals.jsonl` |
| `$SNOWPEA_HOME/plugins/`, `skills/`, `agents/`, `commands/` | installed and hand-written extensions |
| `<project>/.snowpea/settings.json` | default mode, allowlist, backend for this repository |
| `<project>/.snowpea/{skills,agents,commands}/` | project-local extensions |
| `<project>/.claude/{skills,agents,commands}/` | read for Claude Code compatibility |

`SNOWPEA_HOME` is honoured everywhere, so a second, isolated installation is one environment variable away:

```bash
SNOWPEA_HOME=/tmp/snowpea-scratch snowpea daemon status
```

## Getting help

`/help` inside the UI lists every command the core currently has, including the ones your plugins added. From a shell, `snowpea commands list` prints the same registry, and `snowpea tools list` prints the tools with their permission tag and whether they are active.

```bash
snowpea commands list
snowpea tools list --json
```

Bugs and questions belong in [GitHub issues](https://github.com/Wafour-Developer/snowpea-agent/issues). If you want to change the code, [Contributing](../../CONTRIBUTING.md) and [Architecture](../../ARCHITECTURE.md) are the two documents to read first.
