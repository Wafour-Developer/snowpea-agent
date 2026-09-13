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

## Installing

```bash
snowpea skill install ./my-plugin
snowpea skill install https://github.com/someone/their-plugin.git
snowpea skill install oh-my-claudecode
snowpea skill list --json
snowpea skill remove my-plugin
```

Installing copies the plugin into `$SNOWPEA_HOME/plugins/<name>` and reloads the registry. A reload emits a `commands.changed` notification, so the TUI refreshes its palette without a restart.

## Searching

```bash
snowpea skill search "pdf"
snowpea skill search "code review" --json
```

Four sources are queried together, and each hit carries the `source` it came from: `claude-marketplace` (the `marketplace.json` of every registered marketplace repository), `agentskills.io`, `hermes-hub`, and `snowpea-registry` (the hosted registry at `registry.snowpea.ai`). A source that fails contributes nothing rather than failing the search. Feed a hit's install spec straight back to `skill install`; a registry hit's is `registry:<id>`.

```bash
snowpea skill search "planning" --source registry
```

`--source registry` (or `--source snowpea` — both are the same alias) keeps only the hosted-registry hits.

Registered marketplaces live in `$SNOWPEA_HOME/marketplaces.json`, seeded with the oh-my-claudecode marketplace.

## Publishing to the registry

```bash
snowpea skill install registry:ralplan          # download and install by id
snowpea setup tools                              # save a publisher token once (masked)
snowpea skill publish ./my-skill                 # zip + validate + upload
snowpea skill rate ralplan 5 --comment "great"   # 1-5 stars, one per caller
```

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

## Where things are found, and who wins

Roots are scanned in this order, and later wins on a name clash:

1. built-ins — `core/snowpea_core/builtin_skills/`
2. global — `$SNOWPEA_HOME/{skills,agents,commands}/`
3. plugins — `$SNOWPEA_HOME/plugins/*/`
4. project — `<project>/.claude/{skills,agents,commands}/`, then `<project>/.snowpea/{skills,agents,commands}/`

So a project skill overrides a plugin skill of the same name, and a plugin overrides a built-in. The loader records where each one came from, and `skill list` shows it.

## Next

[Scheduler](scheduler.md) — running work while you are away.
