# snowpea manual — index

Every manual page, in every language it exists in. All five languages — English, Korean, Japanese, Simplified Chinese and Spanish — now cover every page.

Start at [English](en/index.md) or [한국어](ko/index.md).

## Pages

| Page | What is in it | English | 한국어 | 日本語 | 简体中文 | Español |
|---|---|---|---|---|---|---|
| Index | what snowpea is, where files live, how to get help | [en](en/index.md) | [ko](ko/index.md) | [ja](ja/index.md) | [zh-CN](zh-CN/index.md) | [es](es/index.md) |
| Install | one-liner, manual install, upgrading, uninstalling, troubleshooting | [en](en/install.md) | [ko](ko/install.md) | [ja](ja/install.md) | [zh-CN](zh-CN/install.md) | [es](es/install.md) |
| Setup | wizard screens, eleven vendors, browser login, search and browser providers, tool categories | [en](en/setup.md) | [ko](ko/setup.md) | [ja](ja/setup.md) | [zh-CN](zh-CN/setup.md) | [es](es/setup.md) |
| Modes | plan/accept/auto, permission matrix, approvals, allowlist | [en](en/modes.md) | [ko](ko/modes.md) | [ja](ja/modes.md) | [zh-CN](zh-CN/modes.md) | [es](es/modes.md) |
| Terminal UI | launch screen, layout, keys, approvals, diffs, context, attachments, voice | [en](en/tui.md) | [ko](ko/tui.md) | [ja](ja/tui.md) | [zh-CN](zh-CN/tui.md) | [es](es/tui.md) |
| Commands | every slash command and CLI subcommand | [en](en/commands.md) | [ko](ko/commands.md) | [ja](ja/commands.md) | [zh-CN](zh-CN/commands.md) | [es](es/commands.md) |
| Attachments and voice | images and files in a prompt, speech to text, text to speech, the audio tools | [en](en/voice.md) | [ko](ko/voice.md) | [ja](ja/voice.md) | [zh-CN](zh-CN/voice.md) | [es](es/voice.md) |
| Plugins | Claude Code plugin format, SKILL.md, hooks, MCP servers (`/mcp`, `snowpea mcp`), marketplaces | [en](en/plugins.md) | [ko](ko/plugins.md) | [ja](ja/plugins.md) | [zh-CN](zh-CN/plugins.md) | [es](es/plugins.md) |
| Agents and delegation | one task one agent, the built-in `explore` and `reviewer` agents, `/review`, team review | [en](en/agents.md) | [ko](ko/agents.md) | — | — | — |
| Language servers | diagnostics after every edit, the seven `lsp_*` tools, per-server settings | [en](en/lsp.md) | [ko](ko/lsp.md) | — | — | — |
| Scheduler | cron and natural-language jobs, delivery channels, unattended modes | [en](en/scheduler.md) | [ko](ko/scheduler.md) | [ja](ja/scheduler.md) | [zh-CN](zh-CN/scheduler.md) | [es](es/scheduler.md) |
| Gateway | Telegram, Discord, Slack, named agents, unattended approvals | [en](en/gateway.md) | [ko](ko/gateway.md) | [ja](ja/gateway.md) | [zh-CN](zh-CN/gateway.md) | [es](es/gateway.md) |
| Backends | local, Docker, SSH | [en](en/backends.md) | [ko](ko/backends.md) | [ja](ja/backends.md) | [zh-CN](zh-CN/backends.md) | [es](es/backends.md) |
| Headless | `-c`, JSON Lines, exit codes, CI | [en](en/headless.md) | [ko](ko/headless.md) | [ja](ja/headless.md) | [zh-CN](zh-CN/headless.md) | [es](es/headless.md) |
| Protocol | handshake, methods, events, versioning and the freeze gate | [en](en/protocol.md) | [ko](ko/protocol.md) | [ja](ja/protocol.md) | [zh-CN](zh-CN/protocol.md) | [es](es/protocol.md) |

## Not in the manual

| Document | What it is |
|---|---|
| [docs/protocol.md](../protocol.md) | the generated protocol reference — every method, result and event schema |
| [docs/ARCHITECTURE.md](../ARCHITECTURE.md) | module map, diagrams, protocol versioning |
| [docs/CONTRIBUTING.md](../CONTRIBUTING.md) · [한국어](../CONTRIBUTING.ko.md) | dev setup, checks, how to add a vendor, tool, command or search provider |
| [docs/vendoring-map.md](../vendoring-map.md) | hermes-agent: upstream path, commit, hash, patch, reason |
| [docs/omc-porting-map.md](../omc-porting-map.md) | oh-my-claudecode: original concept to snowpea replacement |

## READMEs

[English](../../README.md) · [한국어](../../README.ko.md) · [日本語](../../README.ja.md) · [简体中文](../../README.zh-CN.md) · [繁體中文](../../README.zh-TW.md) · [Español](../../README.es.md) · [Français](../../README.fr.md) · [Deutsch](../../README.de.md) · [Português (BR)](../../README.pt-BR.md) · [Русский](../../README.ru.md)

## Keeping the manual honest

`scripts/check_docs_cli.py` reads every fenced code block in `README*.md` and `docs/manual/**/*.md`, extracts each `snowpea …` invocation, and checks the subcommand and every flag against the CLI's own `--help` output. It also verifies that every relative link in those files resolves to a file that exists. It runs in the test suite.

```bash
uv run python scripts/check_docs_cli.py
uv run pytest tests/test_docs_cli.py -q
```

A command that the docs describe but the CLI does not have yet fails this check, which is the point. Anything genuinely still landing is listed in an allowlist inside the script, with the story that will remove it.
