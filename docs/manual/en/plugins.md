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

Since protocol 1.33.0 those calls are scope-aware. `skill.list {workdir}` lists one project's skills, agents and commands beside the global, built-in and plugin ones. `skill.read {name, workdir?, scope?, plugin?}` looks only in `scope` when it is given (`project`, `global`, `builtin` or `plugin`; `workdir` is needed for `project` only, and an empty `workdir` is no directory rather than the daemon's), and without one finds every loaded skill — a built-in or a plugin's included — answering `readOnly: true` for those and for Claude Code's own global skills, which `skill.write` cannot save over; `plugin` (a manifest or directory name) picks one plugin's copy when several ship the same skill. `skill.write` takes `workdir` only for `scope: "project"` (with `global` it only decides which project may shadow the copy) and answers `{path, active}`, where `active` says whether the written copy is the one now in effect — a project skill of the same name, or a Claude Code global one, shadows a global copy. A `workdir` must be absolute; a relative one is `invalid_params`. In the terminal UI, `/skill edit <name>` reads the skill for the session's project and does not open the editor on a read-only one; it says so and suggests copying it to `$SNOWPEA_HOME/skills/<name>/SKILL.md`. `skill.create` on a target that already has a `SKILL.md` answers the error code `skill_exists` (with `details.path`) unless `force` is set, in draft mode before any turn starts. `skill.remove {name, scope?, workdir?, kind?}` deletes a plugin (by its directory or by the manifest name `skill.list` shows), or a global or project skill directory, agent file or command file; `scope` and `kind` narrow where it looks, and `not_found` names what it looked for and every path it tried. Without a `scope` it deletes the copy in effect: the project's `.snowpea`, then `.claude` (only with a `workdir`), then the global bundle, then `plugins/` — so when a skill and a plugin share a name the skill goes first; within a bundle a skill directory comes before an agent file and a command file. A name must be one plain path component (not `.` or `..`, no `/`, `\` or NUL), and nothing outside the directory it was looked for in is ever deleted. The agent's `skill_remove` tool still reaches only `plugins/<name>` and `$SNOWPEA_HOME/skills/<name>`.

## Installing

```bash
snowpea skill install ./my-plugin
snowpea skill install https://github.com/someone/their-plugin.git
snowpea skill install oh-my-claudecode
snowpea skill list --json
snowpea skill remove my-plugin
```

Installing copies the plugin into `$SNOWPEA_HOME/plugins/<name>` and reloads the registry. A reload emits a `commands.changed` notification, so the TUI refreshes its palette without a restart.

Plugins installed in Claude Code are visible automatically: snowpea reads `~/.claude/plugins/installed_plugins.json` on every reload and registers their skills, commands and agents under source `claude-plugin`, skipping any that `enabledPlugins` in `~/.claude/settings.json` marks false. If a plugin or skill with the same name is installed in snowpea, the snowpea copy takes precedence and the Claude copy is skipped.

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

`skill install` accepts any install spec a search hit hands back. `registry:<id>` (a locally published skill) and `clawhub:<id>` resolve through the registry's own download proxy. `github:<owner>/<repo>[@plugin]` (what the registry hands back for a mirrored Claude Code marketplace item) is cloned directly with `git clone` instead — its registry id is not the same string as this spec, so asking the registry to download it would always 404. A directory inside the repo can be named the way a GitHub URL does — `github:anthropics/skills/skills/docx` installs just that directory under its own name. With no `@plugin` (and no path) the whole repo is cloned; with `@plugin`, only that plugin's directory is installed, resolved from a locally registered marketplace pointed at the same repo or, failing that, straight from the repo's own `marketplace.json` on GitHub. A hit's `id` (shown by `skill search --json`) is also always available if you would rather install a locally-published copy by id directly: `skill install registry:<id>`.

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

The command receives `{event, tool_name, tool_input, session_id, cwd}` as JSON on stdin, plus `SNOWPEA_HOME` and `SNOWPEA_TOOL_NAME` in the environment. `PreToolUse` runs after the permission verdict and just before the tool; exit status 2 blocks the call, and its stderr becomes the error the model sees, so the turn continues with a refusal rather than dying. `PostToolUse` runs right after the tool returns, `Stop` when the model is about to end its turn with no further tool calls. Other hook events are parsed and ignored.

**Keeping the agent going (`Stop`).** As in Claude Code, a `Stop` hook can refuse to let the turn end. Its stdin also carries `hook_event_name: "Stop"`, `stop_hook_active` (true once this turn has already been continued by a Stop hook) and `last_assistant_message`. Exit status 2 (stderr is the instruction) or stdout `{"decision": "block", "reason": "…"}` hands the reason to the model and the turn continues; the surface sees a `hook.continue` session event. `{"continue": false}` or a quiet exit lets the turn end. A turn is continued at most 8 times, so a hook that always blocks cannot loop forever — check `stop_hook_active` to continue only once.

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
snowpea mcp add github --preset github     # sends Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}
snowpea mcp add linear --preset linear     # then sign in: see below
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

Over RPC (protocol 1.33.0), `mcp.add` takes two separate answers: `acceptFindings` adds an entry despite the security findings `mcp_unsafe` reported, and `replace` overwrites a name already declared in the target file instead of answering `mcp_exists`. Neither implies the other; `force` is still both. `mcp.update` merges `env` and `headers` key by key, so one secret can be rotated without resending the others; `envRemove` and `headersRemove` delete keys, and `unset` clears optional keys (`args`, `cwd`, `disabled`, `env`, `headers`, `timeoutSec`, `toolTimeoutSec`, `toolsExclude`, `toolsInclude`, `type`) after the patch is applied. In a patch, `toolsInclude: []` or `toolsExclude: []` clears that filter, and `null` or an absent key leaves it as saved. A patch that changes `url` without giving `headers` drops the saved headers, so one endpoint's credentials never go to another. `mcp.update` runs the same security check as `mcp.add` on the entry it would save: a finding the patch brings in is `mcp_unsafe` unless `acceptFindings` (or `force`) is set, while findings the entry already had do not ask again. `mcp.test` takes `envRemove`, `headersRemove` and `unset` too, so a draft probes exactly what the update would save; with a `scope` it merges onto that scope's declaration rather than the winner's, and probes the draft alone when that scope has none. Writes only restart what runs when the copy in effect changes: adding, editing or removing a copy another scope shadows touches the file and nothing else, removing the winner starts the copy it shadowed, and removing the last copy stops the server. Pass the `workdir` (or `sessionId`) of the project you mean, since that decides which copy wins. `mcp.list {includeShadowed: true}` also lists each declaration another scope overrides — a global entry hidden by a project entry of the same name — right after its winner, marked `shadowed: true`.

### Variables in an entry

Since protocol 1.34.0 (capability `mcpVarExpansion`), `${NAME}` in a server's `args`, `env` values, `headers` values and `oauth.clientSecret` is filled in when the server starts, so a secret does not have to sit in `.mcp.json`:

```json
{"mcpServers": {"github": {
  "url": "https://api.githubcopilot.com/mcp/",
  "headers": {"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"}
}}}
```

A name is looked up in `$SNOWPEA_HOME/credentials.json` first (string values only) and then in the daemon's environment — the same order a gateway's `credentialsRef` uses, so one stored key serves both. `${NAME:-default}` falls back to `default`, and `$${NAME}` is the literal text `${NAME}`. A variable that is set nowhere and has no default stops that server from starting, with an error that names the variable and never a value.

Only your own entries expand any name: `$SNOWPEA_HOME/.mcp.json` and `mcp.servers` in settings. A project's `.mcp.json` arrives with a checkout and a plugin's with an install, and an `mcp.test` draft with a request, so those may expand only the names you list in `settings.mcp.allowVars` (for example `["GITHUB_PERSONAL_ACCESS_TOKEN"]`); any other name fails the start with `<server>: ${NAME} is not allowed in a project entry; add it to settings.mcp.allowVars` — otherwise a cloned repository could send `${ANTHROPIC_API_KEY}` to a server of its choosing. A draft that edits a saved global or settings entry may keep using the names that entry already uses. The same rule covers `oauth.clientSecret`, and a default (`${NAME:-x}`) does not get around it. Expansion happens only when the server starts: `mcp.list` and `snowpea mcp get` show the entry as written, and still only key names for `env` and `headers`. The catalog presets that need a key now write it this way and list it in `needs`.

### Signing in to a remote server (OAuth)

A remote (`http` or `sse`) server that answers `401` shows `auth.state: "needs_auth"` in `mcp.list` instead of a generic error (capability `mcpOAuth`, protocol 1.34.0). `mcp.add` still saves such an entry even when its probe meets the 401, and answers `auth.state: "needs_auth"`, because there is no token before the entry exists. To sign in, a client calls `mcp.authorize {name, workdir?, scope?, oauthScope?, redirectUri?}`, which answers `{url, state, expiresAt, redirectUri, serverUrl, resource, scope}`; the host can show `serverUrl` and check `resource` (the canonical resource the token will be for) before the user opens `url` in a browser. `scope` (`global` or `project`) picks which declaration signs in. Without it the one in effect does, except that when a project or plugin entry shadows a global one of the same name at another URL the call is `invalid_params` until `scope` says which — so a cloned project cannot collect the sign-in meant for your global server. `mcp.deauthorize {name, scope?}` takes the same `scope`.

The daemon follows the MCP authorization spec: the `WWW-Authenticate` header's `resource_metadata` (RFC 9728 protected resource metadata, with the well-known fallbacks), then the authorization server's RFC 8414 metadata (or its OpenID configuration), PKCE with S256, and the RFC 8707 `resource` indicator on every authorization and token request. It registers itself with dynamic client registration (RFC 7591) when the authorization server offers it; otherwise put a registered client in the entry as `"oauth": {"clientId": "…", "clientSecret": "${MY_SECRET}"}` (the secret only for a confidential client). OAuth scopes come from `oauthScope`, else `oauth.scope`, else the server's `WWW-Authenticate` scope, else its advertised `scopes_supported`.

Discovery is strict: the protected resource metadata must name this very server as its `resource`, its authorization server and every metadata and endpoint URL must be https (http only on loopback), a loopback, private or link-local address is accepted only when the MCP server itself is on that host, metadata redirects are followed only within their origin and the token and registration requests follow none, and the authorization server's metadata must carry its own `issuer`. When it advertises `authorization_response_iss_parameter_supported`, the redirect's `iss` (RFC 9207) must be that issuer.

Without `redirectUri` the daemon listens once on `http://127.0.0.1:<random port>/callback` and finishes on its own; a request to it with a wrong or missing `state` gets a 400 and the sign-in keeps waiting. A host that catches the redirect in its own tab passes its `redirectUri` — http on 127.0.0.1, localhost or [::1] (any port), https, or a private-use scheme such as `com.example.app:/cb`; never `javascript:`, `data:`, `file:` or a fragment — and then calls `mcp.authorize.complete {state, code, iss?}` (or `{state, error}` when the user refused). Either way `mcp.auth.done {name, ok, error?, state, scope}` reports the end, and on success the server restarts with its token (when the copy that signed in is the one in effect). A flow waits ten minutes; a new `mcp.authorize` for the same server and scope replaces the old one, which then reports nothing — match `mcp.auth.done` on `state`. When the connection that started a flow closes, the flow is dropped, its loopback listener closed, and the remaining clients get `mcp.auth.done {ok: false, error: "cancelled"}`. Failures are the error code `mcp_auth_failed`, with `details.stage` (`discovery`, `metadata`, `registration`, `client`, `exchange`, `state` or `callback`).

Tokens stay in the daemon: `$SNOWPEA_HOME/mcp-oauth.json`, mode 0600, keyed by server name and resource URL, so changing the URL makes them unusable and a project entry that shadows a global one under the same name cannot overwrite its sign-in. The agent's file tools treat that file as a secret, like `credentials.json`. No RPC result, event or log line carries a token, a code or a verifier. The access token is sent as `Authorization: Bearer` on connect (an `Authorization` header configured on the entry wins); it is refreshed before it expires, and on a `401` it is refreshed once — keeping a rotated refresh token — before the server is marked `needs_auth`. `mcp.deauthorize {name}` deletes the stored sign-in.

The catalog marks how each preset authenticates with `auth`: `oauth` (add it, then sign in), `token` (a key from `needs`) or `none`. GitHub's preset is now GitHub's official remote server (`https://api.githubcopilot.com/mcp/`, a personal access token in `Authorization`), with `github-docker` (`ghcr.io/github/github-mcp-server`, pinned) for running it locally; Linear (`https://mcp.linear.app/mcp`) signs in with OAuth; npm and Docker presets are pinned to a release instead of `@latest`.

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
| `disabled` | `[]` | skills and commands switched off (1.34.0) |

`skills.disabled` (capability `skillToggle`) takes skill or command names, or `<plugin>:<name>` for one plugin's copy. A disabled skill leaves the index, the slash commands and `autoInject`, and `skill_view` refuses it; `skill.list` still lists it with `enabled: false`, and `skill.read` and `skill.remove` still reach it. Changing the list with `settings.set` applies at once, the way a skill reload does: new sessions see the new index, and a running session keeps the index it started with until its next compaction. With the list empty the prompt is byte for byte what it was.

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
