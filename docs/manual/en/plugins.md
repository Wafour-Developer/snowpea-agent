# Plugins and skills

snowpea reads the Claude Code plugin layout directly. A plugin written for Claude Code installs and runs here without modification, and a repository that already has a `.claude/` directory works with no changes at all.

## Layout

```
my-plugin/
  plugin.json            # {"name": "...", "version": "...", "description": "..."}
  skills/<name>/SKILL.md # a skill, which becomes a /<name> command
  agents/*.md            # agent definitions, usable as delegate_task targets
  commands/*.md          # plain markdown commands
  hooks/hooks.json       # PreToolUse / PostToolUse / Stop
  .mcp.json              # MCP servers this plugin brings
```

`.claude-plugin/plugin.json` is accepted in place of `plugin.json`. A marketplace repository adds a `marketplace.json` at its root listing `plugins: [{name, source}]`.

## Creating a skill

```bash
/skill create <name> "<what it should do>"
/skill create pdf-merge "merge PDF files with pdftk, checking each one is valid first" --global
/skill create notes "summarise a session into release notes" --force
```

One provider turn writes a complete `SKILL.md` — frontmatter (`name`, `description`, `version`) plus a "When to use this" / "Procedure" / "Inputs" / "Checks" body, in the same agentskills.io / Claude Code format `/skill learn` and a hand-installed plugin both use — and the daemon validates the frontmatter itself (the same check `/skill publish` runs) before writing anything. It lands at `<project>/.snowpea/skills/<name>/SKILL.md`; add `--global` to write `$SNOWPEA_HOME/skills/<name>/SKILL.md` instead, so it is available to every project. An existing `SKILL.md` at the target is left alone unless `--force` is given. Plan mode reports what it would create or overwrite and writes nothing. The registry reloads once the file lands, so `/<name>` works immediately — no restart, same as an install.

`/skill learn [name]` is the other generator: instead of a brief, it summarises the session you just finished into a `SKILL.md` under the same layout.

The desktop app's editor uses `skill.create` (with `content` to save a hand-written document directly, or `description` to run the same generating turn — pass an existing `sessionId` to run it there, rooted at the same `workdir`, or leave it out and the daemon opens a headless one for you; either way it hands back `{turnId, sessionId}`), `skill.read` and `skill.write` — the RPC equivalents of the same paths, for a form-based skill editor.

## Installing

```bash
snowpea skill install ./my-plugin
snowpea skill install https://github.com/someone/their-plugin.git
snowpea skill install oh-my-claudecode
snowpea skill list --json
snowpea skill remove my-plugin
```

Installing copies the plugin into `$SNOWPEA_HOME/plugins/<name>` and reloads the registry. A reload emits a `commands.changed` notification, so the TUI refreshes its palette without a restart.

You can also ask the agent inside the TUI. "Find me a pdf skill" makes it call `skill_search`, and it answers with the candidates and the install spec of each one; "install the second one" makes it call `skill_install` with that spec. `skill_list` shows what is already installed and `skill_remove` deletes a plugin. Searching and listing are `read` tools and happen without a prompt; installing and removing are `exec`, so accept mode asks you first and plan mode refuses — the agent cannot install anything you did not approve. An install reloads in place, and the agent tells you which new `/commands`, agents and MCP servers it brought.

## Searching

```bash
snowpea skill search "pdf"
snowpea skill search "code review" --json
```

Two sources are queried together: `claude-marketplace` (the `marketplace.json` of every registered marketplace repository) and the hosted registry at `registry.snowpea.ai` — which is itself a *federation* of other skill hubs, so one call to it can return hits from several places at once. Each hit carries the `source` it actually came from: `local` (the registry's own published skills), `clawhub` (ClawHub), `claude-marketplaces` (GitHub-hosted Claude Code marketplaces the registry mirrors), shown by their human label (e.g. `ClawHub`) in `snowpea skill search`'s output. A hub, or the whole registry, being unreachable contributes nothing rather than failing the search — run `snowpea skill sources` to see which hubs are up.

```bash
snowpea skill search "planning" --source registry
snowpea skill sources
```

`--source registry` (or `--source snowpea` — both are the same alias) keeps only hosted-registry hits, across every hub it federates. `snowpea skill sources` lists each hub's id, label, enabled/disabled state (and why, if disabled), skill count and last sync status.

Registered marketplaces live in `$SNOWPEA_HOME/marketplaces.json`, seeded with the oh-my-claudecode marketplace.

> agentskills.io and hermes-hub adapters that shipped in an earlier version
> are gone: agentskills.io turned out to be the Agent Skills *specification*
> site with no skill-listing API, and hermes-hub.ai does not resolve at all.
> Both are handled — disabled, with the reason — on the registry's federation
> side instead of being guessed at here.

## Publishing to the registry

```bash
snowpea skill install registry:ralplan          # download and install by id
snowpea skill install clawhub:@cua/driver       # any federated hub's spec works too
snowpea setup tools                              # save a publisher token once (masked)
snowpea skill publish ./my-skill                 # zip + validate + upload
snowpea skill rate ralplan 5 --comment "great"   # 1-5 stars, one per caller
```

`skill install` accepts any install spec a search hit hands back. `registry:<id>` (a locally published skill) and `clawhub:<id>` resolve through the registry's own download proxy. `github:<owner>/<repo>[@plugin]` (what the registry hands back for a mirrored Claude Code marketplace item) is cloned directly with `git clone` instead — its registry id is not the same string as this spec, so asking the registry to download it would always 404. With no `@plugin` the whole repo is cloned; with one, only that plugin's directory is installed, resolved from a locally registered marketplace pointed at the same repo or, failing that, straight from the repo's own `marketplace.json` on GitHub. A hit's `id` (shown by `skill search --json`) is also always available if you would rather install a locally-published copy by id directly: `skill install registry:<id>`.

`publish` reads `<dir>/SKILL.md`, checks its frontmatter locally (`name` must
match `^[a-z0-9][a-z0-9._-]{1,63}$`, `description` must be 8-500 characters —
the same rules the registry enforces), zips the directory (`.git`,
`__pycache__`, `node_modules` and other build junk excluded), and uploads it
with `Authorization: Bearer <token>`. The token comes from `--token`, the
`SNOWPEA_REGISTRY_TOKEN` environment variable, or `settings.skills.registry.token`
(set once via `snowpea setup tools`'s masked prompt) — checked in that order.
Bump the `version` in the frontmatter before republishing; the registry
refuses a version it has already seen. `/skill publish <dir>` does the same
thing from inside a running session, resolving a relative path against the
session's working directory.

The registry base URL is `https://registry.snowpea.ai/v1` by default,
overridable per call with `--registry <url>`, for a whole shell with
`SNOWPEA_REGISTRY_URL`, or permanently via `settings.skills.registry.url`.

## SKILL.md

Front matter follows the [agentskills.io](https://agentskills.io) standard:

```markdown
---
name: changelog
description: Write a release changelog from the commits since the last tag.
argument-hint: "<tag>"
allowed-tools: [git_log, git_diff, read_file, write_file]
---

Collect the commits since $ARGUMENTS. Group them by type, drop noise,
and write CHANGELOG.md with the newest release on top.
```

The body becomes a `/changelog` command. `$ARGUMENTS` is substituted with whatever followed the command, the body is injected as an instruction, and a turn starts. `allowed-tools` is enforced for the duration of that turn — a skill that lists only read tools cannot write, whatever the mode allows. Omit it and the skill gets the session's normal tool set. `user-invocable: false` keeps a skill out of the command list while leaving it available to the agent.

## Agent definitions

```markdown
---
name: reviewer
description: Reviews a diff for correctness and missing tests.
model: inherit
tools: [read_file, grep, git_diff]
permission: plan
max_turns: 12
---

You review changes. Be specific, cite file and line, and say APPROVE or
list what must change. Never edit files yourself.
```

Drop it in `<project>/.snowpea/agents/reviewer.md`, or let `/agent create "reviews diffs for missing tests"` write one for you. Either way it becomes a `delegate_task` target, and `snowpea agents --json` lists it.

## Hooks

`hooks/hooks.json` uses the Claude Code shape:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "shell|write_file", "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/guard.sh"}]}
    ]
  }
}
```

The command receives `{event, tool_name, tool_input, session_id, cwd}` as JSON on stdin, plus `SNOWPEA_HOME` and `SNOWPEA_TOOL_NAME` in the environment. `PreToolUse` runs after the permission verdict and just before the tool; exit status 2 blocks the call, and its stderr becomes the error the model sees, so the turn continues with a refusal rather than dying. `PostToolUse` runs right after the tool returns, `Stop` when a turn ends with no further tool calls. Other hook events are parsed and ignored.

`${CLAUDE_PLUGIN_ROOT}`, `${SNOWPEA_PLUGIN_ROOT}` and `${SNOWPEA_PYTHON}` expand inside hook and MCP commands, with or without braces.

## MCP servers

`.mcp.json` uses the Claude Code format and is read from `$SNOWPEA_HOME`, from the project directory, and from each installed plugin:

```json
{
  "mcpServers": {
    "notes": {"command": "${SNOWPEA_PYTHON}", "args": ["-m", "my_notes_server"]},
    "remote": {"url": "https://example.internal/mcp"}
  }
}
```

Servers start lazily, are cached for the life of the daemon, and their tools appear as `mcp__<server>__<tool>`:

```bash
snowpea tools list --json
```

Permission defaults to `network` per server and can be overridden in settings under `mcp.permissions`.

### Adding one without editing JSON

`/mcp` does the same thing from inside a session, and `snowpea mcp` from a shell. Both write the same `.mcp.json`, so a file you hand-edited and one snowpea wrote are the same file.

```bash
snowpea mcp list
snowpea mcp add notes -- python -m my_notes_server
snowpea mcp add remote --url https://example.internal/mcp --header Authorization=Bearer-xxx
snowpea mcp add github --preset github --env GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxx
snowpea mcp test notes
snowpea mcp get notes
snowpea mcp disable notes
snowpea mcp remove notes
snowpea mcp catalog
```

Everything after a bare `--` is the command and its arguments, argv style; snowpea never builds a shell string out of it, and it refuses an entry that tries to (a `bash -c …` server needs `--force`). The same check also refuses the shapes a real MCP server never has: a shell script that fetches and runs code, one that writes to `~/.ssh/authorized_keys`, PAM, sudoers, cron or a shell rc file, and known indicators of compromise anywhere in the command, arguments or environment. `--scope global` writes `$SNOWPEA_HOME/.mcp.json` instead of the project's file, `--preset` starts from a curated catalog entry (`snowpea mcp catalog`), and `--no-test` saves without probing first. By default nothing is written until the server has answered `tools/list` once, so a typo in the command fails before it reaches the file.

Inside a session the same verbs are `/mcp`, `/mcp add <name> -- <command> [args…]`, `/mcp test <name>`, `/mcp enable|disable <name>`, `/mcp configure <name> [tool…]`, `/mcp reload [name]` and `/mcp catalog`. In the terminal UI, typing `/mcp add` with no arguments walks a form instead. The desktop app has the same surface under **Settings → MCP servers**, where a state dot per row updates live.

Secrets are never echoed back: `env` and `headers` stay in the file you chose, and every listing shows only their key names (`TOKEN=•••`). Servers declared by a plugin or by `mcp.servers` in settings are listed too, but they are read-only here — remove the plugin, or edit the settings file, instead.

Adding, removing or updating a server takes effect immediately: the old process is stopped, its tools leave the registry, and the new entry is picked up without restarting the daemon.

## The skills index and `skill_view`

Every installed skill is listed in the agent's system prompt, one line per skill, grouped by where it came from:

```
## Skills
…
<available_skills>
[project]
- review: How code review is done in this repo
[global]
- deploy: Ship the current branch
[plugin:pdfkit]
- pdf-split: Split a pdf
[builtin]
- init: Set a project up from nothing
</available_skills>
```

The agent is told to scan that list before it replies and to load anything even partially relevant with `skill_view`, which returns the skill's `SKILL.md` body plus the names of the files that ship beside it. The point is that a skill is *how the task is done here*, so it has to be read before the work starts, not consulted afterwards. Running `/review` yourself is unchanged; `skill_view` is the same document reaching the agent on its own initiative.

Viewing the same unchanged skill twice returns a single line saying so, because the body is already in the conversation. A long body that a compaction had to drop is replaced by `[SKILL_PRUNED: content lost in compaction; reload with skill_view(name="review")]`, which is what tells the agent it no longer has instructions it thinks it has. Views from the last couple of turns are never pruned.

Descriptions are clipped at 60 characters in the index, so write a `description:` that reads as a whole sentence in that space. Three settings control the block, all under `skills` in `settings.json`:

| setting | default | what it does |
| --- | --- | --- |
| `indexInPrompt` | `true` | list skills in the prompt at all |
| `indexMaxEntries` | `60` | how many to list before "… and K more" |
| `protectRecentViews` | `2` | turns of `skill_view` results a compaction leaves whole |

## Where things are found, and who wins

Roots are scanned in this order, and later wins on a name clash:

1. built-ins — `core/snowpea_core/builtin_skills/`
2. global — `$SNOWPEA_HOME/{skills,agents,commands}/`
3. plugins — `$SNOWPEA_HOME/plugins/*/`
4. project — `<project>/.claude/{skills,agents,commands}/`, then `<project>/.snowpea/{skills,agents,commands}/`

So a project skill overrides a plugin skill of the same name, and a plugin overrides a built-in. The loader records where each one came from, and `skill list` shows it.

A directory holding nothing but a `SKILL.md` **is** one skill, not a bundle. Installing one puts it in `$SNOWPEA_HOME/skills/<name>/` rather than `plugins/`. A bare skill that already sits under `plugins/` keeps working — it is registered as a skill and the log says where it belongs.

## When things are loaded

Loading happens at three moments, so a `/command` is never one restart away:

- **At daemon start.** The built-ins, `$SNOWPEA_HOME/{skills,agents,commands}`, every installed plugin, and the project directories of every session the daemon still remembers — closed sessions included, up to 50 existing directories. Servers declared in `$SNOWPEA_HOME/.mcp.json` are started here too, so their tools are in `tool.list` before any session opens.
- **At `session.create` and `session.resume`.** The session's own workdir is scanned again (`<workdir>/.claude` and `<workdir>/.snowpea`) before the first turn, which is what makes a brand-new checkout's skills available immediately. The project's own `.mcp.json` servers start at the same moment.
- **After an install, a removal or `/skill create`.** A full reload in place.

Each of those broadcasts `commands.changed`, so the TUI palette and the desktop app update without a restart. `skill.list`, `command.list` and `tool.list` reflect the change as soon as it lands.

## Next

[Scheduler](scheduler.md) — running work while you are away.
